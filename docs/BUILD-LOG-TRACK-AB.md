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
- ✅ **NX-183** ResponseEnvelope V2-light + renderer text-only · flag `response_envelope_v2_enabled` (per business)
  · `src/agent/envelope.py` (V2_SCHEMA, evidence OPACE `e{i}_{j}`, `compose_reason` determinist,
  `response_envelope_v2_effective`) · prompt_builder `build_v2_system` + `_V2_RULES` · finalize
  `_finalize_v2` (cards via assemble-reuse `fit_clause`=motiv compus + text-only `answer` cu lead
  scrubuit) integrat în render ÎNAINTE de rich (OFF → nu se intră → byte-identic) · test_envelope_v2 (4)
  · 325 regresie verzi. NOTĂ: calitatea end-to-end (output model) = de verificat LIVE cu evaluatorul
  (deferat); CODUL e verificat (OFF byte-identic + compunere pură). Gotcha rezolvat: ghilimea ASCII
  `"` de închidere în string non-triple-quoted (SyntaxError).
- ✅ **NX-184** FAQ mixed-intent pre-triaj + completare obligație · flag `response_shape_hints_enabled`
  · faq.py `mixed_intent_decision` (tri-state PURE_FAQ/POSSIBLE_MIXED/UNKNOWN; două clauze = semnalul
  cheie; `aveti`+DomainPack vocab; `_MIXED_POLICY_EXTRA` pt verb forms) · faq_stage: mixed → atașează
  `ctx.faq_grounded` + NU early-exit (OFF → early-exit ca azi) · TurnContext.faq_grounded · agent_stage
  `_complete_faq_obligation` (append determinist dacă politica lipsește din reply) · test_mixed_intent (5)
  + 270 regresie verzi. NOTĂ: `obligations` bogat + verificare renderer completă = live-review; aici =
  mecanismul FAQ decis (Codex) + completare deterministă. response_shape a aterizat deja în NX-181.

## Track B — Selection Correctness (shadow-first)
- ✅ **NX-185** QuerySpec shadow (contract + merger owner-unic) · flag `query_spec_shadow_enabled`
  · `src/agent/query_spec.py` PUR: `Constraint`/`QuerySpec` + `build_query_spec` (din RouteDecision,
  owner=triaj) + `merge_query_spec` (owner UNIC = modulul, nu agent.py; turul curent câștigă;
  topic-switch resetează; inherited persistă) + `fingerprint` determinist · triage shadow emit
  `query_spec_shadow` (gated, ZERO schimbare comportament) · test_query_spec (4) + 68 regresie triaj
  verzi. Enforcement (SearchArgs obligatoriu) = NX-188.
- ⬜ **NX-186** typed facet registry + coverage report
- ⬜ **NX-187** Match Gate shadow (MatchSet disjunct) + recall · flag `match_gate_shadow_enabled`
- ⬜ **NX-189** typed facets SQL tri-state (shadow per fațetă) · flag `typed_facet_sql_enabled`
- ⬜ **NX-188** Match Gate enforce + QuerySpec enforce + alternatives UX · flag `match_gate_enforce_enabled`

## Jurnal (per card: fișiere, teste, note)
_(se completează pe măsură ce construiesc)_
