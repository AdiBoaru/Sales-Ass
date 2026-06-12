# Schema reference — sursa de adevăr a numelor

> **Sursa unică de adevăr pentru schema DB este [`docs/schema_v2_production.sql`](schema_v2_production.sql)**
> (829 linii, deja rulată + seedată în Supabase). Acest fișier este harta dintre numele
> vechi din planul inițial (carduri T0xx + versiuni anterioare de CLAUDE.md) și numele
> REALE din schema_v2. Orice cod nou folosește coloana din dreapta.
>
> Decizie de arhitectură (2026-06-12): schema_v2 câștigă. Vezi secțiunea „Decizii" jos.

## Mapare nume vechi → real

| Nume vechi (planul inițial) | Nume REAL (schema_v2) | Note |
|---|---|---|
| `core.businesses` | `businesses` | schemă plată `public`, fără prefix |
| `core.channel_instances` | `channels` | `kind`, `provider_account_id` |
| `core.wa_templates` | `wa_templates` | identic ca structură |
| `core.audit_log` | `audit_log` | |
| `conv.contacts` | `contacts` | `locale` (nu `language`), `lifecycle`, `lead_score` |
| `conv.channel_identities` | `channel_identities` | `channel_kind`, `external_id` (+ `external_id_hash` generat) |
| `conv.messages` | `messages` | **partiționat lunar**; `direction`+`author` (nu `role`); `body` (nu `content`); `provider_msg_id` |
| `conv.outbox` | `outbox` | identic |
| `conv.conversation_state` (tabel separat) | `conversations.state` (coloană jsonb) | + `state_version` (optimistic lock). Bugetul 8KB: vezi 003 |
| `conv.short_memory` | — | nu există; folosește `conversation_summaries` |
| `conv.inbound_dedupe` (tabel) | — | dedupe via `messages` unique pe `(business_id, provider_msg_id, created_at)` |
| `conv.checkout_links` | `checkout_links` | identic |
| `conv.orders` | `orders` | `external_id`, `total`, `attribution`; **fără `customer_phone`** (PII doar în channel_identities) |
| `conv.back_in_stock_subscriptions` | `back_in_stock_subscriptions` | identic |
| `conv.bookings` | `appointments` | `service_name`, `starts_at`/`ends_at`, `external_ref` |
| `conv.gdpr_requests` | `gdpr_requests` | |
| (nou) | `conversations` | tabel central: `bot_active`, `handoff_until`, `last_inbound_at`, `state` |
| (nou) | `message_status_events` | delivered/read/failed de la provider |
| (nou) | `conversation_summaries` | summarizer conversații lungi |
| `catalog.taxonomy` | — | **nu există**; folosește `categories` + `intent_aliases` (vezi Decizii) |
| `catalog.products` | `products` | `price`/`sale_price` (nu `min_price`), `availability`, `ai_summary` |
| `catalog.product_embeddings` | `product_embeddings` | HNSW cosine, `content_hash` |
| `catalog.product_variants` | `product_variants` | `label`, `sku`, `price`, `stock` |
| `catalog.review_summaries` | `product_review_summaries` | `top_pros`/`top_cons`, `sentiment` |
| `catalog.faq` | `faqs` | `locale`, `embedding` direct pe rând |
| `catalog.faq_aliases` | `intent_aliases` | `phrase_norm`, `target_kind`, `status` |
| `catalog.faq_alias_candidates` | `intent_aliases` (status='candidate') | aceeași tabelă, status |
| `catalog.response_cache` | `semantic_cache` | `query_norm`, `embedding`, `expires_at`, `hit_count` |
| `catalog.clarification_templates` | — | nu există; clarificările vin din cod/prompt |
| `catalog.knowledge_guides` | — | nu există în MVP |
| `catalog.services` | — | verticalele servicii folosesc `appointments.service_name` |
| `catalog.locations` | — | nu există în MVP |
| `analytics.message_events` | `analytics_events` | **partiționat lunar**; `event_type`+`properties` (model generic) |
| `analytics.llm_calls` | — | metricele LLM stau pe `messages` (tokens_in/out, cost_usd, latency_ms) + `analytics_events` |
| `analytics.debug_snapshots` | — | nu există; `conversation_evals` + `golden_tests` în loc |
| `analytics.usage_daily` | `usage_daily` | rollup zilnic; PK `(business_id, day)` |
| (nou) | `catalog_sync_runs`, `catalog_quality_alerts` | ingestion monitor |
| (nou) | `shipments`, `proactive_jobs` | AWB + scheduler proactiv |
| (nou) | `conversation_evals`, `golden_tests` | LLM-as-judge + gate CI |

## Câmpuri-cheie care diferă (atenție la cod)

- **`messages`**: `direction` (`inbound`/`outbound`/`internal`) + `author` (`contact`/`bot`/`human_agent`/`system`), NU `role`. Textul e `body`, NU `content`. Dedupe pe `provider_msg_id`.
- **`contacts.locale`** (nu `language`); FAQ/cache au și ele `locale`. În cod intern păstrăm `ctx.language` dar coloana DB e `locale`.
- **`conversations.state`**: jsonb pe conversație, NU tabel separat. `displayed_products` = ref-uri (principiul 8 rămâne valabil, impus în cod).
- **PII**: doar în `channel_identities.external_id` (+ hash). `orders` NU are `customer_phone` în schema_v2 — telefonul se rezolvă prin `contact_id → channel_identities`.

## Decizii de arhitectură (2026-06-12)

1. **schema_v2 e sursa de adevăr.** Nu rescriem schema; codul se aliniază la numele reale.
2. **`taxonomy` nu se adaugă acum.** Promptul agentului (principiul 9) se generează din `categories` (+ `intent_aliases` pentru rutare). Un tabel `taxonomy` bogat (concerns, applicable_filters) se adaugă aditiv DOAR când verticalul cere filtre pe concerns.
3. **Securitate — plasă RLS adăugată peste schema_v2** prin `docs/003_bot_runtime_role.sql` (APLICAT + TESTAT pe Supabase 2026-06-12): rol `bot_runtime` fără bypassrls + politici `app.business_id` + `CHECK pg_column_size(state) < 8192`. Principiul 7 (RLS ca plasă) e respectat și dovedit (izolare cross-tenant testată pe products).
   - **Model de conectare pe Supabase:** prin pooler te conectezi ca `postgres` (nu există login custom prin pooler). Workerul face la fiecare conexiune `SET ROLE bot_runtime; SET app.business_id = $1` → coboară privilegiile + activează RLS. `postgres` e membru al `bot_runtime` (grant în 003) ca să poată face SET ROLE. `service_role` rămâne doar pentru migrări/admin.
   - **Conexiune dev (Windows):** pooler-ul Supabase (`...pooler.supabase.com:5432`, user `postgres.<ref>`); conexiunea directă `db.<ref>.supabase.co` NU se rezolvă pe rețele IPv4. asyncpg pe Windows are bug la getaddrinfo async → workaround în `scripts/` (rezolvare IPv4 sincronă + connect pe IP). Pe Linux/VPS nu e necesar.
4. **Taskurile T021–T033** (scrierea migrării 002 bucată cu bucată) sunt **OBSOLETE** — schema e deja construită. Rămân valide: review-ul (acest doc = T020), 003 (rol+RLS), seed/embed (există în `db/seed/`).
