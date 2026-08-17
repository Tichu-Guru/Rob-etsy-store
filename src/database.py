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
