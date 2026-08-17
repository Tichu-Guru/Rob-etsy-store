from __future__ import annotations
import math
from collections import defaultdict
from typing import Any
import pandas as pd

def normalize_sku(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip().upper()

def money(value: Any) -> float | None:
    try:
        text = str(value).replace("$","").replace(",","").strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None

def build_comparison(listings, etsy_variants, printify_rows):
    etsy = etsy_variants.merge(listings[[
        "etsy_source_row","etsy_listing_key","title","price","currency_code","quantity",
        "variation_1_name","variation_1_values","variation_2_name","variation_2_values"
    ]], on=["etsy_source_row","etsy_listing_key"], how="left")

    printify = pd.DataFrame(printify_rows)
    if printify.empty:
        printify = pd.DataFrame(columns=[
            "printify_product_id","printify_title","printify_variant_id","printify_sku",
            "printify_variant_title","printify_price","printify_cost","printify_enabled",
            "printify_available","print_provider_id","blueprint_id"
        ])

    etsy["normalized_sku"] = etsy["etsy_sku"].map(normalize_sku)
    printify["normalized_sku"] = printify["printify_sku"].map(normalize_sku)

    pmap = defaultdict(list)
    for _, row in printify.iterrows():
        if row["normalized_sku"]:
            pmap[row["normalized_sku"]].append(row.to_dict())

    output = []
    for _, e in etsy.iterrows():
        sku = e["normalized_sku"]
        if not sku:
            status, matches = "MISSING_ETSY_SKU", []
        elif sku == "UNAVAILABLE_SKU":
            status, matches = "UNAVAILABLE_ETSY_SKU", []
        else:
            matches = pmap.get(sku, [])
            status = ("NO_PRINTIFY_MATCH" if not matches else
                      "MATCHED" if len(matches)==1 else "MULTIPLE_PRINTIFY_MATCHES")
        base = e.to_dict()
        if not matches:
            output.append({**base, "match_status":status, "printify_product_id":"",
                           "printify_title":"","printify_variant_id":"","printify_sku":"",
                           "printify_variant_title":"","printify_price":"","printify_cost":"",
                           "print_provider_id":"","estimated_gross_margin":None})
        else:
            for p in matches:
                ep, pc = money(e["price"]), money(p.get("printify_cost"))
                output.append({**base, "match_status":status,
                               "printify_product_id":p.get("printify_product_id"),
                               "printify_title":p.get("printify_title"),
                               "printify_variant_id":p.get("printify_variant_id"),
                               "printify_sku":p.get("printify_sku"),
                               "printify_variant_title":p.get("printify_variant_title"),
                               "printify_price":p.get("printify_price"),
                               "printify_cost":p.get("printify_cost"),
                               "print_provider_id":p.get("print_provider_id"),
                               "estimated_gross_margin": ep-pc if ep is not None and pc is not None else None})

    comparison = pd.DataFrame(output)

    etsy_skus = {s for s in etsy["normalized_sku"] if s and s != "UNAVAILABLE_SKU"}
    orphan = printify[(printify["normalized_sku"]!="") & (~printify["normalized_sku"].isin(etsy_skus))]

    alerts = []
    for _, r in etsy.iterrows():
        s=r["normalized_sku"]
        alert = ("ETSY_ROW_MISSING_SKU" if not s else
                 "ETSY_UNAVAILABLE_SKU" if s=="UNAVAILABLE_SKU" else
                 "ETSY_SKU_NO_PRINTIFY_MATCH" if s not in pmap else "OK")
        if alert != "OK":
            alerts.append({"alert":alert,"etsy_source_row":r["etsy_source_row"],
                           "etsy_listing_key":r["etsy_listing_key"],"etsy_title":r["title"],
                           "etsy_sku":r["etsy_sku"]})
    for _, r in orphan.iterrows():
        alerts.append({"alert":"PRINTIFY_SKU_NOT_ON_ETSY",
                       "printify_product_id":r["printify_product_id"],
                       "printify_title":r["printify_title"],
                       "printify_variant_id":r["printify_variant_id"],
                       "printify_sku":r["printify_sku"]})

    alerts_df = pd.DataFrame(alerts)
    summary = etsy.groupby(["etsy_source_row","etsy_listing_key","title"], as_index=False).agg(
        etsy_sku_rows=("etsy_sku","size"),
        nonblank_skus=("normalized_sku", lambda s:int((s!="").sum())),
        unavailable_skus=("normalized_sku", lambda s:int((s=="UNAVAILABLE_SKU").sum()))
    )
    return comparison.drop(columns=["normalized_sku"], errors="ignore"), summary, alerts_df
