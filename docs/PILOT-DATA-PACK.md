# Pilot Data Pack

This document is the operational companion for `NX-155`.

The goal is not to make the catalog perfect. The goal is to make the pilot tenant good enough that the web assistant can answer with grounded products, real links, useful FAQ answers and predictable follow-ups.

## Read-Only Audit

Run:

```bash
python scripts/audit_pilot_data.py --business <business_id>
```

Useful variants:

```bash
python scripts/audit_pilot_data.py --business <business_id> --format json
python scripts/audit_pilot_data.py --business <business_id> --output reports/pilot-data-pack.md
python scripts/audit_pilot_data.py --business <business_id> --warn-only
```

Default business is `PILOT_BUSINESS_ID` from the environment, or the demo tenant used by the simulator.

The script is read-only. It never backfills, embeds or edits rows.

## Export

After the gates pass, snapshot the pilot data pack:

```bash
python scripts/export_pilot_data_pack.py \
  --business <business_id> \
  --output reports/pilot-data-pack.json
```

The export contains business metadata, brands, categories, active products, `product_images`, product variants, FAQ rows, aliases and an embedding manifest. It does not export raw vector payloads; regenerate embeddings after import with the normal embedding jobs.

## What Must Pass

Minimum pilot gates:

- at least 50 active products;
- top 50 products have absolute `product_url`, price, category and at least one `product_images.url`;
- at least 20 active products have useful `ai_summary`;
- at least 50 active products have embeddings;
- at least 8 active FAQ entries;
- at least 8 active FAQ entries have embeddings;
- at least 5 approved aliases.

These thresholds are configurable:

```bash
python scripts/audit_pilot_data.py \
  --business <business_id> \
  --top-n 50 \
  --min-summaries 20 \
  --min-embeddings 50 \
  --min-faqs 8 \
  --min-aliases 5
```

## Typical Fixes

Missing product URLs:

```bash
python scripts/backfill_product_url.py <business_id>
```

Missing product embeddings:

```bash
python -m src.jobs.embed_products --force
```

Missing FAQ:

```bash
python -m src.jobs.seed_faqs --business <business_id>
```

Missing aliases:

```bash
python scripts/seed_intent_aliases.py --business <business_id>
python scripts/seed_intent_aliases.py --business <business_id> --dry-run
```

- seeds generic route-aliases (handoff + order) with `status='approved'`, idempotent;
- `phrase_norm` is produced by the same `canonicalize()` the alias stage uses (NX-73);
- extend `ROUTE_ALIASES` in the script for more phrasings; category/FAQ aliases need
  tenant-specific slugs / FAQ ids and are curated separately;
- keep only `status='approved'` aliases in the production path.

Duplicate product names (distinct SKUs sharing a generated display name):

```bash
python scripts/dedupe_product_names.py --business <business_id> --dry-run
python scripts/dedupe_product_names.py --business <business_id>
```

- appends the product's hero ingredient from `attributes.key_ingredients`
  (e.g. "… cu Acid Hialuronic") — real, no numeric suffixes;
- greedy, so every new name is unique in-group and globally; `slug` / `product_url`
  stay intact (links and embeddings untouched);
- idempotent: once names are unique a re-run is a no-op;
- a `residual` count > 0 means genuinely-identical seed rows (same category, color,
  no ingredients) — fix those in the source catalog, don't invent a fake difference.

Missing summaries/details:

- run catalog enrichment for the pilot subset;
- at minimum, enrich the products expected to appear in demos and web evals.

Missing images:

- import `product_images` from the store feed;
- each top pilot product should have at least one absolute image URL.

## How To Use The Report

Start with the `Gates` table:

- `FAIL` on P0 means the tenant is not ready for a paid pilot.
- `FAIL` on P1 means the tenant may demo, but quality will be visibly weaker.

Then inspect:

- `Top Product Gaps` for specific products that need fixes;
- `FAQ Topics` for missing policy coverage;
- `Alias Breakdown` for whether exact free-layer routing exists;
- `Coverage` for catalog-level health.

## Production Rule

Do not tune the planner or refactor `agent.py` to compensate for missing data.

Fix the data pack first. Then run web response evals (`NX-157`) and golden regression (`NX-145`) against a tenant that has real products, links, FAQs, aliases and embeddings.
