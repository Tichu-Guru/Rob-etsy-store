from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

import pandas as pd


def normalize_sku(value: Any) -> str:
    if value is None or (
        isinstance(value, float)
        and math.isnan(value)
    ):
        return ""

    return str(value).strip().upper()


def normalize_text(value: Any) -> str:
    """
    Normalize variation text for comparison.

    This intentionally keeps the words and numbers while
    removing punctuation and making comparison
    case-insensitive.
    """
    if value is None:
        return ""

    text = str(value).strip().lower()

    if not text:
        return ""

    replacements = {
        "/": " ",
        "-": " ",
        "_": " ",
        ",": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split())


def money(value: Any):
    """
    Convert a money value to dollars.

    Etsy prices are already dollar values.
    """
    try:
        text = (
            str(value)
            .replace("$", "")
            .replace(",", "")
            .strip()
        )

        return float(text) if text else None

    except (TypeError, ValueError):
        return None


def printify_money(value: Any):
    """
    Printify API returns price/cost in cents.
    Convert cents to dollars.
    """
    try:
        text = (
            str(value)
            .replace("$", "")
            .replace(",", "")
            .strip()
        )

        return float(text) / 100 if text else None

    except (TypeError, ValueError):
        return None


def build_etsy_sku_listing_map(etsy):
    """
    Build SKU -> set of distinct Etsy listing IDs.

    A SKU repeated across variations of the same listing
    is normal.

    It is only considered shared when it appears on
    multiple distinct Etsy listings.
    """
    sku_listings = defaultdict(set)

    for _, row in etsy.iterrows():
        sku = normalize_sku(
            row.get("etsy_sku")
        )

        listing_id = str(
            row.get(
                "etsy_listing_id",
                "",
            )
        ).strip()

        if (
            sku
            and sku != "UNAVAILABLE_SKU"
            and listing_id
        ):
            sku_listings[sku].add(
                listing_id
            )

    return sku_listings


def variation_tokens(row: pd.Series) -> set[str]:
    """
    Return normalized tokens representing the Etsy
    variation identity.

    Example:

        "Quantity: 10 pcs"

    becomes tokens containing:

        quantity
        10
        pcs

    The variation name is retained because it can help
    distinguish otherwise similar values.
    """
    values = []

    for column in [
        "etsy_variation_1_name",
        "etsy_variation_1_value",
        "etsy_variation_2_name",
        "etsy_variation_2_value",
        "etsy_variation_label",
    ]:
        value = normalize_text(
            row.get(column, "")
        )

        if value:
            values.extend(
                value.split()
            )

    return set(values)


def printify_option_tokens(row: pd.Series) -> set[str]:
    """
    Extract normalized variation tokens from the
    Printify variant title and option JSON.

    Printify commonly exposes values such as:

        Snowflake / 10 pcs / One size

    so this gives the matcher enough information to select
    the correct Printify variant when one SKU is shared by
    multiple variants.
    """
    values = []

    for column in [
        "printify_variant_title",
        "printify_options_json",
        "printify_product_options_json",
    ]:
        value = row.get(column, "")

        if value is None:
            continue

        text = str(value)

        if not text.strip():
            continue

        normalized = normalize_text(text)

        if normalized:
            values.extend(
                normalized.split()
            )

    return set(values)


def variation_match_score(
    etsy_row: pd.Series,
    printify_row: pd.Series,
) -> int:
    """
    Score how well an Etsy variation matches a Printify
    variant.

    This is deliberately conservative.

    The SKU remains the primary identifier.

    Variation text is used only to distinguish among
    multiple Printify variants carrying the same SKU.

    Exact tokens receive the strongest signal.
    """
    etsy_tokens = variation_tokens(
        etsy_row
    )

    if not etsy_tokens:
        return 0

    printify_tokens = printify_option_tokens(
        printify_row
    )

    if not printify_tokens:
        return 0

    common = (
        etsy_tokens
        & printify_tokens
    )

    if not common:
        return 0

    score = len(common)

    # Quantity values are especially important.
    quantity_words = {
        "1",
        "3",
        "5",
        "10",
        "25",
        "50",
        "100",
    }

    quantity_matches = (
        etsy_tokens
        & quantity_words
        & printify_tokens
    )

    score += (
        len(quantity_matches) * 10
    )

    return score


def choose_printify_variant(
    etsy_row: pd.Series,
    candidates: pd.DataFrame,
):
    """
    Select the appropriate Printify row from candidates
    sharing the same normalized SKU.

    Returns:

        selected_row, status

    where status is one of:

        MATCHED
        MATCHED_BY_VARIATION
        DUPLICATE_PRINTIFY_SKU
    """
    if candidates.empty:
        return None, "ETSY_ONLY"

    if len(candidates) == 1:
        return (
            candidates.iloc[0],
            "MATCHED",
        )

    scored = []

    for index, candidate in candidates.iterrows():
        score = variation_match_score(
            etsy_row,
            candidate,
        )

        scored.append(
            (
                score,
                index,
            )
        )

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score, best_index = (
        scored[0]
    )

    second_score = (
        scored[1][0]
        if len(scored) > 1
        else -1
    )

    # A positive score that is strictly better than
    # every other candidate gives us an unambiguous
    # variation match.
    if (
        best_score > 0
        and best_score > second_score
    ):
        return (
            candidates.loc[best_index],
            "MATCHED_BY_VARIATION",
        )

    # If the variation information cannot distinguish
    # the candidates, do not guess.
    return (
        None,
        "DUPLICATE_PRINTIFY_SKU",
    )


def build_comparison(
    etsy_rows,
    printify_rows,
):
    etsy = pd.DataFrame(
        etsy_rows
    )

    diagnostic_skus = {
        "11975776294672016378",
        "17119814508216010683",
        "27331151771594285206",
        "11731818812456775837",
    }

    diagnostic_rows = etsy[
        etsy["etsy_sku"].astype(str).str.strip().isin(
            diagnostic_skus
        )
    ]

    print()
    print("=" * 80)
    print("BUILD COMPARISON INPUT DIAGNOSTIC")
    print("=" * 80)

    if diagnostic_rows.empty:
        print("NO NUTCRACKER SKUS FOUND")
    else:
        print(
            diagnostic_rows[
                [
                    "etsy_sku",
                    "etsy_price",
                    "etsy_variation_label",
                ]
            ].to_string(index=False)
        )

    print("=" * 80)
    print("END BUILD COMPARISON INPUT DIAGNOSTIC")
    print()

    pri = pd.DataFrame(
        printify_rows
    )

    if etsy.empty:
        etsy = pd.DataFrame(
            columns=[
                "etsy_row_number",
                "etsy_listing_id",
                "etsy_title",
                "etsy_sku",
                "etsy_price",
                "etsy_quantity",
                "etsy_variation_1_name",
                "etsy_variation_1_value",
                "etsy_variation_2_name",
                "etsy_variation_2_value",
                "etsy_variation_label",
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
                "printify_available",
                "printify_variant_title",
                "print_provider_id",
            ]
        )

    etsy["normalized_sku"] = (
        etsy["etsy_sku"].map(
            normalize_sku
        )
    )

    pri["normalized_sku"] = (
        pri["printify_sku"].map(
            normalize_sku
        )
    )

    # Count how many Printify variants use each SKU.
    printify_counts = Counter(
        sku
        for sku in pri["normalized_sku"]
        if sku
    )

    # Determine how many distinct Etsy listings use each SKU.
    sku_listings = (
        build_etsy_sku_listing_map(
            etsy
        )
    )

    # -----------------------------------------------------
    # MATCH EACH ETSY ROW
    # -----------------------------------------------------

    matched_rows = []

    for _, etsy_row in etsy.iterrows():

        sku = normalize_sku(
            etsy_row.get(
                "etsy_sku"
            )
        )

        result = etsy_row.to_dict()

        result["etsy_listing_count_for_sku"] = len(
            sku_listings.get(
                sku,
                set(),
            )
        )

        result[
            "etsy_sku_shared_across_listings"
        ] = (
            result[
                "etsy_listing_count_for_sku"
            ]
            > 1
        )

        if not sku:
            result[
                "match_status"
            ] = "MISSING_SKU"

            matched_rows.append(
                result
            )
            continue

        if sku == "UNAVAILABLE_SKU":
            result[
                "match_status"
            ] = "UNAVAILABLE_SKU"

            matched_rows.append(
                result
            )
            continue

        candidates = pri[
            pri["normalized_sku"]
            == sku
        ]

        if candidates.empty:
            result[
                "match_status"
            ] = "ETSY_ONLY"

            matched_rows.append(
                result
            )
            continue

        selected, status = (
            choose_printify_variant(
                etsy_row,
                candidates,
            )
        )

        if selected is None:
            result[
                "match_status"
            ] = status

            matched_rows.append(
                result
            )
            continue

        # Copy Printify fields into the Etsy row.
        for column in pri.columns:
            if column == "normalized_sku":
                continue

            result[column] = (
                selected.get(column)
            )

        result[
            "match_status"
        ] = status

        matched_rows.append(
            result
        )

    comp = pd.DataFrame(
        matched_rows
    )

    # -----------------------------------------------------
    # KNOWN-GOOD ORNAMENT COMPARISON OUTPUT DIAGNOSTIC
    # -----------------------------------------------------

    diagnostic_skus = {
        "10448240795186895362",
        "13846807837356636519",
        "24666151708130655347",
        "47663316682177534068",
    }

    diagnostic_rows = comp[
        comp["etsy_sku"]
        .astype(str)
        .str.strip()
        .isin(diagnostic_skus)
    ]

    print()
    print("=" * 100)
    print("KNOWN-GOOD ORNAMENT COMPARISON OUTPUT DIAGNOSTIC")
    print("=" * 100)

    if diagnostic_rows.empty:
        print("NO KNOWN-GOOD ORNAMENT SKUS FOUND IN COMPARISON")
    else:
        columns_to_show = [
            column
            for column in [
                "etsy_sku",
                "etsy_price",
                "etsy_variation_label",
                "printify_sku",
                "printify_variant_title",
                "printify_price",
                "printify_cost",
                "match_status",
            ]
            if column in diagnostic_rows.columns
        ]

        print(
            diagnostic_rows[
                columns_to_show
            ].to_string(
                index=False
            )
        )

    print("=" * 100)
    print(
        "END KNOWN-GOOD ORNAMENT "
        "COMPARISON OUTPUT DIAGNOSTIC"
    )
    print()


    # -----------------------------------------------------
    # DOLLAR VALUES
    # -----------------------------------------------------

    if comp.empty:
        comp["etsy_price_numeric"] = (
            pd.Series(dtype="float64")
        )

        comp[
            "printify_price_numeric"
        ] = pd.Series(
            dtype="float64"
        )

        comp[
            "printify_cost_numeric"
        ] = pd.Series(
            dtype="float64"
        )

        comp[
            "estimated_gross_margin"
        ] = pd.Series(
            dtype="float64"
        )

        comp[
            "estimated_gross_margin_pct"
        ] = pd.Series(
            dtype="float64"
        )

    else:
        comp[
            "etsy_price_numeric"
        ] = comp[
            "etsy_price"
        ].map(money)

        comp[
            "printify_price_numeric"
        ] = comp[
            "printify_price"
        ].map(
            printify_money
        )

        comp[
            "printify_cost_numeric"
        ] = comp[
            "printify_cost"
        ].map(
            printify_money
        )

        # Gross product margin before Etsy fees,
        # payment fees, shipping, taxes, etc.
        comp[
            "estimated_gross_margin"
        ] = (
            comp[
                "etsy_price_numeric"
            ]
            - comp[
                "printify_cost_numeric"
            ]
        )

        comp[
            "estimated_gross_margin_pct"
        ] = (
            comp[
                "estimated_gross_margin"
            ]
            / comp[
                "etsy_price_numeric"
            ]
            * 100
        )

        comp.loc[
            comp[
                "etsy_price_numeric"
            ].isna()
            | (
                comp[
                    "etsy_price_numeric"
                ]
                == 0
            ),
            "estimated_gross_margin_pct",
        ] = None

    # -----------------------------------------------------
    # PRINTIFY-ONLY SKUS
    # -----------------------------------------------------

    etsy_skus = {
        sku
        for sku in sku_listings
        if sku
    }

    ponly = pri[
        (pri["normalized_sku"] != "")
        & (
            ~pri[
                "normalized_sku"
            ].isin(etsy_skus)
        )
    ].copy()

    ponly["status"] = (
        "PRINTIFY_ONLY"
    )

    # -----------------------------------------------------
    # ATTENTION
    # -----------------------------------------------------

    attention = comp[
        comp[
            "match_status"
        ].isin(
            [
                "MISSING_SKU",
                "DUPLICATE_PRINTIFY_SKU",
            ]
        )
    ].copy()

    # -----------------------------------------------------
    # LISTING STATUS
    # -----------------------------------------------------

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

        statuses = set(
            real.match_status
        )

        # Both normal SKU matches and unambiguous
        # variation matches count as successful matches.
        successful = {
            "MATCHED",
            "MATCHED_BY_VARIATION",
        }

        if statuses.issubset(
            successful
        ):
            return "FULLY_MATCHED"

        if statuses & successful:
            return "PARTIALLY_MATCHED"

        if statuses == {
            "ETSY_ONLY"
        }:
            return "NO_PRINTIFY_PRODUCTS"

        if (
            "DUPLICATE_PRINTIFY_SKU"
            in statuses
        ):
            return (
                "HAS_PRINTIFY_SKU_PROBLEM"
            )

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
                        s.isin(
                            [
                                "MATCHED",
                                "MATCHED_BY_VARIATION",
                            ]
                        ).sum()
                    ),
                ),

                etsy_only_skus=(
                    "match_status",
                    lambda s: int(
                        (
                            s
                            == "ETSY_ONLY"
                        ).sum()
                    ),
                ),

                unavailable_skus=(
                    "match_status",
                    lambda s: int(
                        (
                            s
                            == "UNAVAILABLE_SKU"
                        ).sum()
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
            .rename(
                "listing_status"
            )
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
            columns=[
                "normalized_sku"
            ],
            errors="ignore",
        ),
        summary,
        attention.drop(
            columns=[
                "normalized_sku"
            ],
            errors="ignore",
        ),
        ponly.drop(
            columns=[
                "normalized_sku"
            ],
            errors="ignore",
        ),
    )
