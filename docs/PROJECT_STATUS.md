# Nativx Assistant — Status proiect

_Actualizat: 2026-06-14 · Bază: `main` la zi (PR #1–#45 + W1) · Document VIU — se
actualizează la fiecare milestone; data stă aici, nu în numele fișierului._

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

## 2. Ce e în main (până la PR #45 + W1)

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
| **W1: carduri compacte** (listă text + butoane-link) | ✅ live | (acest PR) |

Fundațiile anterioare (infra, schema v2 + RLS 003, config/pool/models,
search_products SQL) — vezi istoricul PR #1–#18.

## 3. Stadiu pe pipeline (cele 9 stagii)

| # | Stagiu | Stare |
|---|---|---|
| 1 | Webhook: GET verify + POST inbound | ✅ live |
| 2 | Redis backbone: stream + consumer group + dedupe 2L | ✅ live (TODO: debounce, lock multi-consumer, rate limit, cost guard) |
| 3 | Gates (bot_active, handoff, limbă, risc, media) | ❌ |
| 4 | Straturi gratuite (alias, cache semantic, clarificare) | ❌ (faqs=0, cache=0) |
| 5 | Triaj (nano) | ✅ **live** (simple/clarify răspund, sales/order → agent) |
| 6 | Context builder | ✅ istoric conversație în triaj+agent (follow-up „mai ieftin"); profil/state/summarizer ulterior |
| 7 | Agent (mini) + search semantic | ✅ **live** (RAG: embed query → cosine + filtru preț; tool-calling complet = refinement) |
| 8 | Validator | ✅ inline în agent (zero prețuri inventate; retry → fallback determinist) |
| 9 | Sender → outbox → dispatcher (+ carduri W1) | ✅ **live cap-coadă** |
| — | Status webhook (delivered/read/failed) | ✅ |
| — | Proactiv / Jobs (embed, rollup, cleanup partiții) | ❌ (doar `cleanup_dedupe` ✅) |

## 4. Starea DATELOR demo (verificat live în Supabase, 2026-06-14)

business_id `6098812a-50fc-44bd-a1ba-bc77e6399158` (slug `nativex-demo`):

| Tabel | Count | Notă |
|---|---|---|
| `products` | 500 | seedate; prețuri normalizate (outlier 11M reparat) |
| `product_embeddings` | **500/500** ✅ | embed pe descriere+concerns (1536 dim); incremental pe content_hash |
| `products.product_url` | **500** ✅ | generate din slug (`shop.sole-demo.ro/p/<slug>`) |
| `products.ai_summary` + `attributes.concerns` | **500** ✅ | descrieri reale + concerns filtrabile (LLM enrichment) |
| `product_images` | 2500 | placehold.co `.png` (fixat din SVG pt carduri) |
| `reviews` | 953 | nesumarizate încă (D3 = rezumate → product_review_summaries) |
| `faqs` | 0 | stratul gratuit FAQ gol |
| `channels` | telegram ✅ | `@solechat_bot` seedat; WhatsApp încă 0 (cere T013) |

## 5. Riscuri & datorie tehnică (curente)

1. **NX-50 livrat (cod) — `bot_runtime` rol de LOGIN** (P0-A audit). Două pool-uri:
   tenant path = login direct `bot_runtime` (zero `SET ROLE` → fără scurgere sub
   multiplexare); control plane = `admin_conn` privilegiat (`docs/db_connections.md`).
   **Rămâne:** provisioning manual (`apply_005.py` + `DATABASE_URL_BOT`) — până atunci
   codul cade grațios pe modul compat. NX-04 (assert la checkout) **livrat** —
   `tenant_conn` ridică `IsolationError` dacă rolul/scope-ul nu-s corecte, zero
   overhead (combinat în set_config). NX-53 (test concurent) se construiește peste.
2. **R1 — Debounce lipsă** (stagiul 2): mesajele succesive = tururi separate →
   răspunsuri redundante. Vezi `docs/REFINEMENTS.md`. P1 la hardening worker.
3. **Limita de spend OpenAI NEPUSĂ** — protecția financiară (dashboard) e încă de
   făcut (T017). Cost real mic (enrichment+embed one-time ~$0.5; per tur fracțiuni).
4. **Agentul e RAG, nu tool-calling** — fără context istoric (stagiul 6), fără
   compară/detalii (max-3-tools). Suficient pt demo; refinement ulterior.
5. **Dedupe claim-first:** crash între claim și finalizarea turului = mesaj marcat
   văzut dar neprocesat. Dead-letter / reaper = follow-up.
6. **`get_or_create_conversation` race teoretic** pe primul mesaj al unui contact
   nou (fără unique pe open-conv). Advisory lock = follow-up.
7. **Tokenul Telegram în loguri** — silențiat în #38; `/revoke` la BotFather pt zero risc.
8. **DB password gotcha** (#39): parola din `SUPABASE_DB_URL` percent-encoded sau
   asyncpg crapă în container. Documentat în README.

## 6. Ce urmează (ordine recomandată)

1. **D3 — rezumate recenzii** (953 → `product_review_summaries`, LLM) → botul
   menționează rating + ce laudă clienții. Wow ieftin.
2. **R1 debounce + R2 carduri „pro"** (carusel Telegram / List Messages WhatsApp) —
   vezi `docs/REFINEMENTS.md`.
3. **NX-04/53** (assert la checkout + test concurent) — peste NX-50 (livrat).
   Plus provisioning manual NX-50 (`apply_005.py` + `DATABASE_URL_BOT`).
4. **G5**: gates (limbă, handoff, risc), straturi gratuite (alias, cache semantic).
5. **WhatsApp e2e** (T013/T015 manual) + deploy VPS pentru rulare continuă.

**Milestone atins (2026-06-14):** „bot care VINDE și ține firul" — triaj + agent +
carduri + **memorie de conversație** (follow-up „mai ieftin"), pe catalog de calitate.
Următorul: rezumate recenzii + debounce.

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
