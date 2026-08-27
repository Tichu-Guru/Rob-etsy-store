from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


EXPECTED_COLUMNS = [
    "TITLE",
    "DESCRIPTION",
    "PRICE",
    "CURRENCY_CODE",
    "QUANTITY",
    "TAGS",
    "MATERIALS",
    "IMAGE1",
    "IMAGE2",
    "IMAGE3",
    "IMAGE4",
    "IMAGE5",
    "IMAGE6",
    "IMAGE7",
    "IMAGE8",
    "IMAGE9",
    "IMAGE10",
    "VARIATION 1 TYPE",
    "VARIATION 1 NAME",
    "VARIATION 1 VALUES",
    "VARIATION 2 TYPE",
    "VARIATION 2 NAME",
    "VARIATION 2 VALUES",
    "SKU",
]


def read_etsy_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Etsy CSV not found: {path}"
        )

    try:
        df = pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype=str,
            keep_default_na=False,
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            path,
            encoding="latin-1",
            dtype=str,
            keep_default_na=False,
        )

    missing = [
        c for c in EXPECTED_COLUMNS
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            "Unexpected Etsy export format. "
            f"Missing columns: {missing}"
        )

    return df


def split_csv_field(value: Any) -> list[str]:
    text = (
        "" if value is None
        else str(value).strip()
    )

    return (
        [
            x.strip()
            for x in text.split(",")
            if x.strip()
        ]
        if text
        else []
    )


def listing_key(row: pd.Series, source_row: int) -> str:
    """
    Return a stable identity for one physical Etsy CSV row.

    IMPORTANT:
    The Etsy CSV export does not contain an Etsy listing ID.
    Therefore the physical CSV row is the safest identity
    available at this stage.

    Do NOT derive listing identity from title/description/
    price because multiple real Etsy listings can legitimately
    have identical listing content and differ only in photos.
    """

    return f"CSV_ROW_{source_row}"


def build_variation_labels(
    row: pd.Series,
    sku_index: int,
    sku_count: int,
) -> tuple[str, str, str]:
    """
    Preserve the Etsy variation information associated with
    each SKU row.

    Etsy's CSV export provides variation values as comma-
    separated lists and SKUs as a comma-separated list.

    When there is one variation, map the SKU position to the
    corresponding variation value.

    When there are two variations, Etsy's export represents
    the SKU rows in the variation-combination order. Build
    a readable label from the available values.

    If the relationship cannot be determined reliably, leave
    the value blank rather than inventing one.
    """

    name1 = str(
        row.get(
            "VARIATION 1 NAME",
            "",
        )
    ).strip()

    name2 = str(
        row.get(
            "VARIATION 2 NAME",
            "",
        )
    ).strip()

    values1 = split_csv_field(
        row.get(
            "VARIATION 1 VALUES",
            "",
        )
    )

    values2 = split_csv_field(
        row.get(
            "VARIATION 2 VALUES",
            "",
        )
    )

    value1 = ""
    value2 = ""

    if values1 and sku_count == len(values1):
        if 0 <= sku_index < len(values1):
            value1 = values1[sku_index]

    elif values1 and not values2:
        if len(values1) == 1:
            value1 = values1[0]

    if values2 and sku_count == len(values2):
        if 0 <= sku_index < len(values2):
            value2 = values2[sku_index]

    elif values2 and not values1:
        if len(values2) == 1:
            value2 = values2[0]

    label_parts = []

    if name1 and value1:
        label_parts.append(
            f"{name1}: {value1}"
        )

    if name2 and value2:
        label_parts.append(
            f"{name2}: {value2}"
        )

    label = " / ".join(label_parts)

    return (
        value1,
        value2,
        label,
    )


def build_etsy_tables(df: pd.DataFrame):
    listings = []
    variants = []

    # ---------------------------------------------------------
    # IDENTIFY DUPLICATE-TITLE LISTINGS
    # ---------------------------------------------------------
    #
    # We retain ALL CSV rows.
    #
    # For identical titles:
    #   first occurrence = primary
    #   later occurrences = secondary
    #
    # Secondary listings are not deleted here. They are marked
    # so the analysis layer can explicitly exclude them.
    #
    # This is intentional because identical titles can represent
    # separate Etsy listings whose photos differ.
    # ---------------------------------------------------------

    normalized_titles = (
        df["TITLE"]
        .fillna("")
        .astype(str)
        .map(
            lambda value:
            " ".join(
                value.strip().lower().split()
            )
        )
    )

    title_occurrence = {}
    duplicate_group_counts = (
        normalized_titles.value_counts(
            dropna=False
        )
        .to_dict()
    )

    for idx, row in df.iterrows():
        source_row = idx + 2

        title_key = normalized_titles.iloc[idx]

        occurrence = (
            title_occurrence.get(
                title_key,
                0,
            )
            + 1
        )

        title_occurrence[title_key] = occurrence

        is_duplicate_title = (
            bool(title_key)
            and duplicate_group_counts.get(
                title_key,
                0,
            ) > 1
        )

        is_primary = occurrence == 1

        key = listing_key(
            row,
            source_row,
        )

        listings.append(
            {
                "etsy_source_row": source_row,
                "etsy_listing_key": key,

                # -------------------------------------------------
                # Analysis identity
                # -------------------------------------------------
                "etsy_analysis_include": (
                    is_primary
                ),
                "etsy_duplicate_title": (
                    is_duplicate_title
                ),
                "etsy_duplicate_title_group": (
                    title_key
                    if is_duplicate_title
                    else ""
                ),
                "etsy_duplicate_title_occurrence": (
                    occurrence
                ),

                "title": row["TITLE"],
                "description": row["DESCRIPTION"],
                "price": row["PRICE"],
                "currency_code": row[
                    "CURRENCY_CODE"
                ],
                "quantity": row["QUANTITY"],
                "tags": row["TAGS"],
                "materials": row[
                    "MATERIALS"
                ],
                "variation_1_type": row[
                    "VARIATION 1 TYPE"
                ],
                "variation_1_name": row[
                    "VARIATION 1 NAME"
                ],
                "variation_1_values": row[
                    "VARIATION 1 VALUES"
                ],
                "variation_2_type": row[
                    "VARIATION 2 TYPE"
                ],
                "variation_2_name": row[
                    "VARIATION 2 NAME"
                ],
                "variation_2_values": row[
                    "VARIATION 2 VALUES"
                ],
                "image_count": sum(
                    bool(
                        str(
                            row.get(
                                f"IMAGE{i}",
                                "",
                            )
                        ).strip()
                    )
                    for i in range(1, 11)
                ),
                "raw_row_json": json.dumps(
                    row.to_dict(),
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )

        skus = split_csv_field(
            row["SKU"]
        )

        if not skus:
            variants.append(
                {
                    "etsy_source_row": source_row,
                    "etsy_listing_key": key,

                    # Match the listing-level analysis flag.
                    "etsy_analysis_include": (
                        is_primary
                    ),
                    "etsy_duplicate_title": (
                        is_duplicate_title
                    ),

                    "etsy_sku_index": None,
                    "etsy_sku": "",
                    "etsy_sku_status": "MISSING",

                    "etsy_variation_1_name": str(
                        row.get(
                            "VARIATION 1 NAME",
                            "",
                        )
                    ).strip(),

                    "etsy_variation_1_value": "",

                    "etsy_variation_2_name": str(
                        row.get(
                            "VARIATION 2 NAME",
                            "",
                        )
                    ).strip(),

                    "etsy_variation_2_value": "",
                    "etsy_variation_label": "",
                }
            )

        else:
            sku_count = len(skus)

            for n, sku in enumerate(
                skus,
                1,
            ):
                (
                    variation1,
                    variation2,
                    variation_label,
                ) = build_variation_labels(
                    row,
                    n - 1,
                    sku_count,
                )

                variants.append(
                    {
                        "etsy_source_row": source_row,
                        "etsy_listing_key": key,

                        # Match the listing-level analysis flag.
                        "etsy_analysis_include": (
                            is_primary
                        ),
                        "etsy_duplicate_title": (
                            is_duplicate_title
                        ),

                        "etsy_sku_index": n,
                        "etsy_sku": sku,
                        "etsy_sku_status": (
                            "UNAVAILABLE"
                            if sku.lower()
                            == "unavailable_sku"
                            else "PRESENT"
                        ),

                        "etsy_variation_1_name": str(
                            row.get(
                                "VARIATION 1 NAME",
                                "",
                            )
                        ).strip(),

                        "etsy_variation_1_value": (
                            variation1
                        ),

                        "etsy_variation_2_name": str(
                            row.get(
                                "VARIATION 2 NAME",
                                "",
                            )
                        ).strip(),

                        "etsy_variation_2_value": (
                            variation2
                        ),

                        "etsy_variation_label": (
                            variation_label
                        ),
                    }
                )

    return (
        pd.DataFrame(listings),
        pd.DataFrame(variants),
    )
