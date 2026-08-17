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

    # Read Etsy export and build its listing/variant tables.
    etsy_df = read_etsy_csv(ETSY_CSV)
    etsy_listings, etsy_variants = build_etsy_tables(etsy_df)

    # Convert the Etsy variant table into the fields expected by
    # the comparison engine.
    etsy_rows = etsy_variants.merge(
        etsy_listings[
            [
                "etsy_listing_key",
                "title",
                "price",
                "quantity",
            ]
        ],
        on="etsy_listing_key",
        how="left",
    )

    etsy_rows = etsy_rows.rename(
        columns={
            "etsy_source_row": "etsy_row_number",
            "etsy_listing_key": "etsy_listing_id",
            "title": "etsy_title",
            "price": "etsy_price",
            "quantity": "etsy_quantity",
        }
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

    etsy_rows = etsy_rows.to_dict("records")

    # Download the current Printify catalog.
    client = PrintifyClient()
    shop_id = client.get_shop_id()
    printify_rows = client.export_variant_rows(shop_id)

    # Compare Etsy SKUs with Printify SKUs.
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

    comparison.to_csv(
        OUTPUT_DIR / "etsy_printify_comparison.csv",
        index=False,
    )

    listing_summary.to_csv(
        OUTPUT_DIR / "listing_summary.csv",
        index=False,
    )

    attention.to_csv(
        OUTPUT_DIR / "needs_attention.csv",
        index=False,
    )

    printify_only.to_csv(
        OUTPUT_DIR / "printify_only.csv",
        index=False,
    )

    pd.DataFrame(
        printify_rows
    ).to_csv(
        OUTPUT_DIR / "printify_products.csv",
        index=False,
    )

    # Create a simple human-readable summary.
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

    lines = [
        "ETSY ↔ PRINTIFY SYNC SUMMARY",
        f"Run UTC: {ts}",
        "",
        f"Etsy listings: {len(etsy_listings):,}",
        f"Etsy SKU rows: {len(etsy_rows):,}",
        f"Printify variants: {len(printify_rows):,}",
        "",
        "SKU STATUS",
    ]

    for status in [
        "MATCHED",
        "ETSY_ONLY",
        "UNAVAILABLE_SKU",
        "MISSING_SKU",
        "DUPLICATE_SKU",
        "DUPLICATE_ETSY_SKU",
        "DUPLICATE_PRINTIFY_SKU",
    ]:
        lines.append(
            f"{status}: "
            f"{int(status_counts.get(status, 0)):,}"
        )

    lines += [
        "",
        "LISTING STATUS",
    ]

    for status in [
        "FULLY_MATCHED",
        "PARTIALLY_MATCHED",
        "NO_PRINTIFY_PRODUCTS",
        "HAS_DUPLICATE_SKU",
        "NEEDS_REVIEW",
        "NO_REAL_SKUS",
    ]:
        lines.append(
            f"{status}: "
            f"{int(listing_counts.get(status, 0)):,}"
        )

    lines += [
        "",
        "Printify-only variants are informational, not alerts.",
        "ETSY_ONLY means the Etsy SKU is not currently found in Printify.",
    ]

    (
        OUTPUT_DIR / "sync_summary.txt"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    # Update the SQLite database.
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
