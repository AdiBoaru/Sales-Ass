# 04-EVIDENCE — registrul de dovezi al diagramelor 04 (NX-250)

> **Verdict: `BLOCKED`.** Cutoverul Stage 1 **nu a avut loc**, iar producția rulează un artefact
> mai vechi decât `main`. Diagramele „04a/04b1/04b2/04b3/04c **as-built** post-cutover" cerute de
> NX-250 **nu pot fi scrise**: nu există o cale v2 as-built de descris.
>
> **Ce s-a livrat în schimb, integral:** (1) auditul măsurat al celor 9 drifturi, cu dispoziție și
> dovezi (§3); (2) cele cinci diagrame, ca **topologie ȚINTĂ evidențiată** — `Diagram 11-15` în
> `ARCHITECTURE-WORKFLOWS.md` — cu fiecare element legat de un simbol real la SHA și marcat
> `OPTIONAL/OFF` sau `UNVERIFIED`, **zero elemente prezentate ca `LIVE_V2`**; (3) registrul
> node/edge/invariant, 65/72 noduri cu evidence, cu cauza unică a celor 7 lipsă declarată (§3b);
> (4) corectarea afirmațiilor DOVEDIT FALSE din documentația normativă; (5) gate-ul E2E NX-247
> rulat pe infrastructură reală: **151 passed** (§6).
>
> Ce rămâne blocat: cuvântul **as-built**. Detalii în §2 și §7.

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

## 3b. Registrul node/edge/invariant al diagramelor Stage 1

Diagramele 04a-04c există în `ARCHITECTURE-WORKFLOWS.md` ca **Diagram 11-15** (numerotare distinctă
ca să nu se ciocnească cu 4a/4b/4c, care documentează calea LIVE; identificatorii ceruți de card
sunt în titlul fiecăreia și sunt cheia de mai jos).

**Câmpuri hoistate — identice pe TOATE rândurile, deci scrise o dată aici, nu repetate de 72 de ori:**

| Câmp | Valoare pentru tot registrul |
|---|---|
| `verified at` | backend `ba1e44f` · frontend UNKNOWN · digest UNKNOWN · 2026-08-18 |
| `runtime status` | **`OPTIONAL/OFF`** pentru orice element marcat `[OFF]`/`[OFF*]`, **`LIVE_V2` = 0 elemente** (nu există cutover). `[LIVE]` = piesă v1 refolosită. `[UNVERIF]` = fără dovadă la acest SHA. |
| `privacy/tenant` | `business_id` SERVER-OWNED pe toate rândurile (injectat server-side, niciodată din client sau din output de model). Forma safe la frontiere: `apply_boundary` (`processor.py:525`). Excepțiile sunt notate explicit pe rând. |

**Acoperire mecanică** (extrasă din blocurile Mermaid, nu numărată de mână):

| Diagramă | Noduri | Muchii | Noduri cu evidence | `LIVE_V2` |
|---|---|---|---|---|
| 04a (Diagram 11) | 20 | 22 | 20/20 | 0 |
| 04b1 (Diagram 12) | 15 | 16 | 15/15 | 0 |
| 04b2 (Diagram 13) | 13 | 16 | 13/13 | 0 |
| 04b3 (Diagram 14) | 13 | 17 | 13/13 | 0 |
| 04c (Diagram 15) | 11 | 12 | **4/11** — restul `UNVERIF` | 0 |
| **total** | **72** | **83** | **65/72** | **0** |

Cele 7 noduri fără evidence sunt TOATE în 04c și au aceeași cauză unică: repo-ul frontend nu e
accesibil, deci DoR-ul „frontend SHA read-only" e neîndeplinit. Sunt marcate `[UNVERIF]` în diagramă
și nu sunt prezentate ca live. **Un `UNKNOWN` declarat nu e un `LIVE` deghizat** — asta e diferența
pe care cardul o cere.

### 04a — ingress, durabilitate, recovery (Diagram 11)

| element | claim (o singură proprietate) | cod | test |
|---|---|---|---|
| `BROWSER` | `client_turn_id` e stabil înaintea oricărui I/O | — (contract, `[EXT]`) | repo FE (`UNVERIF`) |
| `CAP` | corpul e plafonat pe middleware înaintea parsării | `webhook/app.py:59` | `tests/e2e/test_stage1_failure_matrix.py` |
| `GATE` | flag OFF ⇒ 404, nu 403 (nu confirmă feature-ul) | `web/app.py:810-821` | `test_web_turn_api_v2.py` |
| `R404` | ieșire terminală pentru poarta închisă | `web/app.py:813` | `test_web_turn_api_v2.py` |
| `PRIV` | forma safe e singura care atinge un disc | `worker/processor.py:525` | `test_privacy_boundary.py` |
| `ADM` | plafon global + per tenant, fail-closed | `worker/admission.py:149` | `test_admission.py`, `test_admission_gate.py` |
| `ACC` | insert-or-inspect pe cheie de idempotency | `web/turn_service.py:291` | `test_web_turn_ledger.py` |
| `LEDGER` | Postgres e AUTORITATEA turului | `db/queries/web_turns.py:89` | `test_web_turns_db.py` |
| `DUP` | duplicat detectat înainte de orice cost LLM | `web/app.py:522-575` | `test_turn_replay.py` |
| `R202` | tur activ ⇒ status, nu conținut gol | `web/turn_events.py:209` | `test_web_turn_api_v2.py` |
| `R409` | aceeași cheie + alt corp ⇒ conflict, nu suprascriere | `web/app.py:549` | `test_web_turn_ledger.py` |
| `REPLAY` | duplicat terminal ⇒ același ViewModel, byte-identic | `web/app.py:431` | `test_turn_replay.py` |
| `WAKE` | trezirea e best-effort, nu autoritate | `web/turn_executor.py:80` | `test_web_turn_executor.py` |
| `SWEEP` | recovery care NU depinde de Redis | `web/turn_recovery.py:108` | `test_web_turn_recovery.py` |
| `CLAIM` | maximum un owner fenced pe epoch | `db/queries/web_turns.py:204` | `test_web_turn_executor_db.py` |
| `EXEC` | execuția rulează sub deadline-ul turului | `web/turn_executor.py:190` | `test_web_turn_latency.py` |
| `COMMIT` | commit terminal atomic pe rândurile din tranzacție | `db/queries/web_turns.py:282` | `test_web_turns_db.py` |
| `FAILT` | orice eșec are cod și destinație terminală (P6) | `web_turns.py:310`, `turn_recovery.py:66` | `test_web_turn_recovery.py` |
| `GETR` | rezultatul se reconstruiește din ledger fără Redis | `web/app.py:1402` | `test_web_turn_api_v2.py` |
| `SSE` | livrare repetabilă, niciodată sursă de adevăr | `web/app.py:1428` | `test_web_turn_api_v2.py` |

### 04b1 — snapshot, stare, referință (Diagram 12)

| element | claim | cod | test |
|---|---|---|---|
| `CTXIN` | clientul trimite identificatori opaci, nu fapte | contract NX-234 | `test_context.py` |
| `NORM` | un câmp comercial în context ⇒ 422 | `web/context.py:129` | `test_context.py` |
| `R422` | refuz explicit, nu ignorare tăcută | `web/context.py:129` | `test_context.py` |
| `HYDR` | rehidratare canonică tenant-scoped, UN query | `catalog/context_resolver.py:210` | `test_context_resolver_db.py` |
| `FRESH` | `UNKNOWN` nu devine `0` | `catalog/context_resolver.py:84` | `test_context_resolver_db.py` |
| `TENANT` | `business_id` server-owned | `worker/processor.py` | `test_tenant_isolation.py` |
| `SNAP` | snapshot IMUABIL între load și pipeline | `worker/turn_snapshot.py:53-81` | `test_context.py` |
| `STATE` | UN singur scriitor de stare | `conversation/state_reducer.py:179` | `test_conversation_state_v2.py` |
| `REF` | precedență UNICĂ de referință | `agent/reference_resolver.py:258` | `test_reference_resolver_v2.py` |
| `STALE` | referință expirată ⇒ refuz, nu ghicit | `agent/reference_resolver.py:258` | `test_page_reference_resolution.py` |
| `ACT` | o acțiune e o decizie, nu o intenție | `agent/action_kernel.py` | `test_action_kernel.py` |
| `KERN` | kernelul rulează ÎNAINTEA oricărui strat de text | `worker/runner.py` (`DEFAULT_STAGES`) | `test_action_kernel.py` |
| `FAST` | fast path doar pe acoperire completă și sigură | `agent/control_plane.py:88` | `test_control_plane.py` |
| `FASTOUT` | ieșire deterministă, zero model | `agent/control_plane.py:135` | `test_control_plane.py` |
| `BRAIN` | UN singur scriitor semantic | `agent/brain.py:222` | `test_single_brain.py` |

### 04b2 — bugete, retrieval, evidence (Diagram 13)

| element | claim | cod | test |
|---|---|---|---|
| `DL` | UN buget monoton, neprelungit la reclaim | `runtime/deadline.py:85` | `test_turn_deadline.py` |
| `BUD` | manifest VERSIONAT pe clase de tur | `runtime/turn_budget.py:73-77` | `test_turn_execution_budget.py` |
| `ROUND` | rundele de model sunt plafonate explicit | `runtime/turn_budget.py:73` | `test_turn_execution_budget.py` |
| `MODEL` | runda emite plan structurat, nu proză liberă | `agent/brain.py:222` | `test_single_brain.py` |
| `TCALL` | tool calls rezervate ATOMIC (anti tool-storm) | `runtime/turn_budget.py:74` | `test_tool_budget.py` |
| `CLS` | mutațiile sunt exclusive și seriale | `agent/tool_budget.py` | `test_tool_budget.py` |
| `PORT` | candidații sunt REFERINȚE + verdicte tri-state | `retrieval/port.py` | `test_retrieval_port.py` |
| `SEL` | promovarea cere artefact GO semnat HMAC | `retrieval/selector.py` | `test_retrieval_selector.py` |
| `CUR` | paritate cu live prin construcție (apelează tool-ul) | `retrieval/current_live.py` | `test_retrieval_port.py` |
| `CAND` | INERT fără `@register` — un import nu-l activează | `retrieval/search_entities.py` | `test_retrieval_selector.py` |
| `EV` | fapte ÎNGHEȚATE cu sursă și vârstă | `agent/evidence_bundle.py:568` | `test_evidence_bundle.py` |
| `PLAN` | evidence ÎNAINTEA textului | `agent/brain_models.py` | `test_single_brain.py` |
| `EXH` | buget epuizat ⇒ ieșire onestă, nu tăcere (P6) | `runtime/deadline.py:61` | `test_turn_deadline.py` |

### 04b3 — grounding, proiecție, commit (Diagram 14)

| element | claim | cod | test |
|---|---|---|---|
| `PLAN` | plan + fapte înghețate intră împreună | `agent/brain_models.py` | `test_single_brain.py` |
| `GRND` | fiecare cifră din proză se confruntă cu faptele | `agent/grounding_guard.py:264` | `test_grounding_guard_v2.py` |
| `NOSRC` | afirmație fără sursă ⇒ RESPINGE, nu omite tăcut | `agent/grounding_guard.py:264` | `test_grounding_guard_v2.py` |
| `VAL` | validatorul V2 refolosește NX-211 prin `to_v1()` | `agent/answer_plan_runtime.py` | `test_answer_plan_actions.py` |
| `REP` | exact UN repair, validat la construcție | `runtime/turn_budget.py:77` | `test_turn_execution_budget.py` |
| `FBK` | fallback determinist, fără cifre (P6) | `agent/fallbacks.py` | `test_validator.py` |
| `SAFE` | mutațiile cer `policy.allows()` ÎNAINTE de scriere | `safety/contraindications.py` | `test_safety_*.py` |
| `CART` | UN serviciu pentru ambele căi; receipt idempotent | `commerce/cart_service.py:69` | `test_cart_service.py`, `test_cart_concurrency.py` |
| `PROJ` | projector PUR: zero I/O, zero ceas | `channels/web/render_v2.py` | `test_web_render_v2.py` |
| `LOC` | niciun număr pe sârmă în afară de `revision` | `web/localization.py` | `test_web_render_v2.py` |
| `CTA` | fără plan persistat nu există token de acțiune | `channels/web/render_v2.py:157` | `test_web_render_v2.py` |
| `COMMIT` | O tranzacție: stare + mesaje + receipts + rezultat | `worker/turn_uow.py:208` | `test_web_turns_db.py` |
| `AFTER` | aftercare strict post-terminal, bounded | `worker/runner.py` | `test_web_turn_latency.py` |

### 04c — frontend (Diagram 15) · acoperire parțială, declarată

| element | claim | cod | status |
|---|---|---|---|
| `BOOT` | `view_copy` + locale-ul TENANTULUI vin de la server | `web/shell_copy.py` | evidence backend OK |
| `POST` | tokenul opac se retrimite NESCHIMBAT | `web/action_service.py` | evidence backend OK |
| `W202` | 202 + status e contractul de „în lucru" | `web/turn_events.py:209` | evidence backend OK |
| `ERRV` | error view server-owned, nu text inventat | `web/contracts_v2.py` | evidence backend OK |
| `CTRL`, `SSEC`, `POLL`, `DEC`, `REG`, `APPLY`, `NAV` | comportament de browser | repo FE | **`UNVERIF`** — fără SHA de frontend |

### Invarianți P0 — fiecare cu test ȘI drive de eșec

| invariant | test executabil | drive de eșec / operational |
|---|---|---|
| accept idempotent (retry ⇒ același rezultat) | `test_turn_replay.py` | `tests/e2e` R1 double-submit — **rulat, PASS** |
| maximum un owner fenced | `test_web_turn_executor_db.py` | `tests/e2e` reclaim după crash — **rulat, PASS** |
| Postgres e autoritatea (Redis poate tăcea) | `test_web_turn_recovery.py` | `tests/e2e` wake loss — **rulat, PASS** |
| izolare de tenant | `test_tenant_isolation.py` | `tests/e2e` doi tenanți care diferă în ultimul nibble — **rulat, PASS** |
| forma safe la sinkuri durabile | `test_privacy_boundary.py` | ⚠️ **P0-1**: moderation primește corpul BRUT (§3 · D5) |
| niciun drum fără ieșire (P6) | `test_validator.py`, `test_turn_deadline.py` | `tests/e2e` matricea R1-R22 — **rulat, PASS** |

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
| diagrame 04a/04b1/04b2/04b3/04c **as-built** | **0** — imposibile fără cutover (§1) |
| diagrame 04a-04c livrate ca **topologie țintă evidențiată** | **5** (Diagram 11-15), toate marcate `DORMANT`/`UNVERIFIED` |
| noduri cu evidence | **65 / 72** (90%); cele 7 lipsă sunt toate în 04c, cauză unică: repo FE inaccesibil |
| muchii | 83, toate cu semantică declarată |
| elemente prezentate ca `LIVE_V2` | **0** — corect, fiindcă nu există cutover |
| Mermaid parse errors | **0** (`scripts/verify_architecture_doc.py`) |
| citări `fișier.py:linie` rupte | **0** în documentele atinse (aceeași poartă, rulată și pe acest fișier) |
| citări corectate (linia se mutase) | **~32** — `triage_stage` 212→293, `agent_stage` 267→347, `_persist_safety_context` 217→296, `build_plan` 162→182, `try_pre_intents` 469→573, `validate_prose` 195→196, o citare spre linia 957 a lui `agent.py` (fișier de 528 linii), plus **întregul bloc de 16 citări al fazei E**, care era sistematic cu 26-30 de linii în urmă (`planner.py`: login-wall 187→177, checkout 227→248, cross-sell 251→278, superlativ 294→322, cheaper 317→346, `price_gap` 331→362, `retrieval_final` 372→402) |

> **De ce n-au fost prinse de CI:** `check_citations` e **lenient by design** — raportează doar
> linii peste sfârșitul fișierului (drift dovedit), fiindcă „linia mai conține ce spune citarea"
> nu poate ști un script. `planner.py` are 400+ linii, deci o citare la `:331` care ar fi trebuit
> să fie `:362` trece poarta fără zgomot. Se prind doar re-derivând simbolul — de aceea identitatea
> e simbolul, iar linia e doar ajutor.
| findings P0 / P1 deschise | 1 / 2 |

### Gate-ul de repository (docs-only, dar SHA-ul trebuie să rămână verde)

| Comandă | Rezultat |
|---|---|
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **671 files already formatted** |
| `pytest -q --ignore=tests/e2e` | **4232 passed, 1 skipped** (12m09s) |
| `python scripts/verify_architecture_doc.py` | **OK** — flags(122), jobs(8), migrations(40), processes(7), routes(16), stages(12), tools(10) |
| `pytest tests/e2e` | **151 passed** — RULAT pe stackul efemer real (`docker-compose.stage1-e2e.yml`: pgvector/pg16 + redis 7, loopback, tmpfs), după `migrate.py --mark-applied 003,005` + `migrate.py` (**38 migrări aplicate de la zero, 004→044**) + `migrate.py --check` (zero pending). Postgres și Redis REALE, model/embedder falși — exact profilul NX-247. |
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
