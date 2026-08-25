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
        column
        for column in EXPECTED_COLUMNS
        if column not in df.columns
    ]

    if missing:
        raise ValueError(
            "Unexpected Etsy export format. "
            f"Missing columns: {missing}"
        )

    return df


def split_csv_field(value: Any) -> list[str]:
    text = (
        ""
        if value is None
        else str(value).strip()
    )

    if not text:
        return []

    return [
        item.strip()
        for item in text.split(",")
        if item.strip()
    ]


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


def build_variation_rows(
    row: pd.Series,
    sku_count: int,
) -> list[dict[str, Any]]:
    """
    Build one variation record for each Etsy SKU.

    Etsy's CSV stores variation values and SKUs as
    comma-separated fields. The corresponding positions
    are used to associate each SKU with its variation
    value(s).

    This preserves the variation information needed later
    when matching an Etsy SKU to the correct Printify
    variant.

    Important:
      Etsy's export does not provide a separate price for
      each variation. Therefore this function preserves
      variation identity but does not invent variation
      prices.
    """

    variation_1_values = split_csv_field(
        row.get("VARIATION 1 VALUES", "")
    )

    variation_2_values = split_csv_field(
        row.get("VARIATION 2 VALUES", "")
    )

    variation_1_name = str(
        row.get("VARIATION 1 NAME", "")
    ).strip()

    variation_2_name = str(
        row.get("VARIATION 2 NAME", "")
    ).strip()

    variation_rows = []

    for index in range(sku_count):
        value_1 = (
            variation_1_values[index]
            if index < len(variation_1_values)
            else ""
        )

        value_2 = (
            variation_2_values[index]
            if index < len(variation_2_values)
            else ""
        )

        parts = []

        if value_1:
            if variation_1_name:
                parts.append(
                    f"{variation_1_name}: {value_1}"
                )
            else:
                parts.append(value_1)

        if value_2:
            if variation_2_name:
                parts.append(
                    f"{variation_2_name}: {value_2}"
                )
            else:
                parts.append(value_2)

        variation_rows.append(
            {
                "etsy_variation_1_name": (
                    variation_1_name
                ),
                "etsy_variation_1_value": value_1,
                "etsy_variation_2_name": (
                    variation_2_name
                ),
                "etsy_variation_2_value": value_2,
                "etsy_variation_label": " / ".join(
                    parts
                ),
            }
        )

    return variation_rows


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
                "materials": row["MATERIALS"],
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
                    "etsy_variation_1_name": "",
                    "etsy_variation_1_value": "",
                    "etsy_variation_2_name": "",
                    "etsy_variation_2_value": "",
                    "etsy_variation_label": "",
                }
            )

            continue

        variation_rows = build_variation_rows(
            row,
            len(skus),
        )

        for index, sku in enumerate(
            skus,
            start=1,
        ):
            variation = variation_rows[
                index - 1
            ]

            variants.append(
                {
                    "etsy_source_row": source_row,
                    "etsy_listing_key": key,
                    "etsy_sku_index": index,
                    "etsy_sku": sku,
                    "etsy_sku_status": (
                        "UNAVAILABLE"
                        if sku.lower()
                        == "unavailable_sku"
                        else "PRESENT"
                    ),
                    **variation,
                }
            )

    return (
        pd.DataFrame(listings),
        pd.DataFrame(variants),
    )
