# Migrare v1 (catalog demo) → v2 (platformă producție)

## Mapare directă

| v1 | v2 | Ce se schimbă |
|---|---|---|
| `brands` | `brands` | + `business_id`, unique devine `(business_id, slug)` |
| `categories` | `categories` | + `business_id`, + `path` denormalizat |
| `products` | `products` | + `business_id`, `external_id`, `ai_summary`, `availability`, `stock_total`, `product_url`, `synced_at` |
| `product_variants` | la fel | + `business_id`, `external_id`; unique sku per tenant |
| `product_images` | la fel | scos `kind` placeholder/source (era specific demo-ului) |
| `product_sections` | la fel | identic |
| `ingredients` + `product_ingredients` | la fel | **fix**: FK real `ingredient_id` în loc de `ingredient_name` text |
| `product_badges` | la fel | identic |
| `reviews` | `reviews` | + `business_id`, `source`, `external_id` (dedupe la ingest extern) |
| `source_products_raw` | rămâne (sau muți în storage) | nu intră în hot path; ok ca audit |

## Script de migrare (ordinea)

1. Creează `businesses` și inserează tenant-ul demo: `insert into businesses(name, slug) values ('Sole Demo','sole-demo') returning id;`
2. Rulează schema v2 într-un proiect/schema nouă (nu peste v1).
3. Copiază datele cu `business_id` setat la tenant-ul demo. Pentru `product_ingredients`, join pe `ingredients.name` ca să obții `ingredient_id`.
4. Adaptează `seed-supabase.ts`: fiecare upsert primește `business_id` și `onConflict` devine `business_id,slug` / `business_id,sku`.
5. Generează `ai_summary` per produs (job Batch, ieftin) → apoi populezi `product_embeddings`.

## Decizii de design (de ce așa)

**Tenant-first, un singur Postgres.** Toate companiile serioase la scara ta (zeci-sute de tenanți) pornesc cu shared schema + `business_id` + RLS, nu cu DB per client. Migrezi la DB dedicat doar pentru un enterprise care o cere contractual.

**Messages și analytics_events partiționate pe lună.** Astea cresc nelimitat. Partiționarea îți dă retenție ieftină (drop partition, nu DELETE) — exact „retenție · partiționare" din diagrama ta. Automatizează cu pg_partman sau pg_cron.

**Outbox în Postgres, nu doar în Redis.** Redis Streams rămâne transportul (backbone-ul tău), dar răspunsul se scrie tranzacțional cu patch-ul de state în Postgres (`outbox`), iar dispatcherul publică. Dacă Redis moare, nu pierzi mesaje — asta e diferența dintre demo și producție.

**PII concentrat în `channel_identities`.** Telefonul E.164 stă într-un singur loc, cu hash pentru lookup. `gdpr_erase_contact()` anonimizează contactul, șterge identitățile și golește body-urile — agregatele și analytics rămân valide. Asta e modelul corect GDPR: erase = anonimizare, nu ștergere fizică a istoricului.

**Embeddings în tabel separat, nu coloană pe `products`.** Re-embed la schimbare de model fără să blochezi tabelul fierbinte; `content_hash` evită re-embed inutil la sync. HNSW, cosine. Căutarea hibridă = filtre SQL (categorie, preț, stoc) + `order by embedding <=> query` pe subsetul filtrat.

**Atribuirea de revenue e structurală.** `checkout_links.ref_code` → webhook comenzi setează `orders.attributed_checkout_link_id`. Dashboard-ul „botul a generat X RON" e un `select sum(total)` pe `usage_daily`, nu o estimare.

**`usage_daily` ca rollup.** Dashboard-ul și billing-ul citesc rollup-ul, niciodată `analytics_events` raw. Când treci de ~10M events/lună, CDC → ClickHouse exact ca în diagramă; schema asta e deja pregătită (events = append-only, partiționat).

**24h window = derivat, nu stocat.** `in_24h_window()` calculează din `last_inbound_at`. Un flag boolean stocat ar minți mereu la limită; un timestamp nu minte niciodată.

**Ce NU e în Postgres (intenționat):** lock per conversație, debounce, rate limit, DLQ → Redis. Prompt registry / canary → fișiere versionate în git + config. Media → object storage cu TTL, doar `media_ref` în DB.

## Primele 3 lucruri de făcut după schema

1. Webhook → `insert into messages ... on conflict do nothing` (dedupe gratis pe `provider_msg_id`).
2. Rollup job nocturn pentru `usage_daily` (alimentează și cost guard-ul zilnic).
3. Funcția SQL de căutare hibridă (`search_products(business_id, filters, query_embedding)`) — e tool-ul nr. 1 al agentului.
