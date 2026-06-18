# Nativx Assistant — Status proiect

_Actualizat: 2026-06-16 · Bază: `main` la zi (PR #1–#61: + NX-15 moderation, G5c
detecție de limbă, G7-1 agent tool-calling, G2c cost guard + rate limit) · **În review:**
#62 G8-1 evals/golden CI, #63–#65 F2 bucla de bani (checkout_link → atribuire comenzi →
usage_daily) · Document VIU — se actualizează la fiecare milestone; data stă aici,
nu în numele fișierului._

Document de referință pentru: (1) ce e implementat și în ce stadiu, (2) riscuri
și datorie tehnică, (3) ce urmează — material pentru generarea taskurilor.

---

## 1. Executive summary

- **🎉 Milestone „Telegram echo e2e LIVE" ATINS.** Un mesaj REAL trimis botului
  pe Telegram (`@solechat_bot`) parcurge tot lanțul și primește răspuns automat:
  `poller (long polling) → Redis stream → worker → pipeline → outbox →
  dispatcher → Telegram`. Pe infrastructură reală (Supabase + Redis + Docker
  local), nu doar în teste. Arhitectura canal-agnostică e validată în practică.
- **Capătul de ieșire FUNCȚIONEAZĂ.** Dispatcher live (outbox → canal),
  idempotent, cu retry/backoff + dead-letter + reaper implicit (visibility
  timeout). Canal-agnostic (NX-60): WhatsApp (Meta) și Telegram (test) prin
  `ChannelSender` registry — alegere după `channel_kind`.
- **OpenAI verificat** (T017 parțial). Cheia validă + cele 3 modele
  (`gpt-5.4-mini`/`nano`, `text-embedding-3-small`) confirmate live cu
  `scripts/check_openai.py`. **Rămâne** de pus limita de spend în dashboard.
- **🎉 Botul VINDE (G3+G4 LIVE, 2026-06-14).** Triaj (nano) clasifică intenția →
  agent (mini) caută **semantic** în catalog → recomandă **produse reale cu prețuri
  validate** (zero prețuri inventate) + **carduri compacte** (text + butoane-link)
  pe Telegram. Ex. „caut o cremă pentru ten uscat sub 80 lei" → recomandări fix pe
  nevoie. Echo rămâne doar fallback.
- **Catalog de calitate.** 500 produse cu descrieri reale + `concerns` filtrabile
  (LLM enrichment), prețuri realiste, `product_url`, **500/500 embeddings**.
- **Stack-ul rulează LOCAL.** Docker Desktop + WSL2 instalate 2026-06-13 →
  `docker compose up` pornește redis + worker + dispatcher + telegram-poller +
  webhook pe Windows. (VPS rămâne ținta de producție.)

## 2. Ce e în main (până la PR #61)

| Componentă | Stare | PR |
|---|---|---|
| G1: queries runtime (contacts, conversations, messages, outbox) | ✅ | #19 |
| G2a: POST /webhook (semnătură, parser Meta, dedupe L1, XADD) | ✅ | #20 |
| G2b: consumer + runner + processor (echo) | ✅ | #21 |
| NX-51: dedupe layer 2 (`inbound_dedupe`, 004 aplicat live) | ✅ | #23 |
| Dispatcher (outbox → canal, retry, dead-letter) | ✅ | #25 |
| Status webhook (delivered/read/failed → `messages.status`) | ✅ | #26 |
| NX-02: Redis durabil + securizat (AOF, parolă, noeviction) | ✅ | #27 |
| Observabilitate: `analytics_events` persistate din runner | ✅ | #28 |
| NX-60: abstracție de canal (envelope neutru + ChannelSender registry) | ✅ | #30 |
| NX-61/62: Telegram inbound (poller) + outbound (TelegramClient) | ✅ | #31 |
| NX-63: onboarding canal Telegram demo (seed + upsert_channel) | ✅ | #33 |
| Fix verify-token din settings (nu os.environ) | ✅ | #34 |
| T017: `check_openai.py` + notare spend ca TODO | ✅ | #35, #36 |
| Fix seed rulabil direct (sys.path) | ✅ | #37 |
| Fix worker: entrypoint `__main__` + log per-tur + silențiere token | ✅ | #38 |
| `.gitignore .env.bak*` + doc gotcha parolă DB | ✅ | #39 |
| Docs refresh complet | ✅ | #40 |
| **G3: adaptor OpenAI + Triaj (nano)** | ✅ live | #41 |
| Catalog: normalize (preț/url) + enrich (descrieri+concerns, LLM) | ✅ | #42, #43 |
| **P1: embed_products** (product_embeddings=500) | ✅ | #44 |
| **G4: agent (mini) + search semantic + validator preț** | ✅ live | #45 |
| **W1: carduri compacte** (listă text + butoane-link) | ✅ live | #46 |
| **G6: context builder** (istoric conversație, follow-up-uri) | ✅ live | #47 |
| **D3: rezumate recenzii** (rating + laude; `product_review_summaries` 500/500) | ✅ live | #48 |
| **R1: debounce** (mesaje rapide ale aceluiași user → un singur tur) | ✅ live | #49 |
| **NX-50: worker pe rol de login `bot_runtime`** (două pool-uri) | ✅ | #50 |
| **NX-04: assert izolare la checkout** (fail-fast `IsolationError`) | ✅ | #51 |
| **NX-53: test izolare sub concurență** (50 tururi paralele) | ✅ | #52 |
| **R2: carusel de produse pe Telegram** (callback_query + editMessageMedia) | ✅ live | #53 |
| **G5a: gates** (bot_active + handoff + risc → request_human) | ✅ live | #54 |
| **G5b-1: cache semantic** (lookup exact+semantic, write-back static) | ✅ live | #55 |
| **G5b-2: invalidare cache + caching dynamic** (price-check + data_version) | ✅ live | #56 |
| Sync PROJECT_STATUS (#46–#55 livrate) | ✅ | #57 |
| **NX-15: moderation gate** (mesaj flagged → răspuns neutru + abuse blocklist) | ✅ live | #58 |
| **G5c: detecție de limbă** RO/HU/EN în Gates (stagiul 3) | ✅ live | #59 |
| **G7-1: agent tool-calling** (framework + search/details/compare, cap dur 3) | ✅ live | #60 |
| **G2c: cost guard + rate limit** (contoare Redis, fail-open) | ✅ live | #61 |

Fundațiile anterioare (infra, schema v2 + RLS 003, config/pool/models,
search_products SQL) — vezi istoricul PR #1–#18.

### În review (PR-uri deschise, nu încă în main)

| Componentă | PR |
|---|---|
| **G8-1: evals — golden harness + gate CI** (regresii de pipeline) | #62 |
| **F2-1: `checkout_link`** — link de cumpărare atribuibil (`?ref=`) | #63 |
| **F2-2: atribuire comenzi** — webhook → match `ref_code` → `orders.attribution` (stacked pe #63) | #64 |
| **F2-3: usage_daily rollup** — analytics+messages+orders → `usage_daily` | #65 |

## 3. Stadiu pe pipeline (cele 9 stagii)

| # | Stagiu | Stare |
|---|---|---|
| 1 | Webhook: GET verify + POST inbound | ✅ live |
| 2 | Redis backbone: stream + consumer group + dedupe 2L + **debounce (R1)** + **rate limit & cost guard (G2c #61)** | ✅ live (TODO rămas: lock multi-consumer, XAUTOCLAIM) |
| 3 | Gates (bot_active, handoff, risc, **moderare NX-15 #58**, **limbă RO/HU/EN G5c #59**) | ✅ **live** · ❌ rămas: media routing (STT/Vision) |
| 4 | Straturi gratuite (cache semantic, FAQ, alias, clarificare) | ✅ **cache live** (static G5b-1 + dynamic G5b-2 #56) · ✅ **FAQ live (NX-74)**: `faq_stage` (cosine pe `faqs`, business_id+locale, doar embed) + tool `faq_lookup` pe SALES + job `seed_faqs` (bază curatată RO + generare LLM) — RULEAZĂ seed-ul pe demo ca să servească (`faqs=0` până atunci) · ❌ rămas: **alias lookup** (`intent_aliases`) |
| 5 | Triaj (nano) | ✅ **live** (simple/clarify răspund, sales/order → agent) |
| 6 | Context builder | ✅ istoric conversație în triaj+agent (follow-up „mai ieftin"); profil/state/summarizer ulterior |
| 7 | Agent (mini) + **tool-calling (G7-1 #60)** | ✅ **live** (search/details/compare, cap dur 3 apeluri; + `checkout_link` în review #63). **NX-98:** `search_products` cade determinist pe **SQL-only** (`name ilike`) când tenantul n-are embeddings / LLM / semantic gol → niciodată „indisponibil" structural (P6); `mode=sql_only` în analytics = semnal de embed lipsă. **NX-78:** system prompt-ul agentului e GENERAT din `categories` (+ `intent_aliases` aprobate) per (business, locale) prin `src/agent/prompt_builder.py` — ZERO vertical hardcodat (P9); prefix byte-identic + `lru_cache` → prompt caching OpenAI |
| 8 | Validator | ✅ inline în agent (zero prețuri/linkuri inventate; retry → fallback determinist; link de checkout permis doar dacă botul l-a generat, #63) |
| 9 | Sender → outbox → dispatcher (+ carduri W1) | ✅ **live cap-coadă** |
| — | Status webhook (delivered/read/failed) | ✅ |
| — | Proactiv / Jobs (embed, rollup, cleanup partiții) | `cleanup_dedupe` ✅ · `embed_products` ✅ · **`rollup_usage` în review (#65)** · cleanup partiții ❌ |
| — | **Cost obs (NX-78 follow-up)** | ✅ adaptorul captează usage-ul OpenAI (prompt/completion/**cached_tokens**) → event `llm_usage` din runner → `analytics_events` → `rollup_usage` agregă în `usage_daily` (+ coloană `cached_tokens`, migrare 010). Tarife în `src/agent/pricing.py` (estimări). Arată economia din prompt caching (NX-78) |

## 4. Starea DATELOR demo (verificat live în Supabase, 2026-06-15)

business_id `6098812a-50fc-44bd-a1ba-bc77e6399158` (slug `nativex-demo`):

| Tabel | Count | Notă |
|---|---|---|
| `products` | 500 | seedate; prețuri normalizate (outlier 11M reparat) |
| `product_embeddings` | **500/500** ✅ | embed pe descriere+concerns (1536 dim); incremental pe content_hash |
| `products.product_url` | **500** ✅ | generate din slug (`shop.sole-demo.ro/p/<slug>`) |
| `products.ai_summary` + `attributes.concerns` | **500** ✅ | descrieri reale + concerns filtrabile (LLM enrichment) |
| `product_images` | 2500 | placehold.co `.png` (fixat din SVG pt carduri) |
| `reviews` | 953 | brute (fictive, formulaice) — sursă pt D3 |
| `product_review_summaries` | **500/500** ✅ | D3 (#48): summary + top_pros + sentiment; `products.rating` variat 4.3–4.9 |
| `semantic_cache` | warming | G5b-1 live: 006 + write-back static; se încălzește din răspunsuri |
| `faqs` | 0 | stratul gratuit FAQ gol (cache-ul se încălzește oricum din răspunsuri) |
| `intent_aliases` | 0 | gol până vine shadow mode (alias lookup n-are ce servi încă) |
| `channels` | telegram ✅ | `@solechat_bot` seedat; WhatsApp încă 0 (cere T013) |

## 5. Riscuri & datorie tehnică (curente)

1. **NX-50 livrat (#50) — `bot_runtime` rol de LOGIN** (P0-A audit). Două pool-uri:
   tenant path = login direct `bot_runtime` (zero `SET ROLE` → fără scurgere sub
   multiplexare); control plane = `admin_conn` privilegiat (`docs/db_connections.md`).
   NX-04 (assert la checkout, #51) + NX-53 (test concurent, #52) **livrate** peste.
   **Rămâne DOAR pasul de deploy:** provisioning (`apply_005.py` + setarea
   `DATABASE_URL_BOT` în env-ul de prod) — până atunci codul cade grațios pe compat.
2. **R1 — Debounce LIVRAT (#49):** mesajele rapide ale aceluiași user se coalescă
   într-un singur tur (`worker/debounce.py`). Risc închis.
3. **Cost guard + rate limit LIVRATE (G2c #61):** plafon zilnic per business (contor Redis,
   peste prag → LLM dezactivat, degradare grațioasă) + rate limit per contact. Fail-open.
   **Rămâne** limita de spend la nivel de cont OpenAI (dashboard, T017) ca a doua plasă.
4. **Agentul e tool-calling LIVRAT (G7-1 #60):** decide ce unealtă cheamă (search/details/
   compare, cap dur 3). Profil/state/summarizer (context builder bogat) rămân follow-up.
   Bucla de bani (`checkout_link` → atribuire → usage_daily) = F2, în review (#63–#65).
5. **Dedupe claim-first:** crash între claim și finalizarea turului = mesaj marcat
   văzut dar neprocesat. Dead-letter / reaper = follow-up.
6. **`get_or_create_conversation` race teoretic** pe primul mesaj al unui contact
   nou (fără unique pe open-conv). Advisory lock = follow-up.
7. **Tokenul Telegram în loguri** — silențiat în #38; `/revoke` la BotFather pt zero risc.
8. **DB password gotcha** (#39): parola din `SUPABASE_DB_URL` percent-encoded sau
   asyncpg crapă în container. Documentat în README.

## 6. Ce urmează (ordine recomandată)

> NB (2026-06-16): NX-15, G5c, G7-1, G2c sunt DEJA în main (vezi §2); F2 (bucla de bani)
> + G8-1 evals sunt în review (#62–#65). Mai jos = ce rămâne GENUIN nefăcut, verificat în cod.

1. **Merge #62–#65** (evals/golden CI + bucla de bani) → main. Apoi:
   **provisioning checkout** (`CHECKOUT_BASE_URL` / `businesses.settings["checkout_url"]` +
   `ORDERS_WEBHOOK_SECRET`) ca linkurile + atribuirea să meargă pe demo — vezi TODO-MANUAL.
2. **Context builder bogat** (stagiul 6): profil compact + state + summarizer conversații lungi.
3. **Alias lookup** (stagiul 4): `intent_aliases` (phrase_norm → rută). GOL până vine shadow
   mode → valoare imediată mică; de făcut împreună cu shadow.
4. **Limita de spend OpenAI** (T017) — a doua plasă financiară, la nivel de cont (dashboard),
   peste cost guard-ul aplicativ (G2c #61).
5. **WhatsApp e2e** (T013/T015 manual: phone_number_id Meta) + deploy VPS continuu
   (+ provisioning `bot_runtime`: `DATABASE_URL_BOT`).
6. **Backlog NX** (`tasks/NX_backlog_compact.md`): NX-03 alerte lag/outbox,
   NX-07 pacing proactiv, NX-41 create_tenant, etc.

**Milestone atins (2026-06-16):** bot care VINDE cu **tool-calling** (G7-1), ține firul,
menționează recenzii, coalescă mesaje rapide, escaladează la om, detectează limba (RO/HU/EN),
moderează abuzul, servește din cache semantic, și e protejat de **cost guard + rate limit**.
**În review:** evals/golden CI (G8-1) + **bucla de bani** (checkout_link → atribuire comenzi →
usage_daily). Următorul: merge F2 + provisioning checkout / context builder bogat.

## 7. Decizii de arhitectură luate pe parcurs

- **Stream unic `inbound`** (nu per conversație): conversation_id nu e cunoscut la
  webhook fără DB; ordinea per conversație se rezolvă în worker (lock, TODO).
- **`admin_conn` (control plane)** — excepție unică documentată de la „business_id
  pe tot": lookup canal→business precede tenantul.
- **Dedupe NU pe `messages`** — unique-ul include cheia de partiționare; 2 straturi
  (Redis + `inbound_dedupe`).
- **Abstracție de canal la 2 margini (NX-60)** — parser ingestie → envelope neutru;
  `ChannelSender` registry la trimitere. Pipeline-ul/worker-ul nu știu de canal.
- **Parola DB percent-encoded** — `urlparse` (Windows) tolerează `@` neescapat,
  asyncpg (container) nu → encode obligatoriu pentru paritate local/prod.
- **Dev local containerizat** — Docker Desktop + WSL2 pe Win10 Home; `docker compose`
  rulează tot stack-ul local (înainte: doar pe VPS).
