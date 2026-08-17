from __future__ import annotations
from datetime import datetime, timezone
import pandas as pd
from .config import DATABASE_PATH, ETSY_CSV, OUTPUT_DIR
from .database import initialize_database, record_run, replace_table
from .etsy import build_etsy_tables, read_etsy_csv
from .matching import build_comparison
from .printify import PrintifyClient

def main():
    run_timestamp=datetime.now(timezone.utc).isoformat()
    print(f"Reading Etsy CSV: {ETSY_CSV}")
    df=read_etsy_csv(ETSY_CSV)
    listings, variants=build_etsy_tables(df)
    print(f"Etsy listings: {len(listings)}")
    print(f"Etsy SKU rows: {len(variants)}")
    client=PrintifyClient()
    shop_id=client.get_shop_id()
    print(f"Printify shop: {shop_id}")
    print("Downloading Printify products...")
    printify_rows=client.export_variant_rows(shop_id)
    print(f"Printify variants: {len(printify_rows)}")
    comparison, summary, alerts=build_comparison(listings, variants, printify_rows)
    comparison.to_csv(OUTPUT_DIR/"etsy_printify_comparison.csv", index=False)
    summary.to_csv(OUTPUT_DIR/"listing_summary.csv", index=False)
    alerts.to_csv(OUTPUT_DIR/"alerts.csv", index=False)
    pd.DataFrame(printify_rows).to_csv(OUTPUT_DIR/"printify_products.csv", index=False)
    con=initialize_database(DATABASE_PATH)
    replace_table(con,"etsy_listings",listings)
    replace_table(con,"etsy_variants",variants)
    replace_table(con,"printify_variants",pd.DataFrame(printify_rows))
    replace_table(con,"etsy_printify_comparison",comparison)
    replace_table(con,"listing_summary",summary)
    replace_table(con,"alerts",alerts)
    matched=int((comparison["match_status"]=="MATCHED").sum()) if not comparison.empty else 0
    record_run(con,run_timestamp,len(listings),len(variants),len(printify_rows),matched,len(alerts))
    con.close()
    print(f"Sync complete. Database: {DATABASE_PATH}")

if __name__=="__main__":
    main()
