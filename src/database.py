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

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            etsy_listings INTEGER NOT NULL,
            etsy_sku_rows INTEGER NOT NULL,
            printify_variants INTEGER NOT NULL,
            matched_rows INTEGER NOT NULL,
            alert_rows INTEGER NOT NULL
        )
        """
    )

    con.execute(
        """
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
        """
    )

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
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?
        )
        """,
        rows,
    )

    con.commit()


# ---------------------------------------------------------
# HISTORICAL CHANGE DETECTION
# ---------------------------------------------------------

def get_previous_snapshot_id(
    con,
    current_snapshot_id,
):
    """
    Return the snapshot immediately preceding the supplied snapshot.

    Returns None when there is no previous snapshot.
    """

    row = con.execute(
        """
        SELECT MAX(snapshot_id)
        FROM sync_snapshots
        WHERE snapshot_id < ?
        """,
        (int(current_snapshot_id),),
    ).fetchone()

    if row is None or row[0] is None:
        return None

    return int(row[0])


def build_changes_since_previous_snapshot(
    con,
    previous_snapshot_id,
    current_snapshot_id,
):
    """
    Compare two historical comparison snapshots.

    The primary identity is the combination of:

        etsy_listing_id + etsy_sku

    This is intentional. A SKU can legitimately be shared by more
    than one Etsy listing, so SKU alone is not sufficient.

    Rows without a real Etsy SKU are excluded from change detection
    because Etsy CSV row numbers are not a reliable historical key.

    Returns one row per meaningful change.
    """

    output_columns = [
        "change_type",
        "etsy_listing_id",
        "etsy_title",
        "etsy_sku",
        "field_changed",
        "previous_value",
        "current_value",
        "previous_snapshot_id",
        "current_snapshot_id",
    ]

    if (
        previous_snapshot_id is None
        or current_snapshot_id is None
    ):
        return pd.DataFrame(
            columns=output_columns
        )

    columns = [
        "etsy_listing_id",
        "etsy_title",
        "etsy_sku",
        "etsy_price_numeric",
        "printify_cost_numeric",
        "printify_shipping_cost",
        "printify_available",
        "match_status",
    ]

    column_sql = ", ".join(columns)

    previous = pd.read_sql_query(
        f"""
        SELECT {column_sql}
        FROM comparison_history
        WHERE snapshot_id = ?
        """,
        con,
        params=(int(previous_snapshot_id),),
    )

    current = pd.read_sql_query(
        f"""
        SELECT {column_sql}
        FROM comparison_history
        WHERE snapshot_id = ?
        """,
        con,
        params=(int(current_snapshot_id),),
    )

    if previous.empty and current.empty:
        return pd.DataFrame(
            columns=output_columns
        )

    # -----------------------------------------------------
    # CLEAN AND NORMALIZE IDENTIFIERS
    # -----------------------------------------------------

    for frame in [previous, current]:

        frame["etsy_listing_id"] = (
            frame["etsy_listing_id"]
            .astype("string")
            .str.strip()
        )

        frame["etsy_sku"] = (
            frame["etsy_sku"]
            .astype("string")
            .str.strip()
        )

    # A missing/blank Etsy SKU is not a reliable historical key.
    previous = previous[
        previous["etsy_sku"].notna()
        & (previous["etsy_sku"] != "")
    ].copy()

    current = current[
        current["etsy_sku"].notna()
        & (current["etsy_sku"] != "")
    ].copy()

    # -----------------------------------------------------
    # PRIMARY KEY
    # -----------------------------------------------------

    previous["_history_key"] = (
        previous["etsy_listing_id"].fillna("")
        + "\x1f"
        + previous["etsy_sku"].fillna("")
    )

    current["_history_key"] = (
        current["etsy_listing_id"].fillna("")
        + "\x1f"
        + current["etsy_sku"].fillna("")
    )

    # The comparison schema normally contains one row per Etsy
    # SKU/listing combination. If duplicate rows somehow exist,
    # retain the first one rather than producing duplicate changes.
    previous = previous.drop_duplicates(
        subset=["_history_key"],
        keep="first",
    )

    current = current.drop_duplicates(
        subset=["_history_key"],
        keep="first",
    )

    previous_keys = set(
        previous["_history_key"]
    )

    current_keys = set(
        current["_history_key"]
    )

    changes = []

    # -----------------------------------------------------
    # NEW ETSY SKUS
    # -----------------------------------------------------

    for key in sorted(
        current_keys - previous_keys
    ):

        row = current.loc[
            current["_history_key"] == key
        ].iloc[0]

        changes.append(
            {
                "change_type": "NEW_ETSY_SKU",
                "etsy_listing_id": row["etsy_listing_id"],
                "etsy_title": row["etsy_title"],
                "etsy_sku": row["etsy_sku"],
                "field_changed": "",
                "previous_value": "",
                "current_value": "Present",
                "previous_snapshot_id": previous_snapshot_id,
                "current_snapshot_id": current_snapshot_id,
            }
        )

    # -----------------------------------------------------
    # REMOVED ETSY SKUS
    # -----------------------------------------------------

    for key in sorted(
        previous_keys - current_keys
    ):

        row = previous.loc[
            previous["_history_key"] == key
        ].iloc[0]

        changes.append(
            {
                "change_type": "REMOVED_ETSY_SKU",
                "etsy_listing_id": row["etsy_listing_id"],
                "etsy_title": row["etsy_title"],
                "etsy_sku": row["etsy_sku"],
                "field_changed": "",
                "previous_value": "Present",
                "current_value": "",
                "previous_snapshot_id": previous_snapshot_id,
                "current_snapshot_id": current_snapshot_id,
            }
        )

    # -----------------------------------------------------
    # EXISTING SKU CHANGES
    # -----------------------------------------------------

    fields = [
        (
            "etsy_price_numeric",
            "ETSY_PRICE_CHANGED",
        ),
        (
            "printify_cost_numeric",
            "PRINTIFY_COST_CHANGED",
        ),
        (
            "printify_shipping_cost",
            "PRINTIFY_SHIPPING_CHANGED",
        ),
        (
            "printify_available",
            "PRINTIFY_AVAILABILITY_CHANGED",
        ),
        (
            "match_status",
            "MATCH_STATUS_CHANGED",
        ),
    ]

    common_keys = sorted(
        previous_keys & current_keys
    )

    for key in common_keys:

        previous_row = previous.loc[
            previous["_history_key"] == key
        ].iloc[0]

        current_row = current.loc[
            current["_history_key"] == key
        ].iloc[0]

        for field, change_type in fields:

            old_value = previous_row[field]
            new_value = current_row[field]

            old_missing = pd.isna(old_value)
            new_missing = pd.isna(new_value)

            if old_missing and new_missing:
                continue

            if old_missing != new_missing:
                changed = True

            else:
                changed = (
                    old_value != new_value
                )

            if not changed:
                continue

            # -------------------------------------------------
            # MATCH STATUS CHANGES GET A MORE SPECIFIC LABEL
            # -------------------------------------------------

            actual_change_type = change_type

            if field == "match_status":

                old_status = (
                    str(old_value)
                    if not pd.isna(old_value)
                    else ""
                )

                new_status = (
                    str(new_value)
                    if not pd.isna(new_value)
                    else ""
                )

                if (
                    old_status == "MATCHED"
                    and new_status == "ETSY_ONLY"
                ):
                    actual_change_type = (
                        "MATCHED_TO_ETSY_ONLY"
                    )

                elif (
                    old_status == "ETSY_ONLY"
                    and new_status == "MATCHED"
                ):
                    actual_change_type = (
                        "ETSY_ONLY_TO_MATCHED"
                    )

            changes.append(
                {
                    "change_type": actual_change_type,
                    "etsy_listing_id": current_row[
                        "etsy_listing_id"
                    ],
                    "etsy_title": current_row[
                        "etsy_title"
                    ],
                    "etsy_sku": current_row[
                        "etsy_sku"
                    ],
                    "field_changed": field,
                    "previous_value": (
                        ""
                        if old_missing
                        else old_value
                    ),
                    "current_value": (
                        ""
                        if new_missing
                        else new_value
                    ),
                    "previous_snapshot_id": (
                        previous_snapshot_id
                    ),
                    "current_snapshot_id": (
                        current_snapshot_id
                    ),
                }
            )

    if not changes:
        return pd.DataFrame(
            columns=output_columns
        )

    result = pd.DataFrame(
        changes,
        columns=output_columns,
    )

    return result
