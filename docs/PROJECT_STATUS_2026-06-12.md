# Nativx Assistant — Status proiect & analiză

_Data: 2026-06-12 · Autor: sesiune Claude Code · Bază: `main` la zi (0 PR-uri deschise)_

Document de referință pentru: (1) ce e implementat și în ce stadiu, (2) o analiză
critică de design (ce e bine gândit / ce e risc), (3) ce urmează — material pentru
generarea următoarelor taskuri.

---

## 1. Executive summary

- **Faza infra (Z1) e completă** + a început **stratul de date**, funcțional end-to-end.
- **Decizie majoră de arhitectură:** schema DB reală este `docs/schema_v2_production.sql`
  (o schemă plată, deja seedată în Supabase), NU modelul cu 4 scheme din planul inițial.
  Toate cardurile de migrare (T021–T034) au devenit **obsolete**. Vezi `schema_reference.md`.
- **Plasa de securitate RLS** (`bot_runtime` + `app.business_id`) e aplicată ȘI testată
  pe DB-ul live: izolare cross-tenant dovedită (alt tenant → 0 rânduri).
- **Lanțul de date e dovedit:** `config → pool (RLS) → tenant_conn → search_products`,
  testat pe 500 produse reale.
- Restul pipeline-ului (webhook POST, worker, triaj, agent, sender) **nu e început** —
  e faza următoare, fără carduri încă.

**Unde suntem pe drum:** fundațiile (infra + DB + contract) sunt puse și verificate.
Urmează construcția fluxului de mesaj propriu-zis.

---

## 2. Ce e implementat (cu mapare la taskuri)

### Infrastructură & proces (Z1) — ✅ complet
| Task | Livrat |
|---|---|
| T001–T002 | Repo privat, .gitignore, branch protection |
| T003 | PR template + CODEOWNERS (review Senior pe `prompts/`, `docs/*.sql`) |
| T004–T005 | CI: ruff (lint+format) + pytest, required checks pe main |
| T006 | requirements.txt (runtime) + requirements-dev.txt |
| T007 | .env.example complet |
| T008 | Schelet `src/` complet + test_imports |
| T009–T010 | docker-compose dev + Dockerfile multi-stage (scrise; build de verificat pe VPS — fără Docker local) |
| T019 | README onboarding (pași reali, troubleshooting) |
| + | CONTRIBUTING.md, `.claude/settings.json` (permisiuni) |

### Bază de date — ✅ fundație funcțională
| Componentă | Stare |
|---|---|
| **Schema v2** (49 tabele, partiționare, pgvector) | aplicată + seedată (500 produse demo) |
| **T020** reconciliere schemă + `schema_reference.md` | ✅ harta numelor reale |
| **003** rol `bot_runtime` + RLS (`app.business_id`) + guard 8KB | ✅ APLICAT + TESTAT live |
| `src/config.py` (Pydantic settings) | ✅ + teste unit |
| `src/db/connection.py` (pool asyncpg + `tenant_conn`) | ✅ + teste integration |
| `src/models.py` (TurnContext + dataclass-uri) | ✅ + teste |
| `src/db/queries/catalog.py` → `search_products` (filtre SQL) | ✅ + teste integration |
| **T037** spot-check date (raport) | ✅ date curate; a prins fix-ul de preț |
| Scripturi: `db_check.py`, `apply_003.py`, `spot_check.py` | ✅ utilitare DB |

### Webhook — 🟡 parțial
| Task | Stare |
|---|---|
| T014 `GET /webhook` (verify Meta) | ✅ + teste |
| POST inbound (semnătură, dedupe, push Redis) | ❌ TODO |

### Manuale (Adi) — vezi `TODO-MANUAL.md`
- ✅ T018 (Supabase: pooler, RLS aplicat; backups pe Free — Pro înainte de go-live)
- ❌ T013 (Meta app), T015 (tunel), T016 (verif. business), T017 (OpenAI key)

---

## 3. Analiză de design — ce e bine, ce e risc

### ✅ Bine gândit / verificat
1. **Izolarea multi-tenant e dublă și testată.** `WHERE business_id = $1` explicit în cod
   (mecanism primar) + RLS `bot_runtime`/`app.business_id` (plasă). Dovedit live: un query
   fără filtru → 0 rânduri, nu datele altui client. Exact principiul 7.
2. **`search_products`** — parametrizat (zero injection), hard cap 6, 8 câmpuri, preț din
   variantă (corect după T037). Filtre SQL acum, ranking semantic pregătit pentru embeddings.
3. **Contract central curat** — `TurnContext` cu owner documentat per câmp (un singur scriitor),
   helperi `emit()`/`set_reply()` (stagiile nu știu cum sunt măsurate — principiul 10).
4. **Config disciplinat** — totul prin `Settings`, nimic din `os.environ` direct în cod.
5. **CI ca poartă** — ruff + pytest required; CODEOWNERS forțează review pe SQL/prompts.
6. **Schema reconciliată onest** — în loc să scriem migrări peste o schemă greșită, am aliniat
   documentația la realitate și am marcat obsoletele.

### ⚠️ Riscuri / datorie tehnică (de adresat)
1. **`product_url` NULL pe toate produsele** (gap seed). Consecințe: botul nu poate trimite
   linkuri; `checkout_link` n-are URL de bază; validatorul de linkuri (principiul 8) va trebui
   să tolereze lipsa URL în demo. → la sync cu magazin real se populează.
2. **`ai_summary` fictiv/templat** — copy-ul de vânzare al agentului va fi subțire pe date demo.
   OK pentru testat pipeline-ul; la client real vine din sync + LLM.
3. **`taxonomy` nu există** — promptul agentului (principiul 9) se va genera din `categories`
   (+ `intent_aliases`). De confirmat la implementarea `prompt_builder`.
4. **Embeddings = 0** — `search_products` semantic nu există încă; depinde de `OPENAI_API_KEY`
   (T017, manual) + jobul `embed_products`.
5. **SSL `CERT_NONE` pe Windows dev** — workaround pentru conectarea pe IP (bug DNS asyncpg).
   Doar pentru dev local; prod (Linux) folosește DSN cu verificare normală. Risc dacă cineva
   rulează „prod" pe Windows — de păzit în deploy.
6. **`statement_cache_size=0`** pe pooler (corect pentru pgbouncer transaction-pooling, dar
   pierde un pic de performanță). Acceptabil; de reevaluat dacă trecem pe conexiune directă.
7. **Proces git:** de 2 ori commit-uri au rămas orfane (PR merged la primul commit, follow-up
   pierdut). Recuperat de fiecare dată. Recomandare: 1 PR = 1 commit logic complet, SAU
   verifică `gh pr view N --json state` înainte de push-ul follow-up. (Notat în memorie.)

### 🔵 Decizii deschise (de confirmat la implementare)
- Cum se face login-ul workerului pe VPS: pe Supabase prin pooler ne conectăm ca `postgres`
  și facem `SET ROLE bot_runtime`. Funcționează; de validat sub încărcare reală.
- `clarification_templates` / `knowledge_guides` nu există în schema_v2 — clarificările vin
  din cod/prompt. De decis dacă adăugăm tabele sau rămân în cod.

---

## 4. Stadiu pe pipeline (cele 9 stagii)

| # | Stagiu | Stare |
|---|---|---|
| — | **Fundații** (config, connection+RLS, models) | ✅ gata |
| 1 | Webhook: GET verify | ✅ / POST inbound ❌ |
| 2 | Redis backbone (stream, lock, debounce) | ❌ |
| 3 | Gates (bot_active, handoff, identity, language) | ❌ |
| 4 | Straturi gratuite (alias, semantic_cache, clarify) | ❌ (faqs=0, cache=0) |
| 5 | Triaj (nano) | ❌ |
| 6 | Context builder (buget 8KB, summarizer) | ❌ |
| 7 | Agent (mini) + tools | 🟡 doar `search_products` (SQL) |
| 8 | Validator | ❌ |
| 9 | Sender + dispatcher (outbox → Meta) | ❌ |
| — | Proactiv (scheduler, templates, 24h window) | ❌ |
| — | Jobs (embed_products, rollup_usage, cleanup) | ❌ |
| — | LLM adapter (OpenAI) + prompt_builder | ❌ |

---

## 5. Ce urmează — grupuri pentru generarea taskurilor

Ordonate ca dependențe. Fiecare grup = câteva carduri.

**G1. Queries DB (fundație, testabil local pe DB real)**
- `contacts`: get_or_create + identity resolution prin `channel_identities`
- `conversations`: load/create + patch `state` (cu `state_version`, optimistic lock)
- `messages`: insert inbound/outbound + istoric (max 8); dedupe pe `provider_msg_id`
- `outbox`: enqueue tranzacțional + claim cu `FOR UPDATE SKIP LOCKED`

**G2. Webhook inbound + worker (scheletul fluxului)**
- POST `/webhook`: validare semnătură Meta, dedupe, update `last_inbound_at`, push Redis
- worker `consumer`: Redis stream + lock per conversație + debounce
- `runner`: execută stagiile în ordine, scrie observabilitatea (analytics_events)

**G3. LLM adapter + primul LLM**
- adapter OpenAI (mini/nano/embed) cu retry + măsurare tokens/cost — **necesită T017**
- stagiul Triaj (nano): clasificare + validare Pydantic
- `prompt_builder` din `categories`

**G4. Agent + tools + validator + sender (completează turul)**
- agent runner (max 3 tool calls), tool_definitions
- tools rămase: get_product_details, compare_products, check_order, checkout_link, faq_lookup
- validator (preț/produs/link din retrieval)
- sender → outbox → dispatcher → Meta + status webhook

**G5. Straturi gratuite + jobs**
- gates, free_layers (alias/cache/clarify)
- context_builder (buget + summarizer)
- `embed_products` (deblochează search semantic) — **necesită T017**
- `rollup_usage`, `cleanup`

**G6. Proactiv**
- scheduler (`proactive_jobs`), templates (24h window + consent)

---

## 6. Recomandare de ordine

1. **Adi: T017 (cheie OpenAI)** — deblochează LLM + embeddings, e pe drumul critic.
2. **G1 (queries DB)** — fundație pură, testabilă local acum, fără dependențe externe.
3. **G2 (webhook POST + worker + runner)** — scheletul prin care curge un mesaj.
4. **G3 (LLM adapter + triaj)** — primul punct LLM, după cheia OpenAI.
5. **G4 (agent + tools + validator + sender)** — închide turul cap-coadă.
6. **G5/G6** — straturi gratuite, jobs, proactiv.

**Milestone țintă:** un mesaj inbound → procesat prin pipeline → răspuns ieșit prin outbox.
Primul „echo e2e" se poate atinge după G1+G2 (fără LLM, doar cu un răspuns determinist).

---

## 7. Sumar PR-uri livrate azi (main)

#3 T003 · #4 T006 · #5 T007 · #6 T008 · #7 T010 · #8 T009 · #9 T014 · #10 T020 ·
#11 config+connection · #12 models · #13 search_products · #15 fix preț+T037 ·
#16 README · #17 recovery 003 · (#14 închis ca redundant).
