# Nativx Assistant — context complet pentru Claude Code

## Ce e acest proiect
Platformă multi-tenant de AI Sales Assistant pentru ecommerce.
**Canalul de lucru ACUM: WEB WIDGET, exclusiv (NX-179).** WhatsApp/Telegram sunt ÎNGHEȚATE — cod
păstrat, zero investiție, nimic nu rulează pe ele. Orice task nou se măsoară pe web sau nu se face.
Nume comercial: **Nativx Assistant** (by Nativx Technology — nativxtech.com)
Clienți țintă: magazine ecommerce și retaileri din România (beauty, HVAC, auto, salon).
Model de business: agenție SaaS — setup fee + retainer lunar per client.
Referință de piață: similar cu iZi (eMAG) și Aura (SOLE), livrat ca serviciu managed.

---

## Stack tehnic

| Componentă | Tehnologie |
|---|---|
| Runtime | Python 3.12, asyncio |
| API | FastAPI (webhook + health) |
| Coadă | Redis Streams (lock per conversație, debounce) |
| DB | Postgres 16 — Supabase (**o singură schemă `public`**, multi-tenant pe `business_id`) |
| LLM sales | OpenAI GPT-5.4-mini |
| LLM triaj + simple | OpenAI GPT-5.4-nano |
| Embeddings | text-embedding-3-small (pgvector în Supabase) |
| **Web widget** | **SINGURUL canal de lucru (NX-179)** — `/web/chat` sincron + `/web/stream` SSE; widgetul e în repo FE separat (`docs/FRONTEND-CONTRACT-IZI.md`) |
| WhatsApp | Meta Cloud API direct (NU Twilio) — cod LIVE, dar **niciodată conectat** (0 conversații reale; lipsește phone_number_id, T013). **ÎNGHEȚAT** |
| Telegram | Bot API (long polling) — a fost canal de TEST. **ÎNGHEȚAT** (ultimul mesaj real: 2026-06-18). Poller OFF by default: `docker compose --profile telegram up` ca să-l repornești |
| Validare | Pydantic v2 |
| Teste | pytest + pytest-asyncio |

> **Schema DB: sursa de adevăr este [`docs/schema_v2_production.sql`](docs/schema_v2_production.sql)**
> (deja rulată + seedată). Pentru maparea numelor și deciziile de design vezi
> [`docs/schema_reference.md`](docs/schema_reference.md). Numele din acest fișier
> sunt cele REALE din schema_v2 (schemă plată, fără prefixe `core./conv./catalog.`).

---

## ⚠️ Direcția arhitecturală 2026 — Quality Overhaul (ratificat 2026-07-23)

**Sursa de adevăr a inițiativei: [`docs/QUALITY-OVERHAUL-2026.md`](docs/QUALITY-OVERHAUL-2026.md)**
(ADR APPROVED, deciziile D1-D15 + matricea de dispoziție a cardurilor + 13 faze cu gate-uri).

Arhitectura descrisă mai jos (pipeline liniar în 9 stagii) e **starea CURENTĂ, validă până la
gate-ul NX-210**. Direcția aprobată către care migrăm:

- **Creier unic (D1):** un singur agent principal (frontier) vede mesajul **BRUT** + istoric +
  profil. **Niciun model mic nu clasifică/rezumă mesajul înaintea lui** — triajul nano dispare
  de pe drumul sincron al conversației (rămâne shadow până la gate).
- **Fast path determinist (D2):** înaintea agentului doar COD; poate încheia turul singur DOAR
  pentru clasa „factual exact și sigur" (preț/stoc pe produs identificat exact, status comandă,
  FAQ high-confidence), cu **contract propriu + validator** (identitate/autorizare, evidence +
  version anti-stale, cache niciodată cross-tenant/cross-locale, P6). Orice dubiu → agent.
- **Control plane determinist în jur:** hard constraints inviolabile de model (D7),
  `UNKNOWN ≠ MISMATCH`, AnswerPlan cu evidence ÎNAINTEA textului (D8), validator determinist
  pentru fapte + critic semantic selectiv pentru afirmații.
- **Structura e adevărul (D4/D5):** faptele structurate = sursa; orice text AI
  (`search_document`, blurb) = artefact **derivat, versionat, regenerabil** — nescris de mână.
- **Pilot `ro-RO`, nucleu locale-aware (D3):** limba activă a pilotului e româna, dar
  `business_id` / `locale` / `domain_pack` / `schema_version` / `document_version` rămân în TOATE
  contractele și artefactele. **Nu hardcoda română nicăieri** — vezi și principiul 11.
- **`business_id` e SERVER-OWNED:** injectat server-side, **niciodată** din output-ul modelului
  și niciodată parametru controlabil de LLM.
- **Nicio schimbare mare pe speranță (D15):** model, embeddings, reranker, framework — toate se
  decid pe măsurători (golden set + retrieval benchmark), nu pe intuiție.

**Înghețate până la GO-ul de la NX-210:** enforcement-ul QuerySpec/Match Gate (NX-188, NX-189).

---

## Arhitectura — pipeline liniar (9 stagii)

Fiecare mesaj inbound parcurge stagiile în ordine fixă.
Un singur obiect `TurnContext` curge prin toate stagiile.
Orice stagiu poate seta `reply` → early exit direct la Sender (stagiul 9).

```
[1] WEBHOOK SVC  (implementat: src/webhook/ — subțire, FĂRĂ DB)
    • validare semnătură Meta X-Hub-Signature-256 peste corpul BRUT (signature.py)
    • dedupe LAYER 1 (NX-51): Redis SET NX EX pe (phone_number_id, wamid).
      NB: unique-ul de pe messages include cheia de partiționare (created_at) →
      retry-ul Meta vine cu alt created_at, ON CONFLICT nu prinde. De aceea
      dedupe-ul e în 2 straturi, NU pe messages.
    • push pe stream-ul Redis unic `inbound` (conversation_id nu e cunoscut
      la webhook fără round-trip în DB; ordinea per conversație = în worker)
    • ACK 200 în < 50ms (Meta face retry agresiv la timeout)
    • update conversations.last_inbound_at s-a mutat în worker (processor)

[2] REDIS BACKBONE + WORKER  (implementat: redis_bus.py, worker/consumer.py + processor.py)
    • stream unic `inbound` + consumer group `workers` (XREADGROUP + ACK)
    • worker: resolve phone_number_id → business (admin_conn, control plane)
      → tenant_conn → dedupe LAYER 2 durabil (inbound_dedupe, claim ÎNAINTE
      de orice scriere — prinde retry scăpat de Redis după restart/FLUSHALL)
      → contact/conversație → last_inbound_at → pipeline
    • TODO: lock per conversație (multi-consumer), debounce adaptiv 2-3s,
      rate limit per user + abuse blocklist (contacts.is_blocked),
      cost guard zilnic per business (contor Redis; sursa de adevăr
      pentru facturare = usage_daily, rollup nocturn), XAUTOCLAIM

[3] GATES (cod pur, fără LLM)
    • bot_active check (conversations.bot_active) → early exit cu handoff dacă false
    • handoff_until check → dacă în viitor, tăcere (om preia)
    • risc detection (pattern-uri) → request_human dacă necesar — DOAR pe canale cu
      handoff activ (config.handoff_enabled_channels). Web exclus by default: fără
      operator → nu escaladăm/nu tăcem, mesajul curge normal (botul asistă singur)
    • media routing: vocale → STT (Whisper), poze → Vision (match catalog)
    • language detect → RO / HU / EN (setează ctx.language; TOATE
      lookup-urile în faqs / semantic_cache / wa_templates includ locale)
    • identity resolution: lookup în channel_identities →
      același user pe 2 canale = un singur contact

[4] STRATURI GRATUITE (fără LLM, țintă 40-60% din trafic opresc aici)
    • alias lookup: phrase_norm(text) → match în intent_aliases
      (status='approved', filtrat pe business_id)
    • cache semantic: embedding → cosine search în semantic_cache
      (filtrat pe business_id + locale)
    • clarificare: dacă state are pending_question → formulare din cod/prompt
    • oricare produce reply → early exit la Sender

[5] TRIAJ (GPT-5.4-nano, ~300 tokens input)
    • clasificare: simple | sales | order | handoff | clarify
    • output JSON validat cu Pydantic: {route, category_key, filters, missing_field}
    • category_key validat contra categories (dacă inventează → CLARIFY)
    • «simple»: nano compune și răspunsul → early exit la Sender
    • incertitudinea = CLARIFY, NU recovery agent

[6] CONTEXT BUILDER (buget impus în cod)
    • istoric: max 8 mesaje (cele mai recente)
    • state: max 8KB (impus în cod + CHECK pe conversations.state din 003)
    • profil client compact din contacts.profile
    • summarizer conversații lungi (> 20 mesaje → conversation_summaries + ultimele 8)
    • prefix static byte-identic → prompt caching OpenAI (75-90% discount)

[7] AGENT (GPT-5.4-mini)
    • system prompt GENERAT din categories (+ intent_aliases pt rutare), nu hardcodat
    • buying stages framework: browsing → narrowing → comparing → ready_to_buy
    • AGENT decide mutarea de vânzare (NU routerul)
    • MAX 3 tool calls per tur (limită dură în cod)
    • tool results: max 6 produse × 8 câmpuri (nu obiecte complete)
    • P0-safety CONTRAINDICAȚII (NX-173, src/safety/) — UN SINGUR punct de decizie:
      `SafetyPolicy.for_turn(ctx).evaluate(products, purpose)` → `Decision` tipizat. Context
      (sarcină/alăptare) detectat DETERMINIST + PERSISTAT în state.safety (istoricul de 8 e
      prea scurt); registru CURAT cu provenance + reviewed_by (db/seed/safety_rules.json),
      validat STRICT și FAIL-CLOSED (poartă de boot; registru stricat + context activ ⇒ nu se
      expune nimic). Chemat de TOATE căile: search/page/details/compare, link+compare intent,
      cross-sell/superlativ/cheaper/rehidratare, enforcement final pe ctx.retrieval, backstop
      în ToolRun. MUTAȚIILE (cart/checkout/back-in-stock) cer `policy.allows()` ÎNAINTE de
      scriere — un filtru de rezultat nu poate anula un rând scris. Cache-ul (stagiul 4) face
      BYPASS pe context de siguranță (citire + scriere): un hit ar sări peste tot gate-ul.
      DRUMURILE DIN AFARA PIPELINE-ului au poarta lor (n-au TurnContext → `SafetyPolicy
      .from_state`): caruselul (worker/callback.py, ◀/▶ e inbound NON-LLM) și PROACTIVUL
      (back_in_stock/abandoned_cart — un job vechi ar promova produsul zile mai târziu;
      awb_update/follow_up NU se gate-uiesc, sunt tranzacționale).
      COMPUNERE: codul garantează O SINGURĂ frază localizată (recunoaștere + medic/farmacist),
      în runner, idempotent (src/safety/compose.py + messages.py); modelul scrie doar partea
      comercială. Nicio inferență LLM nu devine contraindicație; zero sfat medical.
      Kill-switch: safety_contraindications_enabled.

[8] VALIDATOR (cod pur)
    • fiecare preț din reply există în ctx.retrieval
    • fiecare produs menționat există în ctx.retrieval
    • linkurile sunt din catalog (products.product_url, nu inventate)
    • P0-safety: niciun claim MEDICAL/terapeutic (tratează afecțiuni / sigur în sarcină /
      fără alergeni / recomandat de medic) — proză: invalid→retry→fallback; bogată: scrub→DROP
      (has_medical_claim, kill-switch safety_medical_guardrail_enabled). Răspundere juridică.
    • invalid → 1 retry cu feedback → formulare fără cifre
    • ZERO prețuri inventate structural

[9] SENDER (singurul punct de ieșire din sistem)
    • typing indicator trimis instant la primire (Meta API)
    • răspuns spart în 2 mesaje scurte dacă > 200 caractere
    • scriere tranzacțională în aceeași TX: reply în outbox +
      patch conversations.state (cu state_version) + insert messages
    • dispatcher separat citește outbox → trimite la Meta →
      salvează provider_msg_id pe messages → retry cu backoff la fail
    • statusurile delivered/read/failed (webhook status) intră în
      message_status_events → update messages.status pe provider_msg_id
    • POST-TUR async (nu blochează): extractor profil nano + lead_score update

PROACTIV (în afara pipeline-ului, scheduler separat — proactive_jobs)
    • AWB la expediere (shipments) · back-in-stock · follow-up coș abandonat
    • verifică opt-in: contacts.consent
    • verifică 24h window: in_24h_window(conversation) →
      mesaj normal; altfel → DOAR template cu status='approved'
      din wa_templates
```

---

## Canale (multi-channel) — cuplajul stă DOAR la margini

Pipeline-ul (stagiile 3-9) și worker-ul sunt **agnostice de canal**: operează pe
`TurnContext` (contact, conversație, mesaj, reply). Cuplajul de canal trăiește la
exact DOUĂ margini, izolat prin contracte (NX-60):

- **Ingestie** (stagiul 1): fiecare canal are parser-ul + verificarea lui →
  produc un **envelope NEUTRU** pe stream-ul unic `inbound`:
  `channel_kind`, `channel_account_id` (id-ul canalului RECEPTOR — phone_number_id
  la WhatsApp, bot id la Telegram), `sender_external_id` (id-ul userului — wa_id /
  chat.id), `provider_msg_id`, `body`, ... Worker-ul rezolvă tenantul cu
  `resolve_channel(channel_kind, channel_account_id)` și nu mai știe de canal.
- **Trimitere** (stagiul 9): `outbox` e singurul punct de ieșire; un **registru
  `ChannelSender`** mapează `channel_kind → client`. Dispatcher-ul alege clientul
  după `channel_kind` (zero logică de coadă duplicată).

Canale — **NX-179: se lucrează DOAR pe web widget.**
- **WEB WIDGET (`webchat`)** — **singurul canal activ și singurul pe care se lucrează.**
  `POST /web/chat` (sincron, request/response — reply-ul se mapează direct în HTTP, fără
  outbox/dispatcher, prin `render_web`) + `GET /web/stream` (SSE) + `POST /web/messages` +
  `GET /web/bootstrap` (`src/web/app.py`). Widgetul propriu-zis trăiește într-un **repo FE
  separat**; backendul emite DOAR JSON — [`docs/FRONTEND-CONTRACT-IZI.md`](docs/FRONTEND-CONTRACT-IZI.md).
  Fără fereastră 24h, fără template-uri. Handoff dezactivat by default (fără operator). Identitate:
  anonim by default; login passthrough JWT în spatele `WEB_IDENTITY_ENABLED` (NX-128/129/130).
  Audit conversațional pe calea reală: `scripts/sim/web_audit.py`.
- **WhatsApp** — Meta Cloud API, webhook semnat. Codul e LIVE și testat, dar canalul **n-a fost
  niciodată conectat** (0 conversații reale; lipsește `phone_number_id` — T013). **ÎNGHEȚAT.**
  Fereastră 24h + template-uri (proactiv) — relevant doar când se reia.
- **Telegram** — Bot API prin long polling. A fost canal de TEST pe VPS fără HTTPS.
  **ÎNGHEȚAT** (17 conversații, ultimul mesaj 2026-06-18). Poller OFF by default în ambele
  compose-uri (`profiles: ["telegram"]`) → `docker compose --profile telegram up` ca să-l repornești.

**De ce rămâne codul de canal:** abstracția (NX-60) NU e o dependență de Telegram/WhatsApp — e
motivul pentru care pipeline-ul (stagiile 3-9) e agnostic. A o scoate ar cupla engine-ul la web și
ar arunca seam-ul care face WhatsApp posibil pentru clienții români (modelul de business). Îngheț ≠
ștergere: nu se investește, nu rulează, dar nici nu blochează.

---

## TurnContext — contractul central

```python
@dataclass
class TurnContext:
    turn_id: str                        # uuid generat la intrare în pipeline
    business: BusinessConfig            # citit din businesses
    contact: Contact                    # citit din contacts (+ channel_identities)
    message: InboundMessage             # body, content_type, provider_msg_id
    history: list[Message]              # max 8, cel mai recent ultimul
    state: ConversationState            # conversations.state jsonb, max 8KB
    language: str                       # 'ro' | 'hu' | 'en' (setat în Gates; DB: locale)
    route: RouteDecision | None         # scris DOAR de stagiul Triaj
    retrieval: RetrievalResult | None   # scris DOAR de stagiul Retrieval
    reply: Reply | None                 # orice stagiu poate seta → early exit
    events: list[Event]                 # acumulat pentru analytics
```

**Regula absolută**: fiecare câmp are exact un stagiu care îl scrie.
Dacă două stagii vor să scrie același câmp, arhitectura e greșită.

---

## Schema DB — o singură schemă `public`, tenant pe `business_id`

**Sursa de adevăr: [`docs/schema_v2_production.sql`](docs/schema_v2_production.sql)**
(829 linii, validată Postgres 16 / Supabase, deja seedată).
**Mapare nume vechi → real + decizii: [`docs/schema_reference.md`](docs/schema_reference.md).**

Convenții generale:
- TOATE tabelele tenant-scoped au `business_id` NOT NULL + index compus.
- Idempotență: unique pe `(business_id, external/provider id)`.
- Hot tables (`messages`, `analytics_events`) sunt **partiționate pe lună**.
- PII (telefon E.164 / id canal) trăiește DOAR în `channel_identities`.

### Tenants și canale
```
businesses        — id, slug, name, vertical, status, default_locale,
                    supported_locales[], timezone, settings jsonb,
                    daily_cost_cap_usd
business_users    — business_id, user_id (auth.users), role  (dashboard)
channels          — id, business_id, kind(whatsapp|telegram|...),
                    provider_account_id, credentials_ref (secret manager, NU secrete în DB)
wa_templates      — id, business_id, channel_id, name, language, category,
                    version, body, variables jsonb, status(draft|submitted|
                    approved|rejected|paused|deprecated), provider_template_id
                    • proactivul în afara ferestrei 24h folosește DOAR status='approved'
```

### Contacts & identitate
```
contacts          — id, business_id, display_name, locale, profile jsonb,
                    lead_score, lifecycle, consent jsonb, is_blocked,
                    erased_at (GDPR: anonimizat, nu șters)
channel_identities— id, business_id, contact_id, channel_kind, external_id,
                    external_id_hash (generated, sha256), UNIQUE(business_id,
                    channel_kind, external_id)
                    • PII-ul de canal stă DOAR aici; identity resolution = lookup aici
```

### Conversații & mesaje (hot path)
```
conversations     — id, business_id, contact_id, channel_id, status,
                    bot_active, handoff_until, last_inbound_at (24h window),
                    last_outbound_at, locale, state jsonb (≤8KB), state_version
                    (optimistic lock), risk_flags[], shadow_mode
                    • in_24h_window(conv) = funcție SQL (derivat, nu flag stocat)
                    • state = ref-uri (displayed_products: {id,name,price}), NU obiecte
conversation_summaries — id, business_id, conversation_id, upto_message_at, summary
messages [PARTIȚIONAT] — id, business_id, conversation_id, contact_id,
                    direction(inbound|outbound|internal), author(contact|bot|
                    human_agent|system), provider_msg_id, content_type, body,
                    payload jsonb, media_ref, status, model_route, tokens_in/out,
                    cost_usd, latency_ms
                    • unique(business_id, provider_msg_id, created_at) = doar consistență;
                      dedupe-ul REAL la retry e inbound_dedupe (vezi mai jos, NX-51)
                    • textul e `body`, rolul e `direction`+`author` (NU `role`/`content`)
inbound_dedupe    — business_id + provider_msg_id (PK compus), first_seen
                    • NE-partiționat → ON CONFLICT funcționează; claim în worker
                      înainte de orice scriere; purjă >48h (jobs/cleanup_dedupe)
                    • migrare: docs/004_inbound_dedupe.sql (aplicată live)
message_status_events — provider_msg_id, status, occurred_at  (delivered/read/failed)
outbox            — id, business_id, conversation_id, idempotency_key UNIQUE,
                    kind, payload jsonb, status(pending|dispatching|sent|failed|dead),
                    attempts, next_attempt_at, last_error
                    • Sender scrie aici tranzacțional; dispatcherul trimite
```

### Catalog (read-only pentru bot, scris de sync)
```
products          — id, business_id, brand_id, primary_category_id, external_id,
                    name, slug, ai_summary, price, sale_price, availability,
                    stock_total, rating, status, attributes jsonb, product_url
                    • search hibrid: filtre SQL pe products + ORDER BY embedding <=>
product_embeddings— product_id PK, business_id, model, embedding vector(1536),
                    content_hash  • HNSW cosine; re-embed DOAR la content_hash diferit
product_variants  — id, business_id, product_id, label, sku, price, sale_price, stock
product_review_summaries — product_id PK, business_id, summary, sentiment,
                    top_pros[], top_cons[]  • job offline; citit de get_product_details
brands, categories — tenant-scoped; categories are parent_id + path
reviews, product_images, product_sections, ingredients, product_ingredients,
product_badges, product_category_map — detalii produs
catalog_sync_runs, catalog_quality_alerts — ingestion monitor („alertă, nu publicare")
```

### Knowledge (straturile gratuite 40-60%)
```
faqs              — id, business_id, question, answer, locale, embedding vector(1536)
                    • lookup ÎNTOTDEAUNA: business_id + locale + cosine
intent_aliases    — id, business_id, phrase_norm, target_kind(faq|product|category|
                    route), target_id, status(candidate|approved|rejected)
                    • lookup pe status='approved'; candidates din shadow mode
semantic_cache    — id, business_id, locale, query_norm, embedding vector(1536),
                    answer, hit_count, expires_at
                    • lookup ÎNTOTDEAUNA: business_id + locale + cosine
```

### Comerț & atribuire (bucla de bani)
```
checkout_links    — id, business_id, conversation_id, contact_id, ref_code UNIQUE,
                    cart jsonb, url, clicked_at, converted_order_id, expires_at
                    • checkout_link(ref=...) scrie aici; webhook comenzi face match pe ref_code
orders            — id, business_id, contact_id, external_id, status, total,
                    attributed_checkout_link_id, attribution(none|assisted|direct_bot)
                    • PII: NU are customer_phone — telefonul vine din channel_identities
order_items, shipments (AWB → proactiv)
back_in_stock_subscriptions — UNIQUE(business_id, contact_id, product_id, variant_id)
proactive_jobs    — kind(awb_update|back_in_stock|abandoned_cart|follow_up),
                    scheduled_at, status, template_id
appointments      — business_id, contact_id, service_name, starts_at, ends_at,
                    status, external_ref (Google Calendar)
```

### Analytics (append-only — botul are doar INSERT)
```
analytics_events [PARTIȚIONAT] — business_id, conversation_id, event_type,
                    properties jsonb, tokens_in/out, cost_usd, turn_id (NX-122)
                    • model generic: intent_detected/route/tool_call/cache_hit/handoff...
                    • turn_id: corelare per-tur (emit() îl injectează; replay traiectorie)
usage_daily       — business_id, day PK, conversations, messages_in/out,
                    templates_sent, tokens, cost_usd, cache_hits, handoffs,
                    orders_attributed, revenue_attributed, intents jsonb
                    • rollup nocturn; dashboard-ul și facturarea citesc DOAR de aici
conversation_evals, golden_tests — LLM-as-judge + gate CI
```

### GDPR & audit
```
gdpr_requests     — id, business_id, contact_id, kind(erase|export|access), status
audit_log         — business_id, actor, action, entity, entity_id, details jsonb
funcția gdpr_erase_contact(contact_id):   (security definer, în schema_v2)
    • contacts: display_name=NULL, profile='{}', rfm=NULL, erased_at=now()
    • channel_identities: DELETE (telefonul dispare)
    • messages: body=NULL, payload='{}', media_ref=NULL (păstrezi structura pt analytics)
    • audit_log: insert
Retenție: partiții vechi messages/analytics_events → drop partition (job pg_cron).
```

---

## Tool-uri agentului (cod determinist, activate per business)

```python
# toate tool-urile au semnătura: async def tool(ctx: TurnContext, **params) -> ToolResult
# MAX 3 apeluri per tur — limitat în agent runner

search_products(category, filters, budget_max, concerns, suitable_for, limit=6)
  # filtre SQL dure (categories + attributes) + ranking semantic (product_embeddings) + reranker
  # returnează max 6 produse × 8 câmpuri: id, name, brand, price, product_url, ai_summary, stock, variant

get_product_details(product_id)
  # detalii complete + review summary din product_review_summaries

compare_products(product_ids: list[str])
  # diferențe structurate între 2-3 produse (tabel pros/cons)

check_order(order_number_or_contact)
  # status + tracking din orders + shipments

delivery_eta(product_id, address)
  # ETA din integrarea cu curier/ERP

reorder(contact_id)
  # ultimele comenzi ale contactului → sugestie reorder

cart_add(product_id, variant_id)
checkout_link(cart_items, ref=turn_id)
  # scrie checkout_links (ref_code) → link cu ?ref= pentru atribuire conversie

subscribe_back_in_stock(product_id, variant_id)
  # insert în back_in_stock_subscriptions; proactivul notifică la restock

faq_lookup(query)
  # căutare în faqs (filtrat pe ctx.language → faqs.locale)

book_appointment(service_name, preferred_datetime, contact_info)
  # creare în appointments + Google Calendar sync

request_human(reason)
  # setează conversations.handoff_until, notifică operatorul
```

---

## Roluri DB și securitate

Schema_v2 are **RLS enabled pe toate tabelele** + politici dashboard
(`auth.uid()` → membership în `business_users`). Workerii NU folosesc
`service_role` (ar fi bypass RLS total). Plasa de izolare pentru worker se
adaugă în [`docs/003_bot_runtime_role.sql`](docs/003_bot_runtime_role.sql):

```
bot_runtime  (rolul cu care se conectează workerul aplicației — FĂRĂ bypassrls)
   — SELECT pe catalog (products, variants, embeddings, faqs, ...)
   — INSERT/UPDATE semantic_cache, intent_aliases (candidates)
   — SELECT/INSERT/UPDATE/DELETE pe runtime (contacts, conversations, messages,
     outbox, orders, ...)
   — INSERT analytics_events (append-only); SELECT/INSERT/UPDATE usage_daily (rollup)
   — politici RLS: business_id = current_business_id()  (din SET app.business_id)

service_role — DOAR migrări + joburi admin (bypass RLS). NU pentru worker.
gdpr_svc     — EXECUTE gdpr_erase_contact + export + audit_log (security definer)
```

**Izolarea multi-tenant primară: `WHERE business_id = $1` în cod, FĂRĂ excepție.**
**Defense-in-depth:** workerul se conectează pe tenant path cu rolul de **LOGIN**
`bot_runtime` (NX-50, pool dedicat `bot_pool`); `tenant_conn` setează DOAR
`app.business_id` per checkout — fără `SET ROLE` (care se scurgea sub
multiplexarea poolerului). Politicile RLS pe `bot_runtime` transformă un query
greșit în „zero rezultate", nu „datele altui client". `bot_runtime` NU are
bypassrls. Control plane-ul (`admin_conn`) rulează pe un pool privilegiat separat.
Detalii: `docs/db_connections.md`.

**Excepție unică, documentată — `admin_conn` (control plane):** lookup-ul
`phone_number_id → business_id` (db/queries/channels.py) rulează ÎNAINTE ca
tenantul să fie cunoscut — e operația care îl derivă. Suprafața e limitată la
maparea canal→business + mentenanță non-PII (cleanup inbound_dedupe). Orice
alt query pe admin_conn = bug de izolare.

---

## Principii — respectă-le în tot codul

1. **Pipeline liniar** — niciun stagiu nu sare înapoi, niciun loop de orchestrare
2. **LLM doar la 2 puncte** — triaj (nano) și agent (mini). Tot restul: cod determinist
3. **Un singur proprietar per câmp** — dacă două funcții scriu același câmp din TurnContext, e o greșeală de design
4. **Buget de context impus în cod** — nu în prompturi, nu prin disciplină, în cod (state 8KB tăiat de context builder; CHECK în DB ca plasă)
5. **Un singur punct de ieșire** — Sender → outbox → dispatcher. Orice alt loc care trimite mesaje e o greșeală
6. **Niciodată tăcere** — degradare: mini → retry → nano → template → om notificat
7. **business_id pe tot, SERVER-OWNED** — niciun query fără `WHERE business_id = $1`; RLS (`bot_runtime` + `app.business_id`) ca plasă, nu ca mecanism primar. `business_id` se injectează server-side: **niciodată** din output-ul modelului, niciodată parametru de tool controlabil de LLM
8. **State = ref-uri, nu obiecte** — în displayed_products: {product_id, name, price}, NU obiectul complet
9. **Promptul se generează din DB** — system prompt din `categories` (+ `intent_aliases`), nu hardcodat. (Un tabel `taxonomy` bogat se adaugă aditiv DOAR când verticalul cere filtre pe concerns — vezi schema_reference.)
10. **Observabilitate din runner** — stagiile nu știu că sunt măsurate; runner-ul scrie event-ul
11. **Limba e parte din cheie** — orice lookup în faqs / semantic_cache / wa_templates include locale. Un cache hit în limba greșită e un bug, nu un hit. **Pilotul e `ro-RO`, dar nucleul rămâne locale-aware (D3): nu hardcoda română** — limba activă e configurație, nu constantă
12. **PII trăiește într-un loc** — `channel_identities` (telefon E.164 / id canal, + hash). Nicăieri altundeva. Logurile nu conțin telefoane (redaction în logger)

---

## Structura proiectului

```
nativx-assistant/
├── CLAUDE.md                    ← acest fișier
├── TODO-MANUAL.md               ← taskurile manuale ale lui Adi (conturi/setup extern)
├── docs/
│   ├── schema_v2_production.sql ← SURSA DE ADEVĂR a schemei (Postgres 16, seedată)
│   ├── schema_reference.md      ← mapare nume vechi → real + decizii de design
│   ├── 003_bot_runtime_role.sql ← rol bot_runtime + RLS (app.business_id) + guard 8KB
│   ├── 004_inbound_dedupe.sql   ← NX-51 layer 2 (aplicat live)
│   ├── 0NN_*.sql                ← migrări delta (003→014), aplicate ORDONAT de scripts/migrate.py
│   ├── 014_schema_migrations.sql← NX-123: tabel tracking migrări + backfill 003–013 (legacy)
│   ├── PROJECT_STATUS.md        ← starea proiectului (actualizat la fiecare milestone)
│   ├── DB_MIGRATION_NOTES.md    ← note migrare v1 → v2 + runner migrate.py (NX-123)
│   ├── FRONTEND-CONTRACT-IZI.md ← contractul JSON web (carduri+comparison) pt randarea FE (paritate iZi)
│   └── *audit*                  ← audit CTO (pdf), plan v2 (xlsx), diagramă v4 (drawio)
├── tasks/                       ← cardurile de task (TXXX.md, NX-XX.md) + backlog compact
├── scripts/                     ← migrate.py (runner ordonat + poartă boot, NX-123); apply_0NN.py
│                                  DEPRECATE (istoric); db_check.py, spot_check.py
├── db/
│   └── seed/                    ← seed.ts + embed.ts (Supabase JS client, tsx)
├── src/
│   ├── config.py                ← settings (Pydantic BaseSettings)
│   ├── models.py                ← TurnContext + toate dataclass-urile
│   ├── redis_bus.py             ← client Redis + dedupe layer 1 + XADD inbound
│   ├── db/
│   │   ├── connection.py        ← pool asyncpg, tenant_conn (RLS) + admin_conn (control plane)
│   │   └── queries/             ← SQL per domeniu (contacts, conversations, messages,
│   │                              outbox, inbound_dedupe, catalog, channels, businesses)
│   ├── webhook/
│   │   ├── app.py               ← FastAPI: GET verify + POST inbound (ambele LIVE)
│   │   ├── signature.py         ← verificare X-Hub-Signature-256 (corp brut)
│   │   ├── meta.py              ← parser payload Meta → InboundEvent
│   │   ├── status.py            ← LIVE: delivered/read/failed → messages.status (#26)
│   │   └── orders.py            ← TODO: webhook comenzi → match ref_code → atribuire
│   ├── worker/
│   │   ├── consumer.py          ← consumer group Redis (XREADGROUP + ACK) + entrypoint __main__
│   │   ├── processor.py         ← handle_turn: dedupe L2 → contact/conv → pipeline → outbox (+log per-tur)
│   │   ├── runner.py            ← pipeline runner (stagii în ordine, early-exit, măsoară)
│   │   ├── dispatcher.py        ← LIVE: outbox → ChannelSender (Meta/Telegram), retry idempotent
│   │   ├── context.py           ← stagiul 6: istoric conversație bugetat (triaj+agent)
│   │   └── stages/             ← triage.py (nano) ✅ + agent.py (mini, RAG+validator) ✅;
│   │                             TODO: gates, free_layers; echo=fallback
│   ├── channels/                ← abstracția de canal (NX-60+); cuplajul de transport
│   │   ├── base.py              ← ChannelSender Protocol + Capability matrix (NX-115) + registry
│   │   └── telegram/            ← client.py (Bot API) + poller.py (long polling, TEST)
│   ├── meta_client.py           ← MetaClient (WhatsApp Cloud API send); implementează ChannelSender
│   ├── tools/                   ← search_products, get_product_details, ... (vezi mai sus)
│   ├── domain/                  ← NX-114: DomainPack (config per-vertical din DB+seed)
│   │   ├── pack.py + loader.py + normalize.py + defaults/*.json (ecommerce/beauty_salon/...)
│   ├── agent/
│   │   ├── prompt_builder.py    ← system prompt generat din categories
│   │   └── tool_definitions.py  ← OpenAI tool schemas
│   ├── proactive/
│   │   ├── scheduler.py         ← proactive_jobs → outbox (motor NX-70; calea template LIVE, PR #142)
│   │   ├── initiators.py        ← PL-1: sweeper-e care CREEAZĂ proactive_jobs (coș abandonat +
│   │   │                          back-in-stock) + seam-uri awb/follow_up; rulate de jobs/scheduler
│   │   ├── builders.py          ← text per kind (free_text + template_name + variables)
│   │   └── templates.py         ← wa_templates + 24h window + consent check (poartă NX-71)
│   ├── safety/                  ← NX-173 (P0): gate-uri DETERMINISTE, în afara deciziei de model
│   │   └── contraindications.py ← context (sarcină/alăptare) × registru curat → excludere dură
│   ├── gdpr/
│   │   └── erase.py             ← gdpr_erase_contact + export
│   ├── evals/                   ← G8-1: harness golden (regresii de pipeline)
│   │   └── golden.py            ← checker pur (evaluate_reply) + run_case (pipeline real, LLM scriptat) + load_cases
│   └── jobs/
│       ├── cleanup_dedupe.py    ← purjă inbound_dedupe >48h (admin_conn, zilnic)
│       ├── lifecycle.py         ← Val3: scrie contacts.lifecycle nocturn (new/engaged/customer/repeat/churn_risk)
│       ├── rollup_usage.py      ← TODO: nocturn: analytics_events → usage_daily
│       ├── embed_products.py    ← TODO: ai_summary → product_embeddings (content_hash)
│       └── cleanup.py           ← TODO: drop partiții vechi, expire semantic_cache
├── tests/
│   ├── golden/                  ← cazuri golden (cases.json) + fixture-uri de conversație
│   ├── test_golden.py           ← G8-1: gate CI (ScriptedLLM + stub-uri DB, zero OpenAI/DB real)
│   ├── test_pipeline.py
│   ├── test_tools.py
│   ├── test_validator.py
│   └── test_tenant_isolation.py ← fiecare query refuză date cu alt business_id
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Client demo activ

**business_id**: `6098812a-50fc-44bd-a1ba-bc77e6399158`
**Slug**: `nativex-demo` (name „Sole Demo")
**Vertical**: `beauty`
**Date reale în Supabase** (re-verificat 2026-07-17, NX-177): **654 produse în total, din care doar
150 `status='active'`** — restul de 504 sunt seed-ul vechi templatat, ARHIVAT. Catalogul SERVIT =
cele 150 hand-curate v3 (NX-168e). Nu asertați numere fixe în teste: catalogul crește (testele
cuplate la „500" au picat la 654 și au fost raportate ca regresie — vezi tasks/NX-177.md).
- variante: 46/150 active au variante → prețul afișat = min-variantă DOAR pentru ele, altfel
  `products.price` (contract condiționat);
- ⚠️ **78/150 active (52%) au diacritice în nume**, `unaccent` NU e instalat, iar FTS rulează pe
  config `english` → căutarea lexicală e diacritic-SENSITIVE („sampon" → 0 rezultate, „șampon" →
  5). Impact real pe RO. Vezi **tasks/NX-178.md**.
- `faqs` = 32 (RO seedate); ⚠️ 2 duplicate + typo — vezi tasks/NX-175.md.
Datele de simulare (`sim:*`, din `scripts/sim/server.py`) se curăță cu
`scripts/sim/cleanup.py` (dry-run default, `--apply` ca să șteargă).
**Canale** (re-verificat pe DB live 2026-07-17 — NX-179): **webchat = ACTIV** (64 conversații,
ultimul mesaj 2026-07-14) → SINGURUL pe care se lucrează. Telegram ÎNGHEȚAT (17 conv, ultimul
2026-06-18; poller OFF: `profiles: ["telegram"]`). WhatsApp ÎNGHEȚAT (0 conversații reale; canalul
din DB e `SIM-DRIVER`, harness-ul de test). Testele integration își creează channel throwaway
(tranzacție rollback-uită).

Folosește acest `business_id` pentru toate testele locale.

---

## Ce NU facem

- NU n8n pentru miezul sistemului (ok pentru cron-uri și alerte periferice)
- NU LLM pentru filtrare sau routing determinist
- NU obiecte de produs complete în state — doar ID-uri + snapshot mic
- NU categorii/aliase hardcodate în prompturi — vin din `categories` / `intent_aliases`
- NU recovery agent pentru cazuri ambigue — CLARIFY ieftin
- NU scriere în catalog din worker (excepție: `semantic_cache` și `intent_aliases` candidates)
- NU tăcere la erori — întotdeauna ceva iese spre client
- NU trimitere directă la Meta/Telegram din stagii — totul prin `outbox` + dispatcher (ChannelSender)
- NU cod specific de canal în pipeline/worker — doar la margini (parser ingestie + ChannelSender)
- NU mesaje proactive fără consent + (24h window SAU template approved)
- NU telefoane/PII în loguri sau în analytics — doar în `channel_identities`
- NU `service_role` în worker — workerul folosește `bot_runtime` (RLS activ)
```
