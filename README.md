# Etsy ↔ Printify Product Database — v2

Built against the actual Etsy Listings CSV supplied for this project.

The uploaded CSV contains 167 listing rows and 24 columns. It has no Etsy listing ID; the importer preserves source row number and creates a deterministic listing key.

Important: Etsy's SKU column contains comma-separated SKUs, and some SKUs are reused across listings. This version therefore expands SKUs into variant rows and does not incorrectly treat duplicate Etsy SKUs as errors. It also does not guess which variation option belongs to a SKU when the CSV does not encode that relationship reliably.

## Setup

1. Create a private GitHub repository.
2. Upload this project.
3. Keep `data/Etsy.csv` as your Etsy export.
4. Add GitHub Actions secrets:
   - `PRINTIFY_API_TOKEN`
   - `PRINTIFY_SHOP_ID` (optional if you have one shop)
5. Run the workflow manually or wait for its daily schedule.

Never put the Printify token in source code. Use a newly generated token; the token previously shared in the earlier project should remain revoked.

## Outputs

- `output/etsy_printify_comparison.csv`
- `output/listing_summary.csv`
- `output/alerts.csv`
- `output/printify_products.csv`
- `data/etsy_printify.db`

The SQLite database contains Etsy listings, Etsy SKU rows, Printify variants, comparisons, summaries, alerts, and sync history.

## Matching

Primary match:

`normalized Etsy SKU == normalized Printify SKU`

The comparison can represent one Etsy SKU matching multiple Printify variants instead of silently choosing one.

## Future expansion

This is the base for profit calculations, Etsy fees, Etsy Ads, shipping, packaging, price history, Printify cost history, price simulation, and dashboards.
