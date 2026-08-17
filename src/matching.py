from __future__ import annotations

import math
from collections import Counter
from typing import Any

import pandas as pd


def normalize_sku(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip().upper()


def money(value: Any):
    try:
        text = str(value).replace("$", "").replace(",", "").strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def classify(sku, ec, pc):
    if not sku:
        return "MISSING_SKU"

    if sku.lower() == "unavailable_sku":
        return "UNAVAILABLE_SKU"

    if ec[sku] > 1:
        return "DUPLICATE_SKU" if pc[sku] > 1 else "DUPLICATE_ETSY_SKU"

    if pc[sku] == 0:
        return "ETSY_ONLY"

    if pc[sku] > 1:
        return "DUPLICATE_PRINTIFY_SKU"

    return "MATCHED"


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

    etsy["normalized_sku"] = etsy["etsy_sku"].map(normalize_sku)
    pri["normalized_sku"] = pri["printify_sku"].map(normalize_sku)

    ec = Counter(
        s for s in etsy["normalized_sku"]
        if s and s != "UNAVAILABLE_SKU"
    )

    pc = Counter(
        s for s in pri["normalized_sku"]
        if s
    )

    join = pri.drop_duplicates(
        "normalized_sku",
        keep="first"
    )

    comp = etsy.merge(
        join,
        on="normalized_sku",
        how="left",
        suffixes=("", "_printify"),
    )

    comp["match_status"] = comp["normalized_sku"].map(
        lambda s: classify(s, ec, pc)
    )

    comp["etsy_price_numeric"] = comp["etsy_price"].map(money)
    comp["printify_cost_numeric"] = comp["printify_cost"].map(money)

    comp["estimated_gross_margin"] = (
        comp["etsy_price_numeric"]
        - comp["printify_cost_numeric"]
    )

    etsy_skus = set(ec)

    ponly = pri[
        (pri["normalized_sku"] != "")
        & (~pri["normalized_sku"].isin(etsy_skus))
    ].copy()

    ponly["status"] = "PRINTIFY_ONLY"

    attention = comp[
        comp["match_status"].isin(
            [
                "ETSY_ONLY",
                "MISSING_SKU",
                "DUPLICATE_SKU",
                "DUPLICATE_ETSY_SKU",
                "DUPLICATE_PRINTIFY_SKU",
            ]
        )
    ].copy()

    def listing_status(group):
        real = group[
            ~group.match_status.isin(
                ["UNAVAILABLE_SKU", "MISSING_SKU"]
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

        if statuses & {
            "DUPLICATE_SKU",
            "DUPLICATE_ETSY_SKU",
            "DUPLICATE_PRINTIFY_SKU",
        }:
            return "HAS_DUPLICATE_SKU"

        return "NEEDS_REVIEW"

    if comp.empty:
        summary = pd.DataFrame()

    else:
        summary = (
            comp.groupby(
                ["etsy_listing_id", "etsy_title"],
                dropna=False,
                as_index=False,
            )
            .agg(
                etsy_sku_rows=("etsy_sku", "size"),

                matched_skus=(
                    "match_status",
                    lambda s: int((s == "MATCHED").sum()),
                ),

                etsy_only_skus=(
                    "match_status",
                    lambda s: int((s == "ETSY_ONLY").sum()),
                ),

                unavailable_skus=(
                    "match_status",
                    lambda s: int(
                        (s == "UNAVAILABLE_SKU").sum()
                    ),
                ),

                problem_skus=(
                    "match_status",
                    lambda s: int(
                        s.isin(
                            [
                                "DUPLICATE_SKU",
                                "DUPLICATE_ETSY_SKU",
                                "DUPLICATE_PRINTIFY_SKU",
                                "MISSING_SKU",
                            ]
                        ).sum()
                    ),
                ),
            )
        )

        statuses = (
            comp.groupby(
                ["etsy_listing_id", "etsy_title"],
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
            on=["etsy_listing_id", "etsy_title"],
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
