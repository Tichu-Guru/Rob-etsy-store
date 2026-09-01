from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from pathlib import Path


def load_mapping(path: Path) -> dict[str, str]:
    """
    Load the persistent CSV-row -> Etsy listing-ID mapping.

    The mapping is intentionally independent of:
      - images
      - titles
      - prices
      - SKUs

    Those fields can legitimately change or be shared.
    """
    mapping: dict[str, str] = {}

    if not path.exists():
        return mapping

    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)

            for row in reader:
                key = str(
                    row.get("etsy_listing_key", "")
                    or ""
                ).strip()

                listing_id = str(
                    row.get("etsy_api_listing_id", "")
                    or ""
                ).strip()

                if key and listing_id:
                    mapping[key] = listing_id

    except Exception as exc:
        print(
            "WARNING: Could not read Etsy listing mapping: "
            f"{exc}"
        )

    return mapping


def save_mapping(
    mapping: dict[str, str],
    path: Path,
) -> None:
    """
    Atomically save the persistent mapping.
    """
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".etsy_listing_map_",
        suffix=".csv",
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            writer = csv.writer(handle)

            writer.writerow(
                [
                    "etsy_listing_key",
                    "etsy_api_listing_id",
                ]
            )

            for key in sorted(mapping):
                listing_id = str(
                    mapping[key]
                    or ""
                ).strip()

                if not listing_id:
                    continue

                writer.writerow(
                    [
                        key,
                        listing_id,
                    ]
                )

        os.replace(
            temp_name,
            path,
        )

    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def bootstrap_from_database(
    db_path: Path,
    mapping: dict[str, str],
) -> dict[str, str]:
    """
    Recover only SAFE historical mappings from the existing database.

    A database mapping is accepted only when one Etsy API listing ID
    belongs to exactly one Etsy CSV listing key.

    This deliberately refuses to bootstrap ambiguous mappings because
    the old SKU-based enrichment could assign the same Etsy listing ID
    to multiple CSV rows.
    """
    if not db_path.exists():
        return mapping

    try:
        connection = sqlite3.connect(
            db_path
        )

        rows = connection.execute(
            """
            SELECT
                etsy_listing_key,
                etsy_api_listing_id
            FROM etsy_listings
            WHERE
                etsy_listing_key IS NOT NULL
                AND TRIM(etsy_listing_key) <> ''
                AND etsy_api_listing_id IS NOT NULL
                AND TRIM(CAST(etsy_api_listing_id AS TEXT)) <> ''
            """
        ).fetchall()

        connection.close()

    except Exception as exc:
        print(
            "WARNING: Could not bootstrap Etsy listing mapping "
            f"from database: {exc}"
        )
        return mapping

    id_to_keys: dict[str, set[str]] = {}

    for key, listing_id in rows:
        clean_key = str(
            key or ""
        ).strip()

        clean_id = str(
            listing_id or ""
        ).strip()

        if not clean_key or not clean_id:
            continue

        id_to_keys.setdefault(
            clean_id,
            set(),
        ).add(
            clean_key
        )

    safe_count = 0

    for listing_id, keys in id_to_keys.items():

        # Do not accept an API listing ID that was previously
        # assigned to multiple CSV listings.
        if len(keys) != 1:
            continue

        key = next(iter(keys))

        if key not in mapping:
            mapping[key] = listing_id
            safe_count += 1

    if safe_count:
        print(
            "Recovered safe Etsy listing mappings from database: "
            f"{safe_count:,}"
        )

    return mapping
