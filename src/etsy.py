from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
import pandas as pd

EXPECTED_COLUMNS = [
    "TITLE","DESCRIPTION","PRICE","CURRENCY_CODE","QUANTITY","TAGS","MATERIALS",
    "IMAGE1","IMAGE2","IMAGE3","IMAGE4","IMAGE5","IMAGE6","IMAGE7","IMAGE8","IMAGE9","IMAGE10",
    "VARIATION 1 TYPE","VARIATION 1 NAME","VARIATION 1 VALUES",
    "VARIATION 2 TYPE","VARIATION 2 NAME","VARIATION 2 VALUES","SKU"
]

def read_etsy_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Etsy CSV not found: {path}")
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="latin-1", dtype=str, keep_default_na=False)
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Unexpected Etsy export format. Missing columns: {missing}")
    return df

def split_csv_field(value: Any) -> list[str]:
    text = "" if value is None else str(value).strip()
    return [x.strip() for x in text.split(",") if x.strip()] if text else []

def listing_key(row: pd.Series) -> str:
    basis = "|".join([
        str(row.get("TITLE","")).strip(),
        str(row.get("DESCRIPTION","")).strip(),
        str(row.get("PRICE","")).strip(),
        str(row.get("VARIATION 1 NAME","")).strip(),
        str(row.get("VARIATION 2 NAME","")).strip(),
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

def build_etsy_tables(df: pd.DataFrame):
    listings = []
    variants = []
    for idx, row in df.iterrows():
        source_row = idx + 2
        key = listing_key(row)
        listings.append({
            "etsy_source_row": source_row,
            "etsy_listing_key": key,
            "title": row["TITLE"],
            "description": row["DESCRIPTION"],
            "price": row["PRICE"],
            "currency_code": row["CURRENCY_CODE"],
            "quantity": row["QUANTITY"],
            "tags": row["TAGS"],
            "materials": row["MATERIALS"],
            "variation_1_type": row["VARIATION 1 TYPE"],
            "variation_1_name": row["VARIATION 1 NAME"],
            "variation_1_values": row["VARIATION 1 VALUES"],
            "variation_2_type": row["VARIATION 2 TYPE"],
            "variation_2_name": row["VARIATION 2 NAME"],
            "variation_2_values": row["VARIATION 2 VALUES"],
            "image_count": sum(bool(str(row.get(f"IMAGE{i}","")).strip()) for i in range(1,11)),
            "raw_row_json": json.dumps(row.to_dict(), ensure_ascii=False, default=str),
        })
        skus = split_csv_field(row["SKU"])
        if not skus:
            variants.append({
                "etsy_source_row": source_row, "etsy_listing_key": key,
                "etsy_sku_index": None, "etsy_sku": "", "etsy_sku_status": "MISSING"
            })
        else:
            for n, sku in enumerate(skus, 1):
                variants.append({
                    "etsy_source_row": source_row, "etsy_listing_key": key,
                    "etsy_sku_index": n, "etsy_sku": sku,
                    "etsy_sku_status": "UNAVAILABLE" if sku.lower()=="unavailable_sku" else "PRESENT"
                })
    return pd.DataFrame(listings), pd.DataFrame(variants)
