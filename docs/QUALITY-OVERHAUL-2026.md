# Quality Overhaul 2026 — ADR + plan de execuție (calitate maximă Sales Assistant)

**Status:** DRAFT rev.2 (post-review Codex 2026-07-23) · **Autori:** Adi (decizie) + Claude (build) + Codex (verify)
**Regulă de proces:** cardurile NX-200..NX-215 sunt tăiate dar **NU se implementează** până la review-ul
dependențelor + Definition of Done per card. Revizia trăiește pe branch/PR **docs-only**, separat de NX-164.

Acest document e sursa de adevăr a INIȚIATIVEI. **Nu înlocuiește invariantele CLAUDE.md** (pipeline,
tenant isolation, PII, outbox) — le presupune; unde o decizie D schimbă un principiu CLAUDE.md,
schimbarea se face explicit în CLAUDE.md la ratificare (NX-200). Orice card viitor care contrazice
o decizie D1-D15 modifică ÎNTÂI acest document (cu aprobare), nu reinterpretează tacit.

**Invariante moștenite, repetate în DoD-ul fiecărui card runtime/DB:** toate query-urile izolate
prin `business_id`; `business_id` injectat SERVER-SIDE, niciodată controlat de model; zero PII în
logs/traces/telemetrie; P6 — nicio cale nu produce tăcere; kill-switch cu întoarcere completă la
comportamentul vechi; test adversarial cross-tenant; fallback funcțional fără embeddings/reranker/
critic; `ruff check` + `ruff format --check` + `pytest`; validare pe flux end-to-end, nu doar unit.

---

## 1. Context și diagnostic (pe scurt)

- Calitatea răspunsurilor e plafonată de: (a) conținut de catalog anemic (`ai_summary` templat,
  ~7 cuvinte — sursa embeddings), (b) retrieval care ratează query-urile colocviale/compuse,
  (c) model mid-tier (mini) pe stagiul care vinde, (d) pipeline releu de clasificatoare care
  pierde nuanța mesajului, (e) latență măsurată ~13,8s mediană / P90 ~20s vs. buget 5s
  (`turn_latency_budget_ms`, src/config.py), (f) tarife LLM interne stale (src/agent/pricing.py —
  estimări documentate, de reconciliat cu factura).
- Direcția aprobată (3 runde de dezbatere Claude ↔ Codex): **un singur agent principal (creier
  unic) în interiorul unui control plane determinist** — nu releu de modele mici, nu agent liber
  fără garanții.

### Fapte de repo verificate (2026-07-23)
- Căutarea hibridă EXISTĂ deja: `search_products_lexical` + `search_products_semantic` +
  `fuse_candidates`, pool 50 (src/tools/catalog_tools.py). → Task-urile de retrieval sunt
  **audit + îmbunătățire**, nu construcție de la zero.
- `unmet_query` NU conține expresia brută a clientului (interzis prin NX-163, no-PII). →
  Vocabularul viu cere un pipeline separat privacy-safe (D10).
- NX-185 atribuie extracția QuerySpec **triajului** — conflict cu D1; rezolvat prin D11
  (shadow permis, enforcement înghețat, ownership-ul țintă = agentul principal).

---

## 2. Deciziile arhitecturale (ADR) — obligatorii, nenegociabile per card

- **D1. Creier unic.** Mesajele nerezolvate complet de un fast path determinist (D2) ajung
  DIRECT la agentul principal — mesaj BRUT + istoric + profil — fără niciun alt LLM intermediar.
  Niciun model mic nu clasifică/rezumă mesajul înaintea agentului.
- **D2. Fast path determinist, cu dozaj explicit ȘI contract propriu.** Înaintea agentului:
  doar cod. Fast path-ul poate TERMINA turul singur DOAR pentru clasa „factual exact și sigur":
  preț/stoc pe produs identificat exact, status comandă, FAQ aprobat cu potrivire de mare
  încredere — răspunsuri unde formularea nu adaugă valoare conversațională (aici se păstrează
  economia straturilor gratuite: <500ms, cost 0). Pentru ORICE altceva, fast path-ul cel mult
  PREGĂTEȘTE facts și lasă agentul să formuleze. Regula de ambiguitate: la orice dubiu → agent.
  **Clasa care ocolește agentul ocolește și AnswerPlan-ul — deci fast path-ul are propriul
  contract tipizat + validator determinist:** identitate/autorizare verificată pentru status
  comandă; evidence + version pentru preț/stoc/FAQ (facts live, nu snapshot stale); cache
  NICIODATĂ cross-tenant sau cross-locale; P6 (fallback formulat, nu tăcere).
- **D3. Pilot ro-RO, nucleu generic.** Limba activă a pilotului: `ro-RO`. Nucleul rămâne
  locale-aware + multi-vertical: `business_id`, `locale`, `domain_pack`, `schema_version`,
  `document_version` prezente în toate contractele și artefactele de retrieval.
- **D4. Structura e adevărul.** Faptele structurate (facets, variante, preț, stoc) = sursa de
  adevăr. Orice text AI (search document, blurb) = artefact derivat, generat determinist,
  versionat, regenerabil prin `content_hash`. Nimeni nu scrie de mână 500 de rezumate.
- **D5. Confirmat ≠ derivat.** Fapte confirmate (sursă + `verified_at`) sunt separate de
  semnale derivate (cu `rule_id` — regula de derivare e reparabilă global). Claims importante
  (safety, free_of, contraindicații) cer proveniență.
- **D6. Raw query-ul nu se pierde — dar nu se persistă.** Trei reprezentări coexistă: raw /
  normalized / canonical facets; căutarea le folosește în paralel. Separare OBLIGATORIE în două
  obiecte: **`RuntimeQuerySpec`** (conține `raw_query` — trăiește DOAR în memoria turului, nu se
  scrie nicăieri) și **`SafeQuerySpec`** (canonical constraints + metadate normalizate, FĂRĂ
  raw_query, fără PII) — singurul care ajunge în telemetrie sau persistență. Raw query-ul poate
  conține nume, telefon, adresă, date medicale.
- **D7. Hard constraints inviolabile; UNKNOWN ≠ MISMATCH.** Agentul poate reformula query-ul
  semantic dar nu poate relaxa hard constraints (buget, negații, brand exclus, safety).
  Constraint coverage are 3 stări: MATCH / MISMATCH / UNKNOWN (enum canonic din NX-187:
  MISMATCH = datele CONTRAZIC; un singur vocabular în tot sistemul) — UNKNOWN înseamnă „nu știm",
  nu „nu corespunde", și declanșează clarificare sau disclosure, nu excludere.
- **D8. Evidence ID e necesar, nu suficient.** Fapte structurale (preț/stoc/link/variantă/
  ingredient/free_of) → validate determinist. Afirmații semantice („mai potrivit pentru...")
  → evidence ID + verificare semantică (critic selectiv). AnswerPlan face halucinațiile mult
  mai greu de produs și mai ușor de detectat — nu „imposibile".
- **D9. Warnings/contraindicațiile nu intră în vectorul POZITIV de candidate retrieval.**
  `not_recommended_for` / warnings NU intră în documentul pozitiv de căutare (capcana negației
  în embeddings: „NU e pentru ten uscat" ar face match pe „ten uscat"). Intră în: (a) facets cu
  excludere/penalizare deterministă la ranking (`level=hard` verificat exclude; `soft`
  penalizează — conform NX-170), (b) `evidence_chunks` de tip warning/limitation — care POT fi
  vectorizate în indexul de evidence, ca agentul să răspundă la „de ce nu?" / „ce să evit?".
  **Distincție obligatorie: fațetele canonice de ABSENȚĂ CONFIRMATĂ (`free_of: fragrance` →
  „fără parfum adăugat") sunt proprietăți POZITIVE de selecție — intră în documentul pozitiv,
  formulate neambiguu ca beneficiu, NU se elimină ca „negații".** Se exclude contraindicația,
  nu semnalul de selecție.
- **D10. Vocabular viu doar curat.** Expresii reale → redactare PII → normalizare → agregare →
  prag de frecvență → review uman → intrare în concern_map/intent_aliases. NIMIC live direct
  în routing/filtre. (unmet_query actual nu conține textul — pipeline nou necesar.)
- **D11. QuerySpec aparține agentului principal (țintă).** Triajul existent poate emite
  QuerySpec în shadow (NX-185) pentru comparație, dar nu devine sursa finală de adevăr.
  Enforcement-ul (NX-187/188) e ÎNGHEȚAT până la gate-ul prototipului (Faza 7).
- **D12. Latency architecture din ziua 1.** Spans pe fiecare etapă, citiri independente în
  paralel, fără N+1, evidence hydration în batch, post-tur complet async, buget de timp per
  etapă. Optimizarea fină (streaming, reasoning tuning) vine după prototip.
- **D13. Croiala shadow-vs-direct.** Greșeală scumpă/ireversibilă (enforcement QuerySpec,
  swap search documents, claims safety, rollout clienți) → shadow + versionare + kill-switch.
  Greșeală ieftină/reversibilă (template blurb, ponderi FTS, praguri fuzzy) → direct + măsurat
  pe golden set. Fiecare card își declară găleata.
- **D14. Servicii externe cu politică de date.** Înainte de Langfuse / reranker API: ce câmpuri
  pleacă, redactare la export, retenție, rezidență, pseudonimizare, opt-out per business.
  OTel = stratul neutru; Langfuse = un backend posibil, nu dependență de arhitectură.
- **D15. Nicio schimbare mare „pe speranță".** Fără frameworks agentice mari, fără fine-tuning,
  fără vector DB separat, fără model-swap permanent — orice schimbare majoră trece întâi
  printr-un experiment măsurat pe golden set (judecată oarbă).

---

## 2bis. Matricea de dispoziție vechi → nou (OBLIGATORIE)

Două carduri nu pot rămâne simultan implementabile pe aceleași fișiere și concepte. Legendă:
**REUSED** = rămâne valabil, e dependență · **ABSORBED** = scope-ul intră în cardul nou ·
**SUPERSEDED** = nu se mai implementează · **FROZEN** = blocat până la un gate ·
**INDEPENDENT** = continuă separat.

### Epic Catalog v3 (docs/CATALOG-PRODUS-V3.md)

| Card existent | Dispoziție | Motiv / relație |
|---|---|---|
| NX-168d (Product Contract v3: fapte canonice per-categorie, audit v2/v3 versionat, `not_recommended_for{level,source,verified_at}`, `claim_provenance`) | **REUSED** — dependență hard a NX-205 | Contractul + proveniența EXISTĂ deja aici. NX-205 nu-l rescrie: îl **extinde** cu `evidence_chunks` (roles) + `DerivedSignals{rule_id}` + `schema_version/locale` pe artefacte. Vocabularul canonic rămâne al 168d. |
| NX-168e (150 produse la contract v3 + seed graf complet; `ai_summary` derivat determinist) | **REUSED + ABSORBED parțial** de NX-206 | Autoringul + seed-ul rămân ale 168e (e cardul care comută ATOMIC poarta v2→v3). NX-206 absoarbe DOAR: gate-ul de completitudine extins (contradicții + proveniență obligatorie pe contraindicații hard) și raportul per categorie. **`ai_summary` derivat din 168e rămâne valabil ca pas intermediar** — este deprecat abia de NX-207 (search documents), nu de NX-206. |
| NX-169 (Agent Product Surface: proiecția faptelor în `_brief`/`_detail`/`_compare`) | **REUSED** — dependență a NX-209/NX-210 | Proiecția către model rămâne a 169. NX-209 o consumă ca bază pentru `evidence hydration`; NX-210 o folosește ca intrare a agentului. Fără suprapunere de fișiere dacă 169 intră ÎNAINTE. |
| NX-170 (doc embedding determinist + excluderi structurate + `reason_codes`) | **ABSORBED integral** — NU se mai implementează separat | Scope-ul se împarte cu graniță clară: documentul determinist de embedding → NX-207 (`positive_search_document`); excluderea hard/soft + `reason_codes` → NX-209 (treapta de penalizare din `search_entities`). UN singur traseu implementabil, fără „dacă". |
| NX-171a/b (net_content pe variante, product_relations) | **INDEPENDENT** | DDL de catalog, fără conflict cu inițiativa. |
| NX-171c (`content_status`/`schema_version`/`verified_at` + backfill sigur) | **REUSED** — dependență a NX-206 | Quality-gate la nivel DB (published) = plasa de care are nevoie gate-ul NX-206. Nu se dublează. |
| NX-171d (embeddings versionabile: PK compus `(product_id, doc_type, model)`) | **REUSED** — **dependență HARD a NX-207** | Fără PK compus, shadow-ul „index vechi + `search_document_v1` în paralel" e IMPOSIBIL. NX-207 nu poate începe înainte de 171d. |
| NX-172 (golden e2e, 12 scenarii pe cele 150) | **REUSED** ca validare LIVE finală; NX-202 absoarbe DOAR scenariile + checker-ele | Cele 12 scenarii + checker-ele (off-category, invented-facts, compare-diff, reason prezent) migrează în golden set-ul NX-202. **Lanțul de validare live** (audit static → seed dry-run → audit DB → re-embed → retrieval real → sim) RĂMÂNE al NX-172, repoziționat: rulează pe pipeline-ul NOU, ca parte din gate-ul de ieșire F6 (după NX-207 + NX-209), ÎNAINTE de evaluarea oarbă NX-210. NX-202 nu are dependențele necesare ca să închidă obligația asta. |

### Epic Selection Correctness (docs/RESPONSE-QUALITY-EPIC.md)

| Card existent | Dispoziție | Motiv / relație |
|---|---|---|
| NX-185 (QuerySpec shadow + merger owner-unic) | **REUSED în shadow**; ownership-ul ȚINTĂ se mută la agentul principal (D11) prin NX-210 | Contractul + mergerul pur + telemetria `query_spec_disagreement` rămân. NX-208 îl EXTINDE cu D6 (Runtime/Safe + 3 reprezentări). Extracția din triaj rămâne shadow, nu devine sursă finală. |
| NX-186 (typed facet registry + coverage report) | **REUSED** — **dependență HARD a NX-209** | Tri-state MATCH/MISMATCH/UNKNOWN (D7) e imposibil fără contractul de tipuri/operatori/`missing_value` + pragurile de coverage. NX-209 **nu reimplementează** registrul: îl consumă. |
| NX-187 (Match Gate shadow + recall vs scan exhaustiv) | **REUSED** — dependență HARD consumată de NX-209 | NX-187 se implementează (evaluatorul MATCH/MISMATCH/UNKNOWN per produs×constrângere, `MatchSet` disjunct, recall vs scan exhaustiv); NX-209 îl CONSUMĂ ca treaptă internă a `search_entities` — nu îl reimplementează. Verdictul per produs×constrângere se PĂSTREAZĂ în contractul de output NX-209 (`candidates[].constraint_results`). |
| NX-188 (Match Gate + QuerySpec ENFORCEMENT + alternatives UX) | **FROZEN** până la GO-ul de la NX-210 | Enforcement pe arhitectura veche ar fi muncă aruncată dacă gate-ul mare schimbă ownership-ul QuerySpec. Se redeschide după F7, aliniat la agentul unic. |
| NX-189 (typed facets în SQL tri-state) | **FROZEN** până la GO-ul de la NX-210, apoi **REUSED** ca dependență per-fațetă a enforcement-ului | Ordinea per fațetă rămâne cea din 189: shadow/recall → tri-state shadow → paritate → enforce. |
| NX-180..184 (Track A+B, naturalețe) | **INDEPENDENT** | Nu ating retrieval/selection; vezi memoria `agent-response-quality-plan`. |

**Regulă de conflict:** dacă un card marcat REUSED nu e livrat la momentul în care cardul nou îl
cere, cardul nou NU pornește (dependență hard) — nu se implementează o a doua soluție paralelă.

---

## 3. Fazele (ordinea aprobată) și gate-urile

| Faza | Conținut | Card | Gate de ieșire |
|---|---|---|---|
| 0 | ADR ratificat + pricing reconciliat + instrumentare latență/cost per stagiu + **SLO provizoriu** + politici date externe | NX-200, NX-201 | ADR aprobat; baseline profil latență/cost publicat; SLO provizoriu stabilit (contra căruia se judecă NX-210) |
| 1 | Golden set AUDITAT+extins (pornind de la 52 cazuri + 11 conversații existente) + retrieval benchmark (200-500 query-uri) + baseline calitate | NX-202, NX-203 | Baseline: calitate, Recall@20, nDCG@6, latență, cost, clase de erori |
| 1p | Experimente paralele (măsurare, nu switch): model mini→frontier pe pipeline actual + prototip creier v0 pe retrieval actual — izolează variabilele | NX-204 | Raport judecată oarbă; decizie informată pt F7 |
| 2 | Contract Facts / Evidence / Provenance / DerivedSignals | NX-205 | Contract aprobat; validatoare de contract verzi |
| 3 | Completarea + auditarea catalogului (gates de completitudine, nu lungime) | NX-206 | 0 produse publicate cu câmpuri obligatorii lipsă / contradicții / claims fără sursă |
| 4 | `search_document_v1` + `fts_document` ponderat + `evidence_chunks` + `card_blurb`, în SHADOW, embeddings versionate; plan migrare `ai_summary` | NX-207 | Benchmark vechi-vs-nou pe retrieval set; switch doar dacă cifrele cresc |
| 5 | QuerySpec (raw+normalized+facets) în shadow, reconciliere NX-185; pipeline vocabular viu privacy-safe | NX-208 | Dezacorduri shadow analizate; extracția țintă stabilită pe agent |
| 6 | Search tool „gras" (`search_entities`): exact+FTS+dual embedding+filtre+RRF+penalizări+rerank adaptiv+evidence hydration+constraint coverage; selecție embeddings×reranker pe benchmark | NX-209 | Recall@20 ≥90%, nDCG@6 ≥0,85, top-6 relevant ≥90%, 0 încălcări hard — măsurate pe **felia de holdout H2, single-use** (NX-207 folosește H1, NX-210 folosește H3 — o felie deschisă = arsă, anti-contaminare) + **lanțul de validare LIVE din NX-172** (audit → seed → re-embed → retrieval real → sim) verde pe pipeline-ul nou |
| 7 | Prototip agent unic (+ **AnswerPlanV0** experimental, offline) + evaluare OARBĂ vs. sistemul actual | NX-210 | **GATE-UL MARE:** câștig clar pe query-uri grele, 0 regresii facts, cost + latență în **SLO-ul provizoriu din NX-201** (NU în țintele NX-213 — acela e card ulterior) — altfel STOP |
| 8 | AnswerPlan + validator determinist extins + critic semantic selectiv | NX-211 | Preț/link/stoc inventate = 0; claims grounded ≥99%; hard violations = 0 |
| 9 | Needs profile + clarificare deterministă + ancorare + persona | NX-212 | Rubrica need-alignment crește; response-rate la clarificări crește |
| 10 | Optimizare latență + cost (paralelizare, batch, fast path, streaming web, reasoning tuning) | NX-213 | Facts 1,5-3s; recomandare 3-6s; complex 6-10s; P90 <12s |
| 11 | Rollout gradual: shadow → intern → demo → 5% → 20% → 50% → 100%, kill-switch + rollback per componentă | NX-214 | Fiecare treaptă: calitate/latență/cost/fallback/conversie comparate old-vs-new |
| 12 | Ritual săptămânal de îmbunătățire (pornit din Faza 1, permanent) | NX-215 | Rulează săptămânal; top-3 clustere → fix → re-măsurare |

Catalogul (F3) rulează în paralel cu restul. Agentul nou NU intră în producție înainte ca
retrieval-ul + evidence să fie măsurabil mai bune (gate F6).

---

## 4. Ce NU facem (operațional)

- Nu schimbăm modelul permanent „și sperăm" (experiment întâi — NX-204).
- Nu instalăm framework agentic mare înainte de prototip.
- Nu scriem manual 500 de `ai_summary` / search documents.
- Nu punem toate informațiile într-un singur embedding; negativele nu intră în documentul pozitiv.
- Nu eliminăm raw query-ul după canonicalizare.
- Nu introducem expresii live direct în concern_map / routing.
- Nu tratăm UNKNOWN ca MISMATCH.
- Nu permitem agentului să relaxeze hard constraints.
- Nu facem fine-tuning fără date + problemă demonstrată prin evals.
- Nu declarăm scoruri de calitate fără benchmark (nicio cifră „9/10" fără măsurătoare).
- Nu ștergem `ai_summary` direct — depreciere pe traseul: shadow → comparație → mutare
  consumatori → eliminare când nu mai e citit.

---

## 5. Indexul cardurilor

| Card | Titlu | Faza |
|---|---|---|
| NX-200 | ADR ratificat + CLAUDE.md update + politici date externe | 0 |
| NX-201 | Pricing reconciliat + instrumentare per-stagiu (baseline latență/cost) | 0 |
| NX-202 | Golden conversation set (50-100, ro-RO, etichetat) | 1 |
| NX-203 | Retrieval benchmark set (200-500 query-uri) + harness ir-measures | 1 |
| NX-204 | Experimente paralele: model-swap orb + prototip creier v0 | 1p |
| NX-205 | Contract Facts/Evidence/Provenance/DerivedSignals | 2 |
| NX-206 | Completarea + auditarea catalogului (gates de completitudine) | 3 |
| NX-207 | Search documents v1 + evidence chunks (shadow, versionat) + migrare ai_summary | 4 |
| NX-208 | QuerySpec pe 3 reprezentări (shadow) + pipeline vocabular viu | 5 |
| NX-209 | Search tool gras + selecție embeddings×reranker | 6 |
| NX-210 | Prototip agent unic + evaluare oarbă (GATE) | 7 |
| NX-211 | AnswerPlan + validator extins + critic selectiv | 8 |
| NX-212 | Needs profile + clarificare + ancorare + persona | 9 |
| NX-213 | Optimizare latență + streaming | 10 |
| NX-214 | Rollout gradual cu kill-switches | 11 |
| NX-215 | Ritual săptămânal de calitate | 12 |

**Toate cardurile au status DRAFT** până la review-ul dependențelor + DoD (Adi + Codex).
