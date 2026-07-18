# Build log — Track A + Track B (sesiune autonomă 2026-07-18)

Ramură integrare: `feat/NX-track-ab` (stacked pe `feat/NX-181-prompt-vnext` @ 9c2c4c1).
Bază verificată verde: **1885 passed** pe NX-181. Directivă: construiește tot, self-verify riguros,
zero evaluator live. Fiecare card gated de kill-switch (default OFF → byte-identic).

Legendă: ⬜ neînceput · 🔨 în lucru · ✅ construit+self-verified (ruff+pytest) · ⏸ blocat

## Track A — Response Quality
- ✅ **NX-182** relaxed_constraints + disclosure determinist · flag `relaxed_disclosure_enabled`
  · models: `RelaxedConstraint` + `Relevance.relaxed_constraints` · catalog_tools `_relaxed_constraints`
  (base vs winning_step) · compose `_relaxed_disclosure` + registru RO/EN/HU + suprimă pick când relaxat
  · test_relaxed_disclosure (3) + compose regression (40) verzi · getattr defensiv (fail-open)
- ⬜ **NX-183** ResponseEnvelope V2-light + renderer text-only · flag `response_envelope_v2_enabled`
- ⬜ **NX-184** planner obligations + FAQ mixed-intent pre-triaj · flag `response_shape_hints_enabled`

## Track B — Selection Correctness (shadow-first)
- ⬜ **NX-185** QuerySpec shadow (contract + merger owner-unic) · flag `query_spec_shadow_enabled`
- ⬜ **NX-186** typed facet registry + coverage report
- ⬜ **NX-187** Match Gate shadow (MatchSet disjunct) + recall · flag `match_gate_shadow_enabled`
- ⬜ **NX-189** typed facets SQL tri-state (shadow per fațetă) · flag `typed_facet_sql_enabled`
- ⬜ **NX-188** Match Gate enforce + QuerySpec enforce + alternatives UX · flag `match_gate_enforce_enabled`

## Jurnal (per card: fișiere, teste, note)
_(se completează pe măsură ce construiesc)_
