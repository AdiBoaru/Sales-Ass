# STAGE 1 — Traceability: finding → obiectiv → card → gate → dovadă

**Data auditului:** 2026-08-11  
**Backend auditat:** `Sales Ass` la `origin/main@cca5760`  
**Frontend canonic auditat:** `Sales MVP Frontend Final` la `main@e706d27`, inclusiv starea locală inspectată read-only  
**Canal în scope:** exclusiv WebWidget  
**Document normativ de execuție:** [README.md](README.md) + cardul individual  
**Close as-built:** [NX-250](NX-250.md), numai după NX-249

## Cum se citește documentul

Acesta este registrul de trasabilitate al planului, nu dovada că implementarea există. O legătură
spre un card înseamnă **responsabilitate planificată**. Un finding devine `CLOSED` numai când:

1. cardul și dependențele sunt merged în repo-ul corect;
2. Definition of Done este verificată linie cu linie;
3. testele, manual drive-ul și gate-urile cerute au evidence pe SHA/digest;
4. Codex verifică read-only într-un worktree separat;
5. unde este necesar, canary-ul NX-249 confirmă producția;
6. NX-250 sincronizează documentația cu codul as-built, fără să ascundă drifturi.

Legendă:

- **CONFIRMED:** finding observat în codul/configurația auditată, cu cale reproductibilă;
- **GATE:** condiție măsurabilă care poate opri cardul/releaseul;
- **OWNER:** cardul care schimbă sursa de adevăr; celelalte doar consumă/verify;
- **SUPPORT:** card care completează testarea, UX-ul, observability sau rolloutul;
- **UNKNOWN:** nu există date suficiente; nu este tratat ca PASS;
- **LEGACY:** cale păstrată temporar pentru compatibilitate/rollback, nu arhitectură v2.

## Principii de trasabilitate nenegociabile

- Frontendul primește date și le afișează. Nu primește logică de conversație/produs/comerț.
- `business_id` este server-owned și fiecare query tenant-scoped are filtrul explicit.
- PII/text/token nu intră în ledger, state, analytics, traces, feedback sau release artifacts.
- Postgres ledger este authority pentru turnurile v2; Redis/SSE/BroadcastChannel sunt optimizări/proiecții.
- Retry/recovery nu înseamnă turn nou și nu repetă autoritativ rezultatul ori mutația.
- Grounding/hard constraints/safety/P6 sunt gate-uri hard; un scor de stil/conversie nu le compensează.
- „Ca iZi” înseamnă obiective observabile de personal shopper, nu copiere de UI/protocol și nu o
  afirmație de paritate fără benchmark controlat.
- Orice diagramă/README/card este inferior codului+testului+release manifestului când descriem as-built.

## Obiectivele userului și coverage-ul lor

| ID | Obiectiv observabil | Owner principal | Support / dovadă finală |
|---|---|---|---|
| O-01 | WebWidget este singurul canal activ în Stage 1 | README / NX-228 | NX-247, NX-248, NX-249, NX-250; profiles non-web rămân OFF |
| O-02 | Frontendul doar primește și randează un contract display-ready | NX-228, NX-244 | NX-242 decoder strict, NX-245 a11y; boundary/source/bundle tests |
| O-03 | După submit nu se poate scrie/apăsa alt control creator de turn până la terminal | NX-243, NX-245 | NX-232/233 single active turn; two-tab/E2E NX-247 |
| O-04 | Refresh, offline, response loss și restart recuperează exact același rezultat | NX-232, NX-233 | NX-243, NX-247, NX-248 DR; no duplicate model/effect counter |
| O-05 | Răspunde natural ca un personal shopper: coerent, direct, util, de încredere, fără overtalk | **NX-235, NX-238, NX-239, NX-240, NX-246, NX-249** | state/retrieval/brain/grounding + blind pairwise journey eval + live quality loop |
| O-06 | Înțelege typo-uri, corecții, follow-up, clarificări și referințe „primul/acesta” | NX-235, NX-238, NX-239 | NX-234 page snapshot, NX-236 anchors, corpus NX-246 |
| O-07 | Recomandările/comparațiile sunt grounded în catalog actual și respectă hard constraints | NX-238, NX-240 | NX-210 decision: candidate search numai la GO; altfel NX-239 folosește `CurrentLiveRetrievalAdapter`; NX-246/247 gates |
| O-08 | Page/cart context este ID-only din browser și rehidratat server-side | NX-234 | NX-235 references, NX-237 canonical cart, NX-247 journey E2E |
| O-09 | Chips/CTA nu pierd produsul și nu sunt interpretate după label | NX-236 | NX-228 ActionView, NX-244 passive action, NX-237 receipt |
| O-10 | Coșul nu confirmă mutație fără stare canonică și receipt | NX-237 | NX-236 one-shot action, NX-240 projection, NX-247 crash/replay |
| O-11 | Orice terminal este randabil; niciodată payload gol/tăcere | NX-228, NX-232, NX-240 | NX-233 failure state, NX-241 deadline, NX-247 P6 matrix |
| O-12 | Latența este bounded și măsurată, fără conexiuni DB ținute peste LLM/HTTP | NX-231, NX-241 | NX-233 executor, NX-246 SLO, NX-247/248 load/readiness |
| O-13 | Feedbackul 👍/👎 ajunge durabil la backend și intră într-o buclă reală de calitate | NX-246 | NX-236 opaque action, NX-249 ritual/canary; nu este numit CSAT fără metodologie |
| O-14 | Tenant/auth/privacy sunt enforce server-side | NX-229, NX-230 | NX-232 authorized result, NX-236 action binding, NX-248 secrets |
| O-15 | Releaseul este observabil, deployabil, rollbackable și recuperabil | NX-246, NX-248, NX-249 | NX-247 evidence packet, NX-250 as-built docs |
| O-16 | Diagramele 04a/04b1/04b2/04b3/04c descriu ce rulează, nu ce sperăm | NX-250 | 100% node/edge/invariant evidence pe SHA/digest |

### Ce înseamnă concret „personal shopper natural”

Nu se rezolvă printr-un singur prompt sau prin schimbarea modelului. Lanțul de ownership este:

```text
NX-235: memorie curentă, corecții, revocări, referințe și clarificare utilă
   ↓
NX-238: query/retrieval corect pentru typo, hard constraints și no-result
   ↓
NX-239: un singur brain vede mesajul și contextul, fără clasificator care pierde nuanța
   ↓
NX-240: Evidence/AnswerPlan → text și blocuri grounded/display-ready
   ↓
NX-246: journey corpus + rubrici blind naturalness/helpfulness/trust/no-overtalk
   ↓
NX-249: champion-vs-candidate, canary și feedback loop fără tuning online
```

Pragul de succes nu este „demo-ul pare bun”. Sunt necesare: zero hard failures; holdout sigilat;
pairwise blind; scoruri pe fiecare cohort/journey family; latență/cost în buget; feedback live cu
sample/interval; și lipsa regresiei în canary.

## Findinguri confirmate și dispoziția lor

### Backend, corectitudine și securitate

| ID | Finding confirmat la baseline | Evidence/repro seam | OWNER | SUPPORT / close gate |
|---|---|---|---|---|
| F-01 | `/web/chat` rulează pipelineul sincron în request | `src/web/app.py` → `handle_turn` | NX-233 | NX-231 short UoW, NX-241 deadline; POST v2 nu apelează LLM/tool |
| F-02 | `client_msg_id/client_turn_id` nu este o garanție end-to-end; duplicatele pot deveni reply gol | `src/worker/processor.py` duplicate branch (`reply=None`) | NX-232 | NX-233/243/247; exact result replay, model-call count=1 |
| F-03 | lockul conversației este Redis best-effort/fail-open și nu persistă resultul | NX-221 seams / lock tests și failure policy | NX-232, NX-233 | DB claim/lease/fencing + Redis-loss drive |
| F-04 | conexiunea/tranzacția poate rămâne checkout-uită peste compute/LLM | request/processor/context DB lifetime | NX-231 | NX-233/241; pool exhaustion + instrumentation NX-246 |
| F-05 | nu există ledger/status lookup autoritativ pentru refresh/restart | lipsă `web_turns`/GET result la baseline | NX-232 | NX-233 executor/GET/SSE, NX-243 recovery |
| F-06 | tenant/session/id-token/CORS semantics sunt nealiniate și public token nu poate fi secret | `src/web/app.py`, `src/webhook/app.py`, host integration | NX-229 | NX-232 result auth, NX-247 cross-tenant |
| F-07 | privacy masking este prea târziu; raw poate intra în storage/history/model | `processor.py`, `gates.py`, `context.py` | NX-230 | NX-246 sink capture, NX-247; zero canary PII |
| F-08 | page/product/cart context nu are snapshot ID-only rehidratat canonic | contract/endpoint v1 și FE current page seams | NX-234 | NX-235 reference precedence, NX-237 cart |
| F-09 | stateul actual amestecă keys/writers; constraints pot fi append-only și corecțiile reapar | `ConversationState`, merge/history/summarizer seams | NX-235 | NX-239/240/246 multi-turn corpus |
| F-10 | chipsurile v1 pierd payload/refs și labelul poate deveni mesaj/comandă | `src/channels/web/render.py`, FE handlers | NX-236 | NX-228/244/247; token byte-identic |
| F-11 | cart/wishlist local și backendul pot confirma adevăruri diferite | FE `localStorage` helpers + assistant CTA | NX-237 | NX-236 receipt/action, NX-244 removes imports |
| F-12 | `search_entities`/Match Gate sunt shadow/frozen și NX-210 poate fi `NOT_READY` | NX-209/NX-210 harness/readiness | NX-238 | înregistrează `GO|NO_GO|NOT_READY`; candidate search OFF fără GO, NX-239 continuă pe `CurrentLiveRetrievalAdapter` |
| F-13 | AnswerPlan este implementat parțial/dormant, nu authority production | NX-211 flags/runtime seams | NX-239 | NX-240 final gate, NX-246 eval |
| F-14 | multiple pre-agent routes pot pierde nuanța; single brain target nu este activ | triage/simple/current pipeline | NX-239 | NX-235/238 context, NX-246 naturalness |
| F-15 | rich/prose/comparison nu au încă un singur claim↔evidence/final projector gate | `agent.py`, `compose.py`, validator paths | NX-240 | NX-247 adversarial; NX-250 verifies AGUARD/CTAB/GRND/comparison |
| F-16 | round/tool/search budgets și deadlineul total nu sunt un contract unic | tool loop/embedding/current timeouts | NX-241 | NX-246 latency, NX-247 fault matrix; nu spune „max 3 searches” |
| F-17 | aftercare și servicii opționale pot afecta latency/reliability fără budget unificat | runner/aftercare/model/tool seams | NX-241 | NX-233 post-commit, NX-246 spans/SLO |

### Frontendul real și boundary-ul pasiv

| ID | Finding confirmat la baseline FE | Evidence/repro seam | OWNER | SUPPORT / close gate |
|---|---|---|---|---|
| F-18 | API clientul parsează preț, pune default RON, pierde variants și normalizează payloadul | `src/api/chatClient.js` (`parsePrice`, `mapProduct`, normalizers) | NX-242 | NX-228 schema/hash, identity decode test |
| F-19 | widgetul se poate monta per pagină și remonta la Store↔Product | mounts în Store/ProductDetail/App | NX-243 | singleton layout + E2E navigation NX-247 |
| F-20 | transport sincron fără timeout/result lookup; refresh pierde corelația | `sendChatMessage()` / `POST /web/chat` | NX-243 | NX-232/233 + stable ID/recovery |
| F-21 | ID-ul client poate fi creat la apel, reset/403 poate detașa conversația | `chatClient.js`, `handleReset` | NX-243 | persisted-before-I/O ID, stale-result tests |
| F-22 | inputul rămâne editabil, chips/actions arată active în timpul send | `ChatWidget.jsx`, `ChatMessage.jsx` | NX-245 | NX-243 state machine; real DOM disabled + controller guard |
| F-23 | UI simulează thinking/progress prin timere | `ChatWidget.jsx` / legacy demo | NX-244 | NX-243 SSE statuses, NX-245 live region |
| F-24 | frontendul calculează discount/badge/CTA/cart și construiește mesaje din produs | `ChatProductCard.jsx`, `ChatOffer`, cart/wishlist imports | NX-244 | boundary lint/bundle scan + NX-237 receipts |
| F-25 | criteria/memory se acumulează local și pot contrazice backendul | `ChatWidget.jsx` criteria/„Rețin” | NX-244 | NX-235 memory block/projector NX-240 |
| F-26 | frontendul inventează fallback/disclaimer/no-result și reordonează rich content | `ChatMessage.jsx` / renderer | NX-244 | NX-228/240 display-ready order, passive renderer tests |
| F-27 | feedbackul thumbs este numai local | `ChatMessage.jsx` feedback handler | NX-246 | NX-236 action token, NX-244/245 passive control |
| F-28 | accesibilitatea nu are dialog/focus/live-region/Axe gate comun | widget/components current | NX-245 | NX-247 browser matrix; WCAG 2.2 AA evidence |

### Quality, observability, production și documentație

| ID | Finding confirmat | Evidence/repro seam | OWNER | SUPPORT / close gate |
|---|---|---|---|---|
| F-29 | nu există trace OTel async complet sau metric policy low-cardinality | runner events/analytics-only | NX-246 | NX-233 trace propagation, NX-248 exporter/alerts |
| F-30 | nu există SLI denominators/burn/completeness; no-data poate părea verde | lipsă `slo_policy`/report | NX-246 | NX-241 thresholds, NX-249 gates |
| F-31 | golden/qrels nu măsoară singure journey naturalness/trust/no-overtalk | existing `src/evals`, NX-210 | NX-246 | NX-249 champion/candidate ritual |
| F-32 | deployul consumă `latest`, face SSH/git pull/up și nu promovează același digest verificat | `.github/workflows/deploy.yml`, `docker-compose.prod.yml` | NX-248 | NX-249 release policy |
| F-33 | supply chain nu are lock hashes/SBOM/provenance/signature/gate scan complet | Docker/CI workflows | NX-248 | digest evidence NX-247/249 |
| F-34 | healthcheckul API verifică doar socketul; worker/readiness semantică lipsește | `docker-compose.prod.yml` | NX-248 | NX-246 metrics/SLO, NX-249 promotion |
| F-35 | backup/restore/Redis-loss RPO/RTO nu sunt demonstrate | deploy/runbooks actuale | NX-248 | NX-249 blocked dacă UNVERIFIED |
| F-36 | nu există stable canary/controller/drain/decision packet pentru v1→v2 | flags/routes curente | NX-249 | NX-246/247/248 gates |
| F-37 | nu există un singur gate cross-repo pentru contract, recovery, renderer și failures | repo-uri/teste separate | NX-247 | consumat de NX-249 |
| F-38 | diagramele 4a/4b/4c descriu pipelineul legacy și conțin drifturi TVAL/budgets/specs/grounding | `docs/ARCHITECTURE-WORKFLOWS.md` | NX-250 | **PARȚIAL (2026-08-18).** Cele 9 drifturi sunt urmărite în cod și dispuse în `docs/architecture/04-EVIDENCE.md`; TVAL/budgets/AGUARD/CTAB/GRND/spec_numbers corectate în doc. Registrul 100% node/edge/invariant + diagramele 04a–04c rămân BLOCATE: cutoverul n-a avut loc și producția rulează un artefact anterior NX-233/248 (măsurat) |

## Matricea cardurilor: ownership și dovada de ieșire

| Card | Ownership unic | Artefacte/gate de ieșire | Nu are voie să facă |
|---|---|---|---|
| NX-228 | `web-view.v2`, schema/hash, ViewModel/action/error lifecycle | contract fixtures + Pydantic/JSON Schema + v1 compatibility | business logic FE sau v1 shape mutation |
| NX-229 | edge tenant/session/identity/origin/rate limit | crypto/CORS/cross-tenant tests | tenant din browser; CORS ca auth |
| NX-230 | privacy boundary raw→safe | storage/history/model/log/trace sink tests | raw vault implicit ori DLP client-side |
| NX-231 | short UoW/pools/admission | pool exhaustion/cancellation tests; no conn over LLM | ledger/executor duplicat |
| NX-232 | durable turn ledger/idempotency/replay/atomic seam | DB race/fencing/exact replay/P6 | SSE/FE local dedupe ca authority |
| NX-233 | async serial executor/lease/recovery/GET/SSE | crash matrix, Redis-loss, one active turn | token streaming/CoT sau Redis-only correctness |
| NX-234 | ID-only page/cart snapshot și rehidratare | tamper/stale/cross-page tests | facts/labels client-owned |
| NX-235 | ConversationStateV2/reducer/clarify/references | multi-turn corrections/ordinal/revision tests | cart lines/actions/model activation |
| NX-236 | sealed opaque actions/kernel/replay | crypto/source/state/tenant/concurrency tests | label parsing/arbitrary tool invocation |
| NX-237 | canonical cart adapter/receipts | idempotency/reprice/stale/crash tests | localStorage success confirmation |
| NX-238 | controlled `search_entities` decision/promotion | outcome `GO|NO_GO|NOT_READY` + qrels/hard filter/no-result evidence; promovează numai la GO | bypass freeze, candidat ON fără GO sau relax hard constraints |
| NX-239 | single principal agent production path | shadow→candidate eval, no pre-classifier nuance loss | model swap on intuition |
| NX-240 | Evidence/AnswerPlan validation + WebView projector | claim/fact/action/block/P6 fixtures | frontend display repair |
| NX-241 | total deadline, budgets, batching, aftercare | latency/cost/fault/load packet | unbounded retries sau „max 3 searches” ambiguity |
| NX-242 | strict generated FE decoder | schema/hash/identity/no-coercion tests | defaults/normalization/v1→v2 magic |
| NX-243 | singleton transport/state/recovery | double-submit/refresh/two-tabs/offline tests | semantic retry or local transcript authority |
| NX-244 | passive finite block renderer | boundary lint, bundle scan, action byte identity | cart/ranking/copy/fallback logic |
| NX-245 | single-flight UX și WCAG | disabled/focus/Axe/keyboard/browser evidence | unlock la timeout ori copy semantic local |
| NX-246 | OTel, SLO, feedback și personal-shopper eval | privacy capture, SLI report, sealed pairwise gate | CSAT claim, online learning, hard-gate averaging |
| NX-247 | cross-repo E2E/failure gate | immutable evidence packet pentru digests exacte | fixuri ascunse în harness sau repo mixt |
| NX-248 | deploy/secrets/readiness/supply chain/DR | signed digest, smoke, rollback, restore/RPO/RTO | `latest`, down migration, live destructive drill |
| NX-249 | stable canary/cutover/quality ritual | stage decision packets, ≤5m force-control, drain | auto-promotion sau FE feature routing |
| NX-250 | as-built diagrams/runbook sync | 100% evidence registry 04a–04c + legacy v1 | schimbări de cod ori arrows aspirative |

## Graful dependențelor hard

Lista este intenționat explicită pentru `/task stage1/NX-XXX`; Claude verifică simbolurile în main și
se oprește dacă lipsesc.

```text
NX-228
├─ NX-229 ───────────────┐
├─ NX-230 ── NX-234 ── NX-235 ── NX-236 ── NX-237
└─ NX-231 ── NX-232 ── NX-233 ───────────────┐
                   └──────────── NX-246       │

NX-203 + NX-209 + NX-210 decision gate + NX-234 ── NX-238
NX-211 + NX-233 + NX-235 + NX-238 adapter decision ── NX-239
NX-228 + NX-234 + NX-236 + NX-239 ── NX-240
NX-231 + NX-233 + NX-238 + NX-240 ── NX-241

NX-228 ── NX-242
NX-229 + NX-232 + NX-233 + NX-242 ── NX-243
NX-236 + NX-240 + NX-242 ── NX-244
NX-243 + NX-244 ── NX-245

NX-233…NX-246 ── NX-247
NX-232 + NX-233 + NX-246 ── NX-248
NX-241 + NX-247 + NX-248 ── NX-249
NX-247 + NX-248 + NX-249 ── NX-250
```

NX-246 începe după NX-228/NX-232, dar production export/feedback/eval integration consumă ulterior
NX-230/233/236/240/241/247. Acestea sunt activation gates, nu permisiune de a construi duplicate.

## Execution waves și gates

| Gate | Wave/carduri | Condiție de ieșire | Ce blochează |
|---|---|---|---|
| G-A Contract & trust boundary | NX-228–230 | v2 schema/hash, tenant/session/CORS, raw→safe boundary | orice v2 business implementation |
| G-B Durable execution | NX-231–234 + NX-246 skeleton | short UoW, ledger, async serial/recovery, canonical snapshot, trace seam | frontend transport și brain promotion |
| G-C Conversation & commerce intelligence | NX-235–240 | state/action/cart/single brain/grounded projector; NX-238 alege candidate search numai la NX-210 GO, altfel adapterul live curent | claim de quality/personal shopper |
| G-D Latency & passive frontend | NX-241–245 | deadlines/budgets + decoder/singleton/renderer/a11y/single-flight | cross-repo release candidate |
| G-E Evidence & production hardening | NX-246–248 | SLO/feedback/eval + E2E packet + signed deploy/readiness/restore | production canary |
| G-F Canary & cutover | NX-249 | internal→demo→5→20→50→100, gates, soak/drain/approval | v1 public close și Stage 1 close |
| G-G As-built truth | NX-250 | diagrams/runbooks match exact release SHA/digests | declararea documentației Stage 1 complete |

### Global hard gates la fiecare wave

- `ruff check .`, `ruff format --check .`, `pytest -x -q` pe backend;
- gate-urile `lint/typecheck/test/build/contract/E2E` definite de NX-247 pe frontend;
- fiecare query nou: explicit `business_id = $1` + RLS/grants test;
- zero telefon/PII/token/body în logs, metrics, trace, state, ledger, feedback sau artifacts;
- fiecare terminal: ViewModel randabil non-empty;
- retry same ID: același result, fără al doilea model/tool mutabil;
- DB/pool: zero connection held peste LLM/HTTP/backoff/SSE;
- migrations: următorul număr liber verificat după fetch; **niciun card nu hardcodează 039**;
- model/runtime/ranker change: D15 evaluation, nu intuiție.

## Lanțul cross-repo de artefacte

```text
Backend NX-228
  web-view.v2.schema.json + schema hash + canonical fixtures
        ↓ generated, never hand-copied
Frontend NX-242
  standalone strict decoder + recorded source hash
        ↓
NX-243/244/245
  transport/recovery + passive registry + a11y/single-flight build
        ↓ exact backend SHA + frontend SHA
NX-247
  cross-repo contract/E2E/failure evidence packet
        ↓ exact image digest/SBOM/provenance
NX-248
  staging readiness/smoke/rollback/restore packet
        ↓ exact quality/SLO/feedback policy versions
NX-246 + NX-249
  candidate decision packet → canary/cutover
        ↓ exact deployed manifest
NX-250
  as-built 04a/04b1/04b2/04b3/04c evidence registry
```

Un artifact de la alt SHA, schema hash, candidate digest, window sau policy revision este mismatch și
nu poate fi „aproape același”.

## Journey E2E care închide cerința userului

Journey-ul minim de release trebuie să demonstreze împreună, nu în teste izolate:

1. Userul este pe o pagină de produs; bootstrapul trimite numai context IDs/opaque handles.
2. Scrie cu typo și diacritice lipsă; backendul rehidratează pagina și caută tenant/locale-safe.
3. După submit, input/mic/chips/feedback/New chat devin inactive până la terminal.
4. Răspunsul recomandă grounded, scurt și natural; blocurile sunt display-ready și în ordinea backend.
5. „Al doilea, dar fără parfum” rezolvă ordinalul pe display revision și supersede criteriul vechi.
6. „Compară-le” produce comparison trecut prin final gate, fără cifre/claimuri inventate.
7. O action token opac este retrimis byte-identic; labelul schimbat nu îi schimbă semantica.
8. Add-to-cart confirmă numai după canonical rehydrate/reprice/receipt.
9. Response loss + refresh + worker restart recuperează același turn/result, un singur model/effect.
10. Feedbackul ajunge tenant-safe în backend; UI doar afișează receiptul.
11. Trace-ul leagă toate etapele fără text/PII; SLO/eval atribuie exact release track.
12. Candidate defect cade la hard gate și rollbackul oprește accepturile candidate noi în ≤5 minute.

Corpusul NX-246 extinde acest journey cu: clarificare, corecții, no-results, page change, stale cart,
hard constraints, ordinals, two tabs, offline și overtalk. Blind reviewers nu văd modelul/releaseul.

## Dispoziția taskurilor/abordărilor anterioare

| Element anterior | Dispoziție Stage 1 | Card authority |
|---|---|---|
| NX-221 Redis lock | REUSED ca optimizare temporară, nu correctness authority | NX-232/233 |
| NX-225/226/227 | REUSED: embedding deadline, lexical ranking, unmapped need telemetry | NX-238/241/246 |
| NX-209 | REUSED shadow; promotion măsurată | NX-238 |
| NX-210/H3 | HARD GATE pentru candidate search: NX-238 înregistrează `GO|NO_GO|NOT_READY`; fără GO candidatul rămâne OFF, dar NX-239 continuă pe `CurrentLiveRetrievalAdapter` | NX-238; NX-239 consumă adapterul ales |
| NX-211 AnswerPlan | REUSED/dormant; nu al doilea AnswerPlan | NX-239/240 |
| NX-212/213 | ABSORBED | NX-235/239/241 |
| NX-214/215 | ABSORBED | NX-246/249 |
| NX-24 web context | SUPERSEDED pentru v2 | NX-234 |
| NX-188/189 | FROZEN până la GO NX-210 | NX-238 |
| branch NX-181–184 | fără cherry-pick automat; numai idei/teste revalidate | NX-239/246 |
| `docs/FRONTEND-CONTRACT-IZI.md` v1 | LEGACY separat; nu shape mutation | NX-228/249/250 |
| diagrame 04 legacy | HISTORICAL până la as-built sync | NX-250 |

## Ce nu putem afirma la final fără evidence

- „Este exact ca iZi/eMAG” — nu avem acces la arhitectura internă și nu copiem produsul; putem afirma
  doar rezultatele măsurate pe propriile rubrici/journey-uri.
- „Exact once” pentru request, LLM, tool, network sau provider commerce — sistemul folosește
  at-least-once + idempotency/fencing/receipts unde este demonstrat.
- „Zero hallucinations” în general — putem impune zero violations pe release suites și monitoriza
  incidents, cu scope/corpus/version explicit.
- „CSAT” din thumbs — este positive-feedback rate până la metodologie/sample adecvat.
- „Production-ready” doar pentru că CI este verde — cere readiness, signed digest, smoke, DR,
  restore, canary și rollback evidence.
- „Frontend fără logică” doar din review vizual — cere import/bundle/source boundary tests și E2E.

## Out of scope comun

- WhatsApp/Telegram/proactive, voice/image, plăți în chat și autentificare storefront completă;
- framework agentic nou, fine-tuning, vector DB separat sau model swap nemăsurat;
- redesign vizual, remote HTML/CSS/JS/SVG sau migrarea întregului demo store;
- coșul global al magazinului din afara widgetului, exceptând adaptorul canonic pe care widgetul îl
  consumă prin backend;
- Stage 2 multi-region/enterprise admin analytics; Stage 1 construiește seam-uri și dovezi.

## Checklist de închidere a trasabilității

- [ ] Fiecare F-01…F-38 are PR/commit, DoD verification și evidence link ori este declarat OPEN.
- [ ] Fiecare O-01…O-16 are cel puțin un journey/test observabil, nu doar documentație.
- [ ] NX-238 a înregistrat `GO|NO_GO|NOT_READY`; candidate search este activ numai la GO, iar
  NX-239 folosește `CurrentLiveRetrievalAdapter` la `NO_GO|NOT_READY`.
- [ ] Contract/schema/hash/backend/frontend SHAs sunt identice cu evidence packetul NX-247.
- [ ] SLO/quality/feedback/deploy/DR artifacts corespund exact candidate digestului NX-249.
- [ ] Hard constraints, grounding, tenant/privacy/P6/idempotency au zero failures în release suite.
- [ ] Naturalness/helpfulness/trust/no-overtalk au blind pairwise gate și cohort breakdown.
- [ ] Inputul și toate submit controls sunt disabled până după terminal apply.
- [ ] Canary a parcurs etapele/time/sample și are approval; v1 close are soak/drain evidence.
- [ ] NX-250 dovedește 100% node/edge/invariant și marchează v1 legacy separat.
- [ ] Niciun migration/card nu a presupus 039; registrul real a fost verificat la implementare.
- [ ] Codex verify și CI sunt verzi; userul a făcut merge/promotion explicit.
