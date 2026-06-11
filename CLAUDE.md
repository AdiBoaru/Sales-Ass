# Nativx Assistant — context complet pentru Claude Code

## Ce e acest proiect
Platformă multi-tenant de AI Sales Assistant pe WhatsApp.
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
| DB | Postgres — Supabase (4 scheme: core, catalog, conv, analytics) |
| LLM sales | OpenAI GPT-5.4-mini |
| LLM triaj + simple | OpenAI GPT-5.4-nano |
| Embeddings | text-embedding-3-small (pgvector în Supabase) |
| WhatsApp | Meta Cloud API direct (NU Twilio) |
| Validare | Pydantic v2 |
| Teste | pytest + pytest-asyncio |

---

## Arhitectura — pipeline liniar (9 stagii)

Fiecare mesaj inbound parcurge stagiile în ordine fixă.
Un singur obiect `TurnContext` curge prin toate stagiile.
Orice stagiu poate seta `reply` → early exit direct la Sender (stagiul 9).

```
[1] WEBHOOK SVC
    • validare semnătură Meta X-Hub-Signature-256
    • dedupe pe provider_message_id (conv.inbound_dedupe)
    • update conv.contacts.last_inbound_at (alimentează 24h window)
    • insert mesaj brut în Redis stream: inbound:{conversation_id}
    • ACK 200 în < 50ms (Meta face retry agresiv la timeout)

[2] REDIS BACKBONE
    • stream per conversație (FIFO garantat)
    • lock per conversație (procesare serializată, zero race condition)
    • debounce adaptiv 2-3s (lot de mesaje, nu string lipit)
    • rate limit per user + abuse blocklist
    • cost guard zilnic per business (contor Redis; sursa de adevăr
      pentru facturare = analytics.usage_daily, rollup nocturn)

[3] GATES (cod pur, fără LLM)
    • bot_active check → early exit cu handoff dacă false
    • handoff_until check → dacă în viitor, tăcere (om preia)
    • risc detection (pattern-uri) → request_human dacă necesar
    • media routing: vocale → STT (Whisper), poze → Vision (match catalog)
    • language detect → RO / HU / EN (setează ctx.language; TOATE
      lookup-urile în FAQ / cache / templates includ language)
    • identity resolution: lookup în conv.channel_identities →
      același user pe 2 canale = un singur conv.contacts

[4] STRATURI GRATUITE (fără LLM, țintă 40-60% din trafic opresc aici)
    • FAQ alias lookup: f_normalize(text) → match în catalog.faq_aliases
      (filtrat pe language)
    • cache semantic: embedding → cosine search în catalog.response_cache
      (filtrat pe business_id + language)
    • clarificare template: dacă state are pending_question → template din DB
      (catalog.clarification_templates, filtrat pe language)
    • oricare din cele 3 produce reply → early exit la Sender

[5] TRIAJ (GPT-5.4-nano, ~300 tokens input)
    • clasificare: simple | sales | order | handoff | clarify
    • output JSON validat cu Pydantic: {route, category_key, filters, missing_field}
    • category_key validat contra catalog.taxonomy (dacă inventează → CLARIFY)
    • «simple»: nano compune și răspunsul → early exit la Sender
    • incertitudinea = CLARIFY cu template, NU recovery agent

[6] CONTEXT BUILDER (buget impus în cod)
    • istoric: max 8 mesaje (cele mai recente)
    • state: max 8KB (garantat de CHECK constraint în DB)
    • profil client compact din conv.contacts.profile
    • summarizer conversații lungi (> 20 mesaje → rezumat + ultimele 8)
    • prefix static byte-identic → prompt caching OpenAI (75-90% discount)

[7] AGENT (GPT-5.4-mini)
    • system prompt GENERAT din catalog.taxonomy (nu hardcodat)
    • buying stages framework: browsing → narrowing → comparing → ready_to_buy
    • AGENT decide mutarea de vânzare (NU routerul)
    • MAX 3 tool calls per tur (limită dură în cod)
    • tool results: max 6 produse × 8 câmpuri (nu obiecte complete)

[8] VALIDATOR (cod pur)
    • fiecare preț din reply există în ctx.retrieval
    • fiecare produs menționat există în ctx.retrieval
    • linkurile sunt din catalog (nu inventate)
    • invalid → 1 retry cu feedback → formulare fără cifre
    • ZERO prețuri inventate structural

[9] SENDER (singurul punct de ieșire din sistem)
    • typing indicator trimis instant la primire (Meta API)
    • răspuns spart în 2 mesaje scurte dacă > 200 caractere
    • scriere tranzacțională în aceeași TX: reply în conv.outbox +
      patch state + message_event
    • dispatcher separat citește conv.outbox → trimite la Meta →
      salvează provider_message_id pe conv.messages → retry cu backoff la fail
    • statusurile delivered/read/failed (webhook status) se mapează pe
      conv.messages.provider_message_id → update conv.messages.status
    • POST-TUR async (nu blochează): extractor profil nano + lead score update

PROACTIV (în afara pipeline-ului, scheduler separat)
    • AWB la expediere · back-in-stock · follow-up coș abandonat
    • verifică opt-in: conv.contacts.consent
    • verifică 24h window: now() - last_inbound_at < 24h →
      mesaj normal; altfel → DOAR template cu status='approved'
      din core.wa_templates
```

---

## TurnContext — contractul central

```python
@dataclass
class TurnContext:
    turn_id: str                        # uuid generat la intrare în pipeline
    business: BusinessConfig            # citit din core.businesses
    contact: Contact                    # citit din conv.contacts
    message: InboundMessage             # text, media_type, provider_msg_id
    history: list[Message]              # max 8, cel mai recent ultimul
    state: ConversationState            # max 8KB (CHECK în DB)
    language: str                       # 'ro' | 'hu' | 'en' (setat în Gates)
    route: RouteDecision | None         # scris DOAR de stagiul Triaj
    retrieval: RetrievalResult | None   # scris DOAR de stagiul Retrieval
    reply: Reply | None                 # orice stagiu poate seta → early exit
    events: list[Event]                 # acumulat pentru analytics
```

**Regula absolută**: fiecare câmp are exact un stagiu care îl scrie.
Dacă două stagii vor să scrie același câmp, arhitectura e greșită.

---

## Schema DB — 4 scheme Postgres

Migrări: `docs/001_schema_v1.sql` (baza) + `docs/002_schema_fixes.sql`
(outbox, identities, attribution, embeddings, GDPR, multi-limbă).
Tabelele marcate **[002]** vin din a doua migrare.

### core (tenants și canale)
```
core.businesses         — id, slug, name, vertical, timezone, working_hours, settings
core.channel_instances  — id, business_id, channel, provider, instance_key
core.wa_templates [002] — id, business_id, channel_instance_id, name, language,
                          category(utility|marketing|authentication), version,
                          body, variables jsonb, provider_template_id,
                          status(draft|submitted|approved|rejected|paused)
                          • proactivul în afara ferestrei de 24h folosește
                            DOAR rânduri cu status='approved'
```

### catalog (read-only pentru bot, scris de sync)
```
catalog.taxonomy            — business_id, kind(category|concern|intent_keyword),
                              key, label, aliases[], maps_to[], applicable_filters jsonb
catalog.products            — id, business_id, external_id, name, brand, category,
                              ai_summary, concerns[], suitable_for jsonb,
                              attributes jsonb, min_price, is_active
catalog.product_embeddings [002] — product_id PK, business_id, model,
                              embedding vector(1536), content_hash
                              • re-embed DOAR dacă content_hash(ai_summary) diferă
                              • search_products: filtre SQL pe products +
                                ORDER BY embedding <=> query pe subsetul filtrat
catalog.product_variants    — id, business_id, product_id, sku, shade_name,
                              list_price, sale_price, stock_qty, is_active
catalog.review_summaries [002] — product_id PK, business_id, summary,
                              sentiment, top_pros[], top_cons[], built_at
                              • job offline; citit de get_product_details
catalog.services            — id, business_id, category_id, name, price_from, price_to
catalog.locations           — id, business_id, city, address, phone, working_hours
catalog.faq                 — id, business_id, language [002], cache_key, question, answer
catalog.faq_aliases         — id, business_id, faq_id, alias_text, normalized_alias (generated)
catalog.faq_alias_candidates — id, business_id, faq_id, original_text, status(pending|approved|rejected)
catalog.knowledge_guides    — id, business_id, key, topic, content jsonb
catalog.clarification_templates — id, business_id, language [002],
                              missing_field, template_text
catalog.response_cache      — id, business_id, language [002],
                              query_embedding vector(1536), response, expires_at
                              • lookup ÎNTOTDEAUNA: business_id + language + cosine
```

### conv (runtime conversațional — perimetru GDPR)
```
conv.contacts           — id, business_id, display_name, profile jsonb,
                          bot_active, handoff_until,
                          last_inbound_at [002] (alimentează 24h window),
                          consent jsonb [002] (opt-in proactiv/marketing),
                          erased_at [002] (GDPR: anonimizat, nu șters),
                          conversation_id UNIQUE
conv.channel_identities [002] — id, business_id, contact_id, channel,
                          external_user_id, UNIQUE(business_id, channel, external_user_id)
                          • PII-ul de canal (telefon E.164 / tg id) stă DOAR aici
                          • identity resolution = lookup aici, nu pe contacts
conv.messages           — id, business_id, conversation_id, role, content,
                          media_type, created_at,
                          provider_message_id [002] (outbound: wamid de la Meta),
                          status [002] (queued|sent|delivered|read|failed)
conv.outbox [002]       — id, business_id, conversation_id, idempotency_key UNIQUE,
                          payload jsonb, status(pending|dispatching|sent|failed|dead),
                          attempts, next_attempt_at, last_error
                          • Sender scrie aici tranzacțional; dispatcherul trimite
conv.conversation_state — conversation_id PK, business_id, active_search jsonb,
                          displayed_products jsonb (max refs, NU obiecte!),
                          pending_question jsonb, asked_intents jsonb,
                          constraints jsonb, expires_at,
                          CHECK pg_column_size(...) < 8192
conv.short_memory       — id, conversation_id, content, created_at (TTL 7z)
conv.inbound_dedupe     — id, business_id, channel, provider_message_id UNIQUE
                          • cleanup job: păstrează doar ultimele 48h
conv.checkout_links [002] — id, business_id, conversation_id, contact_id,
                          ref_code UNIQUE, cart jsonb, url, clicked_at,
                          converted_order_id, expires_at
                          • checkout_link(ref=...) scrie aici; webhook-ul de
                            comenzi face match pe ref_code → atribuire
conv.orders             — id, business_id, external_order_number,
                          customer_phone, status, tracking_url, raw_data jsonb,
                          attributed_checkout_link_id [002],
                          attribution [002] (none|assisted|direct_bot)
conv.back_in_stock_subscriptions [002] — id, business_id, contact_id,
                          product_id, variant_id, notified_at,
                          UNIQUE(business_id, contact_id, product_id, variant_id)
conv.bookings           — id, business_id, contact_id, service_id,
                          starts_at, ends_at, status, calendar_event_id
```

### analytics (append-only — botul are doar INSERT, fără UPDATE/DELETE)
```
analytics.message_events — id, business_id, conversation_id, route, intent,
                           cache_hit, cache_type, product_ids uuid[],
                           llm_calls_count, total_prompt_tokens,
                           total_completion_tokens, total_cost_usd, latency_ms,
                           success, error, created_at
analytics.llm_calls      — id, event_id, business_id, step, model,
                           prompt_tokens, completion_tokens, cached_tokens,
                           cost_usd, latency_ms, success
analytics.debug_snapshots — id, event_id, reason(sampled|error|handoff),
                            snapshot jsonb, created_at (TTL 14z)
analytics.usage_daily [002] — business_id, day PK, conversations, messages_in,
                           messages_out, templates_sent, tokens, cost_usd,
                           cache_hits, handoffs, orders_attributed,
                           revenue_attributed, intents jsonb
                           • rollup nocturn din message_events + orders
                           • dashboard-ul și facturarea citesc DOAR de aici
```

### GDPR [002]
```
conv.gdpr_requests      — id, business_id, contact_id, kind(erase|export|access),
                          status, result_ref, completed_at
core.audit_log          — id, business_id, actor, action, entity, entity_id,
                          details jsonb, created_at
funcția gdpr_erase_contact(contact_id):
    • contacts: display_name=NULL, profile='{}', erased_at=now()
    • channel_identities: DELETE (telefonul dispare)
    • messages: content=NULL (păstrezi structura pt analytics)
    • orders.customer_phone: NULL
    • audit_log: insert
Retenție: conv.messages > 12 luni → content anonimizat (job lunar).
```

---

## Tool-uri agentului (cod determinist, activate per business)

```python
# toate tool-urile au semnătura: async def tool(ctx: TurnContext, **params) -> ToolResult
# MAX 3 apeluri per tur — limitat în agent runner

search_products(category, filters, budget_max, concerns, suitable_for, limit=6)
  # filtre SQL dure (taxonomie) + ranking semantic (catalog.product_embeddings) + reranker
  # returnează max 6 produse × 8 câmpuri: id, name, brand, min_price, url, ai_summary, stock, shade

get_product_details(product_id)
  # detalii complete + review summary din catalog.review_summaries

compare_products(product_ids: list[str])
  # diferențe structurate între 2-3 produse (tabel pros/cons)

check_order(order_number_or_phone)
  # status + tracking din conv.orders

delivery_eta(product_id, address)
  # ETA din integrarea cu curier/ERP

reorder(contact_id)
  # ultimele comenzi ale contactului → sugestie reorder

cart_add(product_id, variant_id)
checkout_link(cart_items, ref=turn_id)
  # scrie conv.checkout_links (ref_code) → link cu ?ref= pentru atribuire conversie

subscribe_back_in_stock(product_id, variant_id)
  # insert în conv.back_in_stock_subscriptions; proactivul notifică la restock

faq_lookup(query)
  # căutare în catalog.faq + knowledge_guides (filtrat pe ctx.language)

book_appointment(service_id, preferred_datetime, contact_info)
  # creare în conv.bookings + Google Calendar sync

request_human(reason)
  # setează conv.contacts.handoff_until, notifică operatorul
```

---

## Roluri DB și securitate

```
bot_runtime    — SELECT catalog.* + INSERT catalog.response_cache, faq_alias_candidates
               — SELECT/INSERT/UPDATE/DELETE conv.*
               — INSERT analytics.* (fără UPDATE/DELETE — append-only forțat)

catalog_sync   — SELECT/INSERT/UPDATE catalog.*
               — SELECT/INSERT/UPDATE conv.orders

dashboard_read — SELECT pe toate schemele
               — INSERT/UPDATE catalog.faq, taxonomy, templates, core.wa_templates
               — UPDATE conv.contacts (bot_active, handoff_until)

gdpr_svc       — EXECUTE gdpr_erase_contact + SELECT export + INSERT audit_log
```

Izolarea multi-tenant primară: `WHERE business_id = $1` în cod, FĂRĂ excepție.
Defense-in-depth [002]: RLS pe conv.* și catalog.* cu
`SET app.business_id = $1` per conexiune (pool-ul o setează automat în
`db/connection.py`). O greșeală de query devine „zero rezultate",
nu „datele altui client". Rolurile NU au bypassrls pe conv/catalog.

---

## Principii — respectă-le în tot codul

1. **Pipeline liniar** — niciun stagiu nu sare înapoi, niciun loop de orchestrare
2. **LLM doar la 2 puncte** — triaj (nano) și agent (mini). Tot restul: cod determinist
3. **Un singur proprietar per câmp** — dacă două funcții scriu același câmp din TurnContext, e o greșeală de design
4. **Buget de context impus în cod** — nu în prompturi, nu prin disciplină, în cod
5. **Un singur punct de ieșire** — Sender → conv.outbox → dispatcher. Orice alt loc care trimite mesaje e o greșeală
6. **Niciodată tăcere** — degradare: mini → retry → nano → template → om notificat
7. **business_id pe tot** — niciun query fără `WHERE business_id = $1`; RLS ca plasă, nu ca mecanism primar
8. **State = ref-uri, nu obiecte** — în displayed_products: {product_id, name, price}, NU obiectul complet
9. **Taxonomia e sursa unică** — promptul se generează din DB, nu e hardcodat
10. **Observabilitate din runner** — stagiile nu știu că sunt măsurate; runner-ul scrie event-ul
11. **Limba e parte din cheie** — orice lookup în faq / response_cache / clarification_templates / wa_templates include language. Un cache hit în limba greșită e un bug, nu un hit
12. **PII trăiește în două locuri** — conv.channel_identities și conv.orders.customer_phone. Nicăieri altundeva. Logurile nu conțin telefoane (redaction în logger)

---

## Structura proiectului

```
nativx-assistant/
├── CLAUDE.md                    ← acest fișier
├── docs/
│   ├── 001_schema_v1.sql        ← migrare DB completă (validată Postgres 16)
│   ├── 002_schema_fixes.sql     ← outbox, identities, attribution, embeddings,
│   │                              GDPR, multi-limbă, usage_daily, wa_templates
│   └── 010_import_beauty_data.sql ← date reale sole.ro (245 prod, 284 variante)
├── src/
│   ├── config.py                ← settings (Pydantic BaseSettings)
│   ├── models.py                ← TurnContext + toate dataclass-urile
│   ├── db/
│   │   ├── connection.py        ← pool asyncpg, SET app.business_id, context managers
│   │   └── queries/             ← SQL per domeniu (catalog, conv, analytics)
│   ├── webhook/
│   │   ├── app.py               ← FastAPI app
│   │   ├── meta.py              ← semnătură + dedupe + last_inbound_at + push Redis
│   │   ├── status.py            ← delivered/read/failed → conv.messages.status
│   │   └── orders.py            ← webhook comenzi platformă → match ref_code → atribuire
│   ├── worker/
│   │   ├── runner.py            ← pipeline runner (execută stagiile, măsoară)
│   │   ├── consumer.py          ← Redis stream consumer cu lock
│   │   ├── dispatcher.py        ← conv.outbox → Meta API, retry idempotent
│   │   └── stages/
│   │       ├── gates.py
│   │       ├── free_layers.py
│   │       ├── triage.py
│   │       ├── context_builder.py
│   │       ├── agent.py
│   │       ├── validator.py
│   │       └── sender.py
│   ├── tools/
│   │   ├── base.py              ← ToolResult dataclass + registry
│   │   ├── search_products.py
│   │   ├── get_product_details.py
│   │   ├── compare_products.py
│   │   ├── check_order.py
│   │   ├── checkout.py          ← + conv.checkout_links
│   │   ├── back_in_stock.py
│   │   ├── faq_lookup.py
│   │   ├── book_appointment.py
│   │   └── request_human.py
│   ├── agent/
│   │   ├── prompt_builder.py    ← system prompt generat din catalog.taxonomy
│   │   └── tool_definitions.py  ← OpenAI tool schemas
│   ├── proactive/
│   │   ├── scheduler.py         ← AWB / back-in-stock / coș abandonat
│   │   └── templates.py         ← core.wa_templates + 24h window + consent check
│   ├── gdpr/
│   │   └── erase.py             ← gdpr_erase_contact + export
│   └── jobs/
│       ├── rollup_usage.py      ← nocturn: message_events → usage_daily
│       ├── embed_products.py    ← ai_summary → product_embeddings (content_hash)
│       └── cleanup.py           ← inbound_dedupe 48h, short_memory, snapshots, retenție messages
├── tests/
│   ├── golden/                  ← conversații de test (fixture JSON)
│   │   ├── beauty_search.json
│   │   ├── order_status.json
│   │   └── faq_hit.json
│   ├── test_pipeline.py
│   ├── test_tools.py
│   ├── test_validator.py
│   └── test_tenant_isolation.py ← fiecare query refuză date cu alt business_id
├── scripts/
│   └── generate_import.py       ← ETL dump → schema curată
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Client demo activ

**Business ID**: `99999999-0000-0000-0000-000000000001`
**Slug**: `beauty-shop`
**Vertical**: `beauty`
**Date**: 221 produse reale sole.ro active, 284 variante, 38 taxonomii, 11 FAQ-uri

Folosește acest business_id pentru toate testele locale.

---

## Ce NU facem

- NU n8n pentru miezul sistemului (ok pentru cron-uri și alerte periferice)
- NU LLM pentru filtrare sau routing determinist
- NU obiecte de produs complete în state — doar ID-uri + snapshot mic
- NU categorii/aliase hardcodate în prompturi — vin din catalog.taxonomy
- NU recovery agent pentru cazuri ambigue — CLARIFY cu template ieftin
- NU scriere în catalog din worker (excepție: response_cache și faq_alias_candidates)
- NU tăcere la erori — întotdeauna ceva iese spre client
- NU trimitere directă la Meta din stagii — totul prin conv.outbox + dispatcher
- NU mesaje proactive fără consent + (24h window SAU template approved)
- NU telefoane/PII în loguri sau în analytics — doar în channel_identities și orders
```