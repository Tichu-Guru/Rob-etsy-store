from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .config import DATABASE_PATH, ETSY_CSV, OUTPUT_DIR
from .database import initialize_database, record_run, replace_table
from .etsy import build_etsy_tables, read_etsy_csv
from .matching import build_comparison
from .printify import PrintifyClient


# ---------------------------------------------------------
# PROFITABILITY SETTINGS
# ---------------------------------------------------------

LOW_PROFIT_THRESHOLD = 15.0

ETSY_TRANSACTION_RATE = 0.065
ETSY_PAYMENT_RATE = 0.03
ETSY_PAYMENT_FIXED_FEE = 0.25

# Offsite Ads intentionally excluded.
# Etsy listing fee is also excluded from the per-sale
# calculation because it is charged when a listing is
# created/renewed, not on every sale.


def build_profitability_report(comparison: pd.DataFrame):
    """
    Calculate estimated net profit for Etsy/Printify
    matched variants.

    Assumptions:
      - U.S. customer
      - Customer pays $0 shipping
      - Seller pays Printify shipping
      - Printify first-item U.S. shipping is used
      - Etsy transaction fee = 6.5%
      - Etsy payment processing = 3% + $0.25
      - Offsite Ads excluded
      - Etsy listing fee excluded from per-sale calculation

    Returns:
      profitability_all
      low_profit
    """

    if comparison.empty:
        empty = comparison.copy()

        for column in [
            "etsy_transaction_fee",
            "etsy_payment_processing_fee",
            "etsy_total_fees",
            "estimated_net_profit",
            "estimated_net_margin_pct",
            "profitability_status",
        ]:
            empty[column] = pd.Series(dtype="float64")

        return empty, empty

    # Only calculate profitability where we have a real
    # Etsy -> Printify match.
    profit = comparison[
        comparison["match_status"] == "MATCHED"
    ].copy()

    if profit.empty:
        return profit, profit

    # Convert numeric fields safely.
    profit["etsy_price_for_profit"] = pd.to_numeric(
        profit["etsy_price"],
        errors="coerce",
    )

    profit["printify_cost_for_profit"] = pd.to_numeric(
        profit["printify_cost_numeric"],
        errors="coerce",
    )

    profit["printify_shipping_for_profit"] = pd.to_numeric(
        profit["printify_shipping_cost"],
        errors="coerce",
    )

    # Etsy transaction fee.
    profit["etsy_transaction_fee"] = (
        profit["etsy_price_for_profit"]
        * ETSY_TRANSACTION_RATE
    )

    # Etsy Payments processing fee.
    profit["etsy_payment_processing_fee"] = (
        profit["etsy_price_for_profit"]
        * ETSY_PAYMENT_RATE
        + ETSY_PAYMENT_FIXED_FEE
    )

    profit["etsy_total_fees"] = (
        profit["etsy_transaction_fee"]
        + profit["etsy_payment_processing_fee"]
    )

    # We cannot calculate a true net margin without shipping.
    profit["estimated_net_profit"] = (
        profit["etsy_price_for_profit"]
        - profit["printify_cost_for_profit"]
        - profit["printify_shipping_for_profit"]
        - profit["etsy_total_fees"]
    )

    profit["estimated_net_margin_pct"] = (
        profit["estimated_net_profit"]
        / profit["etsy_price_for_profit"]
        * 100
    )

    # Don't report invalid margins.
    invalid_price = (
        profit["etsy_price_for_profit"].isna()
        | (profit["etsy_price_for_profit"] <= 0)
    )

    missing_shipping = (
        profit["printify_shipping_for_profit"].isna()
    )

    profit.loc[
        invalid_price | missing_shipping,
        "estimated_net_profit",
    ] = pd.NA

    profit.loc[
        invalid_price | missing_shipping,
        "estimated_net_margin_pct",
    ] = pd.NA

    profit["profitability_status"] = "OK"

    profit.loc[
        profit["estimated_net_margin_pct"] < LOW_PROFIT_THRESHOLD,
        "profitability_status",
    ] = "LOW_PROFIT"

    profit.loc[
        missing_shipping,
        "profitability_status",
    ] = "SHIPPING_UNAVAILABLE"

    profit.loc[
        invalid_price,
        "profitability_status",
    ] = "PRICE_UNAVAILABLE"

    # Keep the most useful columns first.
    preferred_columns = [
        "etsy_listing_id",
        "etsy_title",
        "etsy_sku",
        "etsy_price_for_profit",
        "printify_product_id",
        "printify_title",
        "printify_variant_id",
        "printify_variant_title",
        "printify_sku",
        "printify_cost_for_profit",
        "printify_shipping_for_profit",
        "etsy_transaction_fee",
        "etsy_payment_processing_fee",
        "etsy_total_fees",
        "estimated_net_profit",
        "estimated_net_margin_pct",
        "profitability_status",
    ]

    available_columns = [
        column
        for column in preferred_columns
        if column in profit.columns
    ]

    remaining_columns = [
        column
        for column in profit.columns
        if column not in available_columns
        and column not in [
            "etsy_price_for_profit",
            "printify_cost_for_profit",
            "printify_shipping_for_profit",
        ]
    ]

    profit = profit[
        available_columns + remaining_columns
    ]

    # Worst margins first.
    profit = profit.sort_values(
        by="estimated_net_margin_pct",
        ascending=True,
        na_position="last",
    )

    low_profit = profit[
        profit["profitability_status"] == "LOW_PROFIT"
    ].copy()

    return profit, low_profit


def main():
    ts = datetime.now(timezone.utc).isoformat()

    # -----------------------------------------------------
    # READ ETSY DATA
    # -----------------------------------------------------

    etsy_df = read_etsy_csv(ETSY_CSV)

    etsy_listings, etsy_variants = build_etsy_tables(
        etsy_df
    )

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

    etsy_rows = etsy_rows.to_dict(
        "records"
    )

    # -----------------------------------------------------
    # GET PRINTIFY DATA
    # -----------------------------------------------------

    client = PrintifyClient()

    shop_id = client.get_shop_id()

    printify_rows = client.export_variant_rows(
        shop_id
    )

    # -----------------------------------------------------
    # MATCH ETSY TO PRINTIFY
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # EXISTING OUTPUTS
    # -----------------------------------------------------

    comparison.to_csv(
        OUTPUT_DIR
        / "etsy_printify_comparison.csv",
        index=False,
    )

    listing_summary.to_csv(
        OUTPUT_DIR
        / "listing_summary.csv",
        index=False,
    )

    attention.to_csv(
        OUTPUT_DIR
        / "needs_attention.csv",
        index=False,
    )

    printify_only.to_csv(
        OUTPUT_DIR
        / "printify_only.csv",
        index=False,
    )

    pd.DataFrame(
        printify_rows
    ).to_csv(
        OUTPUT_DIR
        / "printify_products.csv",
        index=False,
    )

    # -----------------------------------------------------
    # NEW PROFITABILITY REPORT
    # -----------------------------------------------------

    (
        profitability_all,
        low_profit,
    ) = build_profitability_report(
        comparison
    )

    profitability_all.to_csv(
        OUTPUT_DIR
        / "profitability_all.csv",
        index=False,
    )

    low_profit.to_csv(
        OUTPUT_DIR
        / "low_profit_products.csv",
        index=False,
    )

    # -----------------------------------------------------
    # COUNTS
    # -----------------------------------------------------

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

    # Products for which we have enough information to
    # calculate profitability.
    profit_calculable = profitability_all[
        profitability_all[
            "profitability_status"
        ].isin(
            [
                "OK",
                "LOW_PROFIT",
            ]
        )
    ]

    low_profit_count = len(low_profit)

    calculable_count = len(profit_calculable)

    low_profit_percentage = (
        low_profit_count
        / calculable_count
        * 100
        if calculable_count
        else 0
    )

    # -----------------------------------------------------
    # SYNC SUMMARY
    # -----------------------------------------------------

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
        "HAS_PRINTIFY_SKU_PROBLEM",
        "NEEDS_REVIEW",
        "NO_REAL_SKUS",
    ]:
        lines.append(
            f"{status}: "
            f"{int(listing_counts.get(status, 0)):,}"
        )

    lines += [
        "",
        "PROFITABILITY",
        f"Profitability threshold: "
        f"{LOW_PROFIT_THRESHOLD:.0f}%",
        "Printify shipping: U.S. first-item shipping",
        "Customer shipping charged: $0.00",
        "Etsy transaction fee: 6.5%",
        "Etsy payment processing: 3% + $0.25",
        "Offsite Ads: EXCLUDED",
        "Etsy listing fee: EXCLUDED from per-sale calculation",
        "",
        f"Matched products with calculable profit: "
        f"{calculable_count:,}",
        f"Products below {LOW_PROFIT_THRESHOLD:.0f}% margin: "
        f"{low_profit_count:,}",
        f"Percent below threshold: "
        f"{low_profit_percentage:.1f}%",
        "",
        "ETSY_ONLY means the Etsy SKU is not currently found in Printify.",
        "Etsy-only SKUs are informational and are not treated as profitability problems.",
        "Profitability requires a matched Printify SKU and available U.S. shipping data.",
    ]

    (
        OUTPUT_DIR / "sync_summary.txt"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # PROFITABILITY SUMMARY
    # -----------------------------------------------------

    profitability_lines = [
        "LOW PROFITABILITY SUMMARY",
        f"Run UTC: {ts}",
        "",
        f"Profitability threshold: "
        f"{LOW_PROFIT_THRESHOLD:.0f}%",
        "",
        f"Matched products with calculable profit: "
        f"{calculable_count:,}",
        f"Products below {LOW_PROFIT_THRESHOLD:.0f}%: "
        f"{low_profit_count:,}",
        f"Percent below threshold: "
        f"{low_profit_percentage:.1f}%",
        "",
        "ASSUMPTIONS",
        "Customer pays $0 shipping.",
        "Seller pays Printify U.S. first-item shipping.",
        "Etsy transaction fee: 6.5%.",
        "Etsy payment processing: 3% + $0.25.",
        "Offsite Ads excluded.",
        "Etsy listing fee excluded from per-sale calculation.",
        "",
        "See low_profit_products.csv for the products below the threshold.",
    ]

    (
        OUTPUT_DIR / "profitability_summary.txt"
    ).write_text(
        "\n".join(profitability_lines) + "\n",
        encoding="utf-8",
    )

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

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
        pd.DataFrame(
            printify_rows
        ),
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

    # -----------------------------------------------------
    # CONSOLE OUTPUT
    # -----------------------------------------------------

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
    print("")
    print(
        f"Profitability products analyzed: "
        f"{calculable_count:,}"
    )
    print(
        f"Products below 15% margin: "
        f"{low_profit_count:,}"
    )


if __name__ == "__main__":
    main()
