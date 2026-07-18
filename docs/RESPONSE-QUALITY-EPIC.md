# Response Quality — index epic (NX-180..189)

**Status:** carduri curățate după runda 2 Codex (corecții INTEGRATE în corp, fără secțiuni „Review fixes" duplicate) — **pending re-verificare Codex** · **Data:** 2026-07-18
**Brief istoric:** [AGENT-RESPONSE-QUALITY-CLAUDE-REVIEW.md](AGENT-RESPONSE-QUALITY-CLAUDE-REVIEW.md) — SUPERSEDED de acest index + carduri (banner adăugat); acolo unde diferă, cardurile câștigă.
**Istoric review:** runda 1 = APPROVE WITH REQUIRED CHANGES (contracte); runda 2 = APPROVE WITH REQUIRED CLEANUP (contradicții interne carduri) — ambele aplicate.

## Obiectiv
Răspunsuri naturale (fără structură template), directe la turul curent, cu selecție corectă — păstrând grounding-ul (prețuri/linkuri/stoc/produse/safety) și izolarea multi-tenant.

## Pasul 0 (blocant, fără card nou)
Fix **PR #233 / NX-176a** — P0 safety: `route=clarify` direct ocolește gate-ul de contraindicații (mută gate-ul înainte de branch-are pe rută, nu pe `confidence==low`). Reverificare înainte de orice pornire NX-18x.

## Track A — Response Quality (naturalețe; imediat)
| Card | Ce | Cplx | Depinde de | Kill-switch |
|---|---|---|---|---|
| [NX-180](../tasks/NX-180.md) | Evaluator + baseline (reproductibil, paired ON/OFF) | M | — | (tooling) |
| [NX-181](../tasks/NX-181.md) | Prompt vNext + `response_shape` minimal + anti-repetiție | S | NX-180 | `prompt_vnext_enabled` |
| [NX-182](../tasks/NX-182.md) | `relaxed_constraints` + disclosure determinist (registru minimal de labels inclus) | S/M | NX-180 — independent de Track B | `relaxed_disclosure_enabled` |
| [NX-183](../tasks/NX-183.md) | Envelope V2-light + renderer text-only + `answer` inline | M | 180,181 + gate decizie | `response_envelope_v2_enabled` (per business) |
| [NX-184](../tasks/NX-184.md) | Planner `response_shape`+`obligations` + FAQ mixed-intent | M | NX-183 | `response_shape_hints_enabled` |

## Track B — Selection Correctness (shadow-first; separat, nu blochează A)
| Card | Ce | Cplx | Depinde de | Kill-switch |
|---|---|---|---|---|
| [NX-185](../tasks/NX-185.md) | QuerySpec **shadow** (doar detecție) | M | — | `query_spec_shadow_enabled` |
| [NX-186](../tasks/NX-186.md) | Typed facet registry + coverage (per business+category+facet) | M/L | NX-185 | (config) |
| [NX-187](../tasks/NX-187.md) | Match Gate shadow (MatchSet DISJUNCT) + **recall vs scan exhaustiv** | M/L | 185,186 | `match_gate_shadow_enabled` |
| [NX-189](../tasks/NX-189.md) | Typed facets SQL **tri-state** shadow per fațetă (candidate-recall) | L | 186,187 | `typed_facet_sql_enabled` (per fațetă) |
| [NX-188](../tasks/NX-188.md) | Match Gate enforce + **QuerySpec enforce** + alternatives UX (per fațetă, după 189) | M | 187,186,183, **189-per-fațetă** | `match_gate_enforce_enabled` (per business) |

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
1. Fix + reverificare PR #233.
2. NX-180 (evaluator) → baseline măsurat.
3. NX-181 → gate de decizie → NX-182 (paralel, independent).
4. NX-183 → NX-184 (dep strict unidirecțională 184→183).
5. Track B, lanț complet per fațetă:
   `NX-185 (QuerySpec shadow) → NX-186 (registru + coverage) → NX-187 (Match Gate shadow + recall exhaustiv)
   → NX-189 (SQL tri-state SHADOW pe prima fațetă) → verificare paritate exact/alternatives/rejected + recall
   → NX-188 (enforce pe ACEEAȘI fațetă, business pilot) → repetă per fațetă → canary 5%→25%→100%.`
