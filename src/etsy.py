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


def listing_key(row: pd.Series) -> str:
    basis = "|".join(
        [
            str(row.get("TITLE", "")).strip(),
            str(row.get("DESCRIPTION", "")).strip(),
            str(row.get("PRICE", "")).strip(),
            str(
                row.get(
                    "VARIATION 1 NAME",
                    "",
                )
            ).strip(),
            str(
                row.get(
                    "VARIATION 2 NAME",
                    "",
                )
            ).strip(),
        ]
    )

    return hashlib.sha1(
        basis.encode("utf-8")
    ).hexdigest()[:16]


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

    When there is one variation, map the SKU position to
    the corresponding variation value.

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

    for idx, row in df.iterrows():
        source_row = idx + 2
        key = listing_key(row)

        listings.append(
            {
                "etsy_source_row": source_row,
                "etsy_listing_key": key,
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


