# Pilot Data Pack Audit

Generated: `2026-07-06T13:19:00.585856+00:00`
Business: `Sole Demo` (`6098812a-50fc-44bd-a1ba-bc77e6399158`)
Slug: `nativex-demo`  Vertical: `ecommerce`  Status: `active`
Verdict: **PASS**

## Gates

| Gate | Status | Actual | Expected | Severity |
|---|---:|---:|---|---|
| active products | PASS | 468 | >= 50 | P0 |
| top 50 core card fields | PASS | 50/50 | all have absolute product_url, price, category, image | P0 |
| product summaries | PASS | 468 | >= 20 active products with ai_summary | P1 |
| product embeddings | PASS | 468 | >= 50 active products with embeddings | P0 |
| active FAQ | PASS | 32 | >= 8 | P0 |
| embedded FAQ | PASS | 32 | >= 8 | P1 |
| approved aliases | PASS | 13 | >= 5 | P1 |

## Metrics

| Metric | Value |
|---|---:|
| `products_total` | 500 |
| `products_active` | 468 |
| `products_in_stock_or_low_stock` | 468 |
| `active_with_product_url` | 468 |
| `active_with_absolute_product_url` | 468 |
| `active_with_price` | 468 |
| `active_with_category` | 468 |
| `active_with_image` | 468 |
| `active_with_absolute_image` | 468 |
| `active_with_ai_summary` | 468 |
| `active_with_embedding` | 468 |
| `categories_total` | 82 |
| `active_categories_used` | 57 |
| `faqs_active` | 32 |
| `faqs_with_embedding` | 32 |
| `aliases_approved` | 13 |
| `templated_name_suffix_count` | 0 |
| `duplicate_name_groups` | 0 |

## Coverage

| Area | Coverage |
|---|---:|
| `product_url` | 100% |
| `image` | 100% |
| `summary` | 100% |
| `embedding` | 100% |
| `faq_embedding` | 100% |

## Currencies

- `RON`: 468

## FAQ Topics

- `livrare`: 12
- `retur`: 8
- `plata`: 6
- `garantie`: 1
- `program`: 1
- `factura`: 3
- `gdpr`: 1

## Alias Breakdown

- `route`: 13

## Top Product Gaps

- none in sampled top products

## Next Actions

- none
