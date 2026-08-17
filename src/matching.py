from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

import pandas as pd


def normalize_sku(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip().upper()


def money(value: Any):
    """
    Convert a money value to dollars.

    Etsy prices are already dollar values.
    Printify API price/cost values are cents, so those are
    converted separately below.
    """
    try:
        text = str(value).replace("$", "").replace(",", "").strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def printify_money(value: Any):
    """
    Printify API returns price/cost in cents.
    Convert cents to dollars.
    """
    try:
        text = str(value).replace("$", "").replace(",", "").strip()
        return float(text) / 100 if text else None
    except (TypeError, ValueError):
        return None


def build_etsy_sku_listing_map(etsy):
    """
    Build SKU -> set of distinct Etsy listing IDs.

    A SKU repeated across variations of the same listing is normal.
    It is only considered shared when it appears on multiple
    distinct Etsy listings.
    """
    sku_listings = defaultdict(set)

    for _, row in etsy.iterrows():
        sku = normalize_sku(row.get("etsy_sku"))
        listing_id = str(row.get("etsy_listing_id", "")).strip()

        if (
            sku
            and sku != "UNAVAILABLE_SKU"
            and listing_id
        ):
            sku_listings[sku].add(listing_id)

    return sku_listings


def build_comparison(etsy_rows, printify_rows):
    etsy = pd.DataFrame(etsy_rows)
    pri = pd.DataFrame(printify_rows)

    if etsy.empty:
        etsy = pd.DataFrame(
            columns=[
                "etsy_row_number",
                "etsy_listing_id",
                "etsy_title",
                "etsy_sku",
                "etsy_price",
                "etsy_quantity",
            ]
        )

    if pri.empty:
        pri = pd.DataFrame(
            columns=[
                "printify_product_id",
                "printify_title",
                "printify_variant_id",
                "printify_sku",
                "printify_price",
                "printify_cost",
                "printify_enabled",
                "print_provider_id",
            ]
        )

    etsy["normalized_sku"] = etsy["etsy_sku"].map(
        normalize_sku
    )

    pri["normalized_sku"] = pri["printify_sku"].map(
        normalize_sku
    )

    # Count how many Printify variants use each SKU.
    printify_counts = Counter(
        sku
        for sku in pri["normalized_sku"]
        if sku
    )

    # Determine how many distinct Etsy listings use each SKU.
    sku_listings = build_etsy_sku_listing_map(etsy)

    # Primary Printify row for each SKU.
    # If Printify has the same SKU multiple times, we flag it
    # separately rather than multiplying Etsy rows.
    join = pri.drop_duplicates(
        "normalized_sku",
        keep="first",
    )

    comp = etsy.merge(
        join,
        on="normalized_sku",
        how="left",
        suffixes=("", "_printify"),
    )

    # Number of distinct Etsy listings using this SKU.
    comp["etsy_listing_count_for_sku"] = (
        comp["normalized_sku"].map(
            lambda sku: len(
                sku_listings.get(sku, set())
            )
        )
    )

    # Informational flag: the SKU is shared by multiple Etsy listings.
    comp["etsy_sku_shared_across_listings"] = (
        comp["etsy_listing_count_for_sku"] > 1
    )

    # Core match status.
    def classify(row):
        sku = row["normalized_sku"]

        if not sku:
            return "MISSING_SKU"

        if sku == "UNAVAILABLE_SKU":
            return "UNAVAILABLE_SKU"

        if printify_counts[sku] == 0:
            return "ETSY_ONLY"

        if printify_counts[sku] > 1:
            return "DUPLICATE_PRINTIFY_SKU"

        # Shared Etsy SKU is still a successful Printify match.
        return "MATCHED"

    comp["match_status"] = comp.apply(
        classify,
        axis=1,
    )

    # Dollar values.
    comp["etsy_price_numeric"] = comp[
        "etsy_price"
    ].map(money)

    comp["printify_price_numeric"] = comp[
        "printify_price"
    ].map(printify_money)

    comp["printify_cost_numeric"] = comp[
        "printify_cost"
    ].map(printify_money)

    # Gross product margin before Etsy fees, payment fees,
    # shipping, taxes, etc.
    comp["estimated_gross_margin"] = (
        comp["etsy_price_numeric"]
        - comp["printify_cost_numeric"]
    )

    # Gross margin percentage.
    comp["estimated_gross_margin_pct"] = (
        comp["estimated_gross_margin"]
        / comp["etsy_price_numeric"]
        * 100
    )

    # Avoid infinite/invalid percentages.
    comp.loc[
        comp["etsy_price_numeric"].isna()
        | (comp["etsy_price_numeric"] == 0),
        "estimated_gross_margin_pct",
    ] = None

    # Printify SKUs that do not appear anywhere in Etsy.
    etsy_skus = {
        sku
        for sku in sku_listings
        if sku
    }

    ponly = pri[
        (pri["normalized_sku"] != "")
        & (~pri["normalized_sku"].isin(etsy_skus))
    ].copy()

    ponly["status"] = "PRINTIFY_ONLY"

    # Only genuine actionable issues.
    attention = comp[
        comp["match_status"].isin(
            [
                "ETSY_ONLY",
                "MISSING_SKU",
                "DUPLICATE_PRINTIFY_SKU",
            ]
        )
    ].copy()

    def listing_status(group):
        real = group[
            ~group.match_status.isin(
                [
                    "UNAVAILABLE_SKU",
                    "MISSING_SKU",
                ]
            )
        ]

        if real.empty:
            return "NO_REAL_SKUS"

        statuses = set(real.match_status)

        if statuses == {"MATCHED"}:
            return "FULLY_MATCHED"

        if "MATCHED" in statuses:
            return "PARTIALLY_MATCHED"

        if statuses == {"ETSY_ONLY"}:
            return "NO_PRINTIFY_PRODUCTS"

        if "DUPLICATE_PRINTIFY_SKU" in statuses:
            return "HAS_PRINTIFY_SKU_PROBLEM"

        return "NEEDS_REVIEW"

    if comp.empty:
        summary = pd.DataFrame()

    else:
        summary = (
            comp.groupby(
                [
                    "etsy_listing_id",
                    "etsy_title",
                ],
                dropna=False,
                as_index=False,
            )
            .agg(
                etsy_sku_rows=(
                    "etsy_sku",
                    "size",
                ),

                matched_skus=(
                    "match_status",
                    lambda s: int(
                        (s == "MATCHED").sum()
                    ),
                ),

                etsy_only_skus=(
                    "match_status",
                    lambda s: int(
                        (s == "ETSY_ONLY").sum()
                    ),
                ),

                unavailable_skus=(
                    "match_status",
                    lambda s: int(
                        (s == "UNAVAILABLE_SKU").sum()
                    ),
                ),

                printify_duplicate_skus=(
                    "match_status",
                    lambda s: int(
                        (
                            s
                            == "DUPLICATE_PRINTIFY_SKU"
                        ).sum()
                    ),
                ),

                shared_etsy_skus=(
                    "etsy_sku_shared_across_listings",
                    "sum",
                ),
            )
        )

        statuses = (
            comp.groupby(
                [
                    "etsy_listing_id",
                    "etsy_title",
                ],
                dropna=False,
            )
            .apply(
                listing_status,
                include_groups=False,
            )
            .rename("listing_status")
            .reset_index()
        )

        summary = summary.merge(
            statuses,
            on=[
                "etsy_listing_id",
                "etsy_title",
            ],
            how="left",
        )

    return (
        comp.drop(
            columns=["normalized_sku"],
            errors="ignore",
        ),
        summary,
        attention.drop(
            columns=["normalized_sku"],
            errors="ignore",
        ),
        ponly.drop(
            columns=["normalized_sku"],
            errors="ignore",
        ),
    )
