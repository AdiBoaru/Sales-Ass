# Response Quality — index epic (NX-180..189)

> ## ⚠️ Dispoziție sub ADR Quality Overhaul 2026 (ratificat 2026-07-23)
> [`docs/QUALITY-OVERHAUL-2026.md`](QUALITY-OVERHAUL-2026.md) (secțiunea 2bis) stabilește
> pentru cardurile acestui epic:
> - **NX-180..184** (naturalețe/Track A+B) — **INDEPENDENT**, continuă normal.
> - **NX-185** (QuerySpec shadow) — **REUSED în shadow**; ownership-ul ȚINTĂ al extracției se
>   mută la agentul principal (D11), decis la NX-210. Extinderea cu `RuntimeQuerySpec` /
>   `SafeQuerySpec` + 3 reprezentări = NX-208.
> - **NX-186** (typed facet registry + coverage) — **REUSED, dependență HARD a NX-209**.
> - **NX-187** (Match Gate shadow + recall) — **REUSED, dependență HARD a NX-209**, care îl
>   consumă ca treaptă internă (nu îl reimplementează).
> - **NX-188, NX-189** — 🧊 **FROZEN** până la GO-ul de la NX-210.
>
> **Enum canonic în tot sistemul: `MATCH | MISMATCH | UNKNOWN`** (D7). `NO_MATCH` nu se folosește.

**Status carduri:** curățate după runda 2 Codex (corecții INTEGRATE în corp, fără secțiuni „Review fixes" duplicate) · **Data:** 2026-07-18
**Status EXECUȚIE (2026-07-31):** pasul 0 și NX-180 sunt **livrate în `main`**; următorul pas este **baseline v2 după #252/#253** — vezi blocul de mai jos. (Sursa de adevăr pentru stare = `origin/main`, nu tabelele din acest index.)
**Brief istoric:** [AGENT-RESPONSE-QUALITY-CLAUDE-REVIEW.md](AGENT-RESPONSE-QUALITY-CLAUDE-REVIEW.md) — SUPERSEDED de acest index + carduri (banner adăugat); acolo unde diferă, cardurile câștigă.
**Istoric review:** runda 1 = APPROVE WITH REQUIRED CHANGES (contracte); runda 2 = APPROVE WITH REQUIRED CLEANUP (contradicții interne carduri) — ambele aplicate.

## Obiectiv
Răspunsuri naturale (fără structură template), directe la turul curent, cu selecție corectă — păstrând grounding-ul (prețuri/linkuri/stoc/produse/safety) și izolarea multi-tenant.

## Pasul 0 — LIVRAT (PR #233, merged 2026-07-18) ✅
P0 safety: `route=clarify` direct ocolea gate-ul de contraindicații. Reparat în `main`, pe două straturi:
- [`triage.py`](../src/worker/stages/triage.py) — `safety = _safety_sensitive(ctx)` (= `SafetyPolicy.for_turn` = state persistat ∪ turul curent, fail-safe `True`) se calculează **înainte** de branch-are pe rută și acoperă AMBELE căi spre clarify: downgrade-ul `confidence=low` ȘI `clarify` DIRECT de la nano → forțat `sales`, event `triage_safety_kept_sales{from_clarify}`.
- [`runner.py`](../src/worker/runner.py) — `safety_compose.enforce(ctx)` se aplică pe reply-ul FINAL al oricărei căi, inclusiv early-exit din orice stagiu.
- Fără buclă de ocolire prin resume: `resume_route` are un singur caller în `src/`, cu `Route.SALES`.
- Teste: `tests/test_triage_clarify_general.py` (inclusiv contrastul „fără safety → clarify rămâne clarify").

## Baseline — stare reală și pasul următor
**NX-180 e livrat** (PR #234, merged 2026-07-19): `scripts/sim/eval_run.py` + `eval_gates.py` + `eval_judge.py`, 18 conversații / 38 tururi / 20 follow-up × 3 rulări, artefact în `qa-suite/baselines/baseline-v1.json`.

**baseline-v1 NU mai e o referință validă de decizie** (fotografie a sistemului de la 2026-07-18):
pinurile lui sunt `catalog_signature: n=654`, judge `v1`, tarife LLM vechi. Între timp au aterizat #238 (catalog v3), #241 (NX-216 semantic cache), #246 (NX-208 query understanding), #249/#250/#251.

**Următorul pas: baseline v2, o singură rulare, DUPĂ ce intră #253 (NX-201 — tarifele LLM erau subevaluate 2,25–4x) și #252 (NX-204a — judge pinuit + cost per apel).** Ordinea contează: ambele schimbă exact pinurile pe care raportul le înregistrează, deci un baseline rulat înainte ar trebui rulat din nou. Apoi paired OFF/ON pe `prompt_vnext_enabled` = decizia reală NX-181; abia după aceea se decide dacă NX-183/184 (envelope/mixed-intent) se activează sau doar se calibrează.

> Baseline v2 **nu există încă** — nu citi cifrele din `baseline-v1.json` ca stare curentă a sistemului.

## Track A — Response Quality (naturalețe; imediat)
| Card | Ce | Cplx | Depinde de | Kill-switch |
|---|---|---|---|---|
| [NX-180](../tasks/NX-180.md) | Evaluator + baseline (reproductibil, paired ON/OFF) | M | — | (tooling) |
| [NX-181](../tasks/NX-181.md) | Prompt vNext + `response_shape` minimal + anti-repetiție | S | NX-180 | `prompt_vnext_enabled` |
| [NX-182](../tasks/NX-182.md) | `relaxed_constraints` + disclosure determinist (registru minimal de labels inclus) | S/M | NX-180 — independent de Track B | `relaxed_disclosure_enabled` |
| [NX-183](../tasks/NX-183.md) | Envelope V2-light + renderer text-only + `answer` inline | M | 180,181 + gate decizie | `response_envelope_v2_enabled` (per business) |
| [NX-184](../tasks/NX-184.md) | Planner `response_shape`+`obligations` + FAQ mixed-intent | M | NX-183 | `response_shape_hints_enabled` |

> **Stare (2026-07-31):** NX-180 merged (#234) — **tooling offline, fără kill-switch runtime** (vezi cardul); NX-181 merged (#235) și NX-182..184 merged (#236) — **acestea cu kill-switch-ul default OFF**, deci construite dar **nemăsurate ON** pe catalogul de azi. Coloana „Depinde de" descrie ordinea de DECIZIE (gate pe baseline), nu de cod: codul e deja în `main`.

## Track B — Selection Correctness (shadow-first; separat, nu blochează A)
| Card | Ce | Cplx | Depinde de | Kill-switch |
|---|---|---|---|---|
| [NX-185](../tasks/NX-185.md) | QuerySpec **shadow** (doar detecție) | M | — | `query_spec_shadow_enabled` |
| [NX-186](../tasks/NX-186.md) | Typed facet registry + coverage (per business+category+facet) | M/L | NX-185 | (config) |
| [NX-187](../tasks/NX-187.md) | Match Gate shadow (MatchSet DISJUNCT) + **recall vs scan exhaustiv** | M/L | 185,186 | `match_gate_shadow_enabled` |
| [NX-189](../tasks/NX-189.md) | Typed facets SQL **tri-state** shadow per fațetă (candidate-recall) | L | 186,187 | `typed_facet_sql_enabled` (per fațetă) |
| [NX-188](../tasks/NX-188.md) | Match Gate enforce + **QuerySpec enforce** + alternatives UX (per fațetă, după 189) | M | 187,186,183, **189-per-fațetă** | `match_gate_enforce_enabled` (per business) |

> **Stare (2026-07-31):** fundația NX-185..187 e în `main` (#236, apoi #249 typed facet registry + coverage + match gate shadow, cu `pool_recall 0,333`); NX-208 (#246) a preluat extinderea QuerySpec. NX-188/189 rămân 🧊 FROZEN până la GO-ul NX-210, conform dispoziției ADR de sus.

## Reguli transversale (din contra-review)
- **Recall precede enforcement:** o fațetă hard se enforce-uiește (NX-188) DOAR dacă participă în retrieval (NX-189-per-fațetă, tri-state, shadow întâi) — `MAX_SEARCH_POOL=24` face ca enforcement post-retrieval să dea false-negative altfel. NX-189 depinde de 186+187, NU de 188.
- **MatchSet disjunct (precedență):** rejected (≥1 hard MISMATCH) → alternatives (0 hard MISMATCH, ≥1 hard UNKNOWN) → exact (toate hard MATCH). Soft = doar ranking, nu apartenență.
- **Shadow ≠ enforce:** shadow doar detectează/măsoară; prevenirea (SearchArgs = proiecție obligatorie, hard neslăbibil, test 150→80) e enforcement, cu **owner = NX-188** (nu există NX-185b).
- **Mixed-intent pre-FAQ TRI-STATE:** `MixedIntentDecision = PURE_FAQ | POSSIBLE_MIXED | UNKNOWN` (DomainPack, fără LLM); DOAR PURE_FAQ permite early-exit; UNKNOWN → pipeline complet (NX-184).
- **Vocabular unic:** `response_shape` (JSON + `ResponsePlan.response_shape`), `relaxed_constraints` — zero sinonime în cod.
- **evidence_ids opace** (e1,e2 din cod), nu căi semantice inventabile; motivele factuale compuse determinist de cod din evidence validate.
- **Flag per-business** = global master switch AND `businesses.settings` opt-in; lipsă→OFF, invalid→fail-closed, rollback per business.
- **Cache:** namespace `envelope_version`/`prompt_version` (pre-triaj); `response_mode` NU în cheie V1; direct/detail/repeat = `cacheable=False`.
- **Baseline** = fotografia realității (incl. eșecuri), nu poartă verde.

## Criterii numerice (transversale, în DoD-ul relevant)
naturalețe+relevanță ≥4/5 pe ≥90% cazuri · follow-up corect ≥95% (cu destule tururi follow-up) · 0 hard MISMATCH ca „exact" · 0 preț/link/produs inventat · 0 deschideri identice în 2 tururi consecutive · p95 per-tur +≤10% vs baseline · apeluri LLM ne-crescute.

## Ordine de execuție recomandată
1. ~~Fix + reverificare PR #233.~~ ✅ merged 2026-07-18, reverificat static în `main` 2026-07-31.
2. ~~NX-180 (evaluator)~~ ✅ merged; baseline-v1 măsurat, dar **expirat** ca referință (pinuri vechi).
   **2bis (următorul pas real):** merge #253 → #252 → **o singură rulare baseline v2** pe evaluatorul stabil + catalogul actual.
3. NX-181 → paired OFF/ON pe `prompt_vnext_enabled` vs baseline v2 = gate de decizie → NX-182 (paralel, independent).
4. NX-183 → NX-184 (dep strict unidirecțională 184→183).
5. Track B, lanț complet per fațetă:
   `NX-185 (QuerySpec shadow) → NX-186 (registru + coverage) → NX-187 (Match Gate shadow + recall exhaustiv)
   → NX-189 (SQL tri-state SHADOW pe prima fațetă) → verificare paritate exact/alternatives/rejected + recall
   → NX-188 (enforce pe ACEEAȘI fațetă, business pilot) → repetă per fațetă → canary 5%→25%→100%.`
