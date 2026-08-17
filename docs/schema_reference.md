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
| `catalog.products` | `products` | `price`/`sale_price` (nu `min_price`), `availability`, `ai_summary`, + NX-171c: `content_status` (draft/reviewed/published/rejected — doar published servit, flag per-tenant), `schema_version`, `verified_at` |
| `catalog.product_embeddings` | `product_embeddings` | HNSW cosine, `content_hash`, + NX-171d: PK COMPUS `(product_id, doc_type, model)` — versiuni paralele; read-path filtrează `doc_type`+`model` activ (anti-duplicate) |
| `catalog.product_variants` | `product_variants` | `label`, `sku`, `price`, `stock`, + NX-171a: `gtin`, `net_content_value`, `net_content_unit`, `image_url`, `price_per_unit` (generated: preț/100ml ori /100g; `buc`→NULL) |
| (nou) | `product_relations` | NX-171b: relații explicite (`substitute`/`complement`/`accessory`/`routine_next`); integritate tenant prin FK compus (cross-tenant imposibil); citit de `get_complementary_products` |
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
   - **Model de conectare (NX-50, `docs/db_connections.md`):** două pool-uri. **Tenant path** = `bot_runtime` ca rol de **LOGIN** (conexiune directă 5432, `DATABASE_URL_BOT`, fără bypassrls); `tenant_conn` setează DOAR `app.business_id` la checkout (zero `SET ROLE` → nu se mai scurge sub multiplexare). **Control plane** (`resolve_channel`, joburi) = `admin_conn` pe rol privilegiat (`SUPABASE_DB_URL`). Fără `DATABASE_URL_BOT`, `bot_pool` cade compat pe `postgres` + `SET ROLE` în init (dev/test). `005_bot_runtime_login.sql` face doar `ALTER ROLE ... LOGIN` — grant-urile/politicile sunt deja din 003+004.
   - **Conexiune dev (Windows):** pooler-ul Supabase (`...pooler.supabase.com:5432`, user `postgres.<ref>`); conexiunea directă `db.<ref>.supabase.co` NU se rezolvă pe rețele IPv4. asyncpg pe Windows are bug la getaddrinfo async → workaround în `scripts/` (rezolvare IPv4 sincronă + connect pe IP). Pe Linux/VPS nu e necesar.
4. **Taskurile T021–T033** (scrierea migrării 002 bucată cu bucată) sunt **OBSOLETE** — schema e deja construită. Rămân valide: review-ul (acest doc = T020), 003 (rol+RLS), seed/embed (există în `db/seed/`).

## Convenții `businesses.settings` (jsonb)

`businesses.settings` (jsonb existent, `schema_v2_production.sql:43`) ține config per-tenant fără
schimbare de schemă. Chei convenite (citite defensiv — lipsă → default):

- **`settings["domain_pack"]`** (NX-114): override per-tenant peste default-ul JSON al verticalului
  (`src/domain/defaults/<vertical>.json`). Formă (toate cheile opționale, merge deep peste default):
  ```json
  {
    "concern_map":   {"<termen liber>": "<cheie canonică attributes->'concerns'>"},
    "risk_terms":    {"<locale>": {"human_request": ["..."], "legal_complaint": ["..."]}},
    "greetings":     {"<locale>": ["salut adițional", ...]},
    "profile_whitelist": ["skin_type", "budget_band", ...],
    "settled_order_statuses": ["delivered", "closed"]
  }
  ```
  Cheile/frazele se normalizează la încărcare (lower + fără diacritice). Loader: `src/domain/loader.py`
  (`load_domain_pack`), atașat pe `BusinessConfig.domain_pack` de `load_business`. Kill-switch
  `DOMAIN_PACK_ENABLED=false` → `domain_pack=None` (consumatorii cad pe constantele de cod).
- **`settings["currency"]`** (NX-114): moneda afișată (ex. `"RON"`, `"EUR"`). Fallback `"RON"`.
  `prompt_builder` o folosește în loc de „lei" hardcodat.
- **`settings["welcome"]`**: config de welcome (enabled, bot_name, suggestions) — vezi `greeting.py`.

## `web_turns` — ledgerul turelor web (NX-232, migrarea 040)

Tabel nou (nu exista în schema_v2): un rând per turn web acceptat, cheia de idempotency
`(business_id, conversation_id, client_turn_id)` + partial unique „un singur `accepted|running`
per conversație". Statusuri INTERNE `accepted|running|completed|failed|cancelled` — distincte de
statusul de contract NX-228 (`working`/`validating` sunt proiecții, `running` nu iese pe sârmă;
mapping-ul e `src/web/turn_service.py:project_wire_status`). `response_json` = ViewModel-ul
terminal EXACT (replay fără al doilea apel LLM); CHECK în DB: terminal ⇒ `response_json` NE-NULL
(P6). PII: `request_fingerprint` = HMAC (body-ul nu se stochează), `session_ref_hash` = sha256 —
zero body/token/visitor_id în clar. Scriitori: marginea web (accept/claim) + tranzacția
`TurnCommit` (complete, prin seam-ul `on_commit`); DELETE doar pe control plane (retenție + GDPR).

## `conversation_carts` / `conversation_cart_items` / `commerce_action_receipts` — coșul canonic (NX-237, migrarea 041)

Tabele noi (nu existau în schema_v2): coșul conversației devine UN sistem canonic server-side,
mutat exclusiv prin `CartService` (`src/commerce/cart_service.py`); `conversations.state` păstrează
doar `cart_ref` `{id, version, lines}` — liniile cu preț copiat din `state.cart` (NX-79) sunt
LEGACY sub `CONVERSATION_CART_ENABLED` (OFF = byte-identic, nimic nu se atinge).

- `conversation_carts`: un singur coș ACTIV per conversație (partial unique pe
  `(business_id, conversation_id) where status='active'`), `version` monotonă (optimistic
  concurrency pentru acțiunile NX-240), `status ∈ active|checked_out|expired`. Checkout-ul care
  acoperă integral coșul îl închide; următorul add deschide altul.
- `conversation_cart_items`: REFS + cantități, NU cache de name/price (prețul se rehidratează la
  fiecare citire/mutație). FK COMPUS `(business_id, product_id) → products(business_id, id)` —
  produsul altui tenant nu intră structural. `UNIQUE NULLS NOT DISTINCT
  (business_id, cart_id, product_id, variant_id)` — două linii „fără variantă" = aceeași linie.
  CHECK `quantity between 1 and 10` (plasa peste politica din cod).
- `commerce_action_receipts`: dovada idempotentă a fiecărei mutații — `UNIQUE (business_id,
  idempotency_key)` (`t:<turn>:<op>:<fp>` pe calea LLM, `a:<action_id>` pe calea de acțiuni),
  `status ∈ pending|succeeded|failed|unknown_reconcile`, before/after version, `result_code` din
  vocabular închis. Fără DELETE pentru `bot_runtime` (receipts = dovezi; retenția e job admin).
  Zero PII pe toate trei; RLS `bot_runtime_tenant` ca la 040. Contract + politici de date:
  `docs/CART-DATA-READINESS.md`.

## `web_feedback` — voturile one-tap (NX-246 felia 2, migrarea 042)

Tabel nou (nu exista în schema_v2). NU s-a folosit `analytics_events`, deliberat: acolo scrierile
sunt append-only și fără unicitate, deci nu pot impune „un singur feedback ACTIV per prompt" și nu
pot exprima o corecție. Un retry de rețea ar produce două rânduri, iar `positive_feedback_rate` ar
număra același vot de două ori — exact semnalul pe care rollout-ul îl folosește ca să decidă.

- `UNIQUE (business_id, feedback_prompt_id)` = invariantul central ȘI ținta lui `ON CONFLICT` din
  `upsert_feedback`: idempotența e o proprietate a SCHEMEI, nu o secvență de apeluri.
- `feedback_prompt_id` e DERIVAT (HMAC peste `turn_id` + versiune de schemă), nu random — vezi
  `docs/WEB-FEEDBACK.md` §2: proiecția v2 e pură, iar un id random ar face ca „un vot per prompt"
  să devină „un vot per reîncărcare de pagină".
- `last_action_id` + `revision`: retry IDENTIC nu atinge rândul (clauza `where` din `do update`);
  o corecție autorizată incrementează `revision`, plafonat la 5 în cod (`MAX_FEEDBACK_REVISIONS`).
- `rating ∈ positive|negative` (vine din KIND-ul sigilat, nu de la client), `reason_code` din
  taxonomia VERSIONATĂ `taxonomy_version` (`feedback.v1`).
- ZERO text liber: nicio coloană de comentariu/IP/user-agent/token/identitate. Verificat de un test
  pe `information_schema.columns`, nu doar pe intenție.
- `turn_id` fără FK către `web_turns`: ledgerul are retenție proprie (168h), iar un vot nu are de ce
  să dispară odată cu fereastra de replay. Legătura cu persoana e prin conversație (cascade).
- Fără DELETE pentru `bot_runtime` — un vot e o dovadă; se corectează prin `revision`. RLS
  `bot_runtime_tenant` + `member read`, ca la 040/041. Contract: `docs/WEB-FEEDBACK.md`.
