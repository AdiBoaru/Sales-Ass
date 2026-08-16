# NX-241 — Deadline total de tur, bugete de execuție, batching și aftercare în afara căii

**Status:** implementat, DARK (`TURN_DEADLINE_ENABLED=false`) · **Baseline:** `origin/main@c39e548`
**Cod:** `src/runtime/deadline.py`, `src/runtime/turn_budget.py`, `src/agent/tool_budget.py`,
`src/observability/turn_latency.py` · **Probă:** `python scripts/sim/web_latency_probe.py`

---

## 1. Problema, în cifre

Înainte de cardul ăsta, fiecare strat avea ceasul LUI:

| strat | plafon | de câte ori pe tur |
|---|---|---|
| apel de model (`llm_timeout_s` × `llm_retry_max`) | 30s × 3 = **90s** | triaj + 1-3 runde + repair + critic |
| embed de query (`embed_timeout_ms`) | 800ms | 1-2 |
| retrieval prin port (`retrieval_deadline_ms`) | 0 (dezactivat) | 1-3 |
| turul web (`web_turn_deadline_s`) | 120s | 1 |

Timeouturile astea se **înmulțesc**, nu se împart: nimeni nu întreba „cât mai am". Un singur
provider lent putea consuma minute pe un tur pe care clientul îl aștepta de trei secunde, iar
`turn_over_budget` (NX-161) doar CONSTATA depășirea, post-factum, fără să oprească nimic.

## 2. Contractul nou

Un tur are **un singur deadline monoton**, născut o dată din `web_turns.deadline_at` (fixat la
accept, NX-233) și **neprelungit la reclaim**. Fiecare operație primește `min(capul ei, cât a mai
rămas − rezerva terminală)`. Rezerva terminală (validator + fallback + commit) e cea care face
diferența dintre „timeout onest" și „tăcere".

```
accept (deadline_at)                                          terminal commit
   │                                                                 │
   ├── queue/admission ──┬── load ── gates ── retrieval ── model ── tools ── validation ──┤
   │  (consumă bugetul)  │                                                     │ rezervă  │
   └─────────────────────┴──── TurnDeadline.remaining_ms() ────────────────────┴──────────┘
                                                       aftercare (buget PROPRIU, post-terminal)
```

**Ce NU face:** nu sacrifică factualitatea. Hard constraints, safety (NX-173), grounding (NX-240)
și validatorul rămân neatinse — la epuizare se livrează un răspuns determinist mai SĂRAC, niciodată
unul mai puțin verificat.

## 3. Manifestul de bugete (`nx241.2026-08-16`)

Versionat în cod (`src/runtime/turn_budget._BASE`), cu totalul per clasă configurabil din env.
Versiunea intră în `turn_latency` → o cifră de latență e mereu atribuibilă unui set de plafoane.

| clasă | total | model | retrieval | tools | validare | rezervă | runde | tool calls | paralel | mutații | repair |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `exact` | 3.0s | 2.0s | 0.8s | 1.2s | 0.3s | 0.4s | 1 | 2 | 2 | 0 | 0 |
| `recommendation` | 6.0s | 4.5s | 1.5s | 2.5s | 0.5s | 0.6s | 2 | 4 | 3 | 0 | 1 |
| `complex` | 10.0s | 7.0s | 2.5s | 4.0s | 0.8s | 0.8s | 3 | 6 | 3 | 0 | 1 |
| `mutation` | 8.0s | 5.0s | 1.5s | 3.0s | 0.6s | 0.8s | 2 | 4 | 2 | 2 | 1 |

Plafon DUR peste tot: `TURN_HARD_DEADLINE_MS=15000`. Tokeni/cost: `max_tokens` per clasă +
`turn_cost_budget_usd` ca plafon de cost. Milisecundele de fază sunt **capuri**, nu o alocare
care se adună: deadline-ul rămâne singura sumă.

**Clasificarea** e deterministă (`turn_budget.classify`), din rută + acțiune + intenție de
cumpărare, re-legată o dată când triajul/kernelul de acțiune decid — contoarele consumate rămân.
Modelul **nu poate cere mai mult buget prin output** (ca `business_id`, P7).

## 4. SLO Stage 1 (provizoriu, ratificat de NX-246 înainte de canary)

| clasă | țintă | p90 |
|---|---|---|
| POST accept | — | ≤ 300ms (p99 ≤ 800ms) |
| fapt simplu | 1,5–3s | ≤ 3s |
| recomandare | 3–6s | ≤ 6s |
| comparație/mixt | 6–10s | ≤ 10s |
| global | — | < 12s, hard deadline 15s |

Pragurile NU se mută după ce se văd rezultatele candidate. Latența e RAPORTATĂ până când NX-246
le ratifică; „no-data" nu e verde.

## 5. Ce se schimbă concret la fiecare treaptă de flag

| flag | ce face | ce NU face |
|---|---|---|
| `TURN_LATENCY_SPANS_ENABLED` (default **ON**) | un event `turn_latency` per tur: faze, degradări, contoare de buget | nu impune nimic |
| `TURN_DEADLINE_ENABLED` (default OFF) | deadline propagat prin ContextVar la LLM/embed/retrieval/tools/admission; oprire cu fallback determinist | nu refuză apeluri pe număr |
| `TURN_BUDGET_ENFORCED` (default OFF) | plafoanele de apeluri REFUZĂ typed (`tool_budget`); modelul primește text scurt și onest | nu schimbă timpul |
| `TURN_PARALLEL_READS_ENABLED` (default OFF) | citiri independente concurent, până la plafonul clasei | nu paralelizează mutații (imposibil prin construcție) |

Poarta de boot refuză combinațiile imposibile: enforce sau paralelism fără deadline, `LLM_CALL_CAP_MS`
peste plafonul dur, manifest cu rezervă terminală lipsă. **Fail-fast**, fiindcă alternativa e
„nelimitat în tăcere" cu un flag aprins care sugerează contrariul.

## 6. Matricea de eșecuri (comportament impus, nu sperat)

| caz | comportament |
|---|---|
| apelul de model consumă aproape tot bugetul | fără retry/critic; validator + fallback + commit au rezervă |
| 429 cu `Retry-After` peste ce a mai rămas | **nu dormim**; degradare terminală (`llm_retry_no_budget`) |
| provider care atârnă | tăiat de `asyncio.timeout` derivat din buget (nu de 30s × 3) |
| embed/rerank timeout | fallback lexical cu degradare cu cod fix; hard gates păstrate |
| tool storm cerut de model | refuz TYPED la plafon; modelul încheie cu ce are |
| read + mutation în aceeași rundă | citirile în paralel, mutațiile seriale, nimic speculativ |
| rezultat de tool supradimensionat | trunchiere bounded pe OCTEȚI, cu notă de acoperire explicită |
| coada depășește deadline-ul | admission respinge `deadline_exceeded`, nu începe lucru inutil |
| reclaim de worker | ACELAȘI `deadline_at`, deci ce a rămas — nu încă 15s |
| aftercare lent / backlog | rezultatul rămâne terminal; aftercare abandonat la `AFTERCARE_DEADLINE_MS` |
| buget invalid în config | procesul **nu pornește** |

## 7. Observabilitate (low-cardinality, zero PII)

Un singur event per tur, `turn_latency`, cu: `e2e_ms` + `e2e_bucket`, `phases{n,ms,outcomes}`,
`turn_class`, `budget_version`, `budget_enforced`, `model_calls_bucket`, `tool_calls_bucket`,
`tokens`, `cost_usd`, `deadline_*`, `degradations`, `exhausted{fază→motiv}`.
Separat: `turn_deadline_exhausted{phase,reason}`, `tool_budget{name,outcome,reason}`,
`turn_class_bound`, `aftercare_lag_ms{outcome}`.

Vocabularele sunt ÎNCHISE (`deadline.PHASES`, `turn_budget.DIMENSIONS`, outcome-urile din
`turn_latency`) — o etichetă construită din date de client ar fi o scurgere de cardinalitate.

## 8. Runbook

**Rollout.** (1) Lasă spans ON o săptămână și citește `turn_latency` per clasă (cold/warm separat).
(2) `TURN_DEADLINE_ENABLED=true` pe tenantul demo, cu totalurile mai GENEROASE decât p99 măsurat.
(3) Strânge totalurile spre SLO. (4) `TURN_BUDGET_ENFORCED=true`. (5) `TURN_PARALLEL_READS_ENABLED`
DOAR unde `deps.db` e `tenant_db` (conn-per-op); pe conexiune partajată codul rămâne la 1 oricum.

**Rollback.** Stinge în ordine inversă. Un rollback complet readuce comportamentul de dinainte —
dar `web_turn_deadline_s` (NX-233) rămâne activ, deci nu revenim niciodată la „retry nelimitat".

**Simptome → cauză.**
- `turn_deadline_exhausted{phase=model}` în creștere → provider lent; verifică `llm_retry`/
  `llm_retry_no_budget` în `degradations` înainte să urci bugetul.
- `tool_budget{outcome=rejected,reason=tool_calls}` frecvent → modelul buclează; e semnal de prompt,
  nu de plafon prea mic.
- `aftercare_lag_ms{outcome=timeout}` → summarizer/profil lent; nu atinge clientul, dar ține workerul.
- `admission{reason=deadline_exceeded}` → coada mănâncă bugetul: scalează workerii, nu deadline-ul.

## 9. Ce a rămas în afara cardului

Streaming de tokeni, răspunsuri speculative, progres fals, schimbări de model/reasoning,
multi-cloud, trucuri de latență percepută în frontend. Frontendul rămâne renderer pasiv: nu decide
timeoutul semantic, nu face retry cu ID nou, nu simulează stadii (`docs/WEB-WIDGET-BOUNDARY-V2.md`).

Pragurile finale de latență și verdictul GO/NO-GO sunt ale **NX-246/NX-247**; cardul ăsta livrează
mecanismul și măsurătoarea, nu decizia de ramp.
