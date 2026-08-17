from __future__ import annotations

from datetime import datetime, timezone
import math

import pandas as pd

from .config import DATABASE_PATH, ETSY_CSV, OUTPUT_DIR
from .database import initialize_database, record_run, replace_table
from .etsy import build_etsy_tables, read_etsy_csv
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
    Roll variant-level profitability up to one row per Etsy
    listing.

    A listing is considered underpriced if ANY matched variant
    is below the 15% target.

    For the 15% target price, we calculate:

        Price - product cost - shipping
        - transaction fee
        - payment fee
        = 15% of Price

    Therefore:

        required price =
        (product cost + shipping + $0.25)
        / (1 - .065 - .03 - .15)

    The listing's required price is the highest required price
    among its matched variants.
    """

    # -----------------------------------------------------
    # TARGET PRICE CALCULATION
    # -----------------------------------------------------

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

        # Round UP to the nearest cent so the calculated price
        # does not fall just below the 15% target because of
        # rounding.
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

        # -------------------------------------------------
        # SUMMARIZE EACH LISTING
        # -------------------------------------------------

        def summarize_listing(group):

            margins = pd.to_numeric(
                group["estimated_net_margin_pct"],
                errors="coerce",
            ).dropna()

            profits = pd.to_numeric(
                group["estimated_net_profit"],
                errors="coerce",
            ).dropna()

            prices = pd.to_numeric(
                group["etsy_price_for_profit"],
                errors="coerce",
            ).dropna()

            target_prices = pd.to_numeric(
                group[
                    "minimum_price_for_15pct_margin_variant"
                ],
                errors="coerce",
            ).dropna()

            if margins.empty:
                worst_margin = None
                best_margin = None
            else:
                worst_margin = float(margins.min())
                best_margin = float(margins.max())

            worst_profit = (
                float(profits.min())
                if not profits.empty
                else None
            )

            current_price_min = (
                float(prices.min())
                if not prices.empty
                else None
            )

            current_price_max = (
                float(prices.max())
                if not prices.empty
                else None
            )

            minimum_price = (
                float(target_prices.max())
                if not target_prices.empty
                else None
            )

            if worst_margin is None:
                status = "NOT_CALCULABLE"

            elif worst_margin < 0:
                status = "LOSS"

            elif worst_margin < 10:
                status = "UNDER_10%"

            elif worst_margin < LOW_PROFIT_THRESHOLD:
                status = "10_TO_14.99%"

            else:
                status = "15%+"

            return pd.Series(
                {
                    "matched_variant_count": len(group),

                    "current_price_min":
                        current_price_min,

                    "current_price_max":
                        current_price_max,

                    "worst_net_profit":
                        worst_profit,

                    "worst_net_margin_pct":
                        worst_margin,

                    "best_net_margin_pct":
                        best_margin,

                    "minimum_price_for_15pct_margin":
                        minimum_price,

                    "status":
                        status,
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
                "current_price_min",
                "current_price_max",
                "worst_net_profit",
                "worst_net_margin_pct",
                "best_net_margin_pct",
                "minimum_price_for_15pct_margin",
                "status",
            ]
        )

    # -----------------------------------------------------
    # START WITH ALL ETSY LISTINGS
    # -----------------------------------------------------

    base = etsy_listings[
        [
            "etsy_listing_key",
            "title",
            "price",
        ]
    ].copy()

    base = base.rename(
        columns={
            "etsy_listing_key":
                "etsy_listing_id",

            "title":
                "etsy_title",

            "price":
                "current_etsy_price",
        }
    )

    base["etsy_listing_id"] = (
        base["etsy_listing_id"]
        .astype(str)
    )

    if listing_profitability.empty:

        report = base.copy()

        for column in [
            "matched_variant_count",
            "current_price_min",
            "current_price_max",
            "worst_net_profit",
            "worst_net_margin_pct",
            "best_net_margin_pct",
            "minimum_price_for_15pct_margin",
        ]:
            report[column] = pd.NA

        report["status"] = "NOT_CALCULABLE"

    else:

        listing_profitability[
            "etsy_listing_id"
        ] = (
            listing_profitability[
                "etsy_listing_id"
            ]
            .astype(str)
        )

        # The title from base is the authoritative Etsy title.
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

    # -----------------------------------------------------
    # PRICE INCREASE NEEDED
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # FINAL COLUMN ORDER
    # -----------------------------------------------------

    report = report[
        [
            "etsy_listing_id",
            "etsy_title",
            "current_etsy_price",

            "matched_variant_count",

            "current_price_min",
            "current_price_max",

            "worst_net_profit",
            "worst_net_margin_pct",
            "best_net_margin_pct",

            "minimum_price_for_15pct_margin",
            "price_increase_needed",

            "status",
        ]
    ]

    # -----------------------------------------------------
    # SORT
    # -----------------------------------------------------

    status_order = {
        "LOSS": 0,
        "UNDER_10%": 1,
        "10_TO_14.99%": 2,
        "15%+": 3,
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

    # Only listings actually below 15%.
    low_profit_listings = report[
        report["status"].isin(
            [
                "LOSS",
                "UNDER_10%",
                "10_TO_14.99%",
            ]
        )
    ].copy()

    return (
        report,
        low_profit_listings,
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

    etsy_listings, etsy_variants = (
        build_etsy_tables(
            etsy_df
        )
    )

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

            "etsy_listing_key":
                "etsy_listing_id",

            "title":
                "etsy_title",

            "price":
                "etsy_price",

            "quantity":
                "etsy_quantity",
        }
    )

    etsy_rows = etsy_rows[
        [
            "etsy_row_number",
            "etsy_listing_id",
            "etsy_title",
            "etsy_sku",
            "etsy_price",
            "etsy_quantity",
        ]
    ]

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

    listing_calculable = listing_profitability[
        listing_profitability[
            "status"
        ].isin(
            [
                "LOSS",
                "UNDER_10%",
                "10_TO_14.99%",
                "15%+",
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

        f"Etsy listings analyzed: "
        f"{listing_calculable_count:,}",

        f"Etsy listings below "
        f"{LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{listing_low_profit_count:,}",

        f"Percent of calculable listings below threshold: "
        f"{listing_low_profit_percentage:.1f}%",

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

        f"Listings below 15%: "
        f"{listing_low_profit_count:,}",

        "",

        "STATUS BREAKDOWN",

        f"LOSS: "
        f"{int((listing_profitability['status'] == 'LOSS').sum()):,}",

        f"UNDER_10%: "
        f"{int((listing_profitability['status'] == 'UNDER_10%').sum()):,}",

        f"10_TO_14.99%: "
        f"{int((listing_profitability['status'] == '10_TO_14.99%').sum()):,}",

        f"15%+: "
        f"{int((listing_profitability['status'] == '15%+').sum()):,}",

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
        f"Etsy listings analyzed: "
        f"{listing_calculable_count:,}"
    )

    print(
        f"Etsy listings below 15%: "
        f"{listing_low_profit_count:,}"
    )


if __name__ == "__main__":
    main()
