from __future__ import annotations

from datetime import datetime, timezone
import math

import pandas as pd

from .config import DATABASE_PATH, ETSY_CSV, OUTPUT_DIR
from .database import (
    initialize_database,
    initialize_history_tables,
    create_snapshot,
    save_comparison_history,
    record_run,
    replace_table,
)
from .etsy import build_etsy_tables, read_etsy_csv
from .etsy_api import EtsyApiClient
from .matching import build_comparison
from .printify import PrintifyClient


# ---------------------------------------------------------
# PROFITABILITY SETTINGS
# ---------------------------------------------------------

LOW_PROFIT_THRESHOLD = 15.0

ETSY_TRANSACTION_RATE = 0.065
ETSY_PAYMENT_RATE = 0.03
ETSY_PAYMENT_FIXED_FEE = 0.25

# Offsite Ads intentionally excluded.
# Etsy listing fee intentionally excluded from the
# per-sale profitability calculation.


# ---------------------------------------------------------
# VARIANT-LEVEL PROFITABILITY
# ---------------------------------------------------------

def build_profitability_report(
    comparison: pd.DataFrame,
):
    """
    Calculate estimated net profit for matched Etsy/Printify
    variants.

    Assumptions:
      - Customer pays $0 shipping.
      - Seller pays Printify U.S. first-item shipping.
      - Etsy transaction fee = 6.5%.
      - Etsy payment processing = 3% + $0.25.
      - Offsite Ads excluded.
      - Etsy listing fee excluded.

    Returns:
      profitability_all
      low_profit
    """

    if comparison.empty:
        empty = comparison.copy()

        for column in [
            "etsy_transaction_fee",
            "etsy_payment_processing_fee",
            "etsy_total_fees",
            "estimated_net_profit",
            "estimated_net_margin_pct",
            "profitability_status",
        ]:
            empty[column] = pd.Series(dtype="float64")

        return empty, empty

    # Only calculate profitability where Etsy and Printify
    # have a real SKU match.
    profit = comparison[
        comparison["match_status"] == "MATCHED"
    ].copy()

    if profit.empty:
        return profit, profit

    # -----------------------------------------------------
    # NUMERIC VALUES
    # -----------------------------------------------------

    profit["etsy_price_for_profit"] = pd.to_numeric(
        profit["etsy_price"],
        errors="coerce",
    )

    profit["printify_cost_for_profit"] = pd.to_numeric(
        profit["printify_cost_numeric"],
        errors="coerce",
    )

    profit["printify_shipping_for_profit"] = pd.to_numeric(
        profit["printify_shipping_cost"],
        errors="coerce",
    )

    # -----------------------------------------------------
    # ETSY FEES
    # -----------------------------------------------------

    profit["etsy_transaction_fee"] = (
        profit["etsy_price_for_profit"]
        * ETSY_TRANSACTION_RATE
    )

    profit["etsy_payment_processing_fee"] = (
        profit["etsy_price_for_profit"]
        * ETSY_PAYMENT_RATE
        + ETSY_PAYMENT_FIXED_FEE
    )

    profit["etsy_total_fees"] = (
        profit["etsy_transaction_fee"]
        + profit["etsy_payment_processing_fee"]
    )

    # -----------------------------------------------------
    # NET PROFIT
    # -----------------------------------------------------

    profit["estimated_net_profit"] = (
        profit["etsy_price_for_profit"]
        - profit["printify_cost_for_profit"]
        - profit["printify_shipping_for_profit"]
        - profit["etsy_total_fees"]
    )

    profit["estimated_net_margin_pct"] = (
        profit["estimated_net_profit"]
        / profit["etsy_price_for_profit"]
        * 100
    )

    # -----------------------------------------------------
    # INVALID DATA
    # -----------------------------------------------------

    invalid_price = (
        profit["etsy_price_for_profit"].isna()
        | (profit["etsy_price_for_profit"] <= 0)
    )

    missing_shipping = (
        profit["printify_shipping_for_profit"].isna()
    )

    profit.loc[
        invalid_price | missing_shipping,
        "estimated_net_profit",
    ] = pd.NA

    profit.loc[
        invalid_price | missing_shipping,
        "estimated_net_margin_pct",
    ] = pd.NA

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    profit["profitability_status"] = "OK"

    profit.loc[
        profit["estimated_net_margin_pct"]
        < LOW_PROFIT_THRESHOLD,
        "profitability_status",
    ] = "LOW_PROFIT"

    profit.loc[
        missing_shipping,
        "profitability_status",
    ] = "SHIPPING_UNAVAILABLE"

    profit.loc[
        invalid_price,
        "profitability_status",
    ] = "PRICE_UNAVAILABLE"

    # -----------------------------------------------------
    # COLUMN ORDER
    # -----------------------------------------------------

    preferred_columns = [
        "etsy_listing_id",
        "etsy_title",
        "etsy_sku",
        "etsy_price_for_profit",

        "printify_product_id",
        "printify_title",
        "printify_variant_id",
        "printify_variant_title",
        "printify_sku",

        "printify_cost_for_profit",
        "printify_shipping_for_profit",

        "etsy_transaction_fee",
        "etsy_payment_processing_fee",
        "etsy_total_fees",

        "estimated_net_profit",
        "estimated_net_margin_pct",
        "profitability_status",
    ]

    available_columns = [
        column
        for column in preferred_columns
        if column in profit.columns
    ]

    remaining_columns = [
        column
        for column in profit.columns
        if column not in available_columns
        and column not in [
            "etsy_price_for_profit",
            "printify_cost_for_profit",
            "printify_shipping_for_profit",
        ]
    ]

    profit = profit[
        available_columns + remaining_columns
    ]

    # Worst margins first.
    profit = profit.sort_values(
        by="estimated_net_margin_pct",
        ascending=True,
        na_position="last",
    )

    low_profit = profit[
        profit["profitability_status"] == "LOW_PROFIT"
    ].copy()

    return profit, low_profit


# ---------------------------------------------------------
# LISTING-LEVEL PROFITABILITY
# ---------------------------------------------------------

def build_listing_profitability_report(
    profitability_all: pd.DataFrame,
    etsy_listings: pd.DataFrame,
):
    """
    Roll variant-level profitability up to one row per Etsy listing.

    IMPORTANT:
    Etsy's exported CSV provides one base listing price, but does not
    reliably tell us which variation price belongs to which SKU.

    Therefore:
      - Variant-level profitability remains authoritative.
      - Listing-level results show how the matched SKUs perform when
        evaluated at the exported Etsy base price.
      - We do NOT assume that every Printify variant should be sold
        at the same actual Etsy price.
      - The highest required price is reported for information only.
    """

    # ---------------------------------------------------------
    # TARGET PRICE CALCULATION
    # ---------------------------------------------------------

    if not profitability_all.empty:

        profit = profitability_all.copy()

        denominator = (
            1.0
            - ETSY_TRANSACTION_RATE
            - ETSY_PAYMENT_RATE
            - LOW_PROFIT_THRESHOLD / 100.0
        )

        product_cost = pd.to_numeric(
            profit["printify_cost_for_profit"],
            errors="coerce",
        )

        shipping_cost = pd.to_numeric(
            profit["printify_shipping_for_profit"],
            errors="coerce",
        )

        base_cost = (
            product_cost
            + shipping_cost
            + ETSY_PAYMENT_FIXED_FEE
        )

        profit[
            "minimum_price_for_15pct_margin_variant"
        ] = base_cost / denominator

        # Round UP to nearest cent.
        profit[
            "minimum_price_for_15pct_margin_variant"
        ] = profit[
            "minimum_price_for_15pct_margin_variant"
        ].apply(
            lambda value:
                math.ceil(value * 100 - 1e-9) / 100
                if pd.notna(value)
                else pd.NA
        )

        # -----------------------------------------------------
        # SUMMARIZE EACH LISTING
        # -----------------------------------------------------

        def summarize_listing(group):

            margins = pd.to_numeric(
                group["estimated_net_margin_pct"],
                errors="coerce",
            )

            profits = pd.to_numeric(
                group["estimated_net_profit"],
                errors="coerce",
            )

            prices = pd.to_numeric(
                group["etsy_price_for_profit"],
                errors="coerce",
            )

            target_prices = pd.to_numeric(
                group[
                    "minimum_price_for_15pct_margin_variant"
                ],
                errors="coerce",
            )

            valid_margins = margins.dropna()

            if valid_margins.empty:

                worst_margin = None
                best_margin = None
                loss_count = 0
                under_10_count = 0
                ten_to_fifteen_count = 0
                at_or_above_15_count = 0
                worst_margin_row = None

            else:

                worst_margin = float(
                    valid_margins.min()
                )

                best_margin = float(
                    valid_margins.max()
                )

                loss_count = int(
                    (valid_margins < 0).sum()
                )

                under_10_count = int(
                    (
                        (valid_margins >= 0)
                        & (valid_margins < 10)
                    ).sum()
                )

                ten_to_fifteen_count = int(
                    (
                        (valid_margins >= 10)
                        & (
                            valid_margins
                            < LOW_PROFIT_THRESHOLD
                        )
                    ).sum()
                )

                at_or_above_15_count = int(
                    (
                        valid_margins
                        >= LOW_PROFIT_THRESHOLD
                    ).sum()
                )

                worst_margin_index = (
                    margins.idxmin()
                )

                worst_margin_row = group.loc[
                    worst_margin_index
                ]

            # -------------------------------------------------
            # KEEP ALL "WORST VARIANT" VALUES TOGETHER
            #
            # Do NOT combine the minimum Etsy price from one
            # SKU with the Printify cost from another SKU.
            # -------------------------------------------------

            if worst_margin_row is None:

                worst_sku = None
                worst_variation_label = None
                worst_current_price = None
                worst_net_profit = None
                worst_printify_cost = None
                worst_printify_shipping = None

            else:

                worst_sku = str(
                    worst_margin_row.get(
                        "etsy_sku",
                        "",
                    )
                    or ""
                ).strip() or None

                worst_variation_label = (
                    worst_margin_row.get(
                        "etsy_variation_label"
                    )
                )

                worst_current_price_value = (
                    pd.to_numeric(
                        worst_margin_row.get(
                            "etsy_price_for_profit"
                        ),
                        errors="coerce",
                    )
                )

                worst_current_price = (
                    float(worst_current_price_value)
                    if pd.notna(
                        worst_current_price_value
                    )
                    else None
                )

                worst_net_profit_value = (
                    pd.to_numeric(
                        worst_margin_row.get(
                            "estimated_net_profit"
                        ),
                        errors="coerce",
                    )
                )

                worst_net_profit = (
                    float(worst_net_profit_value)
                    if pd.notna(
                        worst_net_profit_value
                    )
                    else None
                )

                worst_printify_cost_value = (
                    pd.to_numeric(
                        worst_margin_row.get(
                            "printify_cost_for_profit"
                        ),
                        errors="coerce",
                    )
                )

                worst_printify_cost = (
                    float(worst_printify_cost_value)
                    if pd.notna(
                        worst_printify_cost_value
                    )
                    else None
                )

                worst_printify_shipping_value = (
                    pd.to_numeric(
                        worst_margin_row.get(
                            "printify_shipping_for_profit"
                        ),
                        errors="coerce",
                    )
                )

                worst_printify_shipping = (
                    float(
                        worst_printify_shipping_value
                    )
                    if pd.notna(
                        worst_printify_shipping_value
                    )
                    else None
                )

            current_price_min = (
                float(prices.min())
                if not prices.dropna().empty
                else None
            )

            current_price_max = (
                float(prices.max())
                if not prices.dropna().empty
                else None
            )

            highest_required_price = (
                float(target_prices.max())
                if not target_prices.dropna().empty
                else None
            )

            # -------------------------------------------------
            # LISTING STATUS
            # -------------------------------------------------

            if valid_margins.empty:
                status = "NOT_CALCULABLE"

            elif loss_count > 0:
                status = "HAS_LOSS_VARIANT"

            elif under_10_count > 0:
                status = "HAS_UNDER_10_VARIANT"

            elif ten_to_fifteen_count > 0:
                status = "HAS_10_TO_14.99_VARIANT"

            else:
                status = "ALL_VARIANTS_15%+"

            # -------------------------------------------------
            # ACTION
            # -------------------------------------------------

            if status == "NOT_CALCULABLE":
                action = "NO_PROFITABILITY_DATA"

            elif status == "HAS_LOSS_VARIANT":
                action = "REVIEW_LOSS_VARIANTS"

            elif status == "HAS_UNDER_10_VARIANT":
                action = "REVIEW_LOW_MARGIN_VARIANTS"

            elif status == "HAS_10_TO_14.99_VARIANT":
                action = "SMALL_PRICE_REVIEW"

            else:
                action = "NO_ACTION"

            return pd.Series(
                {
                    "matched_variant_count":
                        len(group),

                    "variants_with_losses":
                        loss_count,

                    "variants_under_10pct":
                        under_10_count,

                    "variants_10_to_14_99pct":
                        ten_to_fifteen_count,

                    "variants_15pct_or_higher":
                        at_or_above_15_count,

                    "current_price_min":
                        current_price_min,

                    "current_price_max":
                        current_price_max,

                    "worst_variant_sku":
                        worst_sku,

                    "worst_variant_label":
                        worst_variation_label,

                    "worst_variant_current_price":
                        worst_current_price,

                    "worst_net_profit":
                        worst_net_profit,

                    "worst_printify_cost":
                        worst_printify_cost,

                    "worst_printify_shipping":
                        worst_printify_shipping,

                    "worst_net_margin_pct":
                        worst_margin,

                    "best_net_margin_pct":
                        best_margin,

                    "minimum_price_for_15pct_margin":
                        highest_required_price,

                    "status":
                        status,

                    "recommended_action":
                        action,
                }
            )

        listing_profitability = (
            profit.groupby(
                [
                    "etsy_listing_id",
                    "etsy_title",
                ],
                dropna=False,
                as_index=False,
            )
            .apply(
                summarize_listing,
                include_groups=False,
            )
            .reset_index(drop=True)
        )

    else:

        listing_profitability = pd.DataFrame(
            columns=[
                "etsy_listing_id",
                "etsy_title",
                "matched_variant_count",
                "variants_with_losses",
                "variants_under_10pct",
                "variants_10_to_14_99pct",
                "variants_15pct_or_higher",
                "current_price_min",
                "current_price_max",
                "worst_variant_sku",
                "worst_variant_label",
                "worst_variant_current_price",
                "worst_net_profit",
                "worst_printify_cost",
                "worst_printify_shipping",
                "worst_net_margin_pct",
                "best_net_margin_pct",
                "minimum_price_for_15pct_margin",
                "status",
                "recommended_action",
            ]
        )

    # ---------------------------------------------------------
    # START WITH ALL ETSY LISTINGS
    # ---------------------------------------------------------

    base = etsy_listings[
        [
            "etsy_listing_key",
            "etsy_api_listing_id",
            "title",
            "price",
            "etsy_api_price",
        ]
    ].copy()

    base = base.rename(
        columns={
            "title": "etsy_title",
            "price": "csv_etsy_price",
            "etsy_api_price": "current_etsy_price",
        }
    )

    base["etsy_listing_id"] = (
        base["etsy_api_listing_id"]
        .fillna(base["etsy_listing_key"])
        .astype(str)
    )
    base["current_etsy_price"] = pd.to_numeric(
        base["current_etsy_price"], errors="coerce"
    ).combine_first(
        pd.to_numeric(base["csv_etsy_price"], errors="coerce")
    )
    base = base.drop(
        columns=["etsy_listing_key", "etsy_api_listing_id", "csv_etsy_price"],
        errors="ignore",
    )

    # ---------------------------------------------------------
    # MERGE PROFITABILITY INTO ALL ETSY LISTINGS
    # ---------------------------------------------------------

    if listing_profitability.empty:

        report = base.copy()

        for column in [
            "matched_variant_count",
            "variants_with_losses",
            "variants_under_10pct",
            "variants_10_to_14_99pct",
            "variants_15pct_or_higher",
            "current_price_min",
            "current_price_max",
            "worst_variant_sku",
            "worst_variant_label",
            "worst_variant_current_price",
            "worst_net_profit",
            "worst_printify_cost",
            "worst_printify_shipping",
            "worst_net_margin_pct",
            "best_net_margin_pct",
            "minimum_price_for_15pct_margin",
        ]:
            report[column] = pd.NA

        report["status"] = "NOT_CALCULABLE"

        report["recommended_action"] = (
            "NO_PROFITABILITY_DATA"
        )

    else:

        listing_profitability[
            "etsy_listing_id"
        ] = (
            listing_profitability[
                "etsy_listing_id"
            ]
            .astype(str)
        )

        listing_data = listing_profitability.drop(
            columns=["etsy_title"],
            errors="ignore",
        )

        report = base.merge(
            listing_data,
            on="etsy_listing_id",
            how="left",
        )

        report["status"] = report[
            "status"
        ].fillna("NOT_CALCULABLE")

        report["recommended_action"] = report[
            "recommended_action"
        ].fillna("NO_PROFITABILITY_DATA")

    # ---------------------------------------------------------
    # PRICE INCREASE INFORMATION
    # ---------------------------------------------------------

    report["current_etsy_price"] = pd.to_numeric(
        report["current_etsy_price"],
        errors="coerce",
    )

    report[
        "minimum_price_for_15pct_margin"
    ] = pd.to_numeric(
        report[
            "minimum_price_for_15pct_margin"
        ],
        errors="coerce",
    )

    report["price_increase_needed"] = (
        report[
            "minimum_price_for_15pct_margin"
        ]
        - report["current_etsy_price"]
    )

    report.loc[
        report["price_increase_needed"] < 0,
        "price_increase_needed",
    ] = 0.0

    # ---------------------------------------------------------
    # IMPORTANT NOTE
    # ---------------------------------------------------------

    report["pricing_note"] = (
        "Uses Etsy API inventory prices for matched SKUs when available; "
        "falls back to the Etsy CSV base price when API pricing is "
        "unavailable. The 15% target is the highest price required by "
        "any matched variant in the listing."
    )

    # ---------------------------------------------------------
    # FINAL COLUMN ORDER
    # ---------------------------------------------------------

    report = report[
        [
            "etsy_listing_id",
            "etsy_title",
            "current_etsy_price",

            "matched_variant_count",

            "variants_with_losses",
            "variants_under_10pct",
            "variants_10_to_14_99pct",
            "variants_15pct_or_higher",

            "current_price_min",
            "current_price_max",

            "worst_net_profit",
            "worst_printify_cost",
            "worst_printify_shipping",
            "worst_net_margin_pct",
            "best_net_margin_pct",

            "minimum_price_for_15pct_margin",
            "price_increase_needed",

            "status",
            "recommended_action",
            "pricing_note",
        ]
    ]

    # ---------------------------------------------------------
    # SORT
    # ---------------------------------------------------------

    status_order = {
        "HAS_LOSS_VARIANT": 0,
        "HAS_UNDER_10_VARIANT": 1,
        "HAS_10_TO_14.99_VARIANT": 2,
        "ALL_VARIANTS_15%+": 3,
        "NOT_CALCULABLE": 4,
    }

    report["_sort"] = (
        report["status"]
        .map(status_order)
        .fillna(9)
    )

    report = report.sort_values(
        [
            "_sort",
            "worst_net_margin_pct",
        ],
        ascending=[
            True,
            True,
        ],
        na_position="last",
    )

    report = report.drop(
        columns=["_sort"]
    )

    # ---------------------------------------------------------
    # ONLY LISTINGS REQUIRING PROFITABILITY REVIEW
    # ---------------------------------------------------------

    # A listing belongs in the review report only when its worst
    # calculable variant margin is strictly below the threshold.
    # This guarantees that listings at exactly 15% or above are
    # completely excluded from the detailed review list.
    low_profit_listings = report[
        pd.to_numeric(
            report["worst_net_margin_pct"],
            errors="coerce",
        ) < LOW_PROFIT_THRESHOLD
    ].copy()

    return (
        report,
        low_profit_listings,
    )


# ---------------------------------------------------------
# ETSY API ENRICHMENT
# ---------------------------------------------------------

def _normalize_match_text(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def enrich_etsy_prices_from_api(etsy_listings, etsy_variants):
    listings = etsy_listings.copy()
    variants = etsy_variants.copy()
    listings["etsy_api_listing_id"] = pd.NA
    listings["etsy_api_price"] = pd.NA
    variants["etsy_api_listing_id"] = pd.NA
    variants["etsy_api_price"] = pd.NA
    variants["etsy_api_variation_1_name"] = pd.NA
    variants["etsy_api_variation_1_value"] = pd.NA
    variants["etsy_api_variation_label"] = pd.NA

    try:
        client = EtsyApiClient.from_environment()
    except RuntimeError as exc:
        print(f"Etsy API pricing enrichment skipped: {exc}")
        return listings, variants

    try:
        api_rows = client.get_listing_inventory_rows()
    except Exception as exc:
        print(f"WARNING: Etsy API pricing enrichment failed: {exc}")
        return listings, variants

    # -----------------------------------------------------
    # LIVE ETSY API vs CSV LISTING UNIVERSE DIAGNOSTIC
    # -----------------------------------------------------
    #
    # Compare unique listing titles represented in the CSV
    # against the unique live Etsy listing IDs returned by
    # the Etsy API inventory retrieval.
    #
    # This identifies listings that exist on Etsy but are
    # completely absent from the local CSV input.
    # -----------------------------------------------------

    try:
        shop_id = client.get_shop_id()

        live_listings = client.get_all_listings(
            shop_id
        )

        csv_title_map = {}

        for _, listing in listings.iterrows():
            title = str(
                listing.get("title", "")
                or ""
            ).strip()

            normalized_title = (
                _normalize_match_text(title)
            )

            if normalized_title:
                csv_title_map.setdefault(
                    normalized_title,
                    []
                ).append(
                    listing.get(
                        "etsy_listing_key",
                        ""
                    )
                )

        live_listing_map = {}

        for listing in live_listings:
            title = str(
                listing.get("title", "")
                or ""
            ).strip()

            normalized_title = (
                _normalize_match_text(title)
            )

            listing_id = listing.get(
                "listing_id"
            )

            if normalized_title:
                live_listing_map[
                    str(listing_id)
                ] = {
                    "title": title,
                    "title_key": normalized_title,
                    "state": listing.get(
                        "state",
                        ""
                    ),
                }

        missing_live_listings = []

        for listing_id, live_listing in (
            live_listing_map.items()
        ):
            if (
                live_listing["title_key"]
                not in csv_title_map
            ):
                missing_live_listings.append(
                    {
                        "etsy_api_listing_id":
                            listing_id,
                        "title":
                            live_listing["title"],
                        "state":
                            live_listing["state"],
                    }
                )

        print()
        print("=" * 120)
        print("LIVE ETSY API vs CSV LISTING UNIVERSE DIAGNOSTIC")
        print("=" * 120)
        print(
            f"CSV listing rows: {len(listings)}"
        )
        print(
            f"Live Etsy API listings: "
            f"{len(live_listing_map)}"
        )
        print(
            f"Live listings missing from CSV by title: "
            f"{len(missing_live_listings)}"
        )

        if missing_live_listings:
            print()
            print(
                "LIVE ETSY LISTINGS NOT FOUND IN CSV:"
            )

            missing_df = pd.DataFrame(
                missing_live_listings
            )

            print(
                missing_df.to_string(
                    index=False
                )
            )
        else:
            print(
                "No live Etsy listings were missing "
                "from the CSV by title."
            )

        print("=" * 120)
        print(
            "END LIVE ETSY API vs CSV LISTING "
            "UNIVERSE DIAGNOSTIC"
        )
        print("=" * 120)
        print()

    except Exception as exc:
        print()
        print(
            "WARNING: Could not run live Etsy API vs "
            f"CSV diagnostic: {exc}"
        )
        print()

    if not api_rows:
        print("WARNING: Etsy API returned no listing inventory rows.")
        return listings, variants

    api_df = pd.DataFrame(api_rows)
    if api_df.empty:
        return listings, variants

    api_df["_title_key"] = api_df["etsy_api_title"].map(
        _normalize_match_text
    )
    api_df["_sku_key"] = api_df["etsy_api_sku"].map(
        lambda v: str(v or "").strip().lower()
    )

    # -----------------------------------------------------
    # ADD LIVE ACTIVE ETSY LISTINGS MISSING FROM CSV
    # -----------------------------------------------------
    #
    # The Etsy CSV is useful for supplemental metadata, but it
    # can become stale when new listings are created after the
    # export. The live Etsy API is therefore authoritative for
    # the current ACTIVE listing universe.
    #
    # Existing CSV listings remain unchanged. This block only
    # appends active Etsy listings whose live SKUs have no match
    # anywhere in the CSV-derived variant table.
    # -----------------------------------------------------

    try:
        shop_id = client.get_shop_id()

        all_live_listings = client.get_all_listings(
            shop_id
        )

        active_listing_map = {
            str(listing.get("listing_id")): listing
            for listing in all_live_listings
            if str(
                listing.get("state", "")
            ).strip().lower() == "active"
            and str(
                listing.get("listing_id", "")
            ).strip()
        }

        csv_sku_keys = set(
            variants["etsy_sku"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
        )

        csv_title_keys = set(
            listings["title"]
            .fillna("")
            .map(_normalize_match_text)
        )

        csv_title_keys.discard("")

        active_api_df = api_df[
            api_df["etsy_api_listing_id"]
            .astype(str)
            .isin(active_listing_map.keys())
        ].copy()

        missing_listing_ids = []

        for listing_id, group in active_api_df.groupby(
            "etsy_api_listing_id",
            dropna=False,
        ):
            live_sku_keys = set(
                group["etsy_api_sku"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
            )

            live_sku_keys.discard("")

            live_listing = active_listing_map.get(
                str(listing_id),
                {},
            )

            live_title_key = _normalize_match_text(
                live_listing.get(
                    "title",
                    group[
                        "etsy_api_title"
                    ].iloc[0],
                )
            )

            has_sku_match = bool(
                live_sku_keys & csv_sku_keys
            )

            has_title_match = bool(
                live_title_key
                and live_title_key in csv_title_keys
            )

            # A live listing is missing only when it has
            # neither a SKU match nor a title match in the
            # CSV-derived listing universe.
            if not has_sku_match and not has_title_match:
                missing_listing_ids.append(
                    str(listing_id)
                )

        missing_listing_ids = sorted(
            set(missing_listing_ids)
        )

        if missing_listing_ids:

            print()
            print("=" * 120)
            print("ADDING LIVE ACTIVE ETSY LISTINGS MISSING FROM CSV")
            print("=" * 120)

            new_listing_rows = []
            new_variant_rows = []

            for listing_id in missing_listing_ids:

                live_listing = active_listing_map.get(
                    listing_id,
                    {}
                )

                group = active_api_df[
                    active_api_df[
                        "etsy_api_listing_id"
                    ].astype(str)
                    == listing_id
                ].copy()

                if group.empty:
                    continue

                title = str(
                    live_listing.get(
                        "title",
                        group[
                            "etsy_api_title"
                        ].iloc[0],
                    )
                    or ""
                ).strip()

                listing_key = (
                    f"API_LISTING_{listing_id}"
                )

                prices = pd.to_numeric(
                    group["etsy_api_price"],
                    errors="coerce",
                ).dropna()

                listing_price = (
                    float(prices.min())
                    if not prices.empty
                    else pd.NA
                )

                new_listing_rows.append(
                    {
                        "etsy_source_row": pd.NA,
                        "etsy_listing_key": listing_key,
                        "etsy_analysis_include": True,
                        "etsy_duplicate_title": False,
                        "etsy_duplicate_title_group": "",
                        "etsy_duplicate_title_occurrence": 1,
                        "title": title,
                        "description": "",
                        "price": listing_price,
                        "currency_code": "",
                        "quantity": "",
                        "tags": "",
                        "materials": "",
                        "variation_1_type": "",
                        "variation_1_name": "",
                        "variation_1_values": "",
                        "variation_2_type": "",
                        "variation_2_name": "",
                        "variation_2_values": "",
                        "image_count": pd.NA,
                        "raw_row_json": "",
                        "etsy_api_listing_id": listing_id,
                        "etsy_api_price": listing_price,
                    }
                )

                for sku_index, (
                    _,
                    api_variant,
                ) in enumerate(
                    group.iterrows(),
                    1,
                ):

                    sku = str(
                        api_variant.get(
                            "etsy_api_sku",
                            "",
                        )
                        or ""
                    ).strip()

                    if not sku:
                        continue

                    new_variant_rows.append(
                        {
                            "etsy_source_row": pd.NA,
                            "etsy_listing_key": listing_key,
                            "etsy_analysis_include": True,
                            "etsy_duplicate_title": False,
                            "etsy_sku_index": sku_index,
                            "etsy_sku": sku,
                            "etsy_sku_status": "PRESENT",
                            "etsy_variation_1_name": (
                                api_variant.get(
                                    "etsy_api_variation_1_name",
                                    "",
                                )
                            ),
                            "etsy_variation_1_value": (
                                api_variant.get(
                                    "etsy_api_variation_1_value",
                                    "",
                                )
                            ),
                            "etsy_variation_2_name": (
                                api_variant.get(
                                    "etsy_api_variation_2_name",
                                    "",
                                )
                            ),
                            "etsy_variation_2_value": (
                                api_variant.get(
                                    "etsy_api_variation_2_value",
                                    "",
                                )
                            ),
                            "etsy_variation_label": (
                                api_variant.get(
                                    "etsy_api_variation_label",
                                    "",
                                )
                            ),
                            "etsy_api_listing_id": listing_id,
                            "etsy_api_price": (
                                api_variant.get(
                                    "etsy_api_price",
                                    pd.NA,
                                )
                            ),
                            "etsy_api_variation_1_name": (
                                api_variant.get(
                                    "etsy_api_variation_1_name",
                                    "",
                                )
                            ),
                            "etsy_api_variation_1_value": (
                                api_variant.get(
                                    "etsy_api_variation_1_value",
                                    "",
                                )
                            ),
                            "etsy_api_variation_label": (
                                api_variant.get(
                                    "etsy_api_variation_label",
                                    "",
                                )
                            ),
                        }
                    )

                print(
                    f"ADDED API LISTING {listing_id}: "
                    f"{title}"
                )

            if new_listing_rows:

                new_listings_df = pd.DataFrame(
                    new_listing_rows
                )

                for column in new_listings_df.columns:
                    if column not in listings.columns:
                        listings[column] = pd.NA

                for column in listings.columns:
                    if column not in new_listings_df.columns:
                        new_listings_df[column] = pd.NA

                new_listings_df = new_listings_df[
                    listings.columns
                ]

                listings = pd.concat(
                    [
                        listings,
                        new_listings_df,
                    ],
                    ignore_index=True,
                )

            if new_variant_rows:

                new_variants_df = pd.DataFrame(
                    new_variant_rows
                )

                for column in new_variants_df.columns:
                    if column not in variants.columns:
                        variants[column] = pd.NA

                for column in variants.columns:
                    if column not in new_variants_df.columns:
                        new_variants_df[column] = pd.NA

                new_variants_df = new_variants_df[
                    variants.columns
                ]

                variants = pd.concat(
                    [
                        variants,
                        new_variants_df,
                    ],
                    ignore_index=True,
                )

            print()
            print(
                "LIVE ACTIVE LISTINGS ADDED: "
                f"{len(new_listing_rows)}"
            )
            print(
                "LIVE ACTIVE VARIANTS ADDED: "
                f"{len(new_variant_rows)}"
            )
            print(
                "TOTAL LISTINGS AFTER LIVE API ADDITION: "
                f"{len(listings)}"
            )
            print("=" * 120)
            print()

        else:
            print(
                "No live active Etsy listings needed to be "
                "added from the API."
            )

    except Exception as exc:
        print(
            "WARNING: Could not add missing live Etsy listings "
            f"from API: {exc}"
        )

    # -----------------------------------------------------
    # RAW API DATA DIAGNOSTIC
    # -----------------------------------------------------

    diagnostic_skus = {
        "11975776294672016378",
        "17119814508216010683",
        "27331151771594285206",
        "11731818812456775837",
    }

    # -----------------------------------------------------
    # RAW API NUTCRACKER DIAGNOSTIC
    #
    # Show ALL current Etsy API inventory rows for listings
    # containing "Nutcracker", not just a predefined SKU set.
    # -----------------------------------------------------

    api_diagnostic = api_df[
        api_df["etsy_api_title"]
        .astype(str)
        .str.contains(
            "Nutcracker",
            case=False,
            na=False,
        )
    ]

    print()
    print("=" * 120)
    print("RAW CURRENT ETSY API NUTCRACKER INVENTORY")
    print("=" * 120)

    if api_diagnostic.empty:
        print("NO NUTCRACKER LISTINGS FOUND IN API_DF")
    else:
        diagnostic_columns = [
            column
            for column in [
                "etsy_api_listing_id",
                "etsy_api_title",
                "etsy_api_sku",
                "etsy_api_price",
                "etsy_api_variation_1_name",
                "etsy_api_variation_1_value",
                "etsy_api_variation_label",
            ]
            if column in api_diagnostic.columns
        ]

        print(
            api_diagnostic[
                diagnostic_columns
            ].to_string(
                index=False
            )
        )

    print("=" * 120)
    print("END RAW CURRENT ETSY API NUTCRACKER INVENTORY")
    print("=" * 120)
    print()


    for listing_index, listing in listings.iterrows():

        # -----------------------------------------------------
        # IDENTIFY THE ETSY API LISTING BY SKU FIRST
        # -----------------------------------------------------
        #
        # SKU is the reliable variant identity.  The Etsy CSV title
        # can differ from the Etsy API title even when both refer to
        # the same real Etsy listing.  Therefore the title must NOT
        # be a required gate for API enrichment.
        #
        # We score every API listing by overlap with the CSV SKUs.
        # The title is retained only as a secondary fallback when
        # no SKU overlap can identify a listing.
        # -----------------------------------------------------

        csv_skus = set(
            str(v).strip().lower()
            for v in variants.loc[
                variants["etsy_listing_key"]
                == listing["etsy_listing_key"],
                "etsy_sku",
            ].tolist()
            if str(v).strip()
            and str(v).strip().lower() != "unavailable_sku"
        )

        scores = []

        for api_listing_id, group in api_df.groupby(
            "etsy_api_listing_id"
        ):
            api_skus = set(
                str(v).strip().lower()
                for v in group["etsy_api_sku"].tolist()
                if str(v).strip()
            )

            overlap = len(csv_skus & api_skus)

            if overlap > 0:
                scores.append(
                    (
                        overlap,
                        len(api_skus),
                        api_listing_id,
                    )
                )

        # -----------------------------------------------------
        # SKU MATCH FOUND
        # -----------------------------------------------------

        if scores:
            scores.sort(reverse=True)
            best_overlap, _, best_id = scores[0]

            selected = api_df[
                api_df["etsy_api_listing_id"]
                == best_id
            ].copy()

        else:
            # -------------------------------------------------
            # TITLE FALLBACK
            # -------------------------------------------------
            #
            # This is only used when there are no usable SKU
            # matches.  It preserves the previous behavior for
            # listings whose CSV/API SKU information cannot be
            # matched.
            # -------------------------------------------------

            title_key = _normalize_match_text(
                listing.get("title", "")
            )

            candidates = api_df[
                api_df["_title_key"] == title_key
            ].copy()

            if candidates.empty:
                continue

            scores = []

            for api_listing_id, group in candidates.groupby(
                "etsy_api_listing_id"
            ):
                api_skus = set(
                    str(v).strip().lower()
                    for v in group["etsy_api_sku"].tolist()
                    if str(v).strip()
                )

                scores.append(
                    (
                        len(csv_skus & api_skus),
                        len(api_skus),
                        api_listing_id,
                    )
                )

            scores.sort(reverse=True)
            best_overlap, _, best_id = scores[0]

            selected = candidates[
                candidates["etsy_api_listing_id"]
                == best_id
            ].copy()

        listings.at[
            listing_index,
            "etsy_api_listing_id",
        ] = str(best_id)

        prices = pd.to_numeric(
            selected["etsy_api_price"],
            errors="coerce",
        ).dropna()

        if not prices.empty:
            listings.at[
                listing_index,
                "etsy_api_price",
            ] = float(prices.min())

        mask = (
            variants["etsy_listing_key"]
            == listing["etsy_listing_key"]
        )

        for variant_index, variant in variants.loc[
            mask
        ].iterrows():
            sku = str(
                variant.get("etsy_sku", "")
            ).strip().lower()

            if not sku or sku == "unavailable_sku":
                continue

            matches = selected[
                selected["_sku_key"] == sku
            ]
            price_values = pd.to_numeric(
                matches["etsy_api_price"],
                errors="coerce",
            ).dropna()

            if price_values.empty:
                continue

            variants.at[
                variant_index,
                "etsy_api_listing_id",
            ] = str(best_id)
            variants.at[
                variant_index,
                "etsy_api_price",
            ] = float(price_values.iloc[0])

            api_match = matches.iloc[0]

            for column in [
                "etsy_api_variation_1_name",
                "etsy_api_variation_1_value",
                "etsy_api_variation_label",
            ]:
                if column in api_match.index:
                    variants.at[
                        variant_index,
                        column,
                    ] = api_match[column]

    print(
        "Etsy API pricing enrichment: "
        f"{int(variants['etsy_api_price'].notna().sum()):,} "
        "SKU prices retrieved."
    )

    return listings, variants


def apply_etsy_api_prices(etsy_rows, etsy_variants):
    """
    Apply Etsy API prices and variation information by SKU.

    The Etsy API associates each price with a specific SKU.
    Therefore, API pricing must be matched by SKU rather than
    by the SKU's positional index in the Etsy CSV.

    The original etsy_sku column in etsy_rows is preserved.
    """

    api_columns = etsy_variants[
        [
            "etsy_listing_key",
            "etsy_sku",
            "etsy_api_listing_id",
            "etsy_api_price",
            "etsy_api_variation_1_name",
            "etsy_api_variation_1_value",
            "etsy_api_variation_label",
        ]
    ].copy()

    api_columns["_sku_key"] = (
        api_columns["etsy_sku"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    rows = etsy_rows.copy()

    rows["_sku_key"] = (
        rows["etsy_sku"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    # Do not merge etsy_sku itself.  etsy_rows already contains
    # the authoritative Etsy SKU column.
    api_columns = api_columns[
        [
            "etsy_listing_key",
            "_sku_key",
            "etsy_api_listing_id",
            "etsy_api_price",
            "etsy_api_variation_1_name",
            "etsy_api_variation_1_value",
            "etsy_api_variation_label",
        ]
    ]

    rows = rows.merge(
        api_columns,
        on=[
            "etsy_listing_key",
            "_sku_key",
        ],
        how="left",
    )

    rows["etsy_listing_id"] = (
        rows["etsy_api_listing_id"]
        .fillna(rows["etsy_listing_id"])
    )

    rows["etsy_price"] = (
        pd.to_numeric(
            rows["etsy_api_price"],
            errors="coerce",
        )
        .combine_first(
            pd.to_numeric(
                rows["etsy_price"],
                errors="coerce",
            )
        )
    )

    # API variation information is authoritative when available.
    api_label_available = (
        rows["etsy_api_variation_label"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    )

    rows.loc[
        api_label_available,
        "etsy_variation_label",
    ] = rows.loc[
        api_label_available,
        "etsy_api_variation_label",
    ]

    api_value_available = (
        rows["etsy_api_variation_1_value"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    )

    rows.loc[
        api_value_available,
        "etsy_variation_1_value",
    ] = rows.loc[
        api_value_available,
        "etsy_api_variation_1_value",
    ]

    api_name_available = (
        rows["etsy_api_variation_1_name"]
        .fillna("")
        .astype(str)
        .str.strip()
        != ""
    )

    rows.loc[
        api_name_available,
        "etsy_variation_1_name",
    ] = rows.loc[
        api_name_available,
        "etsy_api_variation_1_name",
    ]

    return rows.drop(
        columns=[
            "_sku_key",
            "etsy_api_listing_id",
            "etsy_api_price",
            "etsy_api_variation_1_name",
            "etsy_api_variation_1_value",
            "etsy_api_variation_label",
        ],
        errors="ignore",
    )

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    ts = datetime.now(
        timezone.utc
    ).isoformat()

    # -----------------------------------------------------
    # READ ETSY DATA
    # -----------------------------------------------------

    etsy_df = read_etsy_csv(
        ETSY_CSV
    )

    print()
    print("=" * 120)
    print("ETSY LISTING COUNT PIPELINE DIAGNOSTIC")
    print("=" * 120)
    print(f"CSV physical rows: {len(etsy_df)}")

    etsy_listings, etsy_variants = (
        build_etsy_tables(
            etsy_df
        )
    )

    print(f"Listings after build_etsy_tables: {len(etsy_listings)}")
    print(f"Variant rows after build_etsy_tables: {len(etsy_variants)}")

    # -----------------------------------------------------
    # ENRICH FROM LIVE ETSY API BEFORE BUILDING etsy_rows
    # -----------------------------------------------------
    #
    # This allows active Etsy listings missing from the CSV
    # to be added to both etsy_listings and etsy_variants
    # before the comparison row table is constructed.
    # -----------------------------------------------------

    etsy_listings, etsy_variants = enrich_etsy_prices_from_api(
        etsy_listings,
        etsy_variants,
    )

    print(
        "Listings after live Etsy API enrichment: "
        f"{len(etsy_listings)}"
    )
    print(
        "Variant rows after live Etsy API enrichment: "
        f"{len(etsy_variants)}"
    )

    if "etsy_analysis_include" in etsy_listings.columns:
        included_count = int(
            etsy_listings[
                "etsy_analysis_include"
            ].fillna(False).astype(bool).sum()
        )

        excluded_count = (
            len(etsy_listings)
            - included_count
        )

        print(
            f"Listings marked analysis_include=True: "
            f"{included_count}"
        )
        print(
            f"Listings excluded by analysis_include flag: "
            f"{excluded_count}"
        )

        if excluded_count > 0:
            excluded = etsy_listings[
                ~etsy_listings[
                    "etsy_analysis_include"
                ].fillna(False).astype(bool)
            ]

            diagnostic_columns = [
                column
                for column in [
                    "etsy_listing_key",
                    "title",
                    "etsy_duplicate_title",
                    "etsy_duplicate_title_group",
                ]
                if column in excluded.columns
            ]

            print()
            print("EXCLUDED LISTINGS:")
            print(
                excluded[
                    diagnostic_columns
                ].to_string(
                    index=False
                )
            )

    print("=" * 120)
    print("END ETSY LISTING COUNT PIPELINE DIAGNOSTIC")
    print("=" * 120)
    print()

    etsy_rows = etsy_variants.merge(
        etsy_listings[
            [
                "etsy_listing_key",
                "title",
                "price",
                "quantity",
            ]
        ],
        on="etsy_listing_key",
        how="left",
    )

    etsy_rows = etsy_rows.rename(
        columns={
            "etsy_source_row":
                "etsy_row_number",


            "title":
                "etsy_title",

            "price":
                "etsy_price",

            "quantity":
                "etsy_quantity",
        }
    )

    etsy_rows["etsy_listing_id"] = (
        etsy_rows["etsy_listing_key"]
    )

    etsy_rows = etsy_rows[
        [
        "etsy_row_number",
        "etsy_listing_id",
        "etsy_listing_key",
        "etsy_title",
        "etsy_sku",
        "etsy_sku_index",
        "etsy_price",
        "etsy_quantity",
        "etsy_variation_1_name",
        "etsy_variation_1_value",
        "etsy_variation_2_name",
        "etsy_variation_2_value",
        "etsy_variation_label",
        ]
    ]

    etsy_rows = apply_etsy_api_prices(
        etsy_rows,
        etsy_variants,
    )

    listing_id_map = etsy_listings[[
        "etsy_listing_key",
        "etsy_api_listing_id",
    ]].copy()
    etsy_rows = etsy_rows.merge(
        listing_id_map,
        on="etsy_listing_key",
        how="left",
    )
    etsy_rows["etsy_listing_id"] = etsy_rows[
        "etsy_api_listing_id"
    ].fillna(etsy_rows["etsy_listing_id"])
    etsy_rows = etsy_rows.drop(
        columns=["etsy_api_listing_id"],
        errors="ignore",
    )

    etsy_rows = etsy_rows.to_dict(
        "records"
    )

    # -----------------------------------------------------
    # GET PRINTIFY DATA
    # -----------------------------------------------------

    client = PrintifyClient()

    shop_id = client.get_shop_id()

    printify_rows = (
        client.export_variant_rows(
            shop_id
        )
    )

    # -----------------------------------------------------
    # MATCH ETSY TO PRINTIFY
    # -----------------------------------------------------

    (
        comparison,
        listing_summary,
        attention,
        printify_only,
    ) = build_comparison(
        etsy_rows,
        printify_rows,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # -----------------------------------------------------
    # EXISTING OUTPUTS
    # -----------------------------------------------------

    comparison.to_csv(
        OUTPUT_DIR
        / "etsy_printify_comparison.csv",
        index=False,
    )

    # -----------------------------------------------------
    # NUTCRACKER PROFITABILITY DIAGNOSTIC
    # -----------------------------------------------------

    nutcracker_rows = comparison[
        comparison["etsy_title"]
        .astype(str)
        .str.contains(
            "Nutcracker",
            case=False,
            na=False,
        )
    ].copy()

    print()
    print("=" * 120)
    print("NUTCRACKER COMPARISON / PROFITABILITY DIAGNOSTIC")
    print("=" * 120)

    if nutcracker_rows.empty:

        print("NO NUTCRACKER ROWS FOUND")

    else:

        columns_to_show = [
            column
            for column in [
                "etsy_listing_id",
                "etsy_title",
                "etsy_sku",
                "etsy_price",
                "etsy_variation_label",
                "printify_sku",
                "printify_variant_title",
                "printify_cost",
                "printify_shipping_cost",
                "match_status",
            ]
            if column in nutcracker_rows.columns
        ]

        print(
            nutcracker_rows[
                columns_to_show
            ].to_string(
                index=False
            )
        )

    print("=" * 120)
    print("END NUTCRACKER COMPARISON / PROFITABILITY DIAGNOSTIC")
    print()

    listing_summary.to_csv(
        OUTPUT_DIR
        / "listing_summary.csv",
        index=False,
    )

    attention.to_csv(
        OUTPUT_DIR
        / "needs_attention.csv",
        index=False,
    )

    printify_only.to_csv(
        OUTPUT_DIR
        / "printify_only.csv",
        index=False,
    )

    pd.DataFrame(
        printify_rows
    ).to_csv(
        OUTPUT_DIR
        / "printify_products.csv",
        index=False,
    )

    # -----------------------------------------------------
    # NUTCRACKER PRINTIFY PRODUCT VARIANT DIAGNOSTIC
    # -----------------------------------------------------

    nutcracker_product_id = "6a481f6a238fda116e0677ef"

    nutcracker_printify_rows = pd.DataFrame(
        printify_rows
    )

    if not nutcracker_printify_rows.empty:
        nutcracker_printify_rows = (
            nutcracker_printify_rows[
                nutcracker_printify_rows[
                    "printify_product_id"
                ].astype(str)
                == nutcracker_product_id
            ]
        )

    print()
    print("=" * 120)
    print("NUTCRACKER PRINTIFY PRODUCT VARIANT DIAGNOSTIC")
    print("=" * 120)

    if nutcracker_printify_rows.empty:
        print("NO PRINTIFY VARIANTS FOUND")
    else:

        columns_to_show = [
            column
            for column in [
                "printify_product_id",
                "printify_variant_id",
                "printify_sku",
                "printify_variant_title",
                "printify_price",
                "printify_cost",
                "printify_shipping_cost",
                "printify_enabled",
                "printify_available",
                "printify_options_json",
            ]
            if column in nutcracker_printify_rows.columns
        ]

        print(
            nutcracker_printify_rows[
                columns_to_show
            ].to_string(
                index=False
            )
        )

    print("=" * 120)
    print("END NUTCRACKER PRINTIFY PRODUCT VARIANT DIAGNOSTIC")
    print("=" * 120)
    print()

    # -----------------------------------------------------
    # NUTCRACKER PROFITABILITY INPUT DIAGNOSTIC
    # -----------------------------------------------------

    nutcracker_mask = comparison.astype(str).apply(
        lambda col: col.str.contains(
            "Nutcracker",
            case=False,
            na=False,
        )
    ).any(axis=1)

    nutcracker_rows = comparison[
        nutcracker_mask
    ]

    print()
    print("=" * 120)
    print("NUTCRACKER PROFITABILITY INPUT DIAGNOSTIC")
    print("=" * 120)

    if nutcracker_rows.empty:
        print("NO NUTCRACKER ROWS FOUND")
    else:

        columns_to_show = [
            column
            for column in [
                "etsy_listing_id",
                "etsy_title",
                "etsy_sku",
                "etsy_price",
                "etsy_variation_label",
                "printify_product_id",
                "printify_sku",
                "printify_variant_title",
                "printify_price",
                "printify_cost",
                "printify_shipping_cost",
                "match_status",
            ]
            if column in nutcracker_rows.columns
        ]

        print(
            nutcracker_rows[
                columns_to_show
            ].to_string(
                index=False
            )
        )

    print("=" * 120)
    print("END NUTCRACKER PROFITABILITY INPUT DIAGNOSTIC")
    print("=" * 120)
    print()

    # -----------------------------------------------------
    # VARIANT-LEVEL PROFITABILITY
    # -----------------------------------------------------

    (
        profitability_all,
        low_profit,
    ) = build_profitability_report(
        comparison
    )

    profitability_all.to_csv(
        OUTPUT_DIR
        / "profitability_all.csv",
        index=False,
    )

    low_profit.to_csv(
        OUTPUT_DIR
        / "low_profit_products.csv",
        index=False,
    )

    # -----------------------------------------------------
    # LISTING-LEVEL PROFITABILITY
    # -----------------------------------------------------

    (
        listing_profitability,
        low_profit_listings,
    ) = build_listing_profitability_report(
        profitability_all,
        etsy_listings,
    )

    listing_profitability.to_csv(
        OUTPUT_DIR
        / "listing_profitability.csv",
        index=False,
    )

    low_profit_listings.to_csv(
        OUTPUT_DIR
        / "low_profit_listings.csv",
        index=False,
    )

    # -----------------------------------------------------
    # COUNTS
    # -----------------------------------------------------

    status_counts = (
        comparison[
            "match_status"
        ].value_counts()
        if not comparison.empty
        else {}
    )

    listing_counts = (
        listing_summary[
            "listing_status"
        ].value_counts()
        if not listing_summary.empty
        else {}
    )

    profit_calculable = (
        profitability_all[
            profitability_all[
                "profitability_status"
            ].isin(
                [
                    "OK",
                    "LOW_PROFIT",
                ]
            )
        ]
        if not profitability_all.empty
        else pd.DataFrame()
    )

    calculable_count = len(
        profit_calculable
    )

    low_profit_count = len(
        low_profit
    )

    low_profit_percentage = (
        low_profit_count
        / calculable_count
        * 100
        if calculable_count
        else 0
    )

    # -----------------------------------------------------
    # CORRECTED LISTING-LEVEL STATUS NAMES
    # -----------------------------------------------------

    listing_calculable = listing_profitability[
        listing_profitability[
            "status"
        ].isin(
            [
                "HAS_LOSS_VARIANT",
                "HAS_UNDER_10_VARIANT",
                "HAS_10_TO_14.99_VARIANT",
                "ALL_VARIANTS_15%+",
            ]
        )
    ]

    listing_low_profit_count = len(
        low_profit_listings
    )

    listing_calculable_count = len(
        listing_calculable
    )

    listing_low_profit_percentage = (
        listing_low_profit_count
        / listing_calculable_count
        * 100
        if listing_calculable_count
        else 0
    )

    # -----------------------------------------------------
    # SYNC SUMMARY
    # -----------------------------------------------------

    lines = [
        "ETSY ↔ PRINTIFY SYNC SUMMARY",

        f"Run UTC: {ts}",

        "",

        f"Etsy listings: "
        f"{len(etsy_listings):,}",

        f"Etsy SKU rows: "
        f"{len(etsy_rows):,}",

        f"Printify variants: "
        f"{len(printify_rows):,}",

        "",

        "SKU STATUS",
    ]

    for status in [
        "MATCHED",
        "ETSY_ONLY",
        "UNAVAILABLE_SKU",
        "MISSING_SKU",
        "DUPLICATE_SKU",
        "DUPLICATE_ETSY_SKU",
        "DUPLICATE_PRINTIFY_SKU",
    ]:

        lines.append(
            f"{status}: "
            f"{int(status_counts.get(status, 0)):,}"
        )

    lines += [
        "",

        "LISTING STATUS",
    ]

    for status in [
        "FULLY_MATCHED",
        "PARTIALLY_MATCHED",
        "NO_PRINTIFY_PRODUCTS",
        "HAS_DUPLICATE_SKU",
        "HAS_PRINTIFY_SKU_PROBLEM",
        "NEEDS_REVIEW",
        "NO_REAL_SKUS",
    ]:

        lines.append(
            f"{status}: "
            f"{int(listing_counts.get(status, 0)):,}"
        )

    lines += [
        "",

        "PROFITABILITY",

        f"Profitability threshold: "
        f"{LOW_PROFIT_THRESHOLD:.0f}%",

        "Printify shipping: "
        "U.S. first-item shipping",

        "Customer shipping charged: "
        "$0.00",

        "Etsy transaction fee: "
        "6.5%",

        "Etsy payment processing: "
        "3% + $0.25",

        "Offsite Ads: EXCLUDED",

        "Etsy listing fee: "
        "EXCLUDED from per-sale calculation",

        "",

        f"Matched products with calculable profit: "
        f"{calculable_count:,}",

        f"Products below "
        f"{LOW_PROFIT_THRESHOLD:.0f}% margin: "
        f"{low_profit_count:,}",

        f"Percent below threshold: "
        f"{low_profit_percentage:.1f}%",

        "",

        "LISTING-LEVEL PROFITABILITY",

        f"Total active Etsy listings: "
        f"{len(etsy_listings):,}",

        f"Etsy listings with calculable profitability: "
        f"{listing_calculable_count:,}",

        f"Etsy listings without calculable profitability: "
        f"{len(etsy_listings) - listing_calculable_count:,}",

        f"Etsy listings below "
        f"{LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{listing_low_profit_count:,}",

        f"Percent of calculable listings below threshold: "
        f"{listing_low_profit_percentage:.1f}%",

        "",
    ]

    # ---------------------------------------------------------
    # DAILY EMAIL: LISTINGS WITH LOW-PROFIT VARIANTS
    # ---------------------------------------------------------
    #
    # The detailed profitability information is VARIANT-LEVEL.
    #
    # A listing appears here when at least one matched variant
    # is below the profitability threshold. For each flagged
    # listing, show EVERY matched variant so the user can see
    # the actual economics of each size/color/variation rather
    # than seeing only a single "worst" variant.
    # ---------------------------------------------------------

    lines += [
        "LISTINGS REQUIRING ATTENTION",
        "",
    ]

    if low_profit_listings.empty:
        lines += [
            "None.",
            "All calculable Etsy listings are at or above "
            f"{LOW_PROFIT_THRESHOLD:.0f}% net margin.",
            "",
        ]

    else:
        lines += [
            f"Listings with at least one variant below "
            f"{LOW_PROFIT_THRESHOLD:.0f}% net margin are shown.",
            "Each listing includes EVERY matched variant so "
            "variant pricing can be reviewed individually.",
            "",
        ]

        # -----------------------------------------------------
        # GROUP LOW-PROFIT LISTINGS
        # -----------------------------------------------------

        flagged_listing_ids = set(
            low_profit_listings[
                "etsy_listing_id"
            ]
            .astype(str)
        )

        detail_data = profitability_all.copy()

        if "etsy_listing_id" in detail_data.columns:
            detail_data["etsy_listing_id"] = (
                detail_data["etsy_listing_id"]
                .astype(str)
            )

        # Preserve listing order from the low-profit summary.
        for number, (_, listing_row) in enumerate(
            low_profit_listings.iterrows(),
            start=1,
        ):

            listing_id = str(
                listing_row.get(
                    "etsy_listing_id",
                    "",
                )
            )

            title = str(
                listing_row.get(
                    "etsy_title",
                    "Untitled listing",
                )
            ).replace(
                "\n",
                " ",
            ).replace(
                "\r",
                " ",
            ).strip()

            lines.append(
                f"{number}. {title}"
            )

            listing_variants = detail_data[
                detail_data["etsy_listing_id"]
                == listing_id
            ].copy()

            if listing_variants.empty:
                lines.append(
                    "   No variant-level profitability "
                    "data available."
                )
                lines.append("")
                continue

            # Sort the variants by profitability, worst first,
            # while retaining every matched variant.
            listing_variants["_margin_sort"] = pd.to_numeric(
                listing_variants[
                    "estimated_net_margin_pct"
                ],
                errors="coerce",
            )

            listing_variants = listing_variants.sort_values(
                "_margin_sort",
                ascending=True,
                na_position="last",
            )

            for _, variant in listing_variants.iterrows():

                variation_label = str(
                    variant.get(
                        "etsy_variation_label",
                        "",
                    )
                    or ""
                ).strip()

                sku = str(
                    variant.get(
                        "etsy_sku",
                        "",
                    )
                    or ""
                ).strip()

                etsy_price = pd.to_numeric(
                    variant.get(
                        "etsy_price_for_profit"
                    ),
                    errors="coerce",
                )

                printify_cost = pd.to_numeric(
                    variant.get(
                        "printify_cost_for_profit"
                    ),
                    errors="coerce",
                )

                shipping = pd.to_numeric(
                    variant.get(
                        "printify_shipping_for_profit"
                    ),
                    errors="coerce",
                )

                net_profit = pd.to_numeric(
                    variant.get(
                        "estimated_net_profit"
                    ),
                    errors="coerce",
                )

                margin = pd.to_numeric(
                    variant.get(
                        "estimated_net_margin_pct"
                    ),
                    errors="coerce",
                )

                target_price = pd.to_numeric(
                    variant.get(
                        "minimum_price_for_15pct_margin_variant"
                    ),
                    errors="coerce",
                )

                parts = []

                if variation_label:
                    parts.append(
                        variation_label
                    )

                if sku:
                    parts.append(
                        f"SKU {sku}"
                    )

                if pd.notna(etsy_price):
                    parts.append(
                        f"Etsy ${etsy_price:,.2f}"
                    )

                if pd.notna(printify_cost):
                    parts.append(
                        f"Printify ${printify_cost:,.2f}"
                    )

                if pd.notna(shipping):
                    parts.append(
                        f"Shipping ${shipping:,.2f}"
                    )

                if pd.notna(net_profit):
                    parts.append(
                        f"Net ${net_profit:,.2f}"
                    )
                else:
                    parts.append(
                        "Net unavailable"
                    )

                if pd.notna(margin):
                    parts.append(
                        f"Margin {margin:.1f}%"
                    )
                else:
                    parts.append(
                        "Margin unavailable"
                    )

                if pd.notna(target_price):
                    parts.append(
                        f"15% price ${target_price:,.2f}"
                    )

                    if pd.notna(
                        etsy_price
                    ):
                        increase = (
                            target_price
                            - etsy_price
                        )

                        if increase > 0:
                            parts.append(
                                f"Increase ${increase:,.2f}"
                            )

                lines.append(
                    "   - "
                    + " | ".join(parts)
                )

            lines.append("")

    lines += [
        "",
        "ETSY_ONLY means the Etsy SKU is not currently found in Printify.",

        "Profitability requires a matched Printify SKU "
        "and available U.S. shipping data.",
    ]

    (
        OUTPUT_DIR
        / "sync_summary.txt"
    ).write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # PROFITABILITY SUMMARY
    # -----------------------------------------------------

    profitability_lines = [

        "LOW PROFITABILITY SUMMARY",

        f"Run UTC: {ts}",

        "",

        f"Profitability threshold: "
        f"{LOW_PROFIT_THRESHOLD:.0f}%",

        "",

        "VARIANT LEVEL",

        f"Matched products with calculable profit: "
        f"{calculable_count:,}",

        f"Products below "
        f"{LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{low_profit_count:,}",

        f"Percent below threshold: "
        f"{low_profit_percentage:.1f}%",

        "",

        "LISTING LEVEL",

        f"Etsy listings with calculable profitability: "
        f"{listing_calculable_count:,}",

        f"Etsy listings below "
        f"{LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{listing_low_profit_count:,}",

        f"Percent of calculable listings below threshold: "
        f"{listing_low_profit_percentage:.1f}%",

        "",

        "ASSUMPTIONS",

        "Customer pays $0 shipping.",

        "Seller pays Printify U.S. first-item shipping.",

        "Etsy transaction fee: 6.5%.",

        "Etsy payment processing: 3% + $0.25.",

        "Offsite Ads excluded.",

        "Etsy listing fee excluded from per-sale calculation.",

        "",

        "See low_profit_products.csv for variant-level results.",

        "See listing_profitability.csv for one row per Etsy listing.",

        "See low_profit_listings.csv for listings requiring pricing attention.",
    ]

    (
        OUTPUT_DIR
        / "profitability_summary.txt"
    ).write_text(
        "\n".join(profitability_lines)
        + "\n",
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # LISTING PROFITABILITY SUMMARY
    # -----------------------------------------------------

    listing_summary_lines = [

        "LISTING PROFITABILITY SUMMARY",

        f"Run UTC: {ts}",

        "",

        f"Total Etsy listings: "
        f"{len(etsy_listings):,}",

        f"Listings with calculable profitability: "
        f"{listing_calculable_count:,}",

        f"Listings below {LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{listing_low_profit_count:,}",

        "",

        "STATUS BREAKDOWN",

        # CORRECTED STATUS NAMES
        f"LISTINGS WITH LOSS VARIANT: "
        f"{int((listing_profitability['status'] == 'HAS_LOSS_VARIANT').sum()):,}",

        f"LISTINGS WITH UNDER 10% VARIANT: "
        f"{int((listing_profitability['status'] == 'HAS_UNDER_10_VARIANT').sum()):,}",

        f"LISTINGS WITH 10–14.99% VARIANT: "
        f"{int((listing_profitability['status'] == 'HAS_10_TO_14.99_VARIANT').sum()):,}",

        f"LISTINGS WITH ALL VARIANTS AT 15%+: "
        f"{int((listing_profitability['status'] == 'ALL_VARIANTS_15%+').sum()):,}",

        f"NOT_CALCULABLE: "
        f"{int((listing_profitability['status'] == 'NOT_CALCULABLE').sum()):,}",

        "",

        "The listing status uses the worst matched variant in each Etsy listing.",

        "The minimum 15% price is the highest price required by any matched variant in the listing.",

        "Offsite Ads are excluded.",

        "Etsy listing fees are excluded.",

        "Customer shipping is assumed to be $0.",

        "Seller pays Printify U.S. first-item shipping.",
    ]

    (
        OUTPUT_DIR
        / "listing_profitability_summary.txt"
    ).write_text(
        "\n".join(
            listing_summary_lines
        )
        + "\n",
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

    connection = initialize_database(
        DATABASE_PATH
    )

    # -------------------------------------------------
    # HISTORICAL SNAPSHOT TABLES
    # -------------------------------------------------

    initialize_history_tables(
        connection
    )

    replace_table(
        connection,
        "etsy_listings",
        etsy_listings,
    )

    replace_table(
        connection,
        "etsy_variants",
        etsy_variants,
    )

    replace_table(
        connection,
        "printify_variants",
        pd.DataFrame(
            printify_rows
        ),
    )

    replace_table(
        connection,
        "etsy_printify_comparison",
        comparison,
    )

    replace_table(
        connection,
        "listing_summary",
        listing_summary,
    )

    replace_table(
        connection,
        "needs_attention",
        attention,
    )

    replace_table(
        connection,
        "printify_only",
        printify_only,
    )

    matched_count = (
        int(
            (
                comparison[
                    "match_status"
                ]
                == "MATCHED"
            ).sum()
        )
        if not comparison.empty
        else 0
    )

    # -------------------------------------------------
    # HISTORICAL SNAPSHOT
    # -------------------------------------------------

    snapshot_id = create_snapshot(
        connection,
        ts,
        len(etsy_listings),
        len(etsy_rows),
        len(printify_rows),
        matched_count,
        len(attention),
    )

    save_comparison_history(
        connection,
        snapshot_id,
        comparison,
    )

    # -------------------------------------------------
    # EXISTING SYNC HISTORY
    # -------------------------------------------------

    record_run(
        connection,
        ts,
        len(etsy_listings),
        len(etsy_rows),
        len(printify_rows),
        matched_count,
        len(attention),
    )

    connection.close()

    # -----------------------------------------------------
    # CONSOLE OUTPUT
    # -----------------------------------------------------

    print("")

    print("Sync complete.")

    print(
        f"Matched: "
        f"{int(status_counts.get('MATCHED', 0)):,}"
    )

    print(
        f"Etsy-only: "
        f"{int(status_counts.get('ETSY_ONLY', 0)):,}"
    )

    print(
        f"Needs attention: "
        f"{len(attention):,}"
    )

    print(
        f"Printify-only: "
        f"{len(printify_only):,}"
    )

    print("")

    print(
        f"Profitability products analyzed: "
        f"{calculable_count:,}"
    )

    print(
        f"Products below 15% margin: "
        f"{low_profit_count:,}"
    )

    print("")

    print(
        f"Total active Etsy listings: "
        f"{len(etsy_listings):,}"
    )

    print(
        f"Etsy listings with calculable profitability: "
        f"{listing_calculable_count:,}"
    )

    print(
        f"Etsy listings without calculable profitability: "
        f"{len(etsy_listings) - listing_calculable_count:,}"
    )

    print(
        f"Etsy listings below 15%: "
        f"{listing_low_profit_count:,}"
    )


if __name__ == "__main__":
    main()
