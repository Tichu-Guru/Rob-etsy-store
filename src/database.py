from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd


def initialize_database(path: Path):
    con = sqlite3.connect(path)

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            etsy_listings INTEGER NOT NULL,
            etsy_sku_rows INTEGER NOT NULL,
            printify_variants INTEGER NOT NULL,
            matched_rows INTEGER NOT NULL,
            alert_rows INTEGER NOT NULL
        )
        """
    )

    con.commit()

    return con


def replace_table(con, table_name, df):
    df.to_sql(
        table_name,
        con,
        if_exists="replace",
        index=False,
    )


def record_run(
    con,
    run_timestamp,
    etsy_listings,
    etsy_sku_rows,
    printify_variants,
    matched_rows,
    alert_rows,
):
    con.execute(
        """
        INSERT INTO sync_runs (
            run_timestamp,
            etsy_listings,
            etsy_sku_rows,
            printify_variants,
            matched_rows,
            alert_rows
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_timestamp,
            etsy_listings,
            etsy_sku_rows,
            printify_variants,
            matched_rows,
            alert_rows,
        ),
    )

    con.commit()

    # Compact the SQLite database after updating the tables.
    con.execute("VACUUM")


# ---------------------------------------------------------
# HISTORICAL SNAPSHOT SUPPORT
# ---------------------------------------------------------

def initialize_history_tables(con):
    """
    Create the historical snapshot tables if they do not exist.

    Existing current-state tables are left untouched.
    """

    con.execute("""
        CREATE TABLE IF NOT EXISTS sync_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            etsy_listings INTEGER NOT NULL,
            etsy_sku_rows INTEGER NOT NULL,
            printify_variants INTEGER NOT NULL,
            matched_rows INTEGER NOT NULL,
            alert_rows INTEGER NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS comparison_history (
            snapshot_id INTEGER NOT NULL,
            etsy_row_number INTEGER,
            etsy_listing_id TEXT,
            etsy_title TEXT,
            etsy_sku TEXT,
            etsy_price TEXT,
            etsy_quantity TEXT,
            printify_product_id TEXT,
            printify_title TEXT,
            printify_variant_id REAL,
            printify_sku TEXT,
            printify_variant_title TEXT,
            printify_price REAL,
            printify_cost REAL,
            printify_enabled INTEGER,
            printify_available INTEGER,
            print_provider_id REAL,
            blueprint_id REAL,
            printify_options_json TEXT,
            printify_product_options_json TEXT,
            printify_shipping_cost REAL,
            etsy_listing_count_for_sku INTEGER,
            etsy_sku_shared_across_listings INTEGER,
            match_status TEXT,
            etsy_price_numeric REAL,
            printify_price_numeric REAL,
            printify_cost_numeric REAL,
            estimated_gross_margin REAL,
            estimated_gross_margin_pct REAL
        )
    """)

    con.commit()


def create_snapshot(
    con,
    run_timestamp,
    etsy_listings,
    etsy_sku_rows,
    printify_variants,
    matched_rows,
    alert_rows,
):
    """
    Create one historical synchronization snapshot.

    Returns the new snapshot ID.
    """

    cursor = con.execute(
        """
        INSERT INTO sync_snapshots (
            run_timestamp,
            etsy_listings,
            etsy_sku_rows,
            printify_variants,
            matched_rows,
            alert_rows
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_timestamp,
            int(etsy_listings),
            int(etsy_sku_rows),
            int(printify_variants),
            int(matched_rows),
            int(alert_rows),
        ),
    )

    con.commit()

    return cursor.lastrowid


def save_comparison_history(
    con,
    snapshot_id,
    comparison,
):
    """
    Save the complete Etsy/Printify comparison for a snapshot.

    This preserves the state of the comparison at the time of
    synchronization without changing the existing current-state
    tables.
    """

    if comparison is None or comparison.empty:
        return

    history = comparison.copy()
    history["snapshot_id"] = int(snapshot_id)

    columns = [
        "snapshot_id",
        "etsy_row_number",
        "etsy_listing_id",
        "etsy_title",
        "etsy_sku",
        "etsy_price",
        "etsy_quantity",
        "printify_product_id",
        "printify_title",
        "printify_variant_id",
        "printify_sku",
        "printify_variant_title",
        "printify_price",
        "printify_cost",
        "printify_enabled",
        "printify_available",
        "print_provider_id",
        "blueprint_id",
        "printify_options_json",
        "printify_product_options_json",
        "printify_shipping_cost",
        "etsy_listing_count_for_sku",
        "etsy_sku_shared_across_listings",
        "match_status",
        "etsy_price_numeric",
        "printify_price_numeric",
        "printify_cost_numeric",
        "estimated_gross_margin",
        "estimated_gross_margin_pct",
    ]

    for column in columns:
        if column not in history.columns:
            history[column] = None

    history = history[columns]

    rows = history.where(
        history.notna(),
        None,
    ).itertuples(
        index=False,
        name=None,
    )

    con.executemany(
        """
        INSERT INTO comparison_history (
            snapshot_id,
            etsy_row_number,
            etsy_listing_id,
            etsy_title,
            etsy_sku,
            etsy_price,
            etsy_quantity,
            printify_product_id,
            printify_title,
            printify_variant_id,
            printify_sku,
            printify_variant_title,
            printify_price,
            printify_cost,
            printify_enabled,
            printify_available,
            print_provider_id,
            blueprint_id,
            printify_options_json,
            printify_product_options_json,
            printify_shipping_cost,
            etsy_listing_count_for_sku,
            etsy_sku_shared_across_listings,
            match_status,
            etsy_price_numeric,
            printify_price_numeric,
            printify_cost_numeric,
            estimated_gross_margin,
            estimated_gross_margin_pct
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?
        )
        """,
        rows,
    )

    con.commit()
