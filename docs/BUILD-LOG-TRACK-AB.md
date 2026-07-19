# Build log — Track A + Track B (sesiune autonomă 2026-07-18)

Ramură integrare: `feat/NX-track-ab` (stacked pe `feat/NX-181-prompt-vnext` @ 9c2c4c1).
Bază verificată verde: **1885 passed** pe NX-181. Directivă: construiește tot, self-verify riguros,
zero evaluator live.

## ⭐ STARE CURENTĂ (SUPERSEDES orice afirmație inline mai jos) — HEAD `dupa R11` (post-00d80bc)
Stare (protocol): **SELF-TESTED** — teste locale verzi. NU „closed" (VERIFIED = re-review Codex fără
findings). Ultima regresie completă confirmatorie: **1965 passed, exit 0** (la 00d80bc; R11 adaugă
IDN/type-op → re-rulată la commit). Ce e ADEVĂRAT ACUM:
- **OFF byte-identic** pentru: prompt vNext, V2 envelope, mixed-intent, QuerySpec/facets/Match Gate
  (shadow), **medical filter (gated)**.
- **ALWAYS-ON (schimbare intenționată de siguranță, NU byte-identic)**: **URL scrub** (detectare
  GENERICĂ + IDN-aware, fail-closed) în scrub_prose/scrub_intro/scrub_education/_clean_facts/
  _evidence_facts. Un link/domeniu (incl. `.рф`/`.中国`/punycode) în proză/fapt = DROP.
- **`_clean_facts` NU garantează cifre „grounded"** — primește `raw: list[str]`, fără provenance;
  cifrele din recenzii se PĂSTREAZĂ (pre-existent), validarea lor = DEFERRED (§5).
- **Match Gate: type-op compat DA (gte/lte→number, contains→list); FacetSpec.operators allowlist =
  DEFERRED blocant înainte de NX-188** (vocabular op nerezolvat: Constraint „contains" vs FacetSpec
  „contains_any"/„in"). Vezi R11.
> Orice frază de mai jos care spune „toate OFF byte-identic" sau „numere grounded" = **SUPERSEDED**
> de blocul ăsta + secțiunile R8-R11. Le-am lăsat ca istoric al deciziei, nu ca adevăr curent.

Legendă: ⬜ neînceput · 🔨 în lucru · ✅ construit+self-verified (ruff+pytest) · ⏸ blocat

## Track A — Response Quality
- 🟡 **NX-182** relaxed_constraints + disclosure determinist · flag `relaxed_disclosure_enabled`
  · models: `RelaxedConstraint` + `Relevance.relaxed_constraints` · catalog_tools `_relaxed_constraints`
  (base vs winning_step) · compose `_relaxed_disclosure` + registru RO/EN/HU + suprimă pick când relaxat
  · test_relaxed_disclosure (3) + compose regression (40) verzi · getattr defensiv (fail-open)
- 🟡 **NX-183** ResponseEnvelope V2-light + renderer text-only · flag `response_envelope_v2_enabled` (per business)
  · `src/agent/envelope.py` (V2_SCHEMA, evidence OPACE `e{i}_{j}`, `compose_reason` determinist,
  `response_envelope_v2_effective`) · prompt_builder `build_v2_system` + `_V2_RULES` · finalize
  `_finalize_v2` (cards via assemble-reuse `fit_clause`=motiv compus + text-only `answer` cu lead
  scrubuit) integrat în render ÎNAINTE de rich (OFF → nu se intră → byte-identic) · test_envelope_v2 (4)
  · 325 regresie verzi. NOTĂ: calitatea end-to-end (output model) = de verificat LIVE cu evaluatorul
  (deferat); CODUL e verificat (OFF byte-identic + compunere pură). Gotcha rezolvat: ghilimea ASCII
  `"` de închidere în string non-triple-quoted (SyntaxError).
- 🟡 **NX-184** FAQ mixed-intent pre-triaj + completare obligație · flag `response_shape_hints_enabled`
  · faq.py `mixed_intent_decision` (tri-state PURE_FAQ/POSSIBLE_MIXED/UNKNOWN; două clauze = semnalul
  cheie; `aveti`+DomainPack vocab; `_MIXED_POLICY_EXTRA` pt verb forms) · faq_stage: mixed → atașează
  `ctx.faq_grounded` + NU early-exit (OFF → early-exit ca azi) · TurnContext.faq_grounded · agent_stage
  `_complete_faq_obligation` (append determinist dacă politica lipsește din reply) · test_mixed_intent (5)
  + 270 regresie verzi. NOTĂ: `obligations` bogat + verificare renderer completă = live-review; aici =
  mecanismul FAQ decis (Codex) + completare deterministă. response_shape a aterizat deja în NX-181.

## Track B — Selection Correctness (shadow-first)
- 🟡 **NX-185** QuerySpec shadow (contract + merger owner-unic) · flag `query_spec_shadow_enabled`
  · `src/agent/query_spec.py` PUR: `Constraint`/`QuerySpec` + `build_query_spec` (din RouteDecision,
  owner=triaj) + `merge_query_spec` (owner UNIC = modulul, nu agent.py; turul curent câștigă;
  topic-switch resetează; inherited persistă) + `fingerprint` determinist · triage shadow emit
  `query_spec_shadow` (gated, ZERO schimbare comportament) · test_query_spec (4) + 68 regresie triaj
  verzi. Enforcement (SearchArgs obligatoriu) = NX-188.
- 🟡 **NX-186** typed facet registry + coverage · `src/domain/facets.py` PUR: `FacetSpec` (key/
  value_type/operators/values/aliases/missing_policy/min_coverage, validat fail-closed la __post_init__)
  + `build_registry` (respinge duplicate) + `facet_value` (extractor din attributes + alias enum) +
  `facet_coverage` (present vs valid + enforceable: n≥10 ∧ pct≥prag) · test_facets (4) verzi. Modul
  nou, neimportat → zero regresie. NOTĂ: raportul DB per business+category = wrapper subțire (script,
  live — nefăcut aici, directivă no-live); coverage-ul PUR e gata.
- 🟡 **NX-187** Match Gate (MatchSet disjunct) · `src/agent/match_gate.py` PUR: `evaluate_constraint`
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

## Rezumat sesiune — STATUS ONEST (corectat după review Codex Round 4)
**Corecție:** afirmația inițială „6 carduri complet + 2 parțial" a fost GREȘITĂ față de DoD-urile
scrise. Realist, NX-182..189 sunt TOATE parțiale: fundație utilă + fix-urile de mai jos, dar NU gata
de merge / evaluare live. Codex a avut dreptate pe toate findings-urile P1 (verificate în cod).

- 🟡 Track A: NX-182, NX-183, NX-184 — mecanismele decise + fix-uri P1; DoD incomplet (vezi mai jos).
- 🟡 Track B: NX-185/186/187 module pure + fix-uri P1; enforcement/integrare = live-review.
- 🟡 NX-188/189: shadow emit + migrare + flags; ENFORCE + SQL-retrieval = live-review.
- ~~Toate kill-switch OFF → byte-identic în producție~~ **[SUPERSEDED de R8/R9: URL scrub e
  ALWAYS-ON; medical gated. Vezi blocul STARE CURENTĂ.]**

## Review Codex Round 4 — findings P1 + remedieri (2026-07-18)
Toate cele 7 findings CONFIRMATE în cod. Reparate STATIC (fără evaluator live), cu teste:

| # | Finding | Fix | Fișier | Test |
|---|---|---|---|---|
| 1 | Safety: V2 text-only ocolea validatorul (evidence brut) | scrub medical la SURSĂ (menu) + guard `_v2_medical` pe textul final → fall-through la rich | `agent/envelope.py` `_evidence_facts`; `agent/finalize.py` `_v2_medical` | `test_evidence_menu_drops_medical_claim` |
| 2 | Mixed-intent: FAQ dispărea pe web rich (`render.py` ignoră `reply.text`) | injectăm politica ȘI în `rich.education` (randat de `flatten_framing`) | `worker/stages/agent.py` `_complete_faq_obligation` | `test_complete_faq_obligation_injects_into_rich` |
| 3 | Cache poisoning V2 text-only (cacheable + fără envelope_version) | `cacheable=False` pe text-only + namespace `cache_prompt_version` (v1/vnext + v2), single-source lookup==upsert | `agent/finalize.py`; `agent/prompt_builder.py`; `worker/stages/cache.py`; `worker/aftercare.py` | `test_cache_invalidation` (verzi) |
| 4 | Contract V2 half-consumed (`answer` ignorat când sunt products; `follow_up` nerandat) | `answer:inline` are PRIORITATE (reorder înainte de cards); `follow_up` → `education` + floor | `agent/finalize.py` `_finalize_v2` | `test_finalize_v2_*` (verzi) |
| 5 | Match Gate: constrângeri multiple pe aceeași fațetă se suprascriau; `bool("false")==True` | verdicts cheie-uite pe `facet:op:value`; `_as_bool` (tokeni RO/EN, necunoscut→UNKNOWN) | `agent/match_gate.py` | `test_multiple_constraints_same_facet_not_collapsed`, `test_bool_string_coercion_not_truthy` |
| 6 | Coverage `enforceable` pe date invalide (`pct_present` nu `pct_valid`) | `enforceable = pct_valid ≥ min_coverage` | `domain/facets.py` `facet_coverage` | `test_facet_coverage_present_vs_valid_and_enforceable` (extins) |
| 7 | QuerySpec: `brand`/`suitable_for` text liber în telemetrie (PII) | valorile HASH-uite în `fingerprint` (facet+op vizibile) | `agent/query_spec.py` `fingerprint` | `test_fingerprint_no_raw_free_text_pii` |

**Rămâne pt review-ul de diseară (NU reparat orb — behavior-changing / DoD mare):**
- V2 fallback = apel LLM dublu (V2 eșuat → rich). Structural; de decis împreună (early-attempt vs accept).
- NX-184: contractul `obligations` bogat (`ResponsePlan.obligations`) + detector RO/EN/HU + e2e prin
  faq_stage→agent→/web/chat. Azi: `ctx.faq_grounded` + completare deterministă (mecanismul FAQ decis).
- NX-185: `ctx.query_spec` + `query_spec_disagreement` + comparație cu args-urile tool-loop-ului.
- NX-186: integrare DomainPack (labels, merchant provenance) + script/raport pilot per business.
- NX-187: `ctx.match_set` + divergence + soft ranking + scan exhaustiv de recall.
- **Ordine corectă (Codex): NX-189 shadow/paritate/recall PRECEDE NX-188 enforce** (nu invers). Întâi
  filtrarea tipizată SQL participă în retrieval + paritate shadow verde → abia apoi enforce-ul (altfel
  enforce blind pe pool-24 dă false-negative).
  - NX-189: SQL tri-state (MATCH/UNKNOWN) în retrieval + dual-run + paritate recall — atinge SQL-ul core.
  - NX-188: ENFORCE (filtrare rejected + QuerySpec projection + alternatives UX) — DUPĂ NX-189-per-fațetă.

**Next (Codex):** re-review pe `feat/NX-track-ab` (#236) → CI complet. Evaluatorul paired rămâne OPRIT.

## Review Codex Round 5 — 5/7 fixuri erau PARȚIALE, completate (2026-07-18)
Codex a re-verificat commitul 6cc92cb: #3 (cache) și #7 (fingerprint) erau corecte; #1,#2,#4,#5,#6 doar
parțiale. Completate + teste REALE (Round 4 declarase teste care nu acopereau efectiv fixurile):

| # | Ce lipsea (Codex R5) | Fix R5 | Test REAL |
|---|---|---|---|
| 1 | V2 cards/text-only încă lăsau preț/link/claim prin `follow_up` (nescrubuit) și text-only doar medical | `follow_up`→`education` ÎNAINTE de assemble (scrub_education); text-only prin `validate_prose` COMPLET (preț/link/medical/claim), nu doar `_v2_medical` | `test_finalize_v2_text_only_inline_served`, `test_finalize_v2_follow_up_rendered_in_cards` |
| 2 | Mixed-intent pierdea FAQ când reply-ul era o COMPARAȚIE web (nu doar rich) | injectăm politica și în `comparison.intro` | `test_complete_faq_obligation_injects_into_comparison` |
| 4 | `answer.presentation:"card"` acceptat dar neconsumat; evidence invalid → răspuns fără motiv | scos `card` din enum-ul schemei; text-only cere `reason` non-gol → altfel fall-through | `test_v2_schema_answer_presentation_only_inline`, `test_finalize_v2_no_evidence_falls_through` |
| 5 | `bool(2)==True` clasifica greșit numerice non-0/1 | `_as_bool`: numeric DOAR 0/1, altfel None→UNKNOWN | `test_bool_string_coercion_not_truthy` (extins: 2→UNKNOWN) |
| 6 | Coverage valida orice non-enum (bool/number invalid) | `_is_valid_value` per tip (bool→bool real; number→numeric; list→nevid) | `test_facet_coverage_typed_validity_bool_number` |

Plus: cache namespace acum are test propriu (`test_cache_prompt_version_namespaces_prompt_and_envelope`);
build-log-ul corectat pe ordinea NX-189→NX-188. **+13 teste noi total** (R4+R5). `pytest asyncio=AUTO` →
testele de integrare `_finalize_v2` chiar rulează (nu skip tăcut).

## Review Codex Round 6 — 2 probleme de CLASĂ + doc stale (2026-07-18)
Codex a re-verificat 81f1540: inline-only, reason obligatoriu, comparația, numeric 0/1 = închise
bine. Rămâneau 2 probleme de CLASĂ (nu cosmetice) + descrierea PR stale. Reparate static:

| Problemă (clasă) | Rădăcină | Fix | Test |
|---|---|---|---|
| Safety cards încă permitea preț/link/claim | (a) `scrub_education`/`scrub_intro` NU verificau URL-uri; (b) `compose.assemble` re-adăuga `top_pro` BRUT ca `anchor` (via `_pros`→`_join_reason`, nescrubuit) DUPĂ ce meniul V2 îl eliminase ca medical | (a) `_URL_HINT` în `scrub_prose`+`scrub_intro` (deci și education/follow_up/fit_clause) → link în proză = DROP; (b) `_pros` filtrează medical la SURSĂ (gated) → anchor curat pe rich ȘI V2 | `test_scrub_drops_urls`, `test_scrub_education_drops_url_sentence`, `test_pros_drops_medical_top_pro` |
| Divergență typed coverage↔Match Gate | coverage cerea bool REAL dar Match Gate accepta stringuri bool; `NaN` considerat număr valid de coverage | `facets.parse_bool` + `facets.is_valid_number` = SURSĂ UNICĂ, folosite de AMBELE (`_is_valid_value` + `evaluate_constraint`); NaN/inf → invalid/UNKNOWN | `test_facet_coverage_typed_validity_bool_number` (extins), `test_number_nan_and_text_are_unknown` |

Plus: descrierea PR #236 actualizată (nu mai spune „1914 passed" / fixuri parțiale R4). **Notă siguranță:**
filtrul medical din `_pros` + URL-scrub-ul se aplică și pe calea RICH normală (nu doar V2) — închid o
gaură latentă pre-existentă (anchor medical / link în proză), gated de kill-switch → OFF byte-identic.

## Review Codex Round 7 — 2 leak-uri reziduale de clasă (2026-07-18)
Codex a re-verificat eb942e0: URL în proză + gte/lte NaN/inf = corecte; PR body + ordinea NX-189→188 =
OK. Rămâneau 2 leak-uri de CLASĂ (aceeași clasă, altă suprafață). Reparate static:

| Problemă | Rădăcină | Fix | Test |
|---|---|---|---|
| Safety: medical/URL reapărea în TABELUL de comparație + URL pe card | `_pros` curăța doar medical (nu URL) → anchor cu URL; `build_comparison` folosea `top_pros/top_cons` DIRECT (via `_join_list`), ocolind orice scrub | `_clean_facts` (medical gated + URL, păstrează numerele reale) = sursă unică → folosit în `_pros` ȘI `_join_list` (celule comparație) | `test_pros_drops_url_top_pro`, `test_join_list_drops_medical_and_url` |
| Typed: Match Gate ocolea helper-ele pe `eq` | eq folosea `parse_bool` doar la `isinstance bool`; string/string → `_norm` brut (verdict greșit pt tokeni bool diferiți); `NaN==NaN` prin `_norm` ('nan'=='nan') → MATCH | eq folosește tipul (`spec.value_type`): bool→`parse_bool` (ambele părți), număr→numeric (5==5.0), non-finit→UNKNOWN | `test_eq_uses_typed_helpers_bool_number_nan` |

Notă: ~~`_clean_facts` păstrează NUMERELE reale (spec produs = fapt grounded)~~ **[SUPERSEDED de R8 §5:
`raw: list[str]` n-are provenance → cifrele se păstrează dar NU sunt „grounded"; validare = DEFERRED.]**
Filtrul (medical+URL) se aplică și pe calea RICH normală (comparison + anchor); medical gated, **URL
always-on** (vezi §7).

## Review Codex Round 8 — protocol fix-de-clasă (2026-07-18)
Stare (protocol): **SELF-TESTED** (testele mele verzi) — NU „closed". VERIFIED = după re-review Codex.

### Harta clasei (rg pe câmpul brut, nu pe helper)
`top_pros`/`top_cons`/`review_pro` — TOATE suprafețele:
- **Client-facing** (fapt afișat DIRECT): `_pros` (anchor card) + `_join_list` (celule comparație) →
  ambele via `_clean_facts` (medical gated + URL). ✅ acoperit.
- **Model-input** (validat pe OUTPUT, nu client-facing): `_rich_bundle` (finalize.py:252), `_products_
  brief` (fallbacks, prompt retry), `catalog_tools` (rezultat tool), `_evidence_facts` (meniu V2).
  Output-ul e prins de `scrub_prose`/`scrub_education`/`validate_prose`/`assemble`. `_evidence_facts`
  filtrează totuși medical+URL la sursă (defense-in-depth). ✅ justificat.
- `_deterministic_reply` (fallback client) = doar nume+preț. ✅ fără fapt de recenzie.
- FAQ grounded → `rich.education`/`comparison.intro` (mixed-intent) ocolește scrub — **justificat**:
  politica FAQ e curată din DB și conține cifre LEGITIME (ex. „2-3 zile") pe care scrub le-ar tăia.

### Fixuri
| Problemă | Fix | Test |
|---|---|---|
| URL avea DOUĂ detectoare divergente (`_URL_HINT` doar în compose; `_evidence_facts` fără URL) | `has_url` mutat în `text_scrub` = SINGLE SOURCE; compose (scrub_prose/scrub_intro/_clean_facts) + envelope (_evidence_facts) îl folosesc | `test_url_scrub` (matrice adversarială: http/www/path/bare) |
| `has_url` rata BARE domain (`example.com`) | regex extins la domeniu gol cu TLD cunoscut (eu/co/io excluse — colizie „eu" RO) | idem |
| Match Gate `eq` ocolea tipul (§4) — deja reparat R7; adăugată dovada de CONSISTENȚĂ | test parametric: coverage-valid ⟺ Match-Gate-verdict-cunoscut pe bool ȘI number (2/NaN/inf/text) | `test_typed_*_coverage_matches_match_gate` |
| `_clean_facts` DECLARA fals „numere grounded" (§5) | docstring corectat: `raw: list[str]` n-are provenance → cifrele din recenzii se PĂSTREAZĂ (pre-existent), validarea lor = **DEFERRED** | — |

### §7 — schimbare intenționată vs byte-identic (ONEST)
- **URL scrub = ALWAYS-ON** (nu gated) în scrub_prose/scrub_intro/scrub_education/_clean_facts/
  _evidence_facts → **schimbare intenționată de comportament de siguranță**, NU „byte-identic". Regresia
  trece (niciun fixture n-avea URL legitim în proză scrubuită), dar semantic e always-on. Se poate gate
  dacă vrem control de rollback (decizie de review).
- **Medical filter = GATED** (`safety_medical_guardrail_enabled`) → OFF byte-identic pentru medical.
- **Typed (match_gate/facets/coverage)** = shadow-only (flag-uri shadow OFF → necablat în prod) →
  byte-identic în prod.

### DEFERRED (documentat, NU reparat orb)
- Provenance cifre în fapte de recenzie (§5): de decis dacă `_clean_facts`/`_join_list` scrub cifrele
  neverificabile din top_pros (schimbă UX normal-rich) sau primesc produsul + valorile permise.
- Gate pentru URL scrub (dacă vrem rollback).

## Review Codex Round 9 — 3 findings (2026-07-18)
| # | Finding | Fix | Test |
|---|---|---|---|
| P1 | typed `eq` încă greșit: `number` producea MATCH pentru „Infinity" și `1 == True`; testele acopereau gte, nu eq | `eq` rescris: cu `spec`, tipul DECLARAT are prioritate ABSOLUTĂ (nu isinstance runtime); `_eq_number` validează AMBELE cu `is_valid_number` ÎNAINTE de conversie; fără spec, număr-vs-bool = UNKNOWN | `test_eq_number_spec_rejects_infinity_and_bool`, `test_eq_spec_priority_over_runtime_type` (+ matrice) |
| P1 | URL scrub incomplet: `evil.ai`/`shop.hu`/`brand.eu`/`example.co`/`shop.co.uk` treceau | `has_url` extins: TLD set larg (gTLD+ccTLD incl. ai/hu/eu/co/io/uk) + subdomenii + TLD compus (`co.uk`) | `test_url_scrub` (extins) + teste pe OUTPUT `_clean_facts` + `evidence_menu` |
| P2 | doc contradictorie (OFF byte-identic / numere grounded vs R8) | bloc **STARE CURENTĂ** sus (supersedes) + linii istorice marcate **SUPERSEDED** | — |

Note: `1 == True` — pe facet **bool** declarat → MATCH (1 = true); pe facet **number** → UNKNOWN; **fără
spec** → UNKNOWN (tip incompatibil, nu coerce). Testul vechi R7 care aștepta MATCH fără spec = corectat
(era exact coerciția pe care R9 o interzice).

## Review Codex Round 10 — 2 findings P1 (2026-07-18)
| # | Finding | Fix | Test |
|---|---|---|---|
| P1 | typed `eq` corect doar pt bool/number: enum canonicaliza aliasul DOAR pe produs (nu pe constraint → `mat`≠`matte` MISMATCH greșit); text/enum invalid primea verdict cunoscut, nu UNKNOWN; testul enum promis lipsea | `eq` dispatch per tip DECLARAT: `_eq_enum` canonicalizează + validează AMBELE părți în `spec.values`; text = string nevid pe ambele (altfel UNKNOWN); list = eq respins → UNKNOWN | `test_eq_enum_canonicalizes_both_sides_and_validates`, `test_eq_text_invalid_and_list_rejected`, `test_typed_enum_coverage_matches_match_gate` |
| P1 | URL = allowlist finit: `.beauty/.pro/.cloud/.space/.world/.za` treceau | `has_url` = detectare GENERICĂ de TLD (orice TLD alfabetic 2-24, incl. gTLD noi + ccTLD neenumerate); allowlist-ul `_TLD` eliminat | `test_url_scrub` (.beauty/.pro/.cloud/.space/.world/.za/.tz) + OUTPUT `_clean_facts`/`evidence_menu` cu gTLD generic |

Note §4 (consistență): `test_typed_{bool,number,enum}_coverage_matches_match_gate` demonstrează că
coverage-valid ⟺ Match-Gate-verdict-cunoscut pe toate cele trei tipuri.
Note §7 (URL generic): FAIL-CLOSED — poate tăia rar un `cuvânt.cuvânt` adiacent sau o extensie de
fișier; sigur (se pierde o propoziție, nu un link). Rămâne always-on (nu byte-identic).

## Review Codex Round 11 — 1 P1 + 2 P2 (2026-07-18)
| # | Finding | Fix | Test |
|---|---|---|---|
| P1 | URL rata IDN bare (`magazin.рф`, `shop.中国`); punycode doar accidental ca prefix `.xn` | `has_url` IDN-aware: etichetă Unicode + TLD = punycode `xn--…` SAU 2-24 LITERE Unicode (`re.UNICODE`) | `test_url_scrub` (.рф/.中国/.xn--p1ai) + OUTPUT `_clean_facts` |
| P2 | doc stale: STARE CURENTĂ zicea „HEAD după R9" | sincronizat la 00d80bc / **1965 passed** + R11 | — |
| P2 | Match Gate ignora `FacetSpec.operators` (o fațetă „eq-only" putea rula gte/contains) | **type-op compat DA** (gte/lte→number, contains*→list; incompatibil→UNKNOWN); **allowlist `operators` = DEFERRED blocant înainte de NX-188** (vocabular op nealiniat: Constraint „contains" vs FacetSpec „contains_any"/„in") | `test_operator_type_incompatibility_is_unknown` |

DECIZIE §2 (deferred explicit): allowlist-ul `FacetSpec.operators` NU se cablează acum — cere întâi
alinierea vocabularului de operatori (o decizie de design care aparține lui NX-188). Rămâne BLOCANT
înainte de enforcement. Type-op compat (subsetul sigur) e livrat acum.

## Jurnal (per card: fișiere, teste, note)
_(se completează pe măsură ce construiesc)_
