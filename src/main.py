from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .config import DATABASE_PATH, ETSY_CSV, OUTPUT_DIR
from .database import initialize_database, record_run, replace_table
from .etsy import build_etsy_tables, read_etsy_csv
from .matching import build_comparison
from .printify import PrintifyClient


def main():
    ts = datetime.now(timezone.utc).isoformat()

    # Read Etsy export.
    etsy_df = read_etsy_csv(ETSY_CSV)

    # Build Etsy tables.
    etsy_listings, etsy_variants = build_etsy_tables(
        etsy_df
    )

    # IMPORTANT:
    # Use the Etsy CSV source row as the unique listing ID.
    # The previous content-based hash could merge separate
    # Etsy listings that happened to have identical metadata.
    etsy_rows = etsy_variants.merge(
        etsy_listings[
            [
                "etsy_source_row",
                "title",
                "price",
                "quantity",
            ]
        ],
        on="etsy_source_row",
        how="left",
    )

    etsy_rows = etsy_rows.rename(
        columns={
            "etsy_source_row": "etsy_row_number",
            "title": "etsy_title",
            "price": "etsy_price",
            "quantity": "etsy_quantity",
        }
    )

    # The source row uniquely identifies the Etsy listing in
    # this CSV export.
    etsy_rows["etsy_listing_id"] = (
        etsy_rows["etsy_row_number"]
        .astype(str)
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

    # Download current Printify catalog.
    client = PrintifyClient()

    shop_id = client.get_shop_id()

    printify_rows = client.export_variant_rows(
        shop_id
    )

    # Compare Etsy and Printify.
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

    # Detailed SKU comparison.
    comparison.to_csv(
        OUTPUT_DIR
        / "etsy_printify_comparison.csv",
        index=False,
    )

    # One row per Etsy listing.
    listing_summary.to_csv(
        OUTPUT_DIR
        / "listing_summary.csv",
        index=False,
    )

    # Only actionable issues.
    attention.to_csv(
        OUTPUT_DIR
        / "needs_attention.csv",
        index=False,
    )

    # Printify products without an Etsy SKU match.
    printify_only.to_csv(
        OUTPUT_DIR
        / "printify_only.csv",
        index=False,
    )

    # Full Printify variant export.
    pd.DataFrame(
        printify_rows
    ).to_csv(
        OUTPUT_DIR
        / "printify_products.csv",
        index=False,
    )

    # Summary counts.
    status_counts = (
        comparison["match_status"].value_counts()
        if not comparison.empty
        else {}
    )

    listing_counts = (
        listing_summary["listing_status"].value_counts()
        if not listing_summary.empty
        else {}
    )

    shared_sku_count = (
        int(
            comparison[
                "etsy_sku_shared_across_listings"
            ].sum()
        )
        if not comparison.empty
        else 0
    )

    lines = [
        "ETSY ↔ PRINTIFY SYNC SUMMARY",
        f"Run UTC: {ts}",
        "",
        f"Etsy listings: {len(etsy_listings):,}",
        f"Etsy SKU rows: {len(etsy_rows):,}",
        f"Printify variants: {len(printify_rows):,}",
        "",
        "SKU STATUS",
        f"MATCHED: {int(status_counts.get('MATCHED', 0)):,}",
        f"ETSY_ONLY: {int(status_counts.get('ETSY_ONLY', 0)):,}",
        f"UNAVAILABLE_SKU: {int(status_counts.get('UNAVAILABLE_SKU', 0)):,}",
        f"MISSING_SKU: {int(status_counts.get('MISSING_SKU', 0)):,}",
        f"DUPLICATE_PRINTIFY_SKU: {int(status_counts.get('DUPLICATE_PRINTIFY_SKU', 0)):,}",
        "",
        "INFORMATIONAL",
        f"SHARED_ETSY_SKU_ROWS: {shared_sku_count:,}",
        "",
        "LISTING STATUS",
        f"FULLY_MATCHED: {int(listing_counts.get('FULLY_MATCHED', 0)):,}",
        f"PARTIALLY_MATCHED: {int(listing_counts.get('PARTIALLY_MATCHED', 0)):,}",
        f"NO_PRINTIFY_PRODUCTS: {int(listing_counts.get('NO_PRINTIFY_PRODUCTS', 0)):,}",
        f"HAS_PRINTIFY_SKU_PROBLEM: {int(listing_counts.get('HAS_PRINTIFY_SKU_PROBLEM', 0)):,}",
        f"NEEDS_REVIEW: {int(listing_counts.get('NEEDS_REVIEW', 0)):,}",
        f"NO_REAL_SKUS: {int(listing_counts.get('NO_REAL_SKUS', 0)):,}",
        "",
        "Shared Etsy SKUs are informational and are still considered matched when the SKU exists once in Printify.",
        "ETSY_ONLY means the Etsy SKU is not currently found in Printify.",
        "Gross margin = Etsy price minus Printify product cost, before Etsy fees, payment fees, shipping, taxes, etc.",
    ]

    (
        OUTPUT_DIR / "sync_summary.txt"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    # Update SQLite database.
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
        pd.DataFrame(printify_rows),
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
                comparison["match_status"]
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


if __name__ == "__main__":
    main()
