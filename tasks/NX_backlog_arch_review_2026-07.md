# Backlog compact — Architecture Review 2026-07 (P2 + delta fiabilitate)

Format: context → scop → DoD → dependențe. Cardurile FULL ale review-ului (NX-142…NX-148, elementele
P1 A4–A10) sunt în fișiere separate. Aici: elementele P2 (A11–A15) + delta de fiabilitate peste NX-140.
Index + mapare completă: [`docs/ARCH-REVIEW-2026-07.md`](../docs/ARCH-REVIEW-2026-07.md).
Deja carduite din review: **A1/A2 → NX-140**, **A3 → NX-141**.

---

## NX-149 · Docs & docstrings stale + rerun arch explorer (A11 · P2 · XS · 2h)
**Context:** audit §5.2/§8 — docstring runner descrie 9 stagii, codul are 11; `CLAUDE.md` +
`PROJECT_STATUS.md` marcate stale în memorie; `arch_explorer/` e derivat determinist din AST și trebuie
re-rulat după refactor. **Scop:** aliniază docstring-ul `run_pipeline`, secțiunea de pipeline din
`CLAUDE.md` (stagii reale), nota de date din `PROJECT_STATUS.md`, și re-rulează `arch_explorer/analyze.py`.
**DoD:** numărul de stagii din docstring == stagiile reale; `arch_explorer` regenerat (diff comis);
grep de „9 stagii" în docs = 0 unde e greșit. **Dep:** ideal după NX-142/143 (refactor schimbă structura). **Fișiere:** `src/worker/runner.py`, `CLAUDE.md`, `docs/PROJECT_STATUS.md`, `arch_explorer/`.

## NX-150 · Prompt/tool injection tests + tool authorization layer (A12 · P2 · M · 6h)
**Context:** audit §5.13 (scor 7.8). Există gate anti-prompt-injection (NX-16) + moderation (NX-15,
compact). Lipsește un **strat explicit de autorizare de tool-uri** și teste adversariale de tool injection.
Seam-ul natural = `src/agent/tool_executor.py` (NX-143), unde `business_id` se ia deja din `ctx`, nu din
`args` (`agent.py:1043`). **Scop:** (1) fiecare tool declară `allowed_routes` + `required_context` +
schema strictă; executor-ul respinge apeluri în afara politicii; (2) policy dură: modelul NU poate
seta/modifica `business_id`, `contact_id`, `price`, `availability`, `checkout amount` (se iau din
cod/DB); (3) sanitize pe conținut din catalog/FAQ injectat în prompt (descrieri de produs ca vector de
injection); (4) teste adversariale („ignoră instrucțiunile", „schimbă prețul", „datele altui client").
**DoD:** apel de tool cu `business_id` în args → ignorat/respins (test); tool chemat pe rută nepermisă →
blocat; suită adversarială verde; leagă cazuri în NX-145. **Dep:** NX-143 (tool executor extras). **Fișiere:** `src/agent/tool_executor.py`, `src/tools/*`, `tests/test_tool_authz.py`.

## NX-151 · Retrieval evals + reranking (A13 · P2 · M-L · 12h)
**Context:** audit §5.9 (scor 8.0). NX-113 a făcut hybrid (RRF + ILIKE + vector). Lipsesc: reranker
dedicat, set de evaluare, query expansion RO. **Scop:** (1) set de 100-200 query-uri reale/adversariale
(`ten gras`, `acnee`, `coșuri`, `sebum`, SKU/cod exact) cu metrici Recall@5 / Precision@3 / „has
purchasable product" / „price constraint respected"; (2) reranker cross-encoder SAU LLM-as-reranker pe
top-20 → top-5 (kill-switch, guardat de cost); (3) query expansion pe sinonime RO (din DomainPack, NU
listă hardcodată — memorie `prefer-model-context-over-wordlists`). **DoD:** eval set rulabil cu raport de
metrici; reranker îmbunătățește Precision@3 pe setul de test; expansion pe sinonime RO măsurată. **Dep:**
NX-113 (în main); `docs/PRODUCT-RANKING-ANALYSIS-2026.md`. **Fișiere:** `src/tools/search_products.py` sau retrieval core, `scripts/eval_retrieval.py`, `tests/`.

## NX-152 · Cost per intent/stage + model router simplu (A14 · P2 · M · 8h)
**Context:** audit §5.14 (scor 8.0). Există cost caps per business/contact/web + cost real post-tur
(`llm_usage`). Lipsesc vizibilitatea pe intent/stagiu și un router de model. **Scop:** (1) dashboard/raport
cost per business × intent × stagiu (din `analytics_events` + `usage_daily`); (2) model router simplu:
determinist/free pt greeting/FAQ/cache · nano pt triaj/extract facts/classify · mini pt agent sales ·
model mai puternic DOAR pt comparații complexe/escaladări premium. Routerul rămâne în cele 2 puncte LLM
(P2) — alege modelul, nu adaugă puncte noi. **DoD:** raport de cost pe intent disponibil; router alege
nano vs mini pe fixture (greeting → free, sales → mini, compare complex → upgrade); kill-switch
`model_router_enabled`. **Dep:** NX-146 (turn replay) pt atribuirea costului pe traiectorie ajută. **Fișiere:** `src/agent/*` (selecție model), `src/config.py`, `scripts/cost_report.py`.

## NX-153 · Mută migration assert din `scripts` în modul runtime (A15 · P2 · S · 2h)
**Context:** audit §5.10. `assert_migrations_current` e importat din `scripts.migrate` la RUNTIME
(`src/worker/consumer.py:315,321`) — cuplaj build/runtime fragil (imaginea trebuie să conțină `scripts/`,
exact clasa de bug NX-123/PR #132). **Scop:** mută funcția (+ `assert_migrations_current`) într-un modul
runtime, ex. `src/db/migrations_guard.py`; `scripts/migrate.py` importă din `src/` (nu invers). Poarta de
boot rămâne identică funcțional. **DoD:** `grep -rn "from scripts" src/` → 0; poarta de boot funcționează
(worker refuză start pe migrări în urmă); `ruff` + `pytest` verde. **Dep:** niciuna. **Fișiere:** `src/db/migrations_guard.py` (nou), `src/worker/consumer.py`, `scripts/migrate.py`.

---

## NX-154 · dead_letter_inbound durabil + dashboard minim pilot (A1-delta + Quick Win #5 · P1 · M · 1 zi)
**Context:** NX-140 face retry capped + fallback în outbox + `turn_failed`; auditul §5.1/§8 cere ȘI o
coadă **dead-letter durabilă vizibilă** (nu doar log CRITICAL) + un **dashboard minim de pilot** (condiția
#2 pentru primul client plătit, §11). **Scop:** (1) `dead_letter_inbound` (business_id, provider_msg_id,
envelope, last_error, failed_at) — la epuizarea retry-urilor din NX-140, evenimentul aterizează AICI, nu
doar în log; (2) dashboard minim (Metabase — leagă NX-33 — sau view intern) cu: conversații, răspunsuri,
handoff, leads, produse recomandate, **outbox dead/failed**, **dead_letter count**, cost. Definește SLO:
„99.5% inbound primesc stare finală în <30s" (audit §5.1). **DoD:** un tur eșuat permanent apare în
`dead_letter_inbound` + pe dashboard; regula-invariant „niciun inbound fără stare finală" verificabilă;
dashboard-ul afișează cele 8 metrici pe datele demo. **Dep:** NX-140 (mecanica de retry/fallback), NX-147
(metrici outbox lag), NX-146 (buton replay per turn_id). **Fișiere:** `docs/0NN_dead_letter_inbound.sql`, `src/worker/consumer.py` (branch dead-letter), `src/db/queries/`, config Metabase / view.

---

## Pilot Readiness Override - clarificare 2026-07-06

Pentru pilotul curent, dashboardul UI NU este in acest repo. Exista un proiect separat de dashboard. Prin urmare, `NX-154` trebuie tratat ca doua livrabile separate:

### NX-154A - dead_letter_inbound durabil / terminal state (P0)

**Scop:** niciun inbound `kind=message` nu poate ajunge in stare `acked_but_unknown`. La epuizarea retry-urilor din `NX-140`, evenimentul fie are fallback persistat in `outbox`, fie ajunge intr-un `dead_letter_inbound` durabil, vizibil operational.

**DoD:** failure permanent apare in `dead_letter_inbound`; ACK final apare doar dupa stare durabila; metric/invariant `acked_but_unknown{kind=message}=0`.

### NX-154B - data feed / analytics contract pentru dashboard extern (P1)

**Scop:** acest repo trimite date stabile pentru dashboardul din proiectul extern. Nu construieste UI, grafice, layout, filtre sau app de dashboard.

**Date minime:** conversatii, raspunsuri, fallback/error, failed/dead-letter, produse recomandate, leads/CTA, checkout links si cost, filtrabile pe `business_id` si interval.

**DoD:** proiectul extern poate consuma datele fara query-uri ad hoc fragile; schema/export/API este documentat; `turn_id`/replay link/outbox lag sunt optionale pentru prima versiune.

## Pilot Task Order

1. **NX-155** (Pilot Data Pack) - P0, inainte de refactoruri de agent.
2. **NX-157** (Web Response Production Gate) + **NX-145 felia 1** (eval baseline).
3. **NX-140** + **NX-154A** (no silent loss / terminal state).
4. **NX-156** (fallback localizat).
5. **NX-142** (validator extraction) + **NX-146** (turn replay).
6. **NX-154B** (data feed dashboard extern).
7. Dupa baseline: **NX-143**, **NX-144**, **NX-148**, **NX-147**, **NX-141** dupa semnale reale.

## Task Order (lanț de dependențe)
1. **NX-142** (validator) → **NX-143** (intents + executor) → **NX-144** (planner) — spargerea `agent.py`, strict în ordine.
2. **NX-143** → **NX-150** (tool authz pe executor-ul extras).
3. **NX-140** → **NX-154** (dead-letter peste retry/fallback) → alimentează **NX-146** (replay) + dashboard.
4. **NX-146** (turn replay) → ajută **NX-152** (cost pe traiectorie) + **NX-145** (cazuri golden din tururi reale).
5. **NX-149** (docs) — după refactorul de agent (structura se schimbă).

## Parallel Tasks (fără conflict de fișiere)
- **NX-147** (dispatcher) — atinge `dispatcher.py`/`outbox.py`, disjunct de agent.
- **NX-148** (facts) — atinge `context.py`/`processor.py`/migrare, disjunct de agent.
- **NX-151** (retrieval) — atinge `search_products`/retrieval core, disjunct.
- **NX-153** (migration assert) — atinge `consumer.py`/`scripts`, mic, oricând.
- Atenție hotspot: **NX-142/143/144** ating TOATE `agent.py` → serial pe același branch/track, NU în paralel.
