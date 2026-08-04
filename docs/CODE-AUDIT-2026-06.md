# Audit de cod — Nativx Assistant

**Data:** 2026-06-22 · **Branch:** `feat/NX-124a-rest` · **Scope:** întreg codebase-ul (~15k LOC src, ~15.5k LOC teste, 17 migrări)

**Metodologie:** audit multi-agent pe 8 dimensiuni (arhitectură, izolare multi-tenant,
securitate input/webhook, concurență/fiabilitate, LLM/cost, date/DB, performanță,
teste/calitate). Fiecare finding a fost **verificat adversarial** de un al doilea agent
care a citit codul real și a încercat să-l refuze, calibrând severitatea contra celor 12
principii documentate. **52 findings brute → 32 confirmate** (20 respinse ca decizii de
design intenționate sau înțelegeri greșite). Notele de calibrare ale verificatorilor sunt
păstrate în fiecare finding (« Calibrare »).

**Distribuție confirmate:** 1 critical · 2 high · 5 medium · 24 low.

---

## 1. Verdict executiv

Codebase-ul este **fundamental sănătos și disciplinat arhitectural**: pipeline-ul liniar cu
un singur `TurnContext`, regula „un proprietar per câmp", izolarea multi-tenant prin
`business_id` + RLS ca plasă, idempotența outbox-ului și apărarea anti-halucinație prin
validator de output sunt aplicate consecvent și bine documentate. Din cele 32 de findings,
**doar 2 sunt grave** (1 critical, 1 high), restul fiind în covârșitoare majoritate `low` —
semnal de un proiect matur, nu fragil.

Temele dominante NU sunt bug-uri de logică, ci **trei pattern-uri sistemice**:
- **(a)** o discrepanță letală între codecul pgvector și formatul literal de vector care
  poate omorî tăcut straturile gratuite în producție;
- **(b)** **căi de tăcere la client** care contrazic principiul P6 („niciodată tăcere") în
  colțuri de concurență;
- **(c)** **drift doc-vs-cod** (status, partiții, valută, teste fantomă) acumulat din viteza
  de livrare.

Sub stratul de severitate mică se ascund două riscuri operaționale cu **fereastră de
declanșare datată** (partiții `messages` după 2026-07-31, cache semantic mort în prod) care
merită acțiune imediată indiferent de eticheta de severitate.

**Concluzie:** cod **gata de producție după rezolvarea Val 0** (4 taskuri, ~1 sprint scurt).
Prioritatea #1 indiscutabilă este **#15** (cache semantic mort tăcut): small effort, dar
invalidează tăcut chiar metrica de business (deflecția 40-60%) care justifică întreaga
arhitectură a straturilor gratuite.

---

## 2. Top riscuri (P0/P1)

### P0-1 · Cache semantic + FAQ pot fi 100% morți în producție, tăcut — `critical`
**#15** · `src/db/queries/faqs.py:18,38-55` · `src/db/queries/semantic_cache.py:27-29,91-110,147-179` · `src/db/connection.py:118-160` · `src/worker/stages/faq.py:77-78` · `src/worker/stages/cache.py:143-144`

`tenant_conn` (bot_pool) înregistrează un codec text pentru tipul `vector` al cărui encoder
face `for x in value: float(x)`. `search_products_semantic` trimite corect `list[float]`
direct ca `$n::vector`, deci codecul îl encodează bine. DAR `faqs.semantic_lookup`,
`semantic_cache.semantic_lookup` și `semantic_cache.upsert_entry` pre-formatează vectorul cu
`_vec()` la un STRING `'[0.1,...]'` și-l trimit la `$3::vector`. asyncpg inferă OID-ul
parametrului ca `vector` din cast și aplică encoderul pe acel str → `for x in '[0.013,...]'`
iterează caractere → `float('[')` ridică `asyncpg.DataError` la encode → query-ul crapă.
`cache_stage`/`faq_stage` prind orice excepție și o degradează la `miss` cu doar un
`log.warning`.

**Impact:** stratul de cache semantic și FAQ — exact deflecția de 40-60% care justifică
arhitectura — pot fi nefuncționale în prod **fără ca nimeni să observe**, iar `upsert_entry`
care pică înseamnă că write-back-ul nici nu populează cache-ul. Testele NU prind: unit-ul
mock-uiește `semantic_lookup`, iar singurul integration test rulează pe **admin pool** (fără
codec), unde string-ul trece. Mismatch-ul există DOAR pe calea reală de producție.

**Calibrare:** `current_prices` (semantic_cache.py:185-211) e listat inițial ca afectat dar
NU e (castează `$2::uuid[]`, niciun `::vector`). Funcțiile genuin afectate sunt
`faqs.semantic_lookup`, `semantic_cache.semantic_lookup` și `upsert_entry`.

**Fix (small, load-bearing):** uniformizează pe UN contract — elimină `_vec()` din
`faqs.py`/`semantic_cache.py`, trimite `list[float]` DIRECT (ca `search_products_semantic`).
Adaugă un integration test care rulează aceste lookup-uri pe `get_bot_pool()` cu codecul
activ. `cache_stage`/`faq_stage` ar trebui să emită un **event distinct** (nu doar
`log.warning`) la eroare de lookup, ca un 0% deflecție să fie vizibil în analytics.

---

### P0-2 · Webhook comenzi: secret global + `business_id` din path nelegat de semnătură → atribuire de venit cross-tenant — `high`
**#3** · `src/webhook/app.py:127-154` · `src/webhook/signature.py:40-49` · `src/config.py:204` · `src/worker/consumer.py:107-119` · `src/webhook/orders.py:60-122`

`POST /webhook/orders/{business_id}` ia `business_id` din path și îl propagă în envelope
(`kind='order'`), iar worker-ul îl folosește DIRECT ca scope de tenant
(`tenant_conn(business_id)` → `process_order` scrie `orders`/`order_items`/`checkout_links`/
`analytics` + `revenue_attributed` în `usage_daily` = **facturare**). Autentificarea e
HMAC-SHA256 peste DOAR corpul brut, cu un **singur secret GLOBAL**
(`settings.orders_webhook_secret`) care NU include `business_id` în mesajul semnat.

**Consecință:** o semnătură validă pentru un corp e validă pentru ORICE
`/webhook/orders/{alt_business}` cu același corp. Orice parte care deține secretul comun
(integrator de tenant, secret scurs din proxy/log) poate forța comenzi + atribuire de venit
în contul oricărui alt client. Comentariul din cod (app.py:137-138) afirmă fals că HMAC
autentifică `business_id` — verificat direct, e incorect pentru secret global. Încalcă P2+P7.

**Calibrare:** critical → high. Nu e exploatabil complet neautentificat — cere posesia
secretului global (deținut de agenție/integratori). Vectorul realist: o parte care deține
secretul comun forțează revenue cross-tenant.

**Fix (medium):** (a) secret PER-tenant rezolvat din `channels.credentials_ref`/`settings`
pe baza `business_id` din path (ca `resolve_web_session`); ȘI/SAU (b) semnează
`business_id + "." + raw_body`. Ideal ambele. Corectează comentariile false (app.py:137,
consumer.py:107, signature.py:47).

---

### P1-3 · Partiționare messages/analytics_events — landmine datat 2026-07-31 — `high`
**#16** · `docs/schema_v2_production.sql:211-215,675-679,822-829`

`messages` și `analytics_events` sunt partiționate lunar, dar schema creează manual DOAR
`messages_2026_06`, `messages_2026_07` + `messages_default` (idem analytics). Nu există job
activ de creare a partițiilor (pg_partman/pg_cron sunt doar comentarii; `src/jobs/cleanup.py`
nici nu există). **Astăzi e 2026-06-22.** După 2026-07-31, fiecare INSERT cade în `_default`.

**Impact (silențios, nu outage):** INSERT-urile reușesc (cad în `_default`), dar (a) se pierde
pruning-ul/retenția prin `DROP PARTITION` (sabotează jobul NX-84); (b) **capcana
operațională**: odată ce `_default` conține rânduri din august, NU mai poți crea
`messages_2026_08 PARTITION OF` fără DETACH default + migrare manuală de rânduri.

**Fix (medium) — deadline real înainte de 2026-07-31:** task NX-40 (există necompletat) —
job lunar idempotent (`CREATE TABLE IF NOT EXISTS ... PARTITION OF`) care precreează N+1/N+2
+ **migrare delta buffer `2026-08..2026-12` ACUM** ca plasă. Monitorizează dimensiunea
`_default` ca alertă.

---

### P1-4 · Căi de tăcere la client în concurență (încalcă P6) — `medium`/`low`
**#8 + #12** · `src/worker/processor.py:549-636` · `src/worker/consumer.py:90-99,166-169` · `src/db/queries/conversations.py:108-146`

Două colțuri unde principiul cardinal P6 („niciodată tăcere") se rupe:

- **#8 `medium`** — `patch_conversation_state` ridică `StateConflict` la mismatch de
  `state_version`, dar **niciodată prins** (grep găsește doar `raise` + definiție, zero
  `except`, zero retry). Docstring-urile promit „turul reia", dar nu există. La ture
  concurente pe aceeași conversație, excepția se propagă din TX-ul Sender → rollback la
  outbox+state+mark_completed → reply-ul nu mai iese → **tăcere totală**, doar o linie de log.
  - *Calibrare (high→medium):* triggerul e rar (protejat de conv-lock). Pe calea async
    StateConflict NU duce la ACK imediat — mesajul rămâne în PEL fără ACK; dropul real apare
    prin interacțiunea reaper-60s vs re-claim-300s. Net-ul (tăcere) e corect. Reparația e
    cardată deja în `ARCH-2026-ROADMAP.md:96` (extinde NX-85).

- **#12 `low`** — peste `conv_lock_max_requeues=10`, `_requeue_busy` **abandonează** mesajul
  tăcut (`return f'dropped după {n}'`), ȘI fără event de analytics: cardul NX-85 specifică un
  event `conversation_lock_dropped` ca alarmă de contenție, dar **NU a fost implementat**
  (consumer.py:168 face doar `log.info`) → drop invizibil și pentru monitoring.
  - *Calibrare (medium→low):* scenariul cere >1 replică + contenție pe același sender + tur
    lent ~30s. Improbabil în trafic normal.

**Fix (medium):** retry mărginit (2-3) pe `StateConflict` (re-citește conv → re-merge
`new_state` peste starea proaspătă → re-patch; outbox e idempotent pe
`idempotency_key={turn_id}:{i}` → sigur de reluat), cu fallback P6 la epuizare. La drop-ul
#12: emite `conversation_lock_dropped` (turn_id + requeues, fără PII) ÎNAINTE de orice
abandon; ideal coadă dead-letter recuperabilă.

---

### P1-5 · Lock TTL conversație (30s) < durata turului → ture concurente — `medium`
**#9** · `src/config.py:287` · `src/redis_bus.py:93-100` · `src/worker/consumer.py:148-184` · `src/db/queries/inbound_dedupe.py:15`

`conv_lock_ttl_seconds=30`, dar un tur de sales (triaj nano + agent mini + ≤3 tool calls +
multiple round-trip DB + post-tur summarizer + extractor profil + cache write-back, fiecare
cu apel OpenAI) depășește ușor 30s sub latență OpenAI normală. Când lock-ul expiră în mijlocul
turului, altă replică poate prelua aceeași conversație (cheia = business+sender) → tur
concurent → exact `StateConflict`-ul de la P1-4. TTL-urile sunt nealiniate: lock 30s < outbox
visibility 120s < CLAIM_TTL inbound 300s — lock-ul de conversație cedează primul.

**Calibrare (high→medium):** scrierile NU se corup — outbox + patch + mark sunt o singură TX
cu optimistic lock → turul pierzător face rollback atomic (zero double-send, zero corupere).
Impactul real e: **tur LLM irosit (cost), typing duplicat, latență ~60s pe al doilea mesaj,
log ERROR zgomotos** — nu integritate de date.

**Fix (small):** aliniază `conv_lock_ttl_seconds` la ~180-300s (≈ CLAIM_TTL) SAU heartbeat
`PEXPIRE` periodic cât turul rulează. Combinat cu retry-ul P1-4 închide race-ul.

---

### P1-6 · Dublă-trimitere la client la crash între send Meta și mark_sent — `medium`
**#10** · `src/worker/dispatcher.py:100-138` · `src/db/queries/outbox.py:78-122` · `src/meta_client.py:48-70`

Fluxul: `claim_due` trece rândul în `dispatching` + împinge `next_attempt_at +120s` →
`sender.send_text()` face POST la Meta și **primește `wamid`** (mesajul a plecat la client) →
DOAR DUPĂ, într-o TX separată, `mark_sent` + `set_message_provider_id`. Dacă procesul moare
ÎNTRE send-ul reușit și `mark_sent`, rândul rămâne `dispatching`, redevine scadent după 120s
și e **re-trimis**. `send_text` (meta_client.py:54-64) NU trimite nicio cheie de idempotență
la Meta → **al doilea mesaj real la client**. At-least-once fără dedup la provider.

**Calibrare (high→medium):** fereastra e îngustă (sub-secunda dintre HTTP-200 și commit + send
deja reușit). Impactul = un singur mesaj duplicat, auto-limitat de `MAX_ATTEMPTS=6` — nu
corupere, nu securitate, nu outage.

**Fix (medium, ieftin):** la re-claim al unui rând `dispatching`, verifică
`set_message_provider_id` existent / `message_status_events` înainte de re-send; minim
documentează explicit fereastra reziduală ca operatorul s-o accepte conștient.

---

### P1-7 · Validatorul de preț ratează separatorul de mii — preț halucinat „greu" poate trece — `medium`
**#13** · `src/worker/stages/agent.py:51-55,198-209,121`

`_PRICE_RE` permite doar 1-2 zecimale (`\d{1,6}(?:[.,]\d{1,2})?`). Când modelul scrie „1.299
lei", regexul NU prinde 1299 — capturează doar „299". `_prices_ok` validează „299" (nu 1299);
dacă 299 e un preț real din retrieval (frecvent în beauty), **prețul halucinat „1.299 lei"
TRECE** validatorul. Plasa de cifre bare nu acoperă cazul. Gaură structurală în invariantul
„zero prețuri inventate", exact pe valorile mari.

**Calibrare (high→medium):** exploatarea e condiționată — modelul vede prețuri DOAR în format
`:.2f` fără separator de mii (ar trebui să reformateze spontan în stil RO „1.299"), ȘI e
nevoie de o coliziune (coada de 3 cifre să coincidă ±0.5 cu un preț real din retrieval).
Afectează doar prețuri ≥1000 lei, mai rare în beauty. Bug real, dar margine condiționată.

**Fix (small):** normalizează separatorul de mii înainte de validare
(`\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?` colapsat la întreg) + caz în `tests/hallucination`.

---

## 3. Findings pe teme

### Securitate & izolare multi-tenant
Mecanismul primar (`WHERE business_id=$1` + RLS pe `bot_runtime` fail-closed) e solid și bine
testat (`test_grants_smoke` parametrizează 20 tabele, `test_fail_closed_*` dovedesc
fail-closed). Singura breșă reală e webhook-ul de comenzi (P0-2).

- **#4 `low`** — GDPR erase nu anonimizează `conversation_summaries` (eco de PII din corpurile
  de mesaje). `src/gdpr/erase.py:40-67`. *Atenuant:* `summarizer.py:23-75` redactează deja
  telefoanele (singurul PII canonic P12) prin regex pe input+output; expunerea reziduală =
  nume/adrese liber-tastate. **Fix:** `update conversation_summaries set summary=null where
  conversation_id in (select id from conversations where contact_id=p_contact)` în
  `gdpr_erase_contact`.
- **#5 `low`** — handshake GET verify folosește `==` ne-timing-safe pe `verify_token`.
  `src/webhook/app.py:75-77`. **Fix:** `hmac.compare_digest(hub_verify_token or "", expected)`.
- **#6 `low`** — Telegram `_to_event` produce `sender_external_id='None'` la `chat.id` lipsă
  (poluează `channel_identities`). `src/channels/telegram/poller.py:36-46`. Canal TEST,
  contractul API garantează chat.id pe Message cu text → trigger practic neatins. Hardening de
  consistență cu `_to_callback_event` (același guard).
- **#7 `low`** — gol rezidual: prompt-injection NE-numeric (exfiltrare system prompt, schimbare
  ton, scoatere disclaimer, promisiuni ne-numerice) nu are apărare în niciun strat —
  validatorul groundează doar cifre/produse/linkuri. `src/worker/stages/gates.py:214-245`,
  `src/config.py:274-276`. Cel mai bun raport valoare/efort: întărește normalizarea din `_norm`
  (colapsare spații repetate + strip caractere zero-width). `injection_screen_enabled` e default
  False (cere DomainPack seedat).

### Concurență & fiabilitate
Designul de bază (TX unică cu optimistic lock, outbox idempotent, visibility timeout ca reaper)
e corect și protejează integritatea datelor — zero corupere demonstrată. Slăbiciunile sunt la
**margini de tăcere** (P6) și **TTL-uri nealiniate**.

- **#8, #9, #10, #12** — vezi P1-4 / P1-5 / P1-6.
- **#11 `low`** — `mark_sent`/`mark_failed` neguardate pe status/attempts → dispatcher original
  lent poate suprascrie un rând re-revendicat. `src/db/queries/outbox.py:150-209`. Race teoretic
  care cere multi-replica (azi un singur dispatcher în compose) + deținător înghețat >120s
  (timeout HTTP 15s → marjă 8x). **Fix (hardening pre-scalare):** adaugă `and
  status='dispatching'` la mark_*, returnează rows-affected și loghează no-op.

### LLM, agent & cost
Apărarea anti-halucinație (validator de output grounded) e filozofia corectă și load-bearing.
O singură gaură structurală reală (separator de mii) + curățenie minoră.

- **#13 `medium`** — separator de mii → P1-7.
- **#2 `low`** — valuta „lei" hardcodată în `compose.flatten()`/`_deterministic_reply()` (textul
  user-facing) deși NX-114 a făcut promptul currency-aware. `src/worker/compose.py:249`,
  `src/worker/stages/agent.py:341,349,475`. Pentru tenant non-RON (EUR/HUF) recomandarea
  aplatizată afișată clientului spune „89.00 lei" greșit. *Calibrare:* extinde fix-ul și la
  `agent.py:508-510` (`_load_prompt_inputs` nu pasează `currency` din `domain_pack` către
  `PromptInputs.build` — fără asta nici promptul NX-114 nu e corect pentru non-RON). **Fix:**
  helper unic `format_price(value, currency)` reutilizat de prompt + compose + agent (RON rămâne
  byte-identic).
- **#14 `low`** — `_budget()`/`_BUDGET_RE` cod mort în producție (sursa de buget e triajul via
  `_normalize_slots`). `src/worker/stages/agent.py:175-180`. *Calibrare:* NU e cod mort complet
  — are test dedicat `test_budget_extraction` în `tests/test_agent.py:125-127`. **Fix:** ștergere
  trebuie să includă importul + testul, altfel suita pică (ImportError).

### Date & DB
Schema e sursa de adevăr matură și seedată, dar cu două riscuri operaționale datate (codec +
partiții) și un pattern de duplicare `_vec()` care e cauza-rădăcină a celui critic.

- **#15 `critical`, #16 `high`** — vezi P0-1 / P1-3.
- **#18 `low`** — 4 copii `_vec()` duplicate (faqs/semantic_cache/seed_faqs/embed_products) +
  codec. `src/db/connection.py:118-120`. *Calibrare:* NU există divergență de precizie (toate
  `.7f`); `content_hash` e independent de `_vec()` (`sha256(f'{model}:{text}')`). Tech-debt DRY
  pur. **Fix:** un singur `src/db/vector.py` partajat.
- **#19 `low`** — `search_products_semantic` NU filtrează pe `model` (spre deosebire de FAQ/cache
  NX-124a). `src/jobs/embed_products.py:40-41`. *Calibrare:* NU mixaj de dimensiuni (`vector(1536)`
  hard-typed); riscul real = degradare tranzitorie de relevanță într-o fereastră de re-embed
  parțial. **Fix:** `pe.model = <curent>` în search (paritate) SAU re-embed tranzacțional per
  business.
- **#17 `low`** — `cache_hits` din rollup numără doar `cache_lookup(exact|semantic)`, ignoră
  deflecțiile FAQ (`faq_hit`) și alias (`alias_lookup hit=true`) → subraportează rata straturilor
  gratuite (metrica de business). `src/db/queries/usage.py:30-34`. *Calibrare:* NU lărgi
  `cache_hits` (ar polua semantica unei coloane cu contract clar); adaugă coloane separate
  `faq_hits`/`alias_hits` (deja notat în `ANALYTICS-AUDIT-SI-BLUEPRINT.md:354`). Append-only →
  backfill posibil, fără urgență.
- **#20 `low`** — `purge_by_product` cu `@>` jsonb fără index GIN pe `retrieval_signature`.
  `src/db/queries/semantic_cache.py:229-241`. *Calibrare:* funcția NU e cablată în prod
  (invalidarea reală e `bump_data_version` lazy) → datorie LATENTĂ. **Fix:** `create index ...
  using gin (retrieval_signature jsonb_path_ops)` înainte de cablare.

### Performanță
Nicio problemă de perf acută — toate `low` după re-calibrare. Câteva optimizări reale de
latență/cost pentru scalare.

- **#21 `low`** — dimensionare pool-uri fără buget global (replici × pool × procese sub poolerul
  Supabase). `src/db/connection.py:85-92`. *Calibrare:* azi fără replici în prod (compose.prod =
  o instanță per serviciu); webhook API e fără DB pe ingestie; scheduler folosește doar
  admin_pool. **Fix:** `min/max_size` configurabile din settings + buget total documentat ÎNAINTE
  de scalare orizontală + timeout pe `acquire`. **NU** ruta bot_pool prin pooler 6543
  transaction-mode (ar reintroduce gaura de izolare P0-A/NX-50 — session-mode 5432 direct e ales
  deliberat).
- **#22 `low`** — dispatcher polling fix 2s fără wakeup. `src/worker/dispatcher.py:160-167`.
  *Calibrare:* calea sincronă `/web/chat` (NX-25b) NU trece prin outbox (răspuns in-process) →
  impactul e doar pe async (WhatsApp/Telegram/SSE). **Fix:** wakeup Redis după `enqueue_outbox`
  sau `idle_sleep` 200-300ms.
- **#23 `low`** — embed redundant cache→FAQ pe ACELAȘI `canonicalize(body)`.
  `src/worker/stages/cache.py:123`, `src/worker/stages/faq.py:43`. *Calibrare:* suprapunerea
  genuină e DOAR cache↔FAQ (1 round-trip evitabil); `catalog_tools:284` embed-uiește `a.query`
  (text LLM diferit) → nu beneficiază; `processor:156` e post-tur. **Fix:** memoizează vectorul
  canonic pe `TurnContext` (faq_stage refolosește vectorul lui cache_stage).
- **#24 `low`** — `_VARIANTS_AGG` lateral (jsonb_agg + order by) rulează per-rând pe ~50
  candidați × 2 retrievere deși se trimit 6 produse. `src/db/queries/catalog.py:40-55,100-134`.
  **Fix:** SELECT slim (doar id + preț scalar) pe pool/fuziune + hidratare variante/imagine doar
  pe top-6 (al doilea `get_products_by_ids`).
- **#25 `low`** — typing indicator: `asyncio.create_task` per mesaj brut, fără limită, înainte de
  debounce → burst de N mesaje = N POST-uri. `src/worker/consumer.py:219-222`. *Calibrare:* riscul
  GC prematur e inexistent (task await-ul îl ține viu); I/O-bound, nu fură CPU. **Fix:** trimite
  typing o dată per cheie de debounce.

### Arhitectură
Principiile sunt respectate consecvent; cele 2 findings sunt scăpări de
generalizare/documentație, nu încălcări structurale.

- **#1 `low`** — bugetul 8KB state NU e tăiat în context builder (cum spune P4), doar CHECK DB ca
  plasă dură. `src/worker/context.py:59-103`, `src/worker/processor.py:549-579`. *Calibrare:* în
  practică inaccesibil — fiecare component de state e bornat individual (displayed_products,
  cart cap 10, active_search pool cap 24, asked_intents cap 8) → worst-case agregat mult sub 8KB,
  CHECK-ul nu se declanșează niciodată. **Fix:** ori adaugă `clamp_state()` defensiv în processor
  înainte de `patch_conversation_state` (restaurează intenția P4 + elimină calea de tăcere), ori
  corectează documentația (CLAUDE.md P4, conversations.py:9, models.py:168, context.py:2).
- **#2 `low`** — valuta hardcodată (vezi LLM/cost).

### Teste & calitate
Suita de bază e bună (golden CI, fail-closed, grants smoke), dar are **găuri de proces** (zero
type-check, zero coverage gate) și **assets valoroase necablate**.

- **#27 `medium`** — niciun mypy/pyright în CI deși codul e adnotat masiv. `.github/workflows/
  ci.yml:11-52`. Pe un sistem cu `TurnContext` strict per-câmp, type-check prinde clasa „cineva a
  schimbat tipul câmpului X". **Fix:** mypy non-strict gradual pe `src/` + ruff `B`(bugbear)/`UP`
  (NU bandit prioritar — false-positives mari). Bootstrap-ul cere tuning de config (mult Any +
  rânduri asyncpg dinamice).
- **#26 `low`** — `test_tenant_isolation.py` referit în `CLAUDE.md:486` + docstring viu
  `test_grants_smoke.py:10` dar NU există. *Calibrare:* plasa RLS e deja testată larg
  (`test_grants_smoke` 20 tabele, `test_fail_closed_*`) → un WHERE lipsă NU scurge date. **Fix
  low-cost:** corectează referințele fantomă la fișierele reale
  (`test_queries_runtime`/`test_inbound_dedupe`/`test_order_items_grant`). Test canonic per-query
  = nice-to-have.
- **#28 `low`** — retry/backoff/dead-letter dispatcher e DOAR `integration` (nu pe PR).
  `tests/test_dispatcher.py:21`. *Calibrare:* `test_channel_caps.py` (pur, pe PR) acoperă deja
  ramura de succes + edit_media→dead; integration rulează și pe push-pe-main (ci.yml:58), nu doar
  nightly. Gaura îngustă reală = ramura de EȘEC de transport + aritmetica backoff din `mark_failed`
  (Python pur, posibil off-by-one) + reaperul SQL. **Fix:** test cu fake-sender care ridică + unit
  pe `MAX_ATTEMPTS`/`_BACKOFF_SECONDS`.
- **#30 `low`** — 500 cazuri halucinație necablate în CI (runner manual `scripts/sim/halu_run.py`).
  `tests/hallucination/suite.json`. Harness-ul golden din CI are doar ~12 cazuri. Tabelele
  `conversation_evals`/`golden_tests` nu sunt scrise din nicăieri. **Fix:** job nightly cu judge
  pe subset (pică sub prag) + eșantion determinist (must_include/forbidden) ca golden pe PR.
- **#31 `low`** — niciun prag coverage (`pytest-cov` instalat dar neutilizat în CI).
  `.github/workflows/ci.yml:50-52`. *Calibrare:* exemplele de module 0% din finding sunt parțial
  greșite (`fusion.py`/`builders.py` testate direct). **Fix:** `--cov=src --cov-report=term-missing
  --cov-fail-under=N` calibrat la nivelul curent.
- **#32 `low`** — config sprawl (~94 setări / 29 flag-uri). `src/config.py`. *Calibrare:* pattern-ul
  fail-open al kill-switch-urilor e decizie de design documentată, NU defect; testarea matricei de
  combinații = nefezabil/ne-necesar. **Fix:** o singură aserțiune că flag-urile de SIGURANȚĂ
  (`validator_*_enabled`, `moderation_enabled`) au default=True + eventual guard de boot cu WARNING
  dacă un validator e OFF în `env=prod`.
- **#29 `low`** — `PROJECT_STATUS.md` stale (~28 PR-uri; listează #62-65 „în review", proiectul e
  la ~#130). `docs/PROJECT_STATUS.md:154-172`. **Fix:** regenerează din `git log` +
  `scripts/mvp_audit.py` SAU arhivează explicit și mută starea în `docs/ARCH-2026-ROADMAP.md`.

---

## 4. Quick wins (small effort, impact bun)

| # | Fix | De ce acum |
|---|-----|------------|
| **#15** | Elimină `_vec()`, trimite `list[float]` direct + integration test pe bot_pool | **Cel mai mare ROI din audit** — repară cache/FAQ mort în prod |
| **#9** | `conv_lock_ttl_seconds` 30→180-300s (1 linie config) | Elimină majoritatea turelor concurente + StateConflict + cost irosit |
| **#13** | Normalizează separator de mii în `_PRICE_RE` + caz de test | Închide gaura structurală pe invariantul anti-halucinație |
| **#5** | `==` → `hmac.compare_digest` pe verify_token | Trivial, consecvență timing-safe |
| **#2** | `format_price(value, currency)` unic + pasează currency în `agent.py:508` | Repară valuta non-RON; RON byte-identic |
| **#4** | `update conversation_summaries set summary=null` în `gdpr_erase_contact` | Închide reziduul GDPR cu o linie SQL |
| **#27** | mypy non-strict + ruff `B`/`UP` în CI | ~30s/PR, prinde o clasă întreagă de regresii pe TurnContext |
| **#31** | `--cov-fail-under=N` în CI | Aproape zero cost, transformă găurile în semnal |

---

## 5. Roadmap recomandat (3 valuri, stil NX-XX)

### Val 0 — ACUM (hotfix; gata de prod după, ~1 sprint scurt)
1. **NX-15a** (`#15`, critical) — uniformizare contract vector `list[float]` + integration test pe
   `get_bot_pool()` + event distinct la eroare lookup. *Repară cache/FAQ mort în prod.*
2. **NX-40** (`#16`, high) — job partiții lunare idempotent + **migrare delta buffer
   `2026-08..2026-12`**. *Hard deadline: înainte de 2026-07-31.*
3. **NX-94a** (`#3`, high) — secret per-tenant din `channels` ȘI/SAU `business_id` în mesajul
   semnat la webhook orders + corectează comentariile false. *Atribuire de venit cross-tenant.*
4. **NX-9b** (`#9`, small) — aliniază lock TTL la ~180-300s. *Quick win care dezamorsează race-ul.*

### Val 1 — Următorul sprint (P6 + invarianți + igienă CI)
5. **NX-85b** (`#8` + `#12`) — retry mărginit pe `StateConflict` (extinde NX-85, cardat în
   `ARCH-2026-ROADMAP.md:96`) + event `conversation_lock_dropped` + fallback P6.
6. **NX-13b** (`#13`, small) — separator de mii în validator + caz `tests/hallucination`.
7. **NX-10b** (`#10`) — gardă la re-claim dispatcher (verifică `provider_id` existent înainte de
   re-send) sau documentare explicită a ferestrei reziduale.
8. **NX-CI-types** (`#27` + `#31`, small) — mypy non-strict + ruff `B`/`UP` + `--cov-fail-under`.
9. **Quick-wins bundle** (`#5`, `#2`, `#4`) — timing-safe verify + `format_price` + GDPR summaries;
   un singur PR de igienă.

### Val 2 — Mai târziu (hardening, perf, datorie pre-scalare)
10. **Perf bundle** (`#22` wakeup dispatcher, `#23` memoizare embed, `#24` SELECT slim pe fuziune,
    `#25` typing per debounce).
11. **Pre-scalare** (`#11` gărzi `status='dispatching'`, `#21` buget conexiuni configurabil) —
    necesare ÎNAINTE de multi-replica.
12. **DB hygiene** (`#18` un singur `src/db/vector.py`, `#19` paritate model în search, `#17`
    coloane `faq_hits`/`alias_hits`, `#20` GIN pe `retrieval_signature`).
13. **Test assets** (`#30` cablare suită halucinație nightly, `#28` unit dispatcher backoff, `#26`
    referințe fantomă, `#32` aserțiune default-sigur flag-uri).
14. **Doc drift** (`#29` regenerare `PROJECT_STATUS.md`, `#1` clamp_state SAU corectare doc P4,
    `#7` hardening normalizare anti-injection, `#14` ștergere cod mort `_budget`).

---

## Anexă — findings respinse la verificare (20)

Findings-urile inițiale respinse ca **decizii de design intenționate respectate corect** sau
înțelegeri greșite ale codului. Nu necesită acțiune — păstrate pentru transparența auditului:
dedupe în 2 straturi, stream Redis unic, `admin_conn` pentru lookup canal, session-mode 5432
direct pentru izolare, fail-open kill-switches, validator de output ca apărare load-bearing,
etc. (Lista completă în trace-ul workflow-ului `wf_184f2218-775`.)
