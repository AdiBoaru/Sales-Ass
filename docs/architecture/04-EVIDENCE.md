# 04-EVIDENCE — registrul de dovezi al diagramelor 04 (NX-250)

> **Verdict: `BLOCKED`.** Cutoverul Stage 1 **nu a avut loc**, iar producția rulează un artefact
> mai vechi decât `main`. Diagramele „04a/04b1/04b2/04b3/04c as-built post-cutover" cerute de
> NX-250 **nu pot fi scrise**: nu există o cale v2 as-built de descris. Ce s-a livrat în schimb e
> auditul măsurat de mai jos + corectarea afirmațiilor DOVEDIT FALSE din documentația normativă.
> Detalii în §2 și §7.

| Câmp | Valoare |
|---|---|
| Backend SHA auditat | `ba1e44fe383520d8fcdf6a9b14538d2e2bfd3cf5` (`origin/main`, după NX-249 #297) |
| Frontend SHA | **UNKNOWN** — repo separat, nu e accesibil din acest repo (DoR neîndeplinit) |
| Image digest producție | **UNKNOWN** — nu există release manifest (vezi §2.2) |
| Release policy / config revision | **N/A** — `release_controller_enabled = false` |
| Data verificării | 2026-08-18 |
| Metodă | extragere din cod la SHA + rulare de teste + sonde HTTP read-only pe producție |

---

## 1. De ce acest document nu conține diagramele cerute

NX-250 cere diagrame **as-built** ale căii WebWidget v2 „care există după cutover", cu regula
explicită: *„Nicio muchie aspirativă, componentă dormantă sau garanție doar menționată într-un card
nu este colorată/descrisă ca live."*

Măsurătoarea de la §2 arată că fiecare componentă v2 (ledger, executor, action kernel, cart,
grounding, projector, creier unic, controller de release) e **dormantă prin flag** și, în plus,
**absentă din artefactul care rulează în producție**. A desena aceste componente ca as-built ar
încălca exact regula pe care cardul o pune ca nenegociabilă. Cardul prevede și tratamentul:

> *„muchie dormantă prin flag → `OPTIONAL/OFF` cu config evidence, nu `LIVE`"*
> *„code live, production digest vechi → descrie production digest; notează main-ahead separat"*

Aplicate integral, cele două reguli transformă „diagrama as-built v2" într-o diagramă în care
**toate** nodurile v2 sunt `OPTIONAL/OFF`. Aceea nu e o diagramă as-built, e o listă de flag-uri —
care există deja, verificată mecanic, în `docs/ARCHITECTURE-WORKFLOWS.md` (Lentila 4 · FLAG-URI) și
în blocul `claim:flags` păzit de `scripts/verify_architecture_doc.py`.

Prin urmare NX-250 se oprește la poarta lui de evidence, conform propriilor Stop conditions, și
livrează auditul + fixurile documentare care sunt adevărate ASTĂZI, pe calea care chiar rulează.

---

## 2. Starea reală, măsurată (nu presupusă din `git main`)

### 2.1 Producția rulează un artefact mai vechi decât `main`

Sonde HTTP read-only pe `https://bot.nativextech.com`, 2026-08-18:

| Sondă | Producție | Același cod la `ba1e44f`, flag-uri default | Ce dovedește |
|---|---|---|---|
| `GET /health/live` | **404** | **200** | rutele NX-248 LIPSESC din imaginea deployată |
| `GET /health/startup` | **404** | 200/503 | idem |
| `GET /health/ready` | **404** | 200/503 | idem |
| `POST /web/v2/turns` (fără query params) | **404** | **422** | ruta v2 LIPSEȘTE din imaginea deployată |
| `POST /web/chat` (corp gol) | **422** | **422** | **v1 e ruta care servește traficul** |
| `GET /web/bootstrap` (fără token) | **422** | 422 | aplicația E Nativx (uvicorn), nu alt serviciu |

Cele două 404-uri sunt **atribuibile**, nu ambigue:

- `/health/*` e montat **necondiționat**, înaintea oricărui flag (`src/webhook/app.py:57`, cu
  motivul scris acolo: un flag care ar putea stinge health-ul ar stinge exact diagnosticul de care
  ai nevoie). Deci 404 pe `/health/live` **nu poate fi** efectul unui flag — e absența codului.
- `/web/v2/turns` e înregistrată necondiționat ca rută; flagul e verificat **în handler**
  (`src/web/app.py:813`, 404 deliberat ca să nu confirme existența feature-ului). Cu ruta prezentă,
  un POST fără `token/visitor_id/sig` întoarce **422** (validarea de query params rulează înaintea
  handlerului) — verificat local: 422. Producția întoarce **404**, deci nu ajunge la validare:
  ruta nu există în acel build.

**Concluzie:** imaginea din producție e anterioară NX-233 (ruta v2) și NX-248 (health). `main` e
înainte cu cel puțin 6 carduri față de ce rulează. Orice afirmație „as-built" bazată pe `main` ar fi
descris cod care nu servește niciun client.

### 2.2 Nu există release manifest — deci nu există digest de comparat

`python scripts/release/evidence.py` pe SHA-ul auditat:

```
verdict: NOT_READY
missing_critical: dr_restore, manifest, rollback_drill, sbom, scan, signature, staging_smoke
```

`manifest` lipsește ⇒ DoR-ul „Cutover state este confirmat din release manifest/config și health"
e **neîndeplinit prin absența instrumentului**, nu prin neglijență de audit.

### 2.3 Toate componentele v2 sunt dormante

Din `claim:flags` (extras mecanic din `src/config.py`, verificat de CI):

| Flag | Default | Componenta pe care o ține stinsă |
|---|---|---|
| `web_turn_ledger_enabled` | `false` | NX-232 ledger durabil |
| `web_turn_v2_enabled` | `false` | NX-233 calea async v2 (poarta tuturor rutelor `/web/v2/*`) |
| `web_turn_executor_enabled` | `false` | NX-233 executor cu lease/fencing |
| `web_turn_recovery_enabled` | `false` | NX-233 sweeper de recovery |
| `web_turn_sse_enabled` | `false` | NX-233 SSE |
| `web_context_enabled` | `false` | NX-234 context de pagină ID-only |
| `conversation_state_v2_enabled` | `false` | NX-235 stare redusă |
| `web_actions_enabled` | `false` | NX-236 acțiuni opace semnate |
| `conversation_cart_enabled` | `false` | NX-237 coș canonic |
| `retrieval_candidate_enabled` | `false` | NX-238 candidatul de retrieval |
| `single_brain_enabled` | `false` | NX-239 creier unic |
| `web_view_v2_projector_enabled` | `false` | NX-240 projector pur |
| `turn_deadline_enabled` / `turn_budget_enforced` | `false` | NX-241 deadline + bugete |
| `observability_enabled` | `false` | NX-246 traces/metrici |
| `web_feedback_enabled` | `false` | NX-246 feedback |
| `release_controller_enabled` | `false` | NX-249 controller de release |

### 2.4 Porțile upstream, la data auditului

| Card | Verdict propriu | Sursă |
|---|---|---|
| NX-238 | `NOT-READY` | `docs/NX-238-DECISION.md` |
| NX-246 felia 3 | `NOT-READY` | `docs/WEB-QUALITY-EVAL.md` |
| NX-247 | `NO-GO` | `docs/STAGE1-WEB-E2E.md` |
| NX-248 | `NOT_READY` | `scripts/release/evidence.py`, rulat mai sus |
| NX-249 | `BLOCAT` | `docs/STAGE1-RELEASE-DECISIONS.md` |

Un card de închidere nu poate declara „as-built complet" peste cinci porți deschise.

---

## 3. Registrul drifturilor obligatorii

Fiecare rând din tabelul NX-250 a fost urmărit **în cod** la `ba1e44f`, nu copiat din card.
Dispoziții: `fixed-and-live` · `legacy-only` · `removed/dead` · `blocking mismatch` · `doc-defect`.

### D1 · `TVAL` invalid category → **doc-defect (CORECTAT)**

Nodul `TVAL` amesteca două comportamente cu autoritate diferită, iar muchia
`TVAL --"invalid → fără rută"--> FB` era adevărată doar pentru unul din ele.

| Ce se întâmplă | Cod | Terminal real |
|---|---|---|
| JSON-ul nano nu trece de schemă, sau apelul crapă | `src/worker/stages/triage.py:331-336` — `return` fără a seta `ctx.route` | niciun stagiu nu produce reply → `fallback_stage` (`src/worker/runner.py:444`), clarificare. **Muchia spre FB e corectă aici.** |
| `category_key` inventat (în afara listei) | `src/worker/stages/triage.py:339` — `category_key = out.category_key if out.category_key in categories else None` | **NU cade în FB.** Valoarea se aruncă tăcut, `ctx.route` se setează oricum și turul continuă (sales/clarify/...). |

Cardul descria exact asta: *„implementarea actuală doar pierde/drop-uiește decizia în anumite căi"*.
**Confirmat.** Nu e un defect de cod (a nu ruta pe o categorie ghicită e corect); e un defect de
diagramă. Corectat prin split în `TPARSE` (decizie) + `TCAT` (drop tăcut, ruta continuă).

### D2 · „max 3 searches" → **doc-defect (CORECTAT), afirmație DOVEDIT FALSĂ**

Trei numere distincte erau raportate ca unul singur, în trei documente normative
(`CLAUDE.md:401`, `CLAUDE.md:717`, `ARCHITECTURE-WORKFLOWS.md` Diagram 4b).

| Buget | Valoare LIVE | Enforcement |
|---|---|---|
| runde de model | **3**, cap dur | `src/agent/llm.py:364` (`for _ in range(max_steps)`), default `max_steps=3` la `llm.py:348`, nesuprascris de apelant (`src/worker/stages/agent.py:473`) |
| tool calls / tur | **NEplafonat** | `src/agent/llm.py:128` `_run_tool_calls` execută TOATE apelurile emise într-o rundă |
| search calls / tur | **NEplafonat** | nu există contor per-tool pe calea live |

**Dovadă din testele care există deja în repo** (nu au fost scrise pentru asta — de aceea contează):

- `tests/test_llm_tool_loop.py::test_loop_caps_at_max_steps_then_forces_text` — 3 runde, apoi runda
  a 4-a e forțată fără tools. Numără `n == 3` **doar fiindcă scenariul emite un tool call per
  rundă**; nu demonstrează un plafon de apeluri.
- `tests/test_llm_tool_loop.py::test_loop_runs_multiple_tool_calls_in_one_step` — o rundă, două tool
  calls, **ambele executate**.

**Probă adversarială NX-250:** 3 runde × 3 tool calls ⇒ **9 execuții** sub „max 3". Plafoanele
separate (`max_model_rounds`, `max_tool_calls`, `max_mutations`, `max_repair_calls`, per clasă de
tur) există în `src/runtime/turn_budget.py` (NX-241) dar sunt **dormante**: `turn_budget_enforced =
false`.

### D3 · `spec_numbers` → **removed/dead ca muchie de INPUT (nu se desenează)**

Cardul cere: *„nu desena edge-ul decât dacă test capture dovedește payloadul final"*.

`spec_numbers` apare **exclusiv** în `src/worker/compose.py` (definiție la `:739`, unic call-site la
`:426`). Nu apare în `src/agent/prompt_builder.py` și nici în `src/agent/finalize.py` — adică în
niciunul din modulele care construiesc payloadul rich al modelului.

Rolul real e **invers** celui documentat: e un **allowlist de ieșire**, folosit după generare ca să
decidă ce cifre din proza modelului supraviețuiesc scrubbing-ului (`allowed_numbers |=
spec_numbers(...)`), gated pe `spec_digits_grounded_enabled` (default `true`).

**Dispoziție:** muchia „spec_numbers → payload rich LLM" e **inexistentă**; se documentează ca
`deterministic-only, output-side`.

### D4 · validator claims → **capabilitate mai îngustă decât „grounded"**

Validatorul de proză NU leagă fiecare afirmație de o evidence structurată. Ce verifică efectiv, pe
calea live (`src/agent/finalize.py:389-403`, `src/agent/validator.py`):

| Verificare | Kind emis | Flag |
|---|---|---|
| apartenența produsului la `ctx.retrieval` | — | always |
| preț ∈ `grounded_prices` | — | always |
| link ∈ `generated_links` | — | always |
| cifre bare negroundate | `bare_number` | `validator_bare_numbers_enabled=true` |
| claim ne-numeric neverificabil | `claim` | `validator_claims_enabled=true` |
| claim de stoc nefondat | `stock_claim` | `validator_stock_claims_enabled=**false**` |

Contractul claim↔evidence propriu-zis (`EvidenceBundle` + `grounding_guard`) e NX-240 și e
**dormant** (`web_view_v2_projector_enabled=false`). **Interzis** cuvântul „grounded" generic pentru
calea live: garanția reală e *membership + preț + link*, plus două semnale ne-blocante.

### D5 · privacy order → **PARȚIAL FIXAT + un boundary rămas (finding P0, vezi §5)**

Cardul enunța două suspiciuni. Măsurate separat:

| Suspiciune | Verdict | Dovadă |
|---|---|---|
| „history poate fi raw" | **FIXAT de NX-230** | `src/worker/processor.py:525` — `apply_boundary` rulează *înaintea primei scrieri durabile*; `safe_inbound.text` e ce se persistă (`safe_body=`, `:544`). Comentariul de acolo descrie explicit bucla veche pe care a închis-o. |
| „maskingul e după moderation" | **CONFIRMAT** | vezi mai jos |

Ordinea reală în `gates_stage` (`src/worker/stages/gates.py:440-489`):

```
1 bot_active → 2 blocklist → 3 handoff → 4 rate limit
→ 5 MODERATION (extern: deps.llm.moderate)      ← corpul e încă BRUT
→ 6 detect_risk (local)                          ← corpul e încă BRUT
→ 7 vision routing
→ 8 _apply_input_guardrails → mask_pii           ← abia AICI se maschează
```

`ctx.message.body` e setat pe **raw** (`src/worker/processor.py:757`, cu motivul D6 scris acolo:
agentul principal vede query-ul brut în memoria turului), iar `_moderation_blocked` trimite exact
acel `ctx.message.body` la API-ul extern (`src/worker/stages/gates.py:357-360`).

Deci: **sinkurile durabile sunt curate, dar PII-ul brut traversează frontiera externă de moderation
înainte de mascare.** Cardul e categoric: *„dacă raw trece un boundary, close este blocat"* →
finding P0-1.

### D6 · `AGUARD` → **OPTIONAL/OFF (recolorat, nu șters)**

`enforce_answer_plan` (`src/agent/answer_plan_guard.py`) **este** apelat — call-site real la
`src/worker/stages/agent.py:502` — dar sub `answer_plan_enabled`, care e `false`. Nod dormant, nu
mort: se păstrează cu stil punctat `dorm` și config evidence, conform failure matrix.

### D7 · `CTAB` / comparison final gate → **ocolire CONFIRMATĂ (by design), acum desenată ca atare**

Calea de comparație iese **înainte** de validatorul de proză:

```python
if plan.compared and not is_order:              # src/agent/finalize.py:347
    comparison = compose.build_comparison(...)
    if comparison is not None:
        ctx.set_comparison_reply(...)
        ctx.emit("agent_compared", ...)
        return None                              # :357 — terminal
```

Docstring-ul lui `render` recunoaște deschis: *„`None` pe căile FĂRĂ proză validată structural
(comparație/rich/no-result/login): acolo grounding-ul vine din compose (membership) [...] nu din
`validate_prose`"*.

**Dispoziție:** nu e un bug — celulele tabelului sunt compuse determinist din fapte de retrieval,
zero proză de model, deci nu există text de validat. Dar **nu e același gate**, și diagrama nu are
voie să sugereze că e. `CTAB` e marcat TERMINAL, cu gate-ul propriu (membership în compose) numit
explicit. Acelaşi tratament pentru `GRND`.

### D8 · `GRND` → **LIVE, cu garanție mai îngustă decât eticheta veche**

`_finalize_grounded` (`src/agent/finalize.py:148`) e reachable pe calea `is_order`. Validează
linkuri + sume, dar rulează cu `check_bare=False, check_claims=False` (`:162-165`) — deliberat:
datele/AWB-urile/cantitățile sunt numere legitime din DB. Eticheta veche „legat STRICT de datele
comenzii lui" supraevalua garanția; corectată.

### D9 · chips payload/scrub → **v1 = text brut re-intrat; v2 = token opac, dormant**

Pe calea LIVE (v1) un chip **nu are payload**: e o etichetă care se retrimite ca mesaj nou al
clientului. `src/worker/stages/triage.py:296-298` o spune explicit — *„Voce de client → reintră ca
tur nou (fără scrub)"*. Nu există token, nu există legare de turul-sursă, nu există one-shot.

Tokenul opac semnat (AES-SIV, legat de tenant/sesiune/conversație/tur-sursă, one-shot) e NX-236 și e
dormant (`web_actions_enabled=false`) **și** absent din producție (§2.1). Testul „action chip
byte-identic end-to-end" cerut de NX-250 nu se poate rula pe o cale care nu servește.

---

## 4. Semantica de livrare — ce promite sistemul pe calea care RULEAZĂ

Vocabularul cerut de card, aplicat căii v1 (singura live). Frontierele v2 nu sunt listate: nu
livrează nimic.

| Frontieră | Afirmația susținută de dovezi | Ce NU se poate afirma |
|---|---|---|
| browser → `POST /web/chat` | request/response **sincron**; reply-ul se mapează direct în HTTP, fără outbox | „accept idempotent prin `client_turn_id`" — ledgerul e NX-232, **OFF** |
| retry de client pe `/web/chat` | **at-least-once**: fără ledger, un retry după response loss **re-execută turul** | „replay idempotent" |
| dedupe inbound (WhatsApp/Telegram) | idempotent prin `inbound_dedupe` (claim înainte de orice scriere) | — |
| commit intern | atomic pe rândurile din aceeași tranzacție (`TurnCommit`) | atomicitate peste API extern |
| moderation / LLM / tools | **at-least-once**, fără fencing pe calea sincronă | „exactly-once" pe orice apel de model sau tool |
| analytics / `emit()` | best-effort, bounded, cu goluri vizibile | telemetria ca sursă de adevăr |

**Zero afirmații de exactly-once** în documentația normativă — verificat cu `rg`: termenul apare
doar în carduri (`tasks/`), unde e folosit ca INTERDICȚIE, niciodată ca promisiune.

---

## 5. Findings deschise de acest audit

| ID | Sev | Finding | Owner propus |
|---|---|---|---|
| **P0-1** | P0 | PII brut traversează frontiera externă de moderation înainte de mascare (D5). Sinkurile durabile sunt curate (NX-230), deci impactul e limitat la providerul de moderation — dar cardul cere blocarea închiderii Stage 1 până la ratificare explicită de owner sau reordonare. NX-250 e docs-only și **nu** repară. | NX-230 follow-up |
| **P1-1** | P1 | Producția rulează un artefact anterior NX-233/NX-248 (§2.1), fără manifest care să spună CARE (§2.2). Până la un deploy cu digest declarat, nicio documentație nu poate fi numită „as-built". | NX-248 |
| **P1-2** | P1 | „MAX 3 tool calls per tur (limită dură în cod)" a fost fals în `CLAUDE.md` și în diagrama 4b (D2). Corectat aici; merită un test care să LEGE plafonul de documentație când NX-241 se aprinde. | NX-241 |

---

## 6. Contoare de calitate ale documentului

Cardul cere contoare docs, nu metrici runtime. Raportate onest, pe ce s-a auditat efectiv:

| Contor | Valoare |
|---|---|
| drifturi obligatorii din card, urmărite în cod | **9 / 9** (D1–D9) |
| dispoziții: doc-defect corectat | 3 (D1, D2, D8) |
| dispoziții: recolorat `OPTIONAL/OFF` sau terminal propriu | 3 (D6, D7, D9) |
| dispoziții: muchie inexistentă, nedesenată | 1 (D3) |
| dispoziții: capabilitate re-enunțată mai îngust | 1 (D4) |
| dispoziții: blocking mismatch | 1 (D5 → P0-1) |
| diagrame 04a/04b1/04b2/04b3/04c as-built | **0** — blocate (§1) |
| noduri/muchii cu evidence 100% | **N/A** — registrul complet cere diagramele |
| Mermaid parse errors | **0** (`scripts/verify_architecture_doc.py`) |
| citări `fișier.py:linie` rupte | **0** în documentele atinse (aceeași poartă, rulată și pe acest fișier) |
| citări corectate (linia se mutase) | 14 (`triage_stage`, `agent_stage`, `build_plan`, `try_pre_intents`, `validate_prose`, `agent.py:957` → 528 linii) |
| findings P0 / P1 deschise | 1 / 2 |

### Gate-ul de repository (docs-only, dar SHA-ul trebuie să rămână verde)

| Comandă | Rezultat |
|---|---|
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **671 files already formatted** |
| `pytest -q --ignore=tests/e2e` | **4232 passed, 1 skipped** (12m09s) |
| `python scripts/verify_architecture_doc.py` | **OK** — flags(122), jobs(8), migrations(40), processes(7), routes(16), stages(12), tools(10) |
| `pytest tests/e2e` | **NERULAT local** — harnessul NX-247 cere Postgres + Redis reale (`docker-compose.stage1-e2e.yml`); local eșuează la `getaddrinfo redis:6379`. Nu e un eșec de cod. |
| gate-uri frontend NX-247 | **NERULATE** — repo separat, inaccesibil din acest repo (DoR neîndeplinit) |

---

## 7. Ce deblochează scrierea diagramelor as-built

În ordine, toate în afara controlului acestui card:

1. **NX-248 → `READY`** — manifest + semnătură + scan + staging smoke + drill de rollback + DR.
   Fără manifest nu există digest de comparat cu `main`.
2. **Un deploy real** al unui digest declarat, cu `/health/*` care răspunde.
3. **NX-247 → GO** — gate E2E pe buildurile finale (azi `NO-GO`, acoperire 9/16 scenarii).
4. **NX-238 → GO** și **NX-246 felia 3 → PASS** — ambele `NOT-READY`, deblocate de NX-203 (corpus),
   nu de cod.
5. **NX-249 → promovare** — abia atunci există un cohort v2 cu trafic real, adică un „as-built".
6. **P0-1 ratificat sau reparat.**

Când toate șase sunt îndeplinite, NX-250 se reia pe noul SHA și scrie diagramele. Până atunci,
`docs/ARCHITECTURE-WORKFLOWS.md` descrie calea v1 — corect, cu drifturile de mai sus reparate.
