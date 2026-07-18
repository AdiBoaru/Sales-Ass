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
- ✅ **NX-186** typed facet registry + coverage · `src/domain/facets.py` PUR: `FacetSpec` (key/
  value_type/operators/values/aliases/missing_policy/min_coverage, validat fail-closed la __post_init__)
  + `build_registry` (respinge duplicate) + `facet_value` (extractor din attributes + alias enum) +
  `facet_coverage` (present vs valid + enforceable: n≥10 ∧ pct≥prag) · test_facets (4) verzi. Modul
  nou, neimportat → zero regresie. NOTĂ: raportul DB per business+category = wrapper subțire (script,
  live — nefăcut aici, directivă no-live); coverage-ul PUR e gata.
- ✅ **NX-187** Match Gate (MatchSet disjunct) · `src/agent/match_gate.py` PUR: `evaluate_constraint`
  (MATCH/MISMATCH/UNKNOWN; lipsă→UNKNOWN nu MISMATCH; lte/gte/eq/contains, bool-aware) + `classify_product`
  + `match_set` DISJUNCT (precedență rejected→alternatives→exact; soft = doar ranking, nu apartenență)
  · test_match_gate (4: exemplul Codex A/B/C/D + soft ignorat + no-hard) verzi. Modul nou → zero regresie.
  NOTĂ: shadow emit în planner + recall vs scan exhaustiv = de cablat (planner) + live; logica pură e gata.
- 🟡 **NX-188** Match Gate shadow emit în planner (`match_gate_shadow`, gated, ZERO comportament) +
  flag-uri `match_gate_shadow_enabled`/`match_gate_enforce_enabled`. ENFORCE propriu-zis (filtrare
  rejected + QuerySpec projection + alternatives UX) = **LIVE-REVIEW** (behavior-changing, prerechizit
  NX-189-per-fațetă; enforce blind pe pool-24 dă false-negative). Shadow OFF byte-identic (211 regresie).
- 🟡 **NX-189** migrare `031_products_attributes_gin.sql` (index GIN additiv pe attributes, pregătire
  filtrare tipizată) + flag `typed_facet_sql_enabled`. Wiring-ul SQL de retrieval (tri-state MATCH/
  UNKNOWN, paritate shadow, recall) = **LIVE-REVIEW** (atinge SQL-ul core de retrieval, cere paritate live).

## Rezumat sesiune
- ✅ Track A COMPLET: NX-182, NX-183, NX-184 (toate kill-switch OFF byte-identic, testate).
- ✅ Track B fundație+shadow: NX-185 (QuerySpec), NX-186 (facets), NX-187 (Match Gate) — module PURE,
  testate, zero regresie.
- 🟡 NX-188/189: partea SIGURĂ construită (shadow emit + migrare + flags); ENFORCE + SQL-retrieval =
  perechea cuplată, behavior-changing, cu migrare DB → de finalizat + verificat LIVE împreună (directiva
  „no-live" + „nu face greșeli" → nu cablez enforcement orb).
- Regresie totală pe branch: rulare finală `pytest` la commit.

## Jurnal (per card: fișiere, teste, note)
_(se completează pe măsură ce construiesc)_
