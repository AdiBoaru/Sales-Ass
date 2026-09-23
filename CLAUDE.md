# Nativx Assistant — context complet pentru Claude Code

## Ce e acest proiect
Platformă multi-tenant de AI Sales Assistant pentru ecommerce.
**Canalul de lucru: WEB WIDGET, exclusiv (NX-179).** NX-289 a mers mai departe: WhatsApp și
Telegram nu mai sunt înghețate, sunt **ȘTERSE** — cod, migrare de schemă, servicii de compose,
variabile de mediu, teste. Orice task nou se măsoară pe web sau nu se face.
Nume comercial: **Nativx Assistant** (by Nativx Technology — nativxtech.com)
Clienți țintă: magazine ecommerce și retaileri din România (beauty, HVAC, auto, salon).
Model de business: agenție SaaS — setup fee + retainer lunar per client.
Referință de piață: similar cu iZi (eMAG) și Aura (SOLE), livrat ca serviciu managed.

---

## Stack tehnic

| Componentă | Tehnologie |
|---|---|
| Runtime | Python 3.12, asyncio |
| API | FastAPI (webhook + health) |
| Coadă | Redis Streams (lock per conversație, debounce) |
| DB | Postgres **17.6** — Supabase, proiect `NativexSales` eu-west-2 (**o singură schemă `public`**, multi-tenant pe `business_id`). Proiectul vechi (eu-west-1, PG16) e abandonat din 2026-08-28 |
| LLM sales | OpenAI **`gpt-5.6-luna`** (`MODEL_AGENT`; era `gpt-5.4-mini` până pe 2026-08-24), `reasoning_effort=medium` pe compunere (NX-313; era `high`). Escaladarea `MODEL_AGENT_COMPLEX` e GOALĂ implicit |
| Embeddings | text-embedding-3-small (pgvector în Supabase) |
| **Web widget** | **SINGURUL canal de lucru (NX-179)** — `/web/chat` sincron + `/web/stream` SSE; widgetul e în repo FE separat (`docs/FRONTEND-CONTRACT-IZI.md`) |
| Validare | Pydantic v2 |
| Teste | pytest + pytest-asyncio |

> **Schema DB: sursa de adevăr este [`docs/schema_v2_production.sql`](docs/schema_v2_production.sql)**
> (deja rulată + seedată). Pentru maparea numelor și deciziile de design vezi
> [`docs/schema_reference.md`](docs/schema_reference.md). Numele din acest fișier
> sunt cele REALE din schema_v2 (schemă plată, fără prefixe `core./conv./catalog.`).

---

## ⚠️ Direcția arhitecturală 2026 — Quality Overhaul (ratificat 2026-07-23)

**Sursa de adevăr a inițiativei: [`docs/QUALITY-OVERHAUL-2026.md`](docs/QUALITY-OVERHAUL-2026.md)**
(ADR APPROVED, deciziile D1-D15 + matricea de dispoziție a cardurilor + 13 faze cu gate-uri).

Arhitectura descrisă mai jos (pipeline liniar în 12 stagii) e **starea CURENTĂ, validă până la
gate-ul NX-210**. Direcția aprobată către care migrăm:

- **Creier unic (D1):** un singur agent principal (frontier) vede mesajul **BRUT** + istoric +
  profil. **Niciun model mic nu clasifică/rezumă mesajul înaintea lui** — triajul nano dispare
  de pe drumul sincron al conversației (rămâne shadow până la gate).
- **Fast path determinist (D2):** înaintea agentului doar COD; poate încheia turul singur DOAR
  pentru clasa „factual exact și sigur" (preț/stoc pe produs identificat exact, status comandă,
  FAQ high-confidence), cu **contract propriu + validator** (identitate/autorizare, evidence +
  version anti-stale, cache niciodată cross-tenant/cross-locale, P6). Orice dubiu → agent.
- **Control plane determinist în jur:** hard constraints inviolabile de model (D7),
  `UNKNOWN ≠ MISMATCH`, AnswerPlan cu evidence ÎNAINTEA textului (D8), validator determinist
  pentru fapte + critic semantic selectiv pentru afirmații.
- **Structura e adevărul (D4/D5):** faptele structurate = sursa; orice text AI
  (`search_document`, blurb) = artefact **derivat, versionat, regenerabil** — nescris de mână.
- **Pilot `ro-RO`, nucleu locale-aware (D3):** limba activă a pilotului e româna, dar
  `business_id` / `locale` / `domain_pack` / `schema_version` / `document_version` rămân în TOATE
  contractele și artefactele. **Nu hardcoda română nicăieri** — vezi și principiul 11.
- **`business_id` e SERVER-OWNED:** injectat server-side, **niciodată** din output-ul modelului
  și niciodată parametru controlabil de LLM.
- **Nicio schimbare mare pe speranță (D15):** model, embeddings, reranker, framework — toate se
  decid pe măsurători (golden set + retrieval benchmark), nu pe intuiție.

**Înghețate până la GO-ul de la NX-210:** enforcement-ul QuerySpec/Match Gate (NX-188, NX-189).

**NX-238 — retrievalul trece printr-un PORT, iar candidatul e inert (verdict `NOT-READY`).**
`src/retrieval/` e contractul stabil pe care îl consumă NX-239: `RetrievalPort` + `RetrievalBundle`
(candidați = REFERINȚE + verdicte tri-state + evidence + degradări cu cod fix), cu două
implementări. `CurrentLiveRetrievalAdapter` **apelează** `search_products_tool` — nu re-implementează
căutarea, deci paritatea e adevărată prin construcție; el ADNOTEAZĂ verdictele fără să excludă
nimic (`constraints_enforced=False`), fiindcă NX-188/189 sunt înghețate. `SearchEntitiesAdapter`
(candidatul) execută hard constraints — masca de `rejected` ÎNAINTE și DUPĂ rerank — dar **nu are
decorator `@register`**: un import nu-l activează.
Promovarea trece EXCLUSIV prin `src/retrieval/selector.py`. `RETRIEVAL_CANDIDATE_ENABLED=true` **nu
e suficient**: e nevoie de un artefact de decizie (`reports/nx238/decision.json`) cu verdict `GO`,
`decided_by` completat, amprentă SHA-256 care corespunde conținutului și semnătură HMAC verificabilă
cu `RETRIEVAL_DECISION_KEY`. Orice eșec (artefact șters/editat/nesemnat, cheie absentă, manifest
driftat) are cod fix și duce în același loc: **traseul live curent**. Software-ul nu poate emite GO.
Verdictul măsurat pe `origin/main@3cffbf5`: **`NOT-READY`** — H3 are 0 cazuri sigilate din 50 cerute,
qrels-ul are 18 familii din 100. Deblocarea e a NX-203 (corpus) + NX-202 (H3 sigilat), nu a
codului. Detalii: [`docs/NX-238-DECISION.md`](docs/NX-238-DECISION.md) + `reports/nx238/README.md`.

**NX-240 — grounding strict + projector PUR `web-view.v2` (DARK, flag OFF).**
`WEB_VIEW_V2_PROJECTOR_ENABLED=false` (default) = rândul se persistă identic, iar proiecția v2
rămâne cea derivată din payload-ul v1 (NX-233). ON (cere `WEB_TURN_V2_ENABLED` +
`SINGLE_BRAIN_ENABLED`, validat la boot): turul ÎNGHEAȚĂ faptele (`src/agent/evidence_bundle.py` —
`known | unknown(reason) | stale(age, sla)`, cu sursă; `updated_at` NU e verificare, doar
`synced_at`), le trece prin `src/agent/grounding_guard.py` (fiecare cifră/procent/link/stoc din
proză se confruntă cu faptele; livrarea/promoția/garanția n-au sursă ⇒ resping răspunsul;
superlativul la fel), persistă VERDICTUL în `response_json["grounded_v2"]` (aditiv, zero migrare),
iar `src/channels/web/render_v2.py` îl proiectează ca funcție **pură** — zero I/O, zero ceas, deci
două citiri dau aceiași bytes și un catalog schimbat după commit nu poate rescrie un răspuns deja
dat. Tot ce e afișabil e text localizat (`src/web/localization.py`, `Decimal`, plural CLDR,
reducere rotunjită în JOS): **niciun număr pe sârmă** în afară de `conversation.revision`.
CTA-urile de coș se emit acum (NX-237 le dăduse handler), dar DOAR pentru produse pe care guardul
le declară vandabile — fără plan persistat nu există token. Detalii:
[`docs/NX-240-GROUNDED-PROJECTOR.md`](docs/NX-240-GROUNDED-PROJECTOR.md); readiness măsurat:
[`docs/WEB-VIEW-V2-DATA-READINESS.md`](docs/WEB-VIEW-V2-DATA-READINESS.md); probe reproductibile:
`python scripts/nx240_projection_drive.py` + `python scripts/nx240_data_readiness.py`.

**NX-241 — UN deadline total de tur + bugete de execuție versionate (DARK, flag OFF).**
Înainte, fiecare strat avea ceasul lui (`llm_timeout_s` 30s × `llm_retry_max` 2 = până la 90s PE
APEL, `embed_timeout_ms`, `retrieval_deadline_ms`, `web_turn_deadline_s` 120s) și nimeni nu întreba
„cât mai am" — timeouturile se ÎNMULȚEAU. Acum turul are UN singur buget MONOTON
(`src/runtime/deadline.py`), născut o dată din `web_turns.deadline_at` și **neprelungit la
reclaim**; coada îl consumă; fiecare operație primește `min(capul ei, rămas − rezerva terminală)`,
iar rezerva (validator + fallback + commit) e fix diferența dintre „timeout onest" și tăcere.
Peste el stau plafoanele EXPLICITE pe clase de tur (`src/runtime/turn_budget.py`, manifest
VERSIONAT `nx241.2026-08-16`: exact 3s / recomandare 6s / complex 10s / mutație 8s, plafon dur 15s;
runde de model, tool calls, mutații, repair ≤1, tokeni, cost, octeți) — rezervate ATOMIC, deci un
„tool storm" nu poate trece de ultimul slot, iar modelul **nu poate cere mai mult buget prin
output** (P7). Tool-urile sunt CLASIFICATE (`src/agent/tool_budget.py`, registru complet verificat
la import): citirile independente pot rula în paralel până la plafon, mutațiile sunt EXCLUSIVE și
seriale. Retry-ul respectă `Retry-After` doar dacă ÎNCAPE. Aftercare-ul rămâne strict post-terminal,
dar bounded. Totul e măsurat într-UN event per tur (`turn_latency`, `src/observability/`), cu
vocabular ÎNCHIS de faze. Flags: `TURN_LATENCY_SPANS_ENABLED` (ON, doar măsoară) →
`TURN_DEADLINE_ENABLED` → `TURN_BUDGET_ENFORCED` → `TURN_PARALLEL_READS_ENABLED` (toate OFF =
byte-identic; poarta de boot refuză combinațiile imposibile). Pragurile finale + GO sunt ale
NX-246/247. Detalii: [`docs/NX-241-TURN-DEADLINE.md`](docs/NX-241-TURN-DEADLINE.md); probă:
`python scripts/sim/web_latency_probe.py`.

**NX-244 — renderer WebWidget PASIV (repo FE) + `view_copy` la bootstrap (backend, aditiv).**
Implementarea trăiește în repo-ul frontend (`Sales MVP Frontend Final`, branch
`feat/NX-244-passive-block-renderer`): un registry FINIT `block.type → componentă` peste toate cele
**11** tipuri din schemă (schița din card omitea `divider`; sursa de adevăr e schema), acțiuni care
retrimit tokenul opac NESCHIMBAT, și eliminarea din calea v2 a tot ce era creier al doilea în
browser — `Intl`/`Math.round` pe preț, `inferBadgeTone` (ton dedus cu regex din etichetă), thinking
simulat cu timere, `CartView`/`SavedDrawer` pe `localStorage`, acumulatorul `criteria`, greetingul
și cele 4 sugestii hardcodate, `?preview=1` + `chatDemo.js`. Boundary-ul e EXECUTABIL (ESLint pe
`src/chat/**` + `test/passive-renderer-boundary.test.js`, care rezolvă căile în loc să le
potrivească textual). Selecția v1/v2 e la BUILD (`VITE_CHAT_PROTOCOL_V2` comparat literal ⇒ Rollup
elimină ramura moartă): buildul v2 nu conține v1 — verificat prin scan pe `dist/`.
**În backend, singura schimbare** (`src/web/shell_copy.py`, aditivă și gated pe
`WEB_TURN_V2_ENABLED`): `GET /web/bootstrap` întoarce `view_copy` = `{composer, chrome, a11y}` din
ACELEAȘI tabele `src/web/localization.py`, fiindcă `chrome` călătorea doar în interiorul unui view,
iar înainte de primul tur widgetul rămânea fără nume și și-l inventa. `resolve_web_session` aduce
`default_locale` printr-un JOIN pe `businesses` (D3: limba e a tenantului, nu constantă). Detalii:
[`docs/WEB-WIDGET-BOUNDARY-V2.md`](docs/WEB-WIDGET-BOUNDARY-V2.md) §3.3.

**NX-246 felia 1/3 — observabilitate (traces + metrici) + `slo_policy.v1` (DARK, flag OFF).**
`OBSERVABILITY_ENABLED=false` (default) e ABSORBANT: zero span, zero contor, calea fierbinte
byte-identică. Aprins, un turn = UN trace care supraviețuiește restartului **fără nicio migrare**:
`web_turns.id` e UUID = exact 128 de biți = un trace-id W3C, deci `trace_id` se DERIVĂ determinist
(HMAC server-owned) din `turn_id`, iar `attempt` intră în span-id — reclaim-ul e alt span în același
trace, prin construcție. `traceparent`-ul din browser se REFUZĂ (nu devine părinte al nimănui) și se
numără. Eșantionarea e pe COADĂ: iese tot traceul dacă a fost eșantionat SAU dacă vreun span a
eșuat — plătești pentru traficul sănătos, dar ai traceul întreg exact acolo unde te uiți.
Cardinalitatea e mărginită prin registru (`src/observability/contract.py`): metrică declarată,
etichete declarate, valori din set închis SAU sub buget de valori distincte; `business_id`/`turn_id`
sunt interzise ca etichete (`turn_id` rămâne atribut de TRACE). Privacy: `sanitize.py` DELEAGĂ la
NX-230 și adaugă doar forma tehnică (excepții = lanț de TIPURI, URL fără query, headere = prezență,
argumente de tool = tip, nu valoare) — poarta e pe cheie **și** pe valoare, fiindcă un `tool_name`
otrăvit sau o cheie `sk-...` au formă perfectă de identificator. Exportul e mărginit și
non-blocant (coadă plină ⇒ drop al celui mai NOU, numărat); OTLP e import LENEȘ, singurul modul care
știe de OpenTelemetry. `slo_policy.v1` (`src/observability/slo.py` + `scripts/slo_report.py`)
calculează denominatorii din ledgerul `web_turns`, tenant-scoped, cu agregarea `renderable` ÎN SQL
(`response_json` nu iese din DB): lipsa datelor, eșantionul mic, setul trunchiat și pragul
neratificat dau `UNKNOWN`/`INSUFFICIENT`, **niciodată `PASS`**. Latența e RAPORTATĂ, nu judecată,
până când pragurile NX-241 se ratifică pe o fereastră reală de baseline. Felia 1 nu adaugă DDL
(042 a fost luat de felia 2, feedback). Detalii:
[`docs/WEB-OBSERVABILITY-SLO.md`](docs/WEB-OBSERVABILITY-SLO.md).

**NX-246 felia 2/3 — feedback one-tap server-owned (DARK, flag OFF, migrarea 042).**
`WEB_FEEDBACK_ENABLED=false` (default) = niciun prompt emis ⇒ niciun token ⇒ endpointul n-are ce
autoriza (poartă DUBLĂ, ca la comerțul NX-237). Nu există „endpoint care primește un rating":
**ratingul e în KIND, iar kind-ul e SIGILAT** — feedbackul e două `ActionSpec` noi
(`feedback_up`/`feedback_down`), deci browserul poate doar retrimite un token emis de server, nu
poate rosti „positive". `reason` e vocabular ÎNCHIS (`FEEDBACK_REASONS`, taxonomie VERSIONATĂ): un
motiv necunoscut e respingere, nu `other` tăcut. Ruta e SEPARATĂ (`POST /web/v2/feedback`) fiindcă
un „👍" nu e un tur — separarea e structurală prin `ActionSpec.sink` (`turn`|`feedback`), nu un `if`.
Verificările NU se dublează: secvența NX-236 a fost spartă în două funcții PURE
(`verify_envelope`/`verify_source`) folosite de ambele rute — modelul de amenințare are un singur
loc. `feedback_prompt_id` e DERIVAT (HMAC peste `turn_id`), nu random: un id random ar rupe
determinismul pe care se sprijină NX-236/NX-240, iar „un vot per prompt" ar deveni „un vot per
reîncărcare de pagină". Idempotența e în SCHEMĂ, nu în cod: `upsert_feedback` e UN statement cu
`ON CONFLICT` (retry identic = același receipt, `revision` neatins; corecție = `revision+1`; plafon
5). Rândul nu are coloană de text liber, IP, token sau identitate — verificat pe dataclass ȘI pe
`information_schema`. Raportul publică `positive_feedback_rate` cu `n` și interval **Wilson** (nu
Wald, care la 10/10 dă „între 100% și 100%"), cu prag propriu per cohort; sub 30 de voturi verdictul
e `insufficient_sample`, iar cuvântul „CSAT" nu apare nicăieri (testat pe artefact). Detalii:
[`docs/WEB-FEEDBACK.md`](docs/WEB-FEEDBACK.md); raport: `python scripts/feedback_report.py`.

**NX-246 felia 3/3 — gate de calitate „personal shopper" (harness complet, verdict `NOT-READY`).**
Un golden test verifică un RĂSPUNS; produsul vinde o CONVERSAȚIE — de aici stratul de *journey*
(2-6 ture, context de pagină/coș, corecții, referințe ordinale) peste harnessul NX-210, care rămâne
sursa pentru grounding/pairwise și NU s-a rescris. Ordinea e întregul design: **sigiliu+acoperire →
determinist → stil**. `deterministic.passed=False` ⇒ `FAIL` indiferent de rubrici, fiindcă altfel un
text fluent care inventează un preț bate unul onest care spune „nu știu". Patru verdicte, nu două:
`NOT-READY` (n-am măsurat) e DISTINCT de `FAIL` (am măsurat și a picat) — ca la NX-238. Familiile
sunt vocabular ÎNCHIS (10), iar eticheta trebuie să descrie conținutul, altfel acoperirea minte;
duplicatele se resping pe `journey_id` **și** pe amprenta de CONȚINUT (care exclude id-ul —
copiat-lipit cu alt id e același caz de test). Holdoutul NU intră în repo: doar manifest cu SHA-256
peste amprente ordonate, verificat înainte de rulare, fail-closed pe toate ramurile (manifest
absent, hash diferit, conținut indisponibil). Pairwise-ul folosește o PROPORȚIE (`win + 0,5×tie ≥
55%`, limita bootstrap ≥ 50%), nu delta de medii ca NX-210 — se poate câștiga la medii pierzând
majoritatea journey-urilor. Order bias și dezacordul între evaluatori BLOCHEAZĂ, iar o pereche fără
adjudecare nu intră în scor. Pragurile sunt preînregistrate și amprentate (`GatePolicy`). Verdict
măsurat azi: **`NOT-READY`** — 10/60 dev, holdout nesigilat. Deblocarea e a NX-203 (corpus), nu a
codului. Detalii: [`docs/WEB-QUALITY-EVAL.md`](docs/WEB-QUALITY-EVAL.md); probă:
`python scripts/web_quality_eval.py gate --suite tests/golden/web_journeys`.

**NX-247 PR A/2 — gate E2E Stage 1 pe infrastructură REALĂ; a găsit 2 defecte (verdict NO-GO).**
Cele două defecte pe care le-a descoperit sunt REPARATE (#295, vezi nota „Fix NX-236/234" mai jos);
gate-ul rulat pe codul reparat trece **37/37, zero xfail**. Nu au deblocat scenarii suplimentare:
cauzele erau cumulative (flag de stare v2 nepromovat + profil de creier unic necertificabil), deci
acoperirea rămâne 9/16 scenarii — s-au schimbat CAUZELE, nu cifrele.
Harnessul (`tests/e2e/`) nu construiește o aplicație paralelă: ia EXACT obiectul FastAPI din
`src.webhook.app` (middleware de body cap, lifespan de observabilitate, montare condiționată — toate
reale), pe Postgres + Redis REALE, cu model/embedder FALȘI. Poarta e STRUCTURALĂ, nu un flag: nu
există `FAKE_LLM=true` nicăieri — `build_stage1_app` refuză să pornească dacă `ENV != test`, dacă
hostul nu e loopback, dacă secretul de control (per proces) e slab sau dacă `/web/v2/turns` nu e
montat pe aplicația reală; `tests/` nu intră în imaginea de producție (verificat pe Dockerfile).
Embedderul fals e un spațiu vectorial REAL (token → direcție unitară din sha256), deci
`search_products_semantic` rankează pe semnal, nu pe hazard — un stub de zerouri ar face orice produs
egal de aproape și retrievalul n-ar fi exersat. Modelul fals **compune răspunsul din ce au întors
tool-urile REALE**, deci validatorul (stagiul 8) și grounding guardul pot să respingă. Doi tenanți cu
`business_id` care diferă DOAR în ultimul nibble (un bug de izolare pe prefix nu are unde să se
ascundă). Matricea R1–R22 e automatizată la nivel de backend (**35 passed, 2 xfailed, 0 skipped**); R15 e N/A
cu justificare (payloadul trebuie stricat DUPĂ ce a părăsit serverul). Acoperirea e DECLARATĂ pe
scenariu (`backend_coverage`) și legată de execuție prin test: **9/16 scenarii, 16/22 invarianți pe
date reale**; golurile sunt publicate în `gate.known_gaps` cu cauză (3 blocate de defectele de mai
jos, 1 de un flag nepromovat, 1 fără producător în `src/`) — un gate care își ascunde golurile e mai
periculos decât unul care le arată.
Contractul e un artefact canonic consumat de ambele repo-uri (`qa-suite/stage1/web-v2/`): fără
timestamp (determinismul e condiția ca driftul să însemne ceva), `backend_commit` doar în certificatul
de rulare, hash-uri pe bytes normalizați CRLF→LF; `--check` rulează pe FIECARE PR în `ci.yml`.
Pragurile sunt un singur artefact: zerourile de corectitudine sunt ratificate, latența e RAPORTATĂ
(`slo.RATIFIED is False` — un test cere ca artefactul și codul să nu divergă).
**Cele 2 defecte găsite, owner alte carduri, NEreparate aici (Out of Scope), marcate `xfail(strict)`:**
(1) `messages.content_type = 'action'` (NX-236/237) e respins de CHECK-ul schemei ⇒ cu
`WEB_ACTIONS_ENABLED=true` acceptul oricărui turn pornit dintr-un buton crapă; (2)
`load_execution_refs` nu proiectează `m.payload` în selectul exterior ⇒ `page_context` și `action` ies
MEREU `None` (NX-234/236) — persistarea e corectă, citirea e ruptă. Verdict: **NO-GO pentru NX-249**
(lipsesc PR B/browser, cele 2 fixuri, ratificarea pragurilor; NX-238 rămâne `NOT-READY`, deci
profilul certificat e `v2_transport`, nu creierul unic). Detalii + runbook copy/paste:
[`docs/STAGE1-WEB-E2E.md`](docs/STAGE1-WEB-E2E.md).

**Fix NX-236/234 — două defecte care făceau acțiunile opace și contextul de pagină INERTE.**
Găsite de gate-ul E2E NX-247 la prima rulare pe Postgres real; ambele invizibile până atunci fiindcă
flag-urile sunt OFF în producție, iar suitele existente foloseau monkeypatch în loc de DB.
(1) `messages.content_type = 'action'` (scris de `src/web/app.py` la accept) era respins de CHECK-ul
schemei ⇒ cu `WEB_ACTIONS_ENABLED=true` acceptul ORICĂRUI turn pornit dintr-un buton crăpa cu
`CheckViolationError`. Migrarea **043** extinde vocabularul (nu schimbă valoarea scrisă: `body` e gol
pentru o acțiune, deci `text` ar fi o minciună în ledger, iar `interactive` era termenul de
PROVIDER pentru mesaje cu butoane — împrumutat pentru un concept web-only, analytics-ul n-ar mai
putea distinge „a scris" de „a apăsat"; NX-289 l-a scos de tot din CHECK). (2) `load_execution_refs` citea `payload` din Record, dar
proiecția EXTERIOARĂ a query-ului nu-l selecta — coloana exista doar în subqueryul lateral, deci
`page_context` și `action` ieșeau MEREU `None`: ancora de pagină (NX-234) nu ajungea niciodată la
execuție, iar un tur de acțiune reluat își pierdea comanda (NX-236). Persistarea era corectă tot
timpul; doar citirea era ruptă. Cele două se COMPUN: fixul singur al primului ar fi produs ture de
acțiune acceptate care rulează cu `body=""` și fără comandă — un buton care „merge" și răspunde cu o
clarificare, adică un eșec care trece un canary. Regresii: una comportamentală pe DB real
(`test_execution_refs_return_page_context_and_action`) + o gardă ieftină care compară cheile citite
din Record cu proiecția EXTERIOARĂ (`test_load_execution_refs_projects_every_column_it_reads`) —
ținită pe acest query, fiindcă o gardă generală ar cere parsare de SQL, iar varianta ieftină nu
prinde defectul (`payload` APĂREA, doar în locul greșit).

**NX-248 — deploy imutabil, secrete, readiness, supply chain, DR (verdict `NOT_READY`).**
Înainte, un push pe `main` construia imaginea, publica `latest` și intra prin SSH cu
`git pull && docker compose pull && up -d` — deci „ce am testat" nu era neapărat „ce rulează",
rollbackul nu era o operație declarată, iar un merge ERA un deploy. Acum un commit produce UN
artefact (`image@sha256:…` + SBOM + provenance + semnătură cosign + scan fail-closed), iar
promovarea e o decizie umană pe GitHub Environments, cu ACELAȘI digest — zero rebuild, zero
`git pull` pe host, host key PIN-uit dintr-un secret (nu `ssh-keyscan`). Rollbackul are țintă:
`previous_digest` din manifest, iar `rollback_possible()` compară intervalul de schemă al imaginii
precedente cu schema APLICATĂ — dacă a fost depășit, releaseul se BLOCHEAZĂ înainte de deploy, în
loc să descoperi asta în incident. Healthcheckul nu mai e un socket: `live` (zero I/O — un Postgres
jos nu repornește flota), `startup` (schemă/registre/chei, latch), `ready` (sonde MĂRGINITE, în
paralel), plus health de proces pentru worker/scheduler (freshness + PID viu + boot id, fiindcă un
fișier scris de un proces mort arată proaspăt). `required` vs `optional` se citește din COD, nu din
obișnuință: cu Redis jos, `api` iese din rotație (rate-limitul de accept e `fail_closed`) și
`worker` rămâne `degraded` (Postgres e autoritatea, NX-233) — măsurat, nu presupus. Migrarea e job
one-shot cu advisory lock VERIFICAT în `pg_locks` (un lock de sesiune printr-un pooler tranzacțional
e o iluzie) și cu singurul credential de DDL din sistem, în `.env.migrate` — runtime-ul nu mai
poate face DDL. Dependențele sunt hash-locked (`requirements.lock`, `--require-hashes`), acțiunile
CI pin-uite pe SHA, baza pe digest; `fastapi[standard]` a fost tăiat (aducea un CLI, un CLI de cloud
și `sentry-sdk` în runtime): **73 → 54 pachete**. Containerul e non-root/read-only/cap-drop/
no-new-privileges, verificat pe imaginea reală. **Bug găsit și reparat:** `.dockerignore` excludea
`db/seed`, deci registrul de contraindicații NX-173 lipsea din imagine și poarta de boot refuza să
pornească workerul. Măsurat local: migrări (fresh 38/38, idempotent, concurent `[0,3]`), readiness
(4 scenarii) și contractul de imagine — TREC. Verdict de release: **`NOT_READY`** (7/10 elemente
critice cer CI + staging + provider); RPO/RTO rămân `UNVERIFIED` și blochează NX-249. Detalii:
[`docs/PRODUCTION-READINESS.md`](docs/PRODUCTION-READINESS.md) +
[`docs/RELEASE-RUNBOOK.md`](docs/RELEASE-RUNBOOK.md) +
[`docs/DISASTER-RECOVERY.md`](docs/DISASTER-RECOVERY.md) +
[`docs/SECRETS-ROTATION.md`](docs/SECRETS-ROTATION.md); probă: `python scripts/release/evidence.py`.

**NX-249 — controller de release: asignare stabilă, porți de promovare, cutover (DARK, flag OFF).**
Înainte, „cine primește v2" era un env global (`WEB_TURN_V2_ENABLED`, `SINGLE_BRAIN_ENABLED`,
`RELEASE_TRACK`) citit **în timpul turului** — deci un reclaim după deploy rula alt cod pe același
turn, iar raportul candidate-vs-control nu se putea calcula (ledgerul n-avea cohort; `slo.py`
declara deja `per_row_release_sha` LIPSĂ). Acum asignarea e SERVER-OWNED, deterministă (HMAC cu salt
versionat — nu sha256 public ca la NX-238: aici bucketul alege ce versiune de PRODUS primește un
client, deci nu trebuie să fie calculabil de oricine cunoaște `conversation_id`) și **capturată pe
rândul de ledger la accept**, înainte de orice claim (migrarea **044**, expand-only). Executorul
citește trackul de pe RÂND, nu din config. Epochul e sticky prin ledger, nu prin Redis/cookie: 5→20
nu mută o conversație existentă, iar un FLUSHALL nu reasignează pe nimeni. Etapa e **declarată** în
policy, nu dedusă din cifre — etapele 2 („demo", 100%) și 6 („default", 100%) sunt indistinctibile
din (mod, procent), iar o deducere ar fi cerut 24h/100 de ture acolo unde cardul cere 14 zile/2.000.
`force_control` e o revizie de policy ca oricare alta (același CAS, același audit): oprește
accepturile NOI, dar o conversație deja candidate se **drenează** (`503 release_draining`), nu se
convertește tăcut — starea și referințele ei vin din candidate. Policy-ul trăiește în
`release_policies` (append-only, CAS în SCHEMĂ pe `unique (environment, revision)`, **fără grant
pentru `bot_runtime`**: rândul poartă allowlistul), citit pe `admin_conn` cu cache bounded. Porțile
(`src/release/gates.py`) au patru verdicte — `INSUFFICIENT` („mai lasă-l") ≠ `UNKNOWN` („repară
instrumentul") ≠ `FAIL` — cu timp ȘI eșantion, non-inferioritate pe interval **Wilson**, cohort
separat pentru acțiuni (o regresie doar pe coș nu se pierde în medie) și hard stops cu toleranță
zero peste orice scor pozitiv. **`PASS` nu promovează**: `gates.py` n-are acces la store, iar `apply`
cere evidence packet cu amprentă RECALCULATĂ + `--expected-revision` + actor + motiv + `--confirm`.
Verdict de azi: **BLOCAT** — NX-248 `NOT_READY`, NX-247 `NO-GO`, NX-246 felia 3 `NOT-READY`,
NX-238 `NOT-READY`. `RELEASE_CONTROLLER_ENABLED=false` (default) = byte-identic. Detalii:
[`docs/STAGE1-RELEASE-DECISIONS.md`](docs/STAGE1-RELEASE-DECISIONS.md) +
[`docs/STAGE1-CANARY-RUNBOOK.md`](docs/STAGE1-CANARY-RUNBOOK.md) +
[`docs/STAGE1-CUTOVER.md`](docs/STAGE1-CUTOVER.md) +
[`docs/STAGE1-QUALITY-RITUAL.md`](docs/STAGE1-QUALITY-RITUAL.md); probă:
`python scripts/release_control.py plan --policy <fișier> --ids 10000`.

**NX-239 — MainBrain unic + control plane determinist + `AnswerPlanV2` (DARK, flag OFF).**
`SINGLE_BRAIN_ENABLED=false` (default) = pipeline-ul de azi byte-identic. ON (dark/shadow):
fiecare early-exit trece prin `src/agent/control_plane.py` — un reply care nu e fast path
COMPLET (obligații extrase DETERMINIST din mesaj: `src/agent/brain_models.py`) devine SEMNAL, iar
`src/agent/brain.py` (UN singur writer semantic) emite `AnswerPlanV2` structurat în ACEEAȘI buclă
de tool-calling; validatorul V2 REFOLOSEȘTE validatorul NX-211 (`to_v1()`), hard constraints nu se
relaxează, nevoile revocate nu reînvie, clarificarea e unică, no-results are taxonomie onestă.
Retrieval EXCLUSIV prin selectorul NX-238 (NOT-READY → current live). UN repair bounded → fallback
determinist (P6). Producția rămâne OFF până la GO-ul NX-246. Detalii:
[`docs/NX-239-SINGLE-BRAIN.md`](docs/NX-239-SINGLE-BRAIN.md); drive:
`python scripts/sim/single_brain_drive.py`.

**NX-300 / NX-301 — jumătate din tur era invizibil, iar cardul purta fraza de reclamă.**
Găsite analizând UN tur real (`3e582c6d`, «vreau ceva sa scap de cosuri»): 95.153 ms, din care
`turn_latency` raporta 6.157 ms în faze. Pe tot traficul (53 de ture), acoperirea
`phase_ms_total / e2e_ms` avea **p50 46,7%** și **cinci din cele zece faze nu apăreau NICIODATĂ**.
Patru cauze distincte, derivate mecanic: trei apelanți de `_chat` fără span (între ei
`complete_schema`, adică apelul de rich compose — 86,7 s din turul măsurat); span-ul `tools` pus pe
ramura pe care producția n-o ia (`execute` iese devreme când `ledger` și `deadline` sunt `None`,
adică exact profilul de prod); span-ul `load` rulând înainte ca runner-ul să împingă acumulatorul,
iar commit/aftercare după ce l-a scos; și `queue`/`commit`/`aftercare` fără niciun producător în
`src/`. Evenimentul se emite acum DUPĂ commit (`run_pipeline` întoarce `TurnRuntime`; emite doar
cine a deschis acumulatorul), iar aftercare-ul are evenimentul LUI (`phase=post_turn`), ca al doilea
`llm_usage` — a-l pune în bugetul turului ar umfla `e2e_ms` cu muncă pe care clientul n-o așteaptă.
**Latența nu s-a atins**: cardul face timpul VIZIBIL. `reasoning_effort=high` pe apelul rich (2.048
din 2.999 tokeni de output) și retry-urile de 30 s fără plafon per încercare rămân decizii separate.
NX-301: `products.name` are pe catalogul real mediana **195** de caractere (nume + frază de
marketing + gramaj), iar `display_name` **38**. Scurtarea exista din septembrie peste tot unde
produsul e numit pentru MODEL; singurul consumator rămas pe numele întreg era CLIENTUL. Pe cele
patru carduri ale turului real: **905 → 143 de caractere**. Gramajul, pe care scurtarea îl pierdea
integral (0/2.758 în capul numelui), intră într-o cheie proprie `size`, recuperată determinist din
coadă și ABSENTĂ pe cele 52,5% unde nu e recuperabilă. Poarta mecanică a găsit un membru pe care
căutarea îl ratase (`_deterministic_reply`, textul de fallback citit de client) și a corectat un
scop greșit (liniile de coș din `commerce_tools` sunt refs de stare + `llm_view`, nu sârmă).
Carduri: [`tasks/stage1/NX-300.md`](tasks/stage1/NX-300.md) +
[`tasks/stage1/NX-301.md`](tasks/stage1/NX-301.md); probe:
`pytest tests/test_phase_coverage.py tests/test_display_name_on_cards.py -q`.

**NX-302 / NX-304 / NX-305 / NX-306 / NX-307 — o singură conversație reală, cinci mecanisme.**
Conversația `70da107c` (`sole-ro`, 2026-09-21): clientul cere un autobronzant, primește cinci
carduri bune, întreabă cum se aplică, botul îi spune să folosească o mănușă, iar la «cat cost una?»
răspunde **„nu am găsit în catalog o mănușă"** și servește dedesubt patru seturi de pensule de
machiaj de 1.300 de lei. Catalogul are **trei mănuși de aplicare autobronzant** (50 / 50 / 79 lei),
de la aceleași branduri și pe EXACT raftul din care recomandase cu patru mesaje mai devreme.
**NX-305** — cauza nu e „a ghicit raftul", ci că turul avea DOUĂ filtre de subiect, amândouă ghicite
de model și contradictorii: `category=machiaj-accesorii` (28 de produse) și `product_type` din
`concerns=["autobronzant"]` (7 produse, clasa CORECTĂ). De la NX-299 scara ordonează după
(proveniență, tip); aici proveniența e la EGALITATE (`kept=0`, `facets_uttered=false`, fiindcă
subiectul venise din replica BOTULUI), deci a decis TIPUL și a supraviețuit ghicitura GROSIERĂ.
Varianta evidentă, „raftul conversației devine implicit", a fost încercată și RESPINSĂ pe
măsurătoare: cade pe `filters_only` și servește șase geluri, tot nu mănușa. Filtrele ghicite nu se
ÎNLOCUIESC, se SCOT, iar textul răspunde singur: `relaxed_any` → `strict`, mănușa de 50 de lei pe
locul 1. Poarta cere ca NICIUN filtru de subiect să nu fie coroborat de client, ceea ce ține
NX-298/NX-299 neatinse (acolo clientul a rostit «cosuri»), și adoptă rezultatul doar pe o treaptă
STRICT mai bună. Kill-switch `SEARCH_GUESSED_FILTER_RESCUE_ENABLED`.
**NX-306** — pe calea BOGATĂ setul de carduri e cel numit de model, cu apartenența verificată; pe
calea de PROZĂ nu exista nicio legătură, deci `render` atașa retrievalul BRUT indiferent ce spunea
textul. Poarta cere DOUĂ semnale: `no-items-selected` (singura formă de refuz măsurată de model
însuși, deci zero regex pe proză, P11) ȘI `relevance.relaxed` (setul a ieșit doar prin renunțare la
filtre). Singur, refuzul nu ajunge, și suita a arătat de ce: pe „ceva mai ieftin" setul e ales
DETERMINIST de `cheaper_intent`, iar a suprima ar ascunde răspunsul corect. Filtrul se aplică
ÎNAINTE de `rich_from_facts`, altfel NX-302 ar fi AGRAVAT refuzul (aceleași pensule, dar cu badge,
rating și motiv sub card).
**NX-307** — `explain` era în `ObligationKind` de la început, cu doi CONSUMATORI și niciun
PRODUCĂTOR, deci „cum folosesc" ieșea `answer` generic și turul rula cu `retrieval_ids: []`, deși
`product_sections.usage` există pe **2.746 din 2.758** de produse. În plus, `detail_sections` tăia
`usage` la 150 de caractere pe un text cu media 354, iar `dosage` (86,8%) lipsea din pachet. Profil
`howto` + plafoane corectate; flag propriu `HOWTO_FROM_CATALOG_ENABLED` (OFF).
**NX-302** (25% din turele cu produse ajungeau la client cu `rich = null`) și **NX-304**
(`ROUTINE_ENABLED` era legal și INERT pe v1, fiindcă `turn_profile.select` se chema doar în
`brain.py`) erau deja scrise și au intrat în aceeași serie.
Niciunul nu putea fi prins din aval: produsele și prețurile erau REALE (`validator_ok: true` pe tot
turul), deci stagiul 8 și `grounding_guard` le-au lăsat să treacă. Sunt porți de **ADEVĂR**, nu de
**POTRIVIRE**. Carduri: [`tasks/stage1/NX-305.md`](tasks/stage1/NX-305.md) +
[`tasks/stage1/NX-306.md`](tasks/stage1/NX-306.md) +
[`tasks/stage1/NX-307.md`](tasks/stage1/NX-307.md); probe:
`python -m scripts.nx305_guessed_filter_probe` +
`pytest tests/test_guessed_filter_rescue.py tests/test_refused_set_withheld.py tests/test_howto_from_catalog.py -q`.

**NX-311 — un timeout al NOSTRU era tratat ca o eroare a FURNIZORULUI, iar retry-ul o tripla.**
Măsurat pe `sole-ro` (101 ture live, 30 de zile): e2e **p50 24,9s, p90 99,4s**. Turele lente poartă
EXACT `llm_retry×2` și stau în banda 98-122s, adică semnătura aritmetică a lui `llm_timeout_s=30` ×
(1 + `llm_retry_max=2`). Tipul excepției, scos din `conversation_traces.diagnostics.rich_error`:
**6× `APITimeoutError`** — pe acele 6 ture toate cele trei încercări au murit, deci clientul a primit
lista seacă **după 105 secunde**, adică a așteptat un minut și jumătate în plus pentru un răspuns mai
PROST decât cel normal. Cauza nu e retry-ul, e BUGETUL: plafonul se punea O DATĂ, la construcția
clientului, peste toate apelurile — dar apelurile nu sunt de același fel, iar codul o știe deja.
`_sampling` decide DETERMINIST dacă o cerere raționează (pe bucla cu tool-uri e FORȚAT `none`, altfel
`chat.completions` dă 400), deci sunt două populații cu distribuții care nu se ating: rundă de
tool-calling **p50 2,9s / p90 4,5s**, compunerea răspunsului **p50 6,5s / p90 25,9s, cu 24,6% peste
30s**. 30s e anti-hang onest pentru prima și taie prin coada celei de-a doua; iar fiindcă
`APITimeoutError` e în `_TRANSIENT_ERRORS`, tăietura NOASTRĂ declanșa retry pe aceeași generare, cu
același prompt, în fața aceluiași cronometru. Premisa era adevărată la NX-126 (iunie 2026,
`gpt-5.4-mini`, fără raționament); s-a rupt pe 24 aug (`bbb77b3`), când defaultul a trecut pe
`gpt-5.6-luna` ȘI pe `reasoning_effort=high`, deodată.
**Varianta evidentă a fost încercată și RESPINSĂ pe măsurătoare:** „scoate `APITimeoutError` dintre
tranzitorii" — din cele 18 ture cu retry, **12 au REUȘIT la o reîncercare**, deci fără retry alea 12
devin răspunsuri degradate; aș fi reparat ceasul stricând rezultatul. Reparația: ceasul urmează
BITUL care există deja (`Sampling.reasoning_on`), nu o taxonomie paralelă de „roluri" care poate
diverge tăcut de cea care decide legalitatea cererii; `timeout` pleacă **per CERERE** (clientul e
singleton, nu poate purta două ceasuri); bugetul aparține APELULUI, nu încercării
(`llm_call_total_cap_s=90s`, ceas monoton — altfel 75s × 3 = 225s, adică p50 reparat cu coada
stricată); iar `cap_ms` (NX-241) urmează ACELAȘI proprietar, fiindcă `llm_call_cap_ms=8_000` ar fi
strangulat apelul de compunere **în ziua în care cineva aprinde `TURN_DEADLINE_ENABLED`** — același
defect, dar descoperit într-un incident. Cenzura devine vizibilă: `llm_retry` numără la fel două
situații care cer reparații OPUSE (de-aia a trăit o lună), deci cauza e acum distinctă
(`llm_retry_timeout`/`_status`/`_connection`), iar `llm_call_over_30s` măsoară coada pe care peretele
o ascundea (maximul observat era exact **30,1s** — nu coada distribuției, ci zidul). Pragul de 75s e
declarat ca PRIMĂ calibrare, nu ca adevăr. `LLM_CALL_BUDGET_BY_ROLE_ENABLED=false` → byte-identic;
implicit e **ON**, spre deosebire de convenție, fiindcă nu e o capabilitate de câștigat cu
măsurători, ci un defect măsurat în care comportamentul de azi e strict mai prost pe fiecare axă.
NU atinge `reasoning_effort` (ar tăia și baseline-ul de ~25s, dar atinge calitatea ⇒ cere golden,
D15) și nu aprinde `TURN_DEADLINE_ENABLED`. Card: [`tasks/stage1/NX-311.md`](tasks/stage1/NX-311.md);
probe: `pytest tests/test_llm_call_budget.py -q` +
`PYTHONPATH=. python scripts/llm_call_budget_probe.py --business sole-ro`.

**NX-312 — turul de recomandare: trei apeluri de model → două (felia 1 măsurare, felia 2 tăiere).**
Pe v1, recomandarea avea două straturi puse unul peste altul: bucla de tool-calling (apelul 1
caută, apelul 2 scrie proză) și compunerea bogată (apelul 3 rescrie totul ca JSON). Pe calea bogată
reușită proza apelului 2 **nu era citită de nimeni**, iar forma cu trei apeluri era dominantă
(61 din 92 de ture). Felia 1 (#404) măsoară fiecare apel (`llm_usage.per_call`). Felia 2:
`run_tool_loop(stop_after_tools=…)` primește predicatul AGENTULUI (`_prose_round_redundant`, pur),
care sare runda de proză DOAR pe profilul `recommend`, când runda a chemat numai `search_products`
(și în paralel) și a adus produse. ORDER, alt profil, altă unealtă sau zero produse își păstrează
runda. Evenimentul `prose_round{skipped, reason}` are vocabular închis. Pe eșecul rich-ului,
`_finalize(recompose=False)` nu mai cere un apel de recompunere după unul picat (același defect
latent exista pe `show_more`, reparat tot aici). **Premisa din card, corectată pe test:** rezerva de
încadrare NX-299 cere ≥2 tipuri de produs, deci pe «vreau o cremă de hidratare» (șase creme) rich-ul
picat ar fi lăsat cardurile fără niciun cuvânt. Când proza a fost sărită, încadrarea e singura frază
posibilă și coboară la un tip (`rich_from_facts(sole_text=True)`). Rămâne descoperit, declarat: un set
fără niciun `product_type` rămâne fără frază. Kill-switch `TOOL_LOOP_SKIP_PROSE_ENABLED` (**ON**, ca
NX-311: risipă măsurată; OFF = byte-identic). Feliile 3-5 rămân deschise. Card:
[`tasks/stage1/NX-312.md`](tasks/stage1/NX-312.md); probă: `pytest tests/test_skip_prose_round.py -q`.

**NX-316 felia 1 — serverul RECUNOAȘTE apăsarea unui chip (flag OFF).** Pe v1 apăsarea retrimite
doar TEXTUL, iar handlerele deterministe îl reinterpretau: „linkul la X" servea linkurile TUTUROR
produselor afișate, iar comparația lua primele două din poziție. Măsurat pe `sole-ro`
(`scripts/nx316_chip_press_probe.py`, 30 de zile): **24,6%** din turele de după chips sunt
apăsări. `chip_press.recognize` (pur) refolosește lanțul care a PRODUS chips-urile (`from_cards` →
`renderable` → `render_move`) pe setul afișat, deci textul re-randat e identic prin construcție;
recunoscută = `move_id` oferit + egalitate cuvânt cu cuvânt. `ctx.chip_move` (owner: agent) intră
primul în `try_pre_intents` (`serve_chip_move`), pe ACELAȘI handler ca varianta din text, cu
produsele din `move_id`. Chips moarte (`chip_moves.drop_dead`, v1 + creier unic): îngustare pe o
nevoie deja rostită (`subject.needs`, NX-314), detaliu pe un tur cu un singur card. Mutările din
meniu nu se recunosc (sunt fraze de căutare), iar coborârea lui `pivot_shelf` e a feliei 3.
**Felia 2 — mutări PESTE setul afișat.** `choose_within` («Pentru ten uscat, ce aleg dintre
acestea?», forward) pe fațeta partiționantă nerostită aleasă de ACEEAȘI `narrowing_candidate` ca
întrebarea NX-315 (fără prag de câștig), cu dovada = produsele AFIȘATE cu valoarea; `fit_question`
(«X merge pentru ten uscat?», deepen) doar unde fișa cunoaște fațeta. Frazele: `value_phrases`
(round-trip NX-295), aduse de `finalize._facet_moves` pe ambele căi. Recunoașterea re-randează din
`move_id` + frază (starea n-are fațete, P8). Handlere: `fit_question` ⇒ `serve_details` cu
răspunsul din fișă (`fit_yes`/`fit_no` în `answer_shape_templates`, zero model); `choose_within` ⇒
setul afișat ∩ valoare (`planner.resolve_choose_within`): setul planului pe v1, seed pe creierul
unic (tiparul «mai ieftin»).
**Felia 3 — mutări din graful de relații.** `routine_next` («Arata-mi crema contur ochi care merge
cu X», pe produsul discutat, muchia `routine_next` înaintea lui `complement`) și `similar_to`
(substitutele în stoc), din UN query agregat pe tur (`catalog.relation_type_counts`, checkout
`chip_relations` DUPĂ apelul de model; picat ⇒ fail-open + `relations_error`). La apăsare,
`related_in_stock` servește setul, ca la `choose_within`. `pivot_shelf` coboară: pe explain/answer
când graful are o laterală, pe recommend după primul tur al subiectului. Felurile existente trec în
vocea clientului sub `chip_templates_v2` (doar cu flagul, fallback per fel, testat că regexurile de
azi le prind în continuare). Pe `sole-ro`, turul 3 al conversației reale oferă acum
`routine_next` pentru SOME BY MI Yuja Niacin.
`CHIP_MOVES_V2_ENABLED=false` = byte-identic. Card: [`tasks/stage1/NX-316.md`](tasks/stage1/NX-316.md);
probe: `pytest tests/test_chip_press.py tests/test_chip_moves_v2.py tests/test_relation_chips.py -q`
+ `NX_TESTS_READ_ENV_FILE=1 pytest tests/test_relation_chips_db.py -m integration -q`.

**NX-313 — «vreau o cremă de hidratare»: filtrul ghicit se judecă după CERERE.**
Turul real `bcd8e5c6` (`sole-ro`, 2026-09-23), recidiva lui `f7414c3e` DUPĂ #398: 72,4 s, trei BB-uri
de machiaj, două cu același nume, motivul primului card lipsă. Modelul a trimis `category="fata"`
(raftul „Fata" e MACHIAJ) lângă `concerns=hidratare` ROSTIT, iar textul prindea STRICT 4 BB-uri.
NX-305 nu avea ce prinde: scotea filtrele ghicite tot-sau-nimic și doar pe ture degradate.
Acum (`guessed_filter_verdict`, PUR) se scoate DOAR ce e ghicit, iar criteriul e o proporție pe
date: aceeași cerere fără ghicitură, câtă din ea cade în filtrul ghicit. Corect ⇒ doar îngustează
(0,16-0,94 pe trafic), greșit ⇒ contrazis (0,00). Prag 0,10, judecat doar pe `strict` (pe treptele
relaxate proporția măsura zgomot și scotea rafturi bune). Pe drum: `filters_only` + sort explicit
CRĂPA căutarea pe `main` (`$2` legat și nefolosit, «ceva mai ieftin»), poartă generală pe toate
treptele × sorturile; motivul cardului nu mai cade pe un IDENTIFICATOR din fișă („complex v11",
„N05"), cantitățile rămân respinse; un card per familie pe pagină (13% din ture arătau două carduri
cu același nume); `LLM_REASONING_EFFORT_AGENT` `high` → **`medium`** (60,1 s din 72,4 s era
raționamentul compunerii; calitatea rămâne de confirmat pe golden). Neacoperit, declarat: afirmația
falsă despre magazin (poarta de POTRIVIRE, NX-309). Card: [`tasks/stage1/NX-313.md`](tasks/stage1/NX-313.md);
probe: `pytest tests/test_guessed_filter_coherence.py -q` +
`PYTHONPATH=. python scripts/nx313_guessed_filter_coherence_probe.py`.

**NX-314 — subiectul conversației: «mai ieftin» servea benzi de nas la o cerere de cremă.**
Conversația `f4e1431e` (`sole-ro`, 2026-09-23): după SOME BY MI Yuja Niacin (110 lei), «si ceva mai
ieftin» a adus o bandă de nas de 3 lei și cinci măști sheet de 10 lei. `search_cheaper_than`
păstra din conversație doar `primary_category_id`-ul produselor afișate (catalogul e o PARTIȚIE
grosieră) și sorta `preț asc`, deci câștiga cel mai ieftin lucru din categorie. Sonda, pe 30 de
zile: 4 «mai ieftin», 3 cu un tip de subiect, **0%** din cardurile servite de acel tip. Cauza de
clasă: sistemul n-avea nicăieri un **subiect** (ce cumpără clientul), deci fiecare re-căutare îl
reconstruia din ce avea la îndemână. Acum e un obiect PUR (`src/conversation/subject.py`: raft
REZOLVAT prin vocabular, tip DOMINANT al setului arătat, nevoi ROSTITE rezolvate pe fațete), cu
un singur scriitor (`_learn_constraints`), pe ambele stări (`search_constraints["subject"]` pe v1,
`set_topic` + `Topic.product_type` pe v2; planul creierului nu-l poate muta, `subject_owned`).
Consumatorul «mai ieftin» păstrează aceleași porți dure și schimbă ORDINEA: același tip, apoi
nevoile, apoi ratingul, apoi prețul DESCRESCĂTOR (cel mai apropiat sub prag). Tipul ordonează,
nu exclude (`enforce_ready: false`); completările de alt tip poartă `subject_match=false`, spus
modelului în `_brief`. Pe turul real: **6/6 creme de față**, toate sub 110 lei. **Două abateri de
la card, pe măsurătoare:** (1) un set cu UN singur produs tipat (turul de detaliu) nu schimbă un
subiect existent, fiindcă exact setul dinaintea lui «mai ieftin» avea o singură cremă, iar regula
„minimum 2" l-ar fi șters; (2) raftul subiectului se ADAUGĂ la categoria afișată în loc s-o
înlocuiască, fiindcă raftul persistat e cel ghicit de model (NX-313: „Fata" e machiaj). Pe drum:
raftul observat se persistă ca CHEIE de catalog (`topic_switched` răspundea fals pe un sinonim),
iar `bounded_map` nu mai pierde `active_search.filters.concerns` pe v2. Măsurătoare nouă,
neblocantă: `subject_match{path, served, type_matched, share}` pe fiecare tur cu carduri, prima
poartă de POTRIVIRE măsurată (clasa NX-309). Kill-switch `CONVERSATION_SUBJECT_ENABLED` (ON; OFF =
SQL byte-identic). Card: [`tasks/stage1/NX-314.md`](tasks/stage1/NX-314.md); probe:
`pytest tests/test_conversation_subject.py tests/test_cheaper_subject_sql.py -q` +
`PYTHONPATH=. python scripts/nx314_subject_probe.py --replay`.

**NX-318 — câte cuvinte identifică un produs depinde de ce ALTCEVA e pe ecran.** Aceeași conversație
(`f4e1431e`). Codul decidea cu o constantă (minimum 2 cuvinte): `_mention_index` rata „EUBOS” scris
singur, iar `_fit_anchor` tăia «IT'S SKIN The Fresh Blueberries» la «IT'S SKIN The Fresh», prefixul a
DOUĂ carduri, deci apăsarea chip-ului cădea pe o clarificare. Acum un singur utilitar PUR
(`render_text.unique_prefixes`: cel mai scurt prefix de cuvinte unic în setul afișat, fără listă de
branduri, cuvânt gol pe locale nefolosit singur) are trei consumatori: ordinea cardurilor după text,
pragul ancorei unui chip (sub care nu se scurtează; nu încape ⇒ mutarea nu se oferă,
`dropped_ambiguous_anchor`) și `reference_resolver.match_name` (treaptă înaintea scorului pe
tokeni), deci un chip emis se rezolvă prin construcție pe produsul lui. Badge-ul e aceeași clasă pe
altă axă: un badge de CLASAMENT pe >50% dintr-un set de ≥3 carduri se scoate de pe toate
(`badges_suppressed`), cele de preț rămân. Pe drum: `top` se judecă pe ratingul SHRUNK (catalog:
49,4% → 34,5%), iar pragul voucherului vine din `DomainPack.badge_rules`. Măsurat pe 56 de ture cu
carduri: 19 cu același badge pe majoritate, până la 21 cu o ancoră ambiguă. **Premisă corectată:**
pe turul 1 fraza cu EUBOS era în `education`, nu în `intro`, iar ordonatorul citește doar `intro`
(0 ture greșite pe `intro`, 5 pe `education`), deci pe v1 turul acela rămâne neschimbat — extinderea
e o decizie separată. Flaguri `UNIQUE_NAME_PREFIX_ENABLED`, `SET_RELATIVE_BADGES_ENABLED` (ON; OFF =
byte-identic). Card: [`tasks/stage1/NX-318.md`](tasks/stage1/NX-318.md); probe:
`pytest tests/test_unique_prefixes.py tests/test_set_relative_badges.py -q` +
`PYTHONPATH=. python scripts/nx318_display_probe.py`.

**NX-315 — forma răspunsului după ce a CERUT clientul (v1, trei flaguri OFF).** Comparația iZi
pe «vreau o crema de hidratare»: iZi pune o întrebare („ten uscat, mixt/gras sau sensibil?") și un
paragraf „cum alegi"; la «cum se foloseste prima» răspunde din prima frază. Nativx: nicio
întrebare, nicio educație, iar instrucțiunile îngropate sub un paragraf care revinde produsul.
**Premisa draftului era greșită** (creierul unic „aprins"; e stins din 17 sep), deci totul e pe
calea v1, singura care răspunde clientului. (1) `GUIDANCE_REQUIRED_ENABLED`: când
`answer_shape.shape_for` cere `closing`, mesajul turului face `education` obligatorie și numește
axele din `decision_axes`. Lipsa se numără (`guidance_dropped`), fără retry. (2)
`NARROWING_QUESTION_ENABLED`: `narrowing_candidate` (pur) alege o fațetă `partitioning`, nerostită,
neîntrebată, cu 2-4 valori în setul SERVIT și câștig ≥ 0,30 (NX-235). Frazele opțiunilor vin din
meniul închis NX-295, cu round-trip. Schema capătă `question` DOAR pe turele cu ofertă, iar poarta
cere ca întrebarea să numească ≥2 opțiuni (partea care le deosebește, prin `corroborated_by`).
Rezultatul: cel mult o întrebare pe tur, ca ultimă frază a `intro`. (3) `HOWTO_FROM_CATALOG_ENABLED`
duce și instrucțiunile MAGAZINULUI (`DomainPack.howto_sections`) în apelul rich, care altfel
rescria răspunsul pe regula de deep-dive („intro = ce ESTE produsul"). Cifrele lor trec de scrub.
Măsurătoarea `explain_shape` (proporția de instrucțiuni regăsite în răspuns + „prima frază
revinde") rulează și cu flagul stins, ca baseline. OFF = mesaj și schemă byte-identice (testat pe
forma veche). Aprinderea se decide pe golden cu model real (D15). Card:
[`tasks/stage1/NX-315.md`](tasks/stage1/NX-315.md); probe:
`pytest tests/test_plan_guidance.py tests/test_narrowing_question.py tests/test_explain_shape.py -q`.

**Fix 2026-09-16 (2) — creierul unic era pus să citeze dovezi pe care nu i le arăta nimeni.**
Găsit pe prima conversație REALĂ de după aprinderea flagului (`sole-ro`, `conversation_traces` +
`analytics_events`), nu pe fixture: clientul a scris „parca mi uscat parul dupa ce fac dus", apoi
„sincer nu stiu tu ce mi recomanzi?", iar modelul a scris de DOUĂ ORI o recomandare motivată („Aș
începe cu o mască hidratantă, fiindcă…"; „aș alege Kundal Macadamia Hair Serum, 80 lei, fără
clătire…"). Clientul n-a văzut niciuna: a primit fallback-ul determinist. Trei defecte care se
compun.
(1) **Registrul de evidence se construia DUPĂ bucla de tool-calling**, iar planul se cerea ÎN
timpul ei — deci la primul apel modelul nu văzuse niciun `evidence_id`, deși instrucțiunile îi cer
să citeze „evidence_ids din evidence-ul serverului". Le inventa (`search-1`,
`search:<product_id>`): **6 din 6, 100%**, deci `unknown_evidence` era garantat pe ORICE tur cu
produse, iar reparația nu era o plasă, ci o taxă (3 runde de model, 32-82 s e2e). Rândurile se
atașează acum la rezultatul fiecărui tool care aduce produse, construite de ACELAȘI
`build_answer_plan_context` care validează. Calea v1 nu avea defectul (`_plan_prompt` le pune de la
primul apel) — creierul unic l-a introdus, iar flagul l-a făcut vizibil.
(2) **`missing_product_evidence` cerea un ritual nedeclarat.** Validatorul pretindea pe fiecare
produs exact un rând `kind == "identity"` ȘI unul `kind == "variant"`; reparația cita `:url` +
`:price`, adică dovezile care chiar susțin recomandarea, și pica după ce citase corect. Dovedirea
e acum pe APARTENENȚĂ (rândurile sunt construite de server din catalogul viu, deci `:price`
dovedește existența exact cât `:identity`, al cărui `value` e chiar product_id-ul), iar regula e
SPUSĂ în prompt. Poarta rămâne închisă pe id inventat, pe variantă numită fără dovadă a ei și pe
rândul ALTUI produs — ultimul era o gaură latentă: fără `continue`, o dovadă străină de tip
`identity` bifa `has_identity` pe un produs pe care nu-l atingea.
(3) **Textul numea 3 produse, ecranul arăta 6.** `_deterministic_reply` taie la 3, iar
`_serve_exhausted` atașa `run.retrieved[:6]`: două plafoane independente peste aceeași listă nu pot
rămâne de acord. `grounded_fallback_reply` întoarce acum ȘI produsele pe care textul le numește.
Nimic din aval nu putea prinde nimic din toate astea: produsele și prețurile erau REALE, deci
validatorul (stagiul 8) și `grounding_guard` le-au lăsat să treacă — sunt porți de **ADEVĂR**, nu
de **POTRIVIRE**. Regresii: `tests/test_brain_evidence_contract.py` (verificat că pică pe codul
vechi: id-urile arătate trebuie să fie EXACT cele acceptate, nu doar „există un bloc").
**Rămâne deschis, deliberat:** pe aceeași conversație, locul 1 la „păr uscat după duș" a fost un
PROSOP, fiindcă `concerns` s-a rezolvat `not_in_vocabulary` (filtrul n-a rulat) iar numele
produsului conține literal fraza. Fațeta `product_type` l-ar exclude, dar e `enforce_ready: false`
— dreptul de a exclude cere auditul de precizie NX-268/271, nu o decizie luată în trecere (D15).

**NX-251 — autoritatea faptelor + triajul iese de pe drumul sincron (DARK, flag OFF).**
NX-239 rezolvase doar jumătatea de SCRIERE a lui D1: nano nu mai era writer, dar APELUL rămânea —
fiecare tur plătea o clasificare care primea contextul complet, după care ACELEAȘI blocuri plecau
încă o dată la brain. `TRIAGE_SYNC_SHADOW_ENABLED=false` (default, cere `SINGLE_BRAIN_ENABLED`,
validat la boot) = neatins; ON = zero apeluri de model mic pe calea răspunsului, clasificarea se
mută POST-tur ca MĂSURĂTOARE (`classify_message` e EXTRAS, nu duplicat — două prompturi întreținute
separat ar diverge, și atunci ai compara copiile, nu arhitecturile). Proprietarul lui `ctx.route`
devine `agent_stage`, clasa de tur vine din obligațiile deterministe, iar brain-ul primește
REUNIUNEA sales+order (fără triaj nu știm dacă turul e vânzare sau comandă). Dependența care face
mutarea posibilă: **sursa unui fapt o decide CODUL**, nu modelul — `corroborated_by`
(`src/conversation/needs.py`, pur, agnostic de limbă, potrivire pe prefix pentru flexiunea
românească) confirmă că valoarea a fost ROSTITĂ ⇒ `user_explicit`; altfel `model_inferred` ⇒ `soft`.
Fără ea, scoaterea triajului ar fi șters tăcut noțiunea de constrângere `hard` (singurii producători
de `user_explicit` erau triajul și `clarify_resume`). În plus, trei găuri închise: `revoke` din
sursă necapabilă nu mai poate ȘTERGE un fapt al clientului (`unsupported_revoke` — simetric cu
`hard_downgrade`; nevoile create de model rămân `soft`, deci „nu vreau Sony"→„de fapt accept Sony"
trece neatins); clarificarea brain-ului se PERSISTĂ (altfel `clarify_resume` n-avea ce relua, iar
`attempts` rămânea 0 ⇒ aceeași întrebare la infinit cu poarta de gain stinsă); repair-ul primește un
digest de evidence cu trunchierea DECLARATĂ (rula în afara conversației cu tool results, deci nu
putea repara exact planurile care depindeau de ele). Sub state v2, constrângerile nu mai pleacă de
două ori în prompt (`memory_block` e proprietarul; `state_block` rămâne al produselor afișate), iar
octeții se măsoară pe sursă (`context_bytes{consumer}`).
**Consecință găsită după livrare (reparată):** cu `filters` gol la fiecare tur, porțile din
`src/agent/deterministic.py` care foloseau `not route.filters` ca să deosebească o SCURTĂTURĂ de o
RAFINARE au rămas permanent deschise — „mai arată-mi, dar sub 100 lei" pagina pool-ul căutării
VECHI, iar ramura de paginare se consumă ÎNAINTEA creierului unic, deci mesajul nu ajungea deloc la
model. Nimic din aval nu prinde asta: produsele și prețurile sunt reale, iar validatorul și
`grounding_guard` sunt porți de ADEVĂR, nu de POTRIVIRE. `carries_new_constraints` nu enumeră
CONSTRÂNGERILE (mulțime deschisă: clienții nu scriu la fel, iar o listă în urmă = răspuns greșit
tăcut), ci scade FORMULA scurtăturii — declanșator + referințe (`ANY_ORDINAL_RE`, tabel partajat) +
cuvintele funcționale ale locale-i (`catalog.query_terms`) + fillers; orice reziduu ⇒ turul pleacă
la model. Măsurat pe 24 de fraze: ambele forme ratează ~la fel, dar enumerarea ratează RAFINĂRI
(răspuns greșit) și reziduul ratează SCURTĂTURI (o inferență în plus). Aplicat DOAR pe paginare:
pe porțile ancorate (link/compare) cuvintele în plus sunt de obicei referință („linkul către crema
asta"), iar căderea pe model ar risca regresia NX-131 — preț asumat și pinuit prin test. Kill-switch
`REFINEMENT_GUARD_ENABLED=false`. Detalii:
[`docs/NX-251-CONTEXT-ORCHESTRATION.md`](docs/NX-251-CONTEXT-ORCHESTRATION.md); probă:
`pytest tests/test_context_journeys.py tests/test_context_orchestration.py -q`.

**NX-255 — istoricul minte: întrebarea clientului se tăia, iar ce a arătat botul nu se persista
(DARK, flag OFF).** `conversation_transcript` aplica `[-1200:]` pe stringul deja UNIT: tăiere oarbă,
fără noțiune de rol, de graniță de mesaj sau de cuvânt. Măsurat pe `webchat` real (409 inbound / 408
outbound, 2026-08-24): clientul scrie **28** de caractere în medie (p90 46, max 134), botul **750**
(p50 814, p90 1391, max 1822) — o fereastră de 6 mesaje cere ~2334 de caractere, deci se arunca
începutul, adică exact întrebările, iar transcriptul putea porni cu un fragment fără cap
(`"aza sistemul). Spune-mi te rog…"`). A doua gaură, mai mare: ce a ARĂTAT botul nu exista nicăieri
— payload-ul bogat mergea în `outbox` (canale async) sau nu se construia deloc (web sincron, care
iese din `_build_fragment` înainte), `Message` n-avea câmp `payload`, iar `get_recent_messages` nu-l
selecta. Toate rândurile reale aveau exact `{turn_id, fragment_index}`. Rămânea doar
`state.displayed_products`, suprascris per tur și randat cu max 3 intrări — deci „al doilea pe care
mi l-ai arătat" n-avea ancoră, botul re-recomanda ce arătase, iar recitirea propriei proze producea
prețuri pe care validatorul le respinge ca inventate (retry + fallback fără cifre: nu o halucinație
vizibilă, ci **degradare plătită la fiecare follow-up**). Cu `STRUCTURED_HISTORY_ENABLED`: clientul
e **verbatim**, turul botului păstrează proza **integrală** (few-shot din propria voce) PLUS un bloc
`[a aratat]` cu ref-uri `{id, nume, preț}` și vechimea în ture. Regula care face păstrarea prozei
sigură: **proza spune CUM vorbești, blocul spune CE e adevărat** — cifrele se reconfirmă prin tool.
Degradarea e deterministă: proza celor mai vechi ture cedează prima (la graniță de propoziție,
păstrându-le faptele), apoi se elimină intrări întregi; un mesaj de client poate dispărea, dar nu
poate fi mutilat. Card: [`tasks/stage1/NX-255.md`](tasks/stage1/NX-255.md); probă:
`pytest tests/test_structured_history.py -q`.

**NX-289 — WhatsApp și Telegram nu mai există în proiect (cod + schemă).**
NX-179 le declarase ÎNGHEȚATE: cod păstrat, zero investiție. Costul înghețului nu era zero —
fiecare stagiu care le numea era o ramură pe care nimeni n-o executa dar toți o citeau, iar
CHECK-urile care încă acceptau `whatsapp` erau o invitație. Au plecat: pachetul Telegram,
`MetaClient`, parserul Meta, rutele `GET/POST /webhook` + `verify_meta_signature`, `wa_templates`
și poarta de template din proactiv, `message_status_events`, `in_24h_window`, callback-urile de
butoane inline (navigarea de carusel), capabilitățile `TEMPLATE/CAROUSEL/EDIT/TYPING`, serviciile
`telegram-poller` din ambele compose-uri, variabilele `META_*`/`TELEGRAM_BOT_TOKEN`/
`TYPING_ENABLED`. **Seam-ul de canal (NX-60) a RĂMAS** — nu e o dependență de un canal anume, e
motivul pentru care stagiile 3-9 nu știu de niciunul; registrul are azi o intrare.
Trei consecințe găsite prin măsurare, nu presupuse: (a) `IDENTIFIED_CHANNELS` devenea mulțimea
VIDĂ, deci plafonul de cost per-contact (NX-125) și poarta de comandă/retur (NX-128) ar fi murit
TĂCUT — re-cheiate pe `identity_is_stable`, unde sursa rămasă e login passthrough-ul verificat
(NX-129); (b) `check_order` avea o ramură `contact_id` pentru canalele unde id-ul de canal ERA
contul — devenise inaccesibilă prin construcție, deci a fost scoasă; (c) default-ul
`InboundMessage.channel_kind = "whatsapp"` era load-bearing în teste (le făcea „client
identificat" fără să ceară identitate). Migrarea **051** scoate obiectele rămase și restrânge
vocabularul CHECK-urilor; măsurat înainte pe baza live: 22 canale, TOATE `webchat`, zero rânduri
în tabelele care se șterg, și RULATĂ pe baza reală într-o tranzacție cu `ROLLBACK` (idempotentă,
zero rânduri pierdute). Rularea aia a scos un al patrulea defect: `db/queries/proactive.py`
rămăsese cu `template_id` în claim și în INSERT, coloană pe care 051 o dropează — deci motorul
proactiv ar fi crăpat DUPĂ migrare, pe un drum de background. Suita nu-l putea prinde (testele
stub-uiesc `conn`, iar coloana încă există azi), așa că fixul vine cu o poartă:
`tests/test_dropped_objects_guard.py` refuză ca `src/` să numească un obiect pe care o migrare îl
șterge — cu regula „dropat apoi recreat = viu" (`search_tsv` din 046/049) și privind codul, nu
proza din docstring-uri. Detalii:
[`docs/051_drop_frozen_channels.sql`](docs/051_drop_frozen_channels.sql) +
[`tasks/NX-289.md`](tasks/NX-289.md).

**NX-297 — nano a IEȘIT din proiect: un singur model pe drumul răspunsului.**
Triajul (`stages/triage.py`, 559 de linii + un prompt de 2.785 de tokeni pe FIECARE tur) scria 42%
din răspunsuri fără să fi făcut vreo căutare — de acolo veneau chips-urile seci, clarificările
despre produse pe care magazinul nu le are, și turele de rutină închise înainte de orice unealtă.
Justificarea lui economică dispăruse pe 2026-08-24, când `MODEL_AGENT` a trecut pe `gpt-5.6-luna`
(0,20/1,20 $ per 1M): nano costă LA FEL pe input și MAI MULT pe output. Câștigul cardului e de
CALITATE, nu de cost.
Au plecat: stagiul, `MODEL_TRIAGE`, `COST_TRIAGE_USD`, toate flagurile `TRIAGE_*`,
`CLOSURE_CHIPS_ENABLED`, măsurătoarea shadow din `aftercare.py` și referințele din
`control_plane.py`. Pipeline-ul are **10 stagii**. Ruta n-o mai decide nimeni: absența ei e cazul
NORMAL, iar `agent_stage` o tratează ca atare (`route_defaulted` + toolsetul REUNIUNE sales+order,
fiindcă „unde e comanda mea" nu mai e clasificat). `order` nu mai e rută; `check_order` își
păstrează zidul de login (NX-128/129).
**Patru mecanisme rămâneau fără producător și ar fi degradat TĂCUT**, toate re-cheiate pe cod pur:
clasa de tur (`turn_class_for(obligations)` în loc de `classify(route, purchase_intent)` — altfel
orice tur devenea `RECOMMENDATION` în ziua în care cineva aprinde bugetele); stiva de constrângeri
(`OBSERVED_CONSTRAINTS_ENABLED` trece pe **ON**: era a doua sursă, e acum singura); raftul
conversației (`category` cu care a CĂUTAT agentul — decizia OPUSĂ celei din felia 3, fiindcă atunci
alternativa era o sursă mai bună, acum e niciuna); și `_emit_query_spec_shadow` (NX-208), mutat în
`agent_stage` — ștearsă odată cu fișierul, ar fi dispărut tocmai măsurătoarea pe care se decide
dezghețarea NX-188/189. Felia 3 nu rula DELOC pe creierul unic (ramura iese înainte, iar căutarea
ocolește `ToolRun.execute` prin portul NX-238): ambele reparate.
Invarianta D1 devine verificabilă pe FORMĂ, nu pe un flag: o poartă AST refuză ca vreun stagiu din
`src/worker/stages/` să cheme `classify_json`/`complete_schema`/`run_tool_loop`. Un al doilea triaj
scris peste șase luni ar arăta exact ca primul.
**Consecință de produs, declarată:** o întrebare de clarificare nu mai poate cita o cifră pe care
catalogul n-o susține — validatorul stagiului 8 o respinge, iar nano o servea fără validator.
Kill-switch: NU există. Card: [`tasks/stage1/NX-297.md`](tasks/stage1/NX-297.md).

**NX-296 — sugestiile: de la etichete seci la continuări pe care serverul le poate onora.**
Cererea a fost „5 sugestii, ca la iZi, legate de mesaj și de istoric". Cifra nu era însă una, ci
TREI, în module care nu se cunoșteau: producătorul tăia la 4 (`clarify_menu._MAX_CHIPS`), calea
bogată v1 la 6, randorul web la 5 — deci contractul FE scria „max 5" și clientul primea 4, iar o
creștere a producătorului s-ar fi pierdut TĂCUT în randor. Acum cifra e `settings.chip_slots`
(implicit 5) și `ground_suggestions` cere `limit` ca parametru OBLIGATORIU: un apelant care uită de
proprietar nu mai poate cădea pe o constantă locală, fiindcă nu mai există.
Conținutul era ori sec, ori negarantat: pe calea vie chips-urile SUNT etichetele meniului închis
(«Ten», «ten uscat»), iar bogăția de tip iZi exista doar pe v1, unde textul era scris liber de
model — producătorul pe care `CHIP_PRODUCERS` îl declară NEANCORAT. Reparația inversează ordinea:
**un chip nu e un TEXT pe care îl validăm, e o MUTARE pe care serverul o poate executa**
(`src/conversation/chip_moves.py`) — șapte feluri, vocabular ÎNCHIS, fiecare cu dovada calculată
din date pe care turul le are deja (meniul NX-295, cardurile turului, prețurile lor), deci ZERO
I/O nou pe drumul sincron. Modelul poate doar să REFORMULEZE o mutare oferită
(`AnswerPlanV2.chip_labels`, același apel, zero rundă în plus), iar poarta e PER MUTARE: textul
trebuie să păstreze fraza-ancoră, altfel se emite șablonul serverului. Mulțimea de comparat are UN
element, deci nu mai poate trece din întâmplare — exact obiecția pentru care NX-295 spune că
`ground_suggestions` nu e poarta potrivită pentru follow-up-uri. Poarta nu e cosmetică: pe v1
apăsarea retrimite `label` ca MESAJ NOU al clientului, deci **textul ESTE comanda**. Asimetria e
deliberată: fail-CLOSED pe textul modelului, fail-OPEN pe mutare. Copy-ul nu e în cod
(`DomainPack.chip_templates`, per locale, P11). Legătura cu istoricul e `state.offered_chips`
(`move_id`-uri, nu texte — P8; cap 20, în `PASSTHROUGH_KEYS`), iar rolurile vin din
`plan.obligations`: pe un tur `answer` lipsește `forward` cu totul, fiindcă cinci îngustări sub un
răspuns punctual arată ca un bot care nu te-a auzit.
**Patru defecte au ieșit doar la RULARE**, trei pe catalogul real: șablonul NOSTRU trunchia ancora
(«Compara Serum Hidratant LumaDe… cu…»), `reviews` nu apărea NICIODATĂ (la dovadă egală câștiga
alfabetul), pe un tur factual nu rămânea nicio cale de adâncire (numele scurt real are ~33 de
caractere, plafonul e 56), iar primul tur dădea 2 chips din 5 (fără raft, singurul fel disponibil e
`pivot_shelf`, iar plafonul pe fel îl tăia — plafonul e acum o PREFERINȚĂ, nu o limită). Ancora se
scurtează pe CUVINTE ÎNTREGI, minimum două, pe TOATE sloturile. Verdict măsurat pe SOLE:
**14 chips, 0 moarte, `PASS`**; primul tur rămâne 4/5, cauza fiind `_MAX_PER_DIMENSION=4` din
NX-295, declarat, nu schimbat pe tăcute. Kill-switch `CHIP_MOVES_ENABLED=false`. Card:
[`tasks/stage1/NX-296.md`](tasks/stage1/NX-296.md); probă:
`python scripts/chip_press_probe.py --business sole-ro`.

**NX-298 — un card acolo unde catalogul avea 518: numărul de produse are un PROPRIETAR, iar pagina
se umple.** Găsit pe un tur REAL (`conversation_traces`, turn `aa2dc1d9`): la „vreau ceva sa scap
de cosuri", nevoia s-a rezolvat `concerns=acne` cu dovadă **518 produse**, iar clientul a primit
**UN** card. Trei plafoane s-au compus, iar a câștigat cel mai mic. (1) Cifra „câte produse" avea
PATRU proprietari care nu se cunoșteau — promptul de vânzare „2-3", promptul rich „până la 4",
`compose._MAX_RICH_ITEMS = 4`, schema uneltei „1-6" — deci modelul a cerut `limit=3` fiindcă așa
scria în prompt. Acum e `settings.card_slots` (**6**), citit de toți patru (prompturile prin
marcatorul `{CARD_SLOTS}`, cu poarta de import care refuză un marcator nedeclarat; `SearchArgs.limit`
prin `default_factory`), exact ca `chip_slots` la NX-296. (2) Textul se cerea a DOUA oară peste
filtrul care îl însemna deja: pool 5 din 518, fiindcă magazinul scrie „acnee" și clientul „coșuri".
E clasa NX-293 în varianta care NU dă zero, deci pe care `filters_only` n-o prinde (scara se oprește
la prima treaptă cu ORICE rezultat). **Varianta evidentă a fost încercată și RESPINSĂ pe
măsurătoare:** scăzând din text termenii purtați de filtre, fraza rămâne fără niciun cuvânt, deci
fără ordonator, iar pagina devine „cele mai bine notate produse cu eticheta acnee" (un aparat sonic
de curățare în locul plasturilor) — un termen poate fi redundant ca POARTĂ și informativ ca
ORDONATOR. Reparația e la capătul celălalt: pe treapta terminală textul coboară din poartă în
ordonator (`WHERE` = filtrele, `ORDER BY` = `ts_rank_cd`, apoi rating/preț/`p.id`), iar sloturile
neumplute se completează din setul filtrului de SUBIECT (`only_filters_step`), DUPĂ potrivirile de
text și marcate `lexical_step='filters_only'`. (3) Diversificarea echilibra brand și preț, dar nu
știa ce ESTE produsul: cotă pe `attributes.product_type` (max 2/tip), ORDINE nu excludere (tipul e
`enforce_ready: false`), cu relaxare GRADUALĂ — dintr-odată, faza 2 lua înapoi exact ce sărise faza
1, deci efectul cotei era zero. Măsurat: «crema pentru cosuri» 2 → **6** produse (potrivirile de
text rămân primele), cota pe tip 3 → **4** tipuri pe pool real, NX-293 neatins. Kill-switch
`SEARCH_FILL_FROM_SUBJECT_FILTER_ENABLED=false`. Rămâneau declarate NEACOPERITE voucherul și
chips-urile ancorate pe v1; **NX-299 le-a livrat pe amândouă**, iar `delivery_class` = 0/2.758
rămâne (livrarea nu e reprezentabilă, e gaură de DATE). Card:
[`tasks/stage1/NX-298.md`](tasks/stage1/NX-298.md); probă: `python -m scripts.nx298_recall_probe`.

**NX-299 — răspunsul are o FORMĂ, iar scara de relaxare învață PROVENIENȚA.**
Pe turul REAL `42744330` (2026-09-17 20:39, «vreau ceva sa scap de cosuri», DUPĂ NX-298), modelul a
cerut `category="dermato-cosmetice"` — subarbore de **6 produse** din 2.758, când `concerns=acne`
avea **518** și catalogul are 18 plasturi, 60 de seruri, 50 de creme sub acea nevoie. Scara a aruncat
fațeta ROSTITĂ de client («cosuri») pe treapta 1 și a păstrat raftul GHICIT până la treapta
terminală, deci `filters_only` a servit raftul greșit. Nu e o ordine greșită între două tipuri de
câmp, e o **dimensiune care lipsea**: `_relax_ladder` ordona după TIPUL câmpului și nu știa nimic
despre cine a afirmat valoarea, deși proveniența există din NX-251 (`corroborated_by`) și e folosită
în ACELAȘI fișier pentru constrângerile numerice. Acum treptele se ordonează (proveniență, tip), iar
„categoria e dură" devine **„categoria ROSTITĂ e dură"**. `uttered_by_client` extinde coroborarea la
istoricul CLIENTULUI, fiindcă aici asimetria se inversează față de NX-251: acolo un `False` producea
o nevoie mai slabă, aici face valoarea relaxabilă. Două interacțiuni prinse de suită, nu presupuse:
garda off-category ar fi SUPRIMAT tot setul pe treapta nou-deblocată (zero carduri în loc de două
greșite), iar `corroborated_by` fiind potrivire LITERALĂ dă fals „ghicit" la «vreau makeup» cu
`category=machiaj` — de aceea raftul ghicit e relaxabil DOAR când mai rămâne o fațetă, altfel
interogarea ar rămâne fără subiect (eșecul respins la NX-298). În plus, mărimea rafturilor intră în
prompt, rotunjită la 2 cifre semnificative (cifra era deja calculată și se arunca; rotunjirea ține
prefixul de cache stabil la driftul de catalog).
**Forma răspunsului devine un CONTRACT** (`src/agent/answer_shape.py`, PUR): promptul cerea deja
forma iZi cuvânt cu cuvânt („În `intro` spune ce tipuri ai pus pe masă"; „SEGMENTARE (ca iZi)"), dar
nimic nu verifica dacă slotul a ieșit — iar P13 spune de ce nu ține: *vocea e cod, nu speranță*.
Trei sloturi cu CONDIȚII măsurabile pe setul SERVIT (nu pe obligațiile planului, care nu există pe
calea vie): `framing` la ≥2 clase de produs, `closing` la ≥2 carduri și ≥1 axă de decizie,
`fit_line` la orice card. Pe o întrebare punctuală nu se cere nimic în plus, deci contractul **nu e
șablon** — aceeași regulă ca `roles_for` (NX-296), extinsă de la chips la tot răspunsul. Rezerva de
încadrare urmează tiparul NX-296 (șablon în `DomainPack` per locale, construit din tipurile REAL
servite, zero runde în plus, niciun al doilea writer). `scrub_intro` taie acum pe PROPOZIȚIE ca
`scrub_education`: pe turul măsurat superlativul din prima frază ucidea și pe a doua, care era
curată, iar clientul primea ZERO text. Poarta MEDICALĂ rămâne pe tot paragraful (o relaxare n-are
voie să atingă o protecție P0). Defect găsit la rulare: separatorul rupea listele numerotate, deci
«1. Curatare, 2. tonifiere, 4. ceva» devenea «1. Curatare, 2. ceva» — nu trunchiat, RENUMEROTAT.
Eșecul de formă e acum vizibil (`answer_shape`: cerut vs servit vs motiv, vocabular ÎNCHIS); înainte
turul arăta în telemetrie ca un succes curat. `CHIP_MOVES_V1_ENABLED` trece pe **ON** (mecanismul
exista din NX-297 felia 5, stins acolo unde se vede), iar voucherul devine badge derivat din
`coupon_price` vs `price`, cu procentul rotunjit în JOS. Măsurat pe turul real: treapta
`filters_only` → `strict`, iar cele 6 produse trec de la șampon + ulei de corp + cremă de corp la
**plasturi anti-acnee, spumă de curățare cu BHA și creme cu retinal/niacinamidă**, adică sortimentul
iZi. Raftul ROSTIT rămâne neatins. Kill-switch-uri: `SEARCH_RELAX_BY_PROVENANCE_ENABLED`,
`ANSWER_SHAPE_ENABLED`, `CHIP_MOVES_V1_ENABLED`, `CARD_COUPON_ENABLED`.
**Pragul voucherului a fost corectat DUPĂ livrare, pe măsurătoare:** la 5% badge-ul apărea pe
**76,9%** din catalog cu aceeași valoare (un singur cod, `WELCOME15`, iar 90,6% dintre produse au
exact -15%) și fura „Top Favorit" de la **994 din 1.363** de produse eligibile. Un voucher de bun
venit e o promoție de MAGAZIN, nu o proprietate a produsului, deci badge-ul era `noise_badges`
reintrodus pe partea de AFIȘARE. Pragul e acum **25** (prăpastie măsurată: 15% → 76,9%,
16% → 7,2%), unde rămân doar cele ~198 de produse cu reduceri reale de 48-50%. Card:
[`tasks/stage1/NX-299.md`](tasks/stage1/NX-299.md); probă:
`PYTHONPATH=. python scripts/nx299_shape_probe.py`.

**Fix 2026-09-16 — botul oferea chips pentru produse pe care magazinul nu le vinde.**
Găsit pe o conversație REALĂ (`conversation_traces`): la „vreau un cablu usb", un magazin de
COSMETICE a răspuns cu o întrebare de clarificare și patru sugestii — «Pentru telefon, USB-C, 1-2
metri», «Pentru consola, cablu USB de date». `suggestions` era SINGURUL câmp al contractului de
triaj fără poartă: `category_key` inventat se aruncă de mult, `concerns` se filtrează pe
vocabularul pachetului, dar chips-urile plecau la client exact cum le scria nano. Un chip nu e o
părere, e o promisiune apăsabilă: textul lui reintră în pipeline ca mesaj NOU al clientului.
**Reparația nu e un filtru peste ce scrie modelul, ci un MENIU ÎNCHIS dat înainte**
(`src/catalog/clarify_menu.py`), iar distincția e măsurată, nu stilistică: pe catalogul SOLE,
rezolvarea termen-cu-termen a chip-ului cu cablul întoarce `c` = KNOWN pe `key_ingredients` (de la
«vitamina c») și `1`/`2` = KNOWN pe `shade_code`, deci un filtru „măcar un termen se rezolvă" ar fi
PĂSTRAT tocmai chip-ul fals și ar fi aruncat «Am tenul uscat». Un vocabular bogat are un cuvânt
pentru aproape orice, deci nu poate fi folosit ca detector de minciuni.
Meniul se compune din catalogul REAL (categorii servabile + fațetele care există CHIAR sub raftul
discutat, `facet_keys_in_scope`) și din conversație (cheile pe care clientul le-a spus deja nu se
mai oferă). Invarianta care îl face onest e **round-trip-ul**: o frază intră doar dacă se rezolvă
înapoi, prin ACEEAȘI `resolve_any` pe care o folosește căutarea, pe o cheie cu produse. Măsurat:
182 din 186 de fraze candidate trec, iar cele 4 respinse sunt exact «ten mixt»/«ten normal» — chei
pe care pachetul le promite și catalogul nu le are. Nicio listă scrisă de mână nu le-ar fi prins.
Poarta e ASIMETRICĂ, deliberat: **fail-OPEN pe vocabular** (DB jos ⇒ meniu gol ⇒ sugestiile trec ca
azi, altfel o clipeală de DB ar deveni „zero chips, mereu" — aceeași capcană ca la garda
off-category) și **fail-CLOSED pe chip** (nu numește nimic din meniu ⇒ se aruncă; lista se
COMPLETEAZĂ din meniu, nu se înlocuiește).
Afirmația mai tare — „nu vindem așa ceva" — are prag propriu de dovadă și a fost calibrată pe
trafic: regula naivă («≥2 termeni, niciunul rezolvabil») declanșa pe 8 din 25 de ture reale și era
corectă pe 2, adică ar fi răspuns „nu vindem asta" la „Trimite-mi linkul la produs". Cu două
condiții STRUCTURALE în plus — conversația n-a arătat încă produse, și niciun cuvânt nu e aproape
de limba catalogului (typo guard) — ajunge la 2 declanșări, ambele corecte, zero false. Nicio listă
de cuvinte românești (P11).
Patru defecte au ieșit doar la RULAREA pe catalogul real, după ce codul „arăta corect": rafturile
oferite erau cele mai MICI patru (tăiere înainte de sortare), căderea pe meniu ARUNCA singura
sugestie validată, `oily` se oferea drept «luciu» (cel mai scurt alias, un simptom fără context),
iar poarta accepta „am **ten**ul uscat" fiindcă «Ten» e subșir — acum potrivirea e pe cuvinte
întregi consecutive. Toate patru sunt pinuite în `tests/test_clarify_menu.py`.
Kill-switch `CLARIFY_MENU_ENABLED=false` → comportamentul de dinainte, byte-identic.
**Neacoperit, declarat:** calea creierului unic (`SINGLE_BRAIN_ENABLED`) nu emite azi chips deloc
(`brain.py` cheamă `set_reply` fără `suggestions`), deci clasa de defect nu există acolo; sugestiile
bogate ale agentului de vânzare (`compose._suggestion_chips`) rămân NEfiltrate — în turul măsurat
au fost salvate de downgrade-ul `no-items-selected`, ceea ce nu e o garanție.

**Fix 2026-09-16 — o cratimă anula filtrul de raft, iar stiva de constrângeri nu se putea reseta.**
Găsite pe o conversație REALĂ (`conversation_traces` + `analytics_events`), nu pe fixture: clientul
a discutat despre un ruj, apoi a cerut „vreau sa vad produse de par" și a primit UN șampon de
mătreață, iar la „altceva ?" patru carduri de fard de obraz. Trei defecte independente, care se
compun.
(1) **Tokenizare asimetrică în vocabular** (`src/catalog/vocabulary.py`): intrările se despărțeau
pe `-`, TERMENUL cerut nu — deci «ingrijirea parului» se rezolva (AMBIGUOUS, 206 produse), iar
«ingrijirea-parului», forma pe care modelul o scrie natural fiindcă slug-urile arată așa, ieșea
`UNKNOWN(not_in_vocabulary)`. Un UNKNOWN nu ajunge NICIODATĂ în `WHERE`, deci turul a rulat fără
filtru de categorie, iar singurul filtru rămas a fost `brand`, care **nu se relaxează niciodată** —
rezultatul a fost catalogul de machiaj al brandului, servit la o cerere de păr. Un singur
tokenizator (`_tokens`) pentru ambele părți ale potrivirii.
(2) **Resetul stivei depindea de un câmp gol.** `merge_constraints` reseta doar când
`RouteDecision.category_key` există ȘI diferă — dar acela vine EXCLUSIV din triaj, care nu rulează
pe turul de după o clarificare (`clarify_resume` rutează determinist) și oricum nu produce o
categorie pe majoritatea turelor (măsurat pe SOLE: 11/15 NULL; `reset=true` o dată în 11 merge-uri).
Al doilea declanșator citește RAFTUL din arborele de catalog (`topic_switched`, doar rădăcini —
subcategoriile precum «Fata»/«Buze» sunt omografe cu cereri de pe alt raft). Kill-switch
`TOPIC_SWITCH_RESET_ENABLED`. Costul acceptat, pin-uit în test: un nume de raft de un cuvânt poate
fi omograf cu un verb („mi se par cam scumpe" → reset nemeritat) — jumătatea ieftină a asimetriei.
(3) **Garda off-category acoperea mulțimea vidă.** `category_dropped` cerea categorie REZOLVATĂ
**și** renunțată în relaxare, dar treptele care renunță sunt gated pe `search_category_hard_enabled`
(`True` implicit) — deci condiția nu putea fi adevărată: `offcategory_suppressed` = 0 declanșări,
vreodată. Acum suprimă și cazul „categoria a fost JUDECATĂ pe un vocabular viu și nu există, iar
setul l-a format un filtru dur (brand/variantă)". Distincția `not_in_vocabulary` vs
`unknown_dimension` e esențială și a fost găsită de suită: cu vocabularul indisponibil (DB jos) tot
ce cere modelul iese UNKNOWN, iar o regulă scrisă pe „zero chei" ar fi transformat o clipeală de DB
în „niciun rezultat" pe fiecare căutare cu brand.
Nimic din aval nu putea prinde nimic din toate astea: produsele și prețurile erau REALE, deci
validatorul (stagiul 8) și `grounding_guard` le-au lăsat să treacă — sunt porți de **ADEVĂR**, nu
de **POTRIVIRE** (`validator_ok: true` pe turul cu fardurile). În plus, eticheta de downgrade
`all-items-dropped-by-membership` se punea și când modelul REFUZASE setul fără să numească niciun
produs (nu se dropase nimic): două defecte diferite sub aceeași etichetă nu se pot număra separat,
deci motivul e acum tri-state (`no-items-selected` e distinct).

**NX-293 — cererea se anula singură: textul clientului era cerut a doua oară, ca TEXT.**
Găsit pe o conversație REALĂ: „ce produse de barbati ai" → „nu am găsit produse în categoria pentru
bărbați", pe un raft cu 3 produse `published`/`in_stock`. Triajul și vocabularul au funcționat
(`category=barbati`, `known`/`exact`, `evidence=3`); căutarea a golit setul cu propriul predicat de
text, fiindcă `_lexical_fetch` leagă cu ȘI potrivirea de text și filtrele dure, iar când cererea
**este** categoria, textul ei e numele raftului — cuvânt care nu apare în numele produselor de pe el
(„Men Shower Gel", „Madeca Homme"). Măsurat: doar filtrul = 3, filtru ȘI text = 0, iar „barbati"
apare în vectorul de căutare al UNUI produs din 2.758. Cauza e structurală, nu a raftului: sistemul
are DOUĂ scări complementare — cea de TEXT (046) relaxează cuvintele cu filtrele fixe, cea de FILTRE
relaxează filtrele cu textul fix — deci **textul era singurul lucru pe care nimic nu-l putea lăsa
deoparte**. Pe rafturile rădăcină ale aceluiași catalog, 4 din 12 tăceau complet când formularea e
numele raftului, iar restul pierdeau majoritatea (Ten 1461→533, Machiaj 681→368). Nimic din aval
n-o putea prinde: validatorul și `grounding_guard` sunt porți de ADEVĂR, nu de POTRIVIRE
(`validator_ok: true` pe turul mut). Fixul e o treaptă TERMINALĂ `filters_only` care nu pune niciun
predicat de text și servește setul filtrelor — sigură prin construcție, fiindcă rezultatul e o
submulțime a ceea ce filtrele dure permit (spre deosebire de `relaxed`/`fuzzy`, care rătăcesc prin
catalog). Poarta e „filtru de SUBIECT", nu „orice filtru": raftul/fațeta/brandul/varianta NUMESC un
set cerut, pe când `price_max`/`in_stock_only` doar îngustează unul — fără filtru de subiect, un text
care nu prinde nimic RĂMÂNE zero. Se oferă doar pe ULTIMA treaptă a scării de filtre
(`allow_filters_only`, opt-in): a renunța la cuvintele clientului e ultima concesie din sistem, iar
oferită pe treapta 0 ar fi preferat „ce am pe raft" în locul unui răspuns care chiar potrivește, doar
fiindcă o fațetă era prea îngustă. Default-ul opt-in protejează doi apelanți care au nevoie de opusul
ei: sonda de variantă (ar eticheta orice drept `missing_variant`) și harnessul de retrieval (ar
măsura alt sistem decât cel comparat). Perechea obligatorie a fixului: `lexical_step` ajunge acum la
MODEL (`_brief`), fiindcă o treaptă degradată arăta identic cu o potrivire exactă și modelul o
prezenta drept „uite ce ai cerut" — `filters_only` sunt produsele corecte ca RAFT și greșite ca
POTRIVIRE. Semnalul de cerere neîmplinită nu se stinge: `unmet_query reason=text_unmatched`
(„am marfa, n-am potrivirea") e distinct de `no_result` („n-am marfa"), citit ca secțiune proprie în
`demand_report`. Kill-switch `SEARCH_FILTERS_ONLY_FALLBACK_ENABLED=false` → tăcerea de dinainte,
byte-identic. Detalii: [`tasks/stage1/NX-293.md`](tasks/stage1/NX-293.md).

**Fix 2026-09-17 — aprinderea creierului unic golise cardurile, iar identitatea lor era `null`.**
Raportat ca „recomandările nu mai arată ca înainte". `finalize.py` (v1) chema `set_rich_reply`;
`brain.py` cheamă `set_reply`, iar `set_rich_reply` avea doi apelanți, amândoi pe calea v1. Deci
`channels/web/render.py` cădea de pe ramura `rich` pe ramura `products`, care nu poartă `reason`,
`rating`, `review_count`, `badge`/`badge_tone`, `list_price`, `currency`, `details` și nici chips.
Nimic din aval n-o putea prinde, din același motiv ca la NX-293: validatorul (stagiul 8) și
`grounding_guard` sunt porți de ADEVĂR, nu de FORMĂ — cardurile erau reale, doar goale. Al doilea
defect, în aceeași ramură: `_card` citește `product_id`, retrievalul scrie `id`, deci cardurile
plecau la widget cu identitate **`null`** — coșul, acțiunile NX-236 și „spune-mi mai multe" n-aveau
pe ce se lega. Starea a scăpat din întâmplare (`_displayed_product_refs` are fallback pe `id`),
sârma nu.
Reparația NU e un al doilea apel de model (ar reintroduce relay-ul interzis de D1): planul are deja
motivul per produs (`recommendations[].reason`, validat ca orice claim prin `to_v1()` și trecut prin
`grounding_guard`), iar hidratarea faptelor rămâne `compose.assemble` — ACELAȘI cod ca pe v1, deci
paritatea e prin construcție, ca la `CurrentLiveRetrievalAdapter` (NX-238). Proza creierului devine
`intro` NEATINSĂ: a trecut deja porți strict mai tari decât `scrub_intro`, care ar arunca ÎNTREG un
răspuns corect ce numește un preț (șase carduri pe ecran și zero text). Chips-urile vin din meniul
ÎNCHIS (NX-295) cu lista goală la intrare, deci sunt chiar frazele oferibile — niciun model nu le
rostește; producătorul e declarat în `CHIP_PRODUCERS`, ca poarta mecanică să-l vadă.
Un defect vecin, găsit de golden pe drum: un tur de ÎNTREBARE („aveți fondul în nuanța ivory?") are
obligația `answer`, nu `recommend`, deci `recommendations` e gol pe bună dreptate — iar cardul
devenea bogat cu secțiunea de motiv GOALĂ, adică mai rău decât unul simplu (arată ca o recomandare
căreia i-a căzut argumentul). Motivul cade acum pe `best_for` din catalog (NX-169), exact
echivalentul pe care harnessul îl acceptă ca motiv; luat ca atare, fără frază în jur (P11).
Auditul de paritate cu `finalize.render` a scos încă TREI goluri, toate de aceeași clasă („brain-ul
nu citea un câmp pe care ToolRun îl avea deja"): (a) **comparația** — `run.compared` e populat de
`compare_products` pe ACELAȘI `ToolRun`, dar nimeni nu-l consuma, deci un „compară-le" care nu era
prins de poarta deterministă dinaintea buclei ieșea ca proză cu carduri, adică RE-recomandare;
ramura e acum prima, ca în v1, cu tabelul DETERMINIST (`build_comparison`) și leadul creierului —
narativul `compose_comparison` NU se cheamă, ar fi un al doilea writer (D1); (b) **CTA-ul de
checkout** — `_attach_checkout_offer` nu se chema deloc, iar validatorul verifică doar că linkurile
SCRISE sunt reale, niciodată că cel CREAT a fost scris: NX-137 reintrodus tăcut; (c) **chips-urile pe
ramura fără carduri** — un no-result rămânea fundătură, unde v1 atașa căi de continuare.
Al patrulea a fost introdus de fix-ul însuși și prins tot de audit: pasând `intro=None` în `j`,
`_order_by_first_mention` nu mai reordona nimic, deci cardurile se aranjau după ranking în timp ce
proza numea produsele în ordinea ei — exact contradicția măsurată pe trafic în 2026-08-26. Textul
intră acum în `j` PENTRU ORDINE, iar valoarea scrubuită se aruncă.
**Rămâne NEACOPERIT, declarat:** `education` (paragraful „cum alegi", pe v1 scris de modelul rich)
n-are corespondent în plan și rămâne gol — a-l genera ar cere exact al doilea writer pe care
arhitectura îl interzice.
Kill-switch `BRAIN_RICH_REPLY_ENABLED=false` → ramura săracă de dinainte; `BRAIN_CHIPS_ENABLED=false`
→ zero chips. Normalizarea `id → product_id` rămâne pe AMBELE ramuri: flagul e pentru FORMA bogată,
nu pentru dreptul de a trimite carduri mute. Cod: [`src/agent/brain_rich.py`](src/agent/brain_rich.py)
+ `brain._set_brain_reply`; probă: `pytest tests/test_brain_rich_reply.py -q`.

**NX-233 pe v1 — transportul asincron se desprinde de vederea v2 (flag OFF).**
Un tur real pe producție ia ~35s. Sincron, browserul ține o conexiune HTTP deschisă atât (lângă
timeoutul oricărui proxy), nu confirmă nimic, iar dacă procesul moare la mijloc turul e PIERDUT
deși serverul chiar muncise. NX-233 rezolvase toate cele trei, dar livrase transportul CUPLAT cu o
vedere nouă (`web-view.v2`, blocuri), deci „vreau accept 202" costa o rescriere de widget — și
măsurat, o regresie: chips-urile de recomandare, CTA-ul de checkout (`offer`) și comparația
(`heading`/`subtitle`/`closing`) n-aveau destinație în proiecția v2, iar randorul de blocuri n-a
primit redesignul din 2026-08-28. Sunt însă DOUĂ axe, iar codul o spunea deja fără să o
folosească: cererea e `web-turn.v2`, statusul `web-turn-status.v2`, vederea are versiunea ei.
Acum vederea e o SETARE (`WEB_TURN_VIEW_CONTRACT`, implicit `web-chat.v1`), iar
`terminal_payload` e singurul loc care o alege — GET, SSE și replay-ul de la accept trec toate pe
acolo, deci trei răspunsuri la același rând nu pot diverge.
[`src/web/turn_view_v1.py`](src/web/turn_view_v1.py) servește EXACT payload-ul persistat de
executor (`render_web`), cu plic de tur și cheile interne filtrate pe **allowlist** (`actions`,
`grounded_v2` sunt dovezi server-side; o blocklist ar scurge cheia adăugată mâine). Invarianta e
mecanică, nu declarată: `tests/test_web_turn_view_v1.py` compară cheie cu cheie conținutul
asincron cu payload-ul sincron — un câmp în plus inventat de vedere e la fel de grav ca unul
pierdut. Cele trei goluri dispar de la sine: pe contractul v1 chips, `offer` și comparația sunt
câmpuri NATIVE, nu au nevoie de un kind de acțiune ca să existe. Golul `variants` (NX-166) e
măsurat ca fiind nul: **0 din 2.758** produse SOLE au ≥2 variante.
Comutarea are UN owner — serverul își anunță capabilitatea la `GET /web/bootstrap`
(`async_turns`: contract + SSE + cadență + copy-ul localizat al fazelor), iar frontendul nu alege
la build: anunț absent ⇒ `/web/chat`, anunț dispărut între bootstrap și accept ⇒ cade pe sincron
fără să piardă mesajul. Deci rollbackul e stingerea unui flag, nu un redeploy de widget.
Efect secundar onest: indicatorul de „thinking" al widgetului își simula etapele cu timere locale
(o afirmație a browserului despre server); acum primește fazele REALE (`accepted` → `working` →
`validating`). Gate-ul E2E NX-247 rămâne PIN-uit pe `web-view.v2` (invarianții lui sunt scriși pe
blocuri): mutarea matricei pe contractul v1 e o decizie proprie, nu efectul colateral al unui
default schimbat. FE: `src/chat/contract/webChatV1.js` (decoder strict pe PLIC, permisiv pe corp —
contractul v1 e aditiv prin proiectare) + `test/chat-async-v1.test.js`.

---

## Arhitectura — pipeline liniar (12 stagii)

Fiecare mesaj inbound parcurge stagiile în ordine fixă.
Un singur obiect `TurnContext` curge prin toate stagiile.
Orice stagiu poate seta `reply` → early exit direct la Sender (stagiul 9).

```
[1] WEBHOOK SVC  (implementat: src/webhook/ — subțire, FĂRĂ DB)
    • ACCEPT: /web/messages + /web/chat + /web/v2/turns (src/web/app.py) — sesiune
      semnată HMAC (token public per tenant + visitor_id), rate limit, cap de body
    • dedupe LAYER 1 (NX-51): Redis SET NX EX pe (channel_account_id, provider_msg_id).
      NB: unique-ul de pe messages include cheia de partiționare (created_at) →
      un retry vine cu alt created_at, ON CONFLICT nu prinde. De aceea
      dedupe-ul e în 2 straturi, NU pe messages.
    • push pe stream-ul Redis unic `inbound` (conversation_id nu e cunoscut
      la accept fără round-trip în DB; ordinea per conversație = în worker)
    • POST /webhook/orders/{business_id} (F2-2) — semnat HMAC, margine subțire, fără DB
    • NX-289: rutele Meta (GET/POST /webhook) au fost ȘTERSE odată cu canalul
    • update conversations.last_inbound_at s-a mutat în worker (processor)

[2] REDIS BACKBONE + WORKER  (implementat: redis_bus.py, worker/consumer.py + processor.py)
    • stream unic `inbound` + consumer group `workers` (XREADGROUP + ACK)
    • worker: resolve (channel_kind, channel_account_id) → business (admin_conn)
      → tenant_conn → dedupe LAYER 2 durabil (inbound_dedupe, claim ÎNAINTE
      de orice scriere — prinde retry scăpat de Redis după restart/FLUSHALL)
      → contact/conversație → last_inbound_at → pipeline
    • TODO: lock per conversație (multi-consumer), debounce adaptiv 2-3s,
      rate limit per user + abuse blocklist (contacts.is_blocked),
      cost guard zilnic per business (contor Redis; sursa de adevăr
      pentru facturare = usage_daily, rollup nocturn), XAUTOCLAIM

[3] GATES (cod pur, fără LLM)
    • bot_active check (conversations.bot_active) → tăcere intenționată dacă false
      (kill-switch per conversație, setat din DB — NU o escaladare din conversație)
    • abuse blocklist (contacts.is_blocked) → tăcere
    • risc detection (pattern-uri) → DOAR event `risk_detected`, zero efect pe tur.
      TRANSFERUL LA OPERATOR A FOST SCOS DIN PRODUS (nu există consolă, nu există om de
      gardă): un client care cere un om, o reclamație sau o amenințare legală primesc
      răspunsul agentului, nu o promisiune neonorată și nu tăcere (P6)
    • media routing: poze → Vision (match catalog). NX-289: registrul de `MediaFetcher`
      e GOL (singurul era al WhatsApp-ului), deci calea degradează fail-soft pe
      `no_downloader`. STT (Whisper) e won't-do (NX-75)
    • language detect → RO / EN (setează ctx.language; TOATE
      lookup-urile în faqs / semantic_cache includ locale)
    • identity resolution: lookup în channel_identities →
      același user pe 2 canale = un singur contact

[4] STRATURI GRATUITE (fără LLM, țintă 40-60% din trafic opresc aici)
    • alias lookup: phrase_norm(text) → match în intent_aliases
      (status='approved', filtrat pe business_id)
    • cache semantic: embedding → cosine search în semantic_cache
      (filtrat pe business_id + locale)
    • clarificare: dacă state are pending_question → formulare din cod/prompt
    • oricare produce reply → early exit la Sender

[5] TRIAJ — ȘTERS (NX-297). Nu mai există niciun model între client și agent.
    • ruta o decide `agent_stage`: fără rută la intrare → `sales`, cu toolsetul REUNIUNE
      (sales + order), fiindcă „unde e comanda mea" nu mai e clasificat de nimeni
    • clasa de tur vine din obligațiile DETERMINISTE ale mesajului (`turn_class_for`)
    • constrângerile (buget/nevoi/brand/raft) se învață din ce a CĂUTAT agentul
      (`observed_constraints`, ON), nu din sloturi re-extrase de un model mic
    • clarificarea e o UNEALTĂ (`clarify_options`, flag), nu o rută
    • poartă mecanică: niciun stagiu din `src/worker/stages/` n-are voie să cheme un
      clasificator de model (test AST în `test_context_orchestration`)

[6] CONTEXT BUILDER (buget impus în cod)
    • istoric: max 8 mesaje (cele mai recente)
    • state: max 8KB (impus în cod + CHECK pe conversations.state din 003)
    • profil client compact din contacts.profile
    • summarizer conversații lungi (> 20 mesaje → conversation_summaries + ultimele 8)
    • prefix static byte-identic → prompt caching OpenAI (75-90% discount)

[7] AGENT (`gpt-5.6-luna`, vezi tabelul de stack)
    • system prompt GENERAT din categories (+ intent_aliases pt rutare), nu hardcodat
    • CE ACCEPTĂ O CERERE ATÂRNĂ DE UN SINGUR BIT: raționează sau nu. Cu raționamentul PORNIT
      (`reasoning_effort` ≠ `none`, SAU parametrul absent pe un model care raționează implicit —
      `gpt-5.6-*` da, `gpt-5.4-*` nu), furnizorul refuză cu 400 ȘI `temperature` ≠ 1, ȘI
      function tools pe `chat.completions`. Deci bucla de vânzare FORȚEAZĂ `reasoning_effort=none`
      (`llm._sampling`), iar `LLM_REASONING_EFFORT_AGENT` e INERT pe drumul cu tool-uri și activ
      pe apelurile de text/schemă. Raționament + tool-uri ar cere `/v1/responses` — schimbare
      mare, se decide pe măsurători (D15), nu ca să scăpăm de un 400. Divergența config↔sârmă se
      numără (`llm_reasoning_disabled_for_tools`). Fără poarta asta, o schimbare de model sau de
      effort omoară TOATĂ calea de vânzare și numai pe ea: 4xx e terminal în `_with_retry`,
      `agent_stage` îl înghite, iar straturile gratuite rămân intacte deasupra — deci sistemul pare
      sănătos. S-a întâmplat pe 2026-08-24 (bbb77b3, ambele schimbări deodată)
    • buying stages framework: browsing → narrowing → comparing → ready_to_buy
    • AGENT decide mutarea de vânzare (NU routerul)
    • NX-312: pe profilul `recommend`, o rundă care a chemat DOAR `search_products` și a adus
      produse încheie bucla (fără runda de proză): compunerea bogată scrie din produse ⇒ 2 apeluri
      de model pe tur, nu 3. Kill-switch `TOOL_LOOP_SKIP_PROSE_ENABLED`
    • MAX 3 RUNDE de model per tur (limită dură: llm.py:364). NU e un plafon de tool calls:
      o rundă poate emite N apeluri și toate se execută. Plafoanele separate pe apeluri/mutații
      există în src/runtime/turn_budget.py (NX-241), dar sunt OFF (turn_budget_enforced=false)
    • tool results: max 6 produse × 8 câmpuri (nu obiecte complete)
    • P0-safety CONTRAINDICAȚII (NX-173, src/safety/) — UN SINGUR punct de decizie:
      `SafetyPolicy.for_turn(ctx).evaluate(products, purpose)` → `Decision` tipizat. Context
      (sarcină/alăptare) detectat DETERMINIST + PERSISTAT în state.safety (istoricul de 8 e
      prea scurt); registru CURAT cu provenance + reviewed_by (db/seed/safety_rules.json),
      validat STRICT și FAIL-CLOSED (poartă de boot; registru stricat + context activ ⇒ nu se
      expune nimic). Chemat de TOATE căile: search/page/details/compare, link+compare intent,
      cross-sell/superlativ/cheaper/rehidratare, enforcement final pe ctx.retrieval, backstop
      în ToolRun. MUTAȚIILE (cart/checkout/back-in-stock) cer `policy.allows()` ÎNAINTE de
      scriere — un filtru de rezultat nu poate anula un rând scris. Cache-ul (stagiul 4) face
      BYPASS pe context de siguranță (citire + scriere): un hit ar sări peste tot gate-ul.
      DRUMUL DIN AFARA PIPELINE-ului are poarta lui (n-are TurnContext → `SafetyPolicy
      .from_state`): PROACTIVUL (back_in_stock/abandoned_cart — un job vechi ar promova
      produsul zile mai târziu; awb_update/follow_up NU se gate-uiesc, sunt tranzacționale).
      NX-289: al doilea drum era caruselul (`worker/callback.py`, ◀/▶ = inbound NON-LLM), șters
      odată cu butoanele inline Telegram — `from_state` are acum UN singur apelant.
      COMPUNERE: codul garantează O SINGURĂ frază localizată (recunoaștere + medic/farmacist),
      în runner, idempotent (src/safety/compose.py + messages.py); modelul scrie doar partea
      comercială. Nicio inferență LLM nu devine contraindicație; zero sfat medical.
      Kill-switch: safety_contraindications_enabled.

[8] VALIDATOR (cod pur)
    • fiecare preț din reply există în ctx.retrieval
    • fiecare produs menționat există în ctx.retrieval
    • linkurile sunt din catalog (products.product_url, nu inventate)
    • P0-safety: niciun claim MEDICAL/terapeutic (tratează afecțiuni / sigur în sarcină /
      fără alergeni / recomandat de medic) — proză: invalid→retry→fallback; bogată: scrub→DROP
      (has_medical_claim, kill-switch safety_medical_guardrail_enabled). Răspundere juridică.
    • invalid → 1 retry cu feedback → formulare fără cifre
    • ZERO prețuri inventate structural

[9] SENDER (singurul punct de ieșire din sistem)
    • răspuns spart în 2 mesaje scurte dacă > 200 caractere
    • scriere tranzacțională în aceeași TX: reply în outbox +
      patch conversations.state (cu state_version) + insert messages
    • dispatcher separat citește outbox → `ChannelSender` (azi: WebSender, publish
      pe Redis Pub/Sub + backlog SSE) → retry cu backoff la fail
    • NX-289: statusurile de livrare (delivered/read/failed) erau raportate de provider
      prin webhook; nu mai există producător, iar `message_status_events` a fost ștearsă
    • POST-TUR async (nu blochează): extractor profil + lead_score update (pe `model_agent`)

PROACTIV (în afara pipeline-ului, scheduler separat — proactive_jobs)
    • AWB la expediere (shipments) · back-in-stock · follow-up coș abandonat
    • verifică opt-in: contacts.consent — SINGURA poartă (NX-289: fereastra 24h și
      template-urile aprobate erau reguli ale platformei Meta, au plecat cu canalul)
```

---

## Canale (multi-channel) — cuplajul stă DOAR la margini

Pipeline-ul (stagiile 3-9) și worker-ul sunt **agnostice de canal**: operează pe
`TurnContext` (contact, conversație, mesaj, reply). Cuplajul de canal trăiește la
exact DOUĂ margini, izolat prin contracte (NX-60):

- **Ingestie** (stagiul 1): fiecare canal are parser-ul + verificarea lui →
  produc un **envelope NEUTRU** pe stream-ul unic `inbound`:
  `channel_kind`, `channel_account_id` (id-ul canalului RECEPTOR — public_token la
  web), `sender_external_id` (id-ul userului — visitor_id), `provider_msg_id`, `body`,
  ... Worker-ul rezolvă tenantul cu
  `resolve_channel(channel_kind, channel_account_id)` și nu mai știe de canal.
- **Trimitere** (stagiul 9): `outbox` e singurul punct de ieșire; un **registru
  `ChannelSender`** mapează `channel_kind → client`. Dispatcher-ul alege clientul
  după `channel_kind` (zero logică de coadă duplicată).

Canale — **NX-179: se lucrează DOAR pe web widget.**
- **WEB WIDGET (`webchat`)** — **singurul canal activ și singurul pe care se lucrează.**
  `POST /web/chat` (sincron, request/response — reply-ul se mapează direct în HTTP, fără
  outbox/dispatcher, prin `render_web`) + `GET /web/stream` (SSE) + `POST /web/messages` +
  `GET /web/bootstrap` (`src/web/app.py`). Widgetul propriu-zis trăiește într-un **repo FE
  separat**; backendul emite DOAR JSON — [`docs/FRONTEND-CONTRACT-IZI.md`](docs/FRONTEND-CONTRACT-IZI.md).
  Fără fereastră impusă de platformă, fără template-uri. Handoff scos din produs. Identitate:
  anonim by default; login passthrough JWT în spatele `WEB_IDENTITY_ENABLED` (NX-128/129/130).
  Audit conversațional pe calea reală: `scripts/sim/web_audit.py`.
  > **Contract v2 (NX-228→NX-234): rutele există, flags OFF.** Contractul de mai sus e
  > **v1 și rămâne activ, neatins, până la cutoverul NX-249**. În paralel: NX-232 = ledgerul
  > durabil `web_turns` (idempotency + replay pe `/web/chat`, flag `WEB_TURN_LEDGER_ENABLED`);
  > NX-233 = calea ASYNC v2 — `POST/GET /web/v2/turns` (+SSE) în `src/web/app.py`, executor cu
  > lease/fencing (`src/web/turn_executor.py`), sweeper de recovery (`src/web/turn_recovery.py`),
  > proiecția v1→`web-view.v2` (`src/web/turn_events.py`) — totul în spatele flag-urilor
  > `WEB_TURN_V2_ENABLED` / `WEB_TURN_EXECUTOR_ENABLED` / `WEB_TURN_RECOVERY_ENABLED` /
  > `WEB_TURN_SSE_ENABLED` (default OFF). NX-234 = **contextul de pagină ID-only**: browserul
  > trimite suprafața + identificatori opaci (`src/web/context.py`), serverul rehidratează canonic
  > și tenant-scoped (`src/catalog/context_resolver.py`, UN query), iar turul primește un
  > `TurnSnapshot` IMUABIL (`src/worker/turn_snapshot.py`) — un câmp comercial în `context` e 422,
  > o variantă de la alt produs invalidează tot contextul, iar `UNKNOWN` nu devine `0`. Ancora
  > „produsul acesta" de pe PDP: `src/agent/reference_resolver.py`. Flags `WEB_CONTEXT_ENABLED` /
  > `WEB_CONTEXT_PROMPT_ENABLED` (default OFF; al doilea îl cere pe primul).
  > NX-236 = **acțiuni opace semnate**: un buton nu mai e o etichetă retrimisă ca mesaj, ci un
  > token SIGILAT (AES-SIV determinist, `src/web/action_crypto.py`) cu registry FINIT + argumente
  > canonice (`src/web/action_models.py`), legat de tenant/sesiune/conversație/turul-sursă și
  > **one-shot** (`src/web/action_service.py`): dovada de emitere se re-derivă din
  > `response_json["actions"]` (scris în tranzacția terminală), iar consumul E chiar rândul de
  > ledger al turului care folosește acțiunea — ZERO migrare, zero registru paralel în Redis.
  > Execuția e `src/agent/action_kernel.py`, stagiu ÎNAINTEA agentului (o acțiune e o decizie, nu
  > o intenție de ghicit). Numele de acțiuni și cele de tool-uri sunt registre DISJUNCTE (verificat
  > la import). Flag `WEB_ACTIONS_ENABLED` (default OFF; cere `WEB_TURN_V2_ENABLED` +
  > `WEB_ACTION_KEYS`); contract + threat model + runbook de rotație:
  > [`docs/WEB-ACTIONS-V2.md`](docs/WEB-ACTIONS-V2.md);
  > probă reproductibilă: `python scripts/action_drive.py`.
  > NX-237 = **coșul canonic al conversației + mutation receipts idempotente**: UN singur
  > `CartService` (`src/commerce/cart_service.py`) pentru AMBELE căi (tool LLM + click de
  > acțiune) — comandă typed cu refs (niciodată preț/nume de la apelant), rehidratare +
  > revalidare (produs/variantă/preț/stoc/safety NX-173) ÎNAINTE de fiecare mutație
  > (`src/commerce/facts_provider.py`, batch anti-N+1, UNKNOWN ≠ 0), receipt idempotent per
  > (tur/acțiune) și `CartSnapshot` versionat cu totaluri display-ready calculate server-side.
  > Retry/response loss nu dublează nimic (replay pe cheie); `expected_version` stale = conflict
  > + snapshot fresh. Tabele: migrarea 041 (`conversation_carts`/`_items`/
  > `commerce_action_receipts`, RLS + FK compus pe tenant). Starea ține DOAR `cart_ref`
  > `{id, version, lines}`; `state.cart` legacy îngheață sub flag (nu se importă cu preț stale).
  > Fără storefront API (decizie explicită): coșul e AL CONVERSAȚIEI, numit onest; portul de
  > adaptor extern (`src/commerce/adapters/base.py`) are contract exact-once (pending →
  > unknown_reconcile → reconcile prin lookup, niciodată retry orb), testat pe fake. Comerțul
  > din acțiuni (`cart_*`, `checkout`) se EXECUTĂ acum prin același serviciu; emiterea
  > CTA-urilor de coș rămâne a NX-240. Flag `CONVERSATION_CART_ENABLED` (default OFF =
  > byte-identic). Matrice de date + politici + runbook:
  > [`docs/CART-DATA-READINESS.md`](docs/CART-DATA-READINESS.md);
  > probă reproductibilă: `python scripts/sim/cart_receipt_recovery.py`.
  > Există și `web-view.v2`
  > ([`src/web/contracts_v2.py`](src/web/contracts_v2.py)), în care backendul livrează un
  > ViewModel **display-ready**: prețul e `"89,00 lei"`, nu `89.0`; reducerea vine calculată;
  > tot copy-ul (chrome, composer, anunțuri a11y) e server-owned. Frontendul devine renderer
  > pasiv. Matricea `field → owner → source of truth → validator → renderer`:
  > [`docs/WEB-WIDGET-BOUNDARY-V2.md`](docs/WEB-WIDGET-BOUNDARY-V2.md); forma pentru FE:
  > [`docs/FRONTEND-CONTRACT-IZI-V2.md`](docs/FRONTEND-CONTRACT-IZI-V2.md). **Nu modifica v1
  > in-place** ca să adaugi ceva în v2 — sunt contracte, randori și validatori separați.
**WhatsApp și Telegram — ȘTERSE (NX-289).** Erau ÎNGHEȚATE din NX-179 (cod păstrat, zero
investiție). Un canal înghețat nu e însă gratis: fiecare stagiu care îl numește e o ramură pe care
nimeni n-o mai execută dar toți o citesc, iar un CHECK care încă acceptă `whatsapp` e o invitație.
Au plecat: `src/channels/telegram/`, `src/meta_client.py`, `src/webhook/meta.py`, rutele
`GET/POST /webhook`, `wa_templates` + poarta de template din proactiv, `message_status_events`,
callback-urile de butoane inline, serviciile de compose, variabilele de mediu. Migrarea de schemă:
[`docs/051_drop_frozen_channels.sql`](docs/051_drop_frozen_channels.sql).

**Ce a RĂMAS, deliberat: seam-ul de canal (NX-60).** Nu e o dependență de Telegram/WhatsApp — e
motivul pentru care pipeline-ul (stagiile 3-9) e agnostic. A-l scoate ar cupla engine-ul la web.
Registrul `ChannelSender` are azi o singură intrare (`webchat`); `channel_kind` rămâne pe envelope,
pe `conversations` și pe `channel_identities`. Un canal nou = o clasă + o înregistrare, nu o
rescriere a pipeline-ului.

---

## TurnContext — contractul central

```python
@dataclass
class TurnContext:
    turn_id: str                        # uuid generat la intrare în pipeline
    business: BusinessConfig            # citit din businesses
    contact: Contact                    # citit din contacts (+ channel_identities)
    message: InboundMessage             # body, content_type, provider_msg_id
    history: list[Message]              # max 8, cel mai recent ultimul
    state: ConversationState            # conversations.state jsonb, max 8KB (v1)
    state_v2: Any                       # NX-235: starea REDUSĂ (nevoi/revocări/referințe);
                                        # owner processor, None cu flagul stins
    state_proposals: list               # NX-235: propuneri typed ale stagiilor (ca `events`)
    language: str                       # 'ro' | 'hu' | 'en' (setat în Gates; DB: locale)
    route: RouteDecision | None         # scris DOAR de stagiul Triaj
    retrieval: RetrievalResult | None   # scris DOAR de stagiul Retrieval
    reply: Reply | None                 # orice stagiu poate seta → early exit
    events: list[Event]                 # acumulat pentru analytics
```

**Regula absolută**: fiecare câmp are exact un stagiu care îl scrie.
Dacă două stagii vor să scrie același câmp, arhitectura e greșită.

---

## Schema DB — o singură schemă `public`, tenant pe `business_id`

**Sursa de adevăr: [`docs/schema_v2_production.sql`](docs/schema_v2_production.sql)**
(829 linii, validată Postgres 16 / Supabase, deja seedată).
**Mapare nume vechi → real + decizii: [`docs/schema_reference.md`](docs/schema_reference.md).**

Convenții generale:
- TOATE tabelele tenant-scoped au `business_id` NOT NULL + index compus.
- Idempotență: unique pe `(business_id, external/provider id)`.
- Hot tables (`messages`, `analytics_events`) sunt **partiționate pe lună**.
- PII (telefon E.164 / id canal) trăiește DOAR în `channel_identities`.

### Tenants și canale
```
businesses        — id, slug, name, vertical, status, default_locale,
                    supported_locales[], timezone, settings jsonb,
                    daily_cost_cap_usd
business_users    — business_id, user_id (auth.users), role  (dashboard)
channels          — id, business_id, kind (doar `webchat` — restrâns de 051),
                    provider_account_id, credentials_ref (secret manager, NU secrete în DB)
```

### Contacts & identitate
```
contacts          — id, business_id, display_name, locale, profile jsonb,
                    lead_score, lifecycle, consent jsonb, is_blocked,
                    erased_at (GDPR: anonimizat, nu șters)
channel_identities— id, business_id, contact_id, channel_kind, external_id,
                    external_id_hash (generated, sha256), UNIQUE(business_id,
                    channel_kind, external_id)
                    • PII-ul de canal stă DOAR aici; identity resolution = lookup aici
```

### Conversații & mesaje (hot path)
```
conversations     — id, business_id, contact_id, channel_id, status,
                    bot_active, last_inbound_at (alimentează sweeper-ele proactive),
                    handoff_until/risk_flags/assigned_user_id = coloane MOARTE (handoff scos;
                    păstrate în schemă, nimeni nu le mai scrie),
                    last_outbound_at, locale, state jsonb (≤8KB), state_version
                    (optimistic lock), risk_flags[], shadow_mode
                    • state = ref-uri (displayed_products: {id,name,price}), NU obiecte
                    • NX-235: `state` are DOUĂ forme. v1 = azi; v2 (`schema_version: 2`) =
                      stare REDUSĂ (needs cu strength/status/source + revocations + references),
                      scrisă doar sub `CONVERSATION_STATE_V2_WRITE_ENABLED`. Se persistă UN
                      singur format; cititorii v1 primesc o proiecție la citire
                      (`ConversationState.from_jsonb`). Migrare LAZY, fără SQL.
                      Contract: docs/CONVERSATION-STATE-V2.md
conversation_summaries — id, business_id, conversation_id, upto_message_at, summary
messages [PARTIȚIONAT] — id, business_id, conversation_id, contact_id,
                    direction(inbound|outbound|internal), author(contact|bot|
                    human_agent|system), provider_msg_id, content_type, body,
                    payload jsonb, media_ref, status, model_route, tokens_in/out,
                    cost_usd, latency_ms
                    • unique(business_id, provider_msg_id, created_at) = doar consistență;
                      dedupe-ul REAL la retry e inbound_dedupe (vezi mai jos, NX-51)
                    • textul e `body`, rolul e `direction`+`author` (NU `role`/`content`)
inbound_dedupe    — business_id + provider_msg_id (PK compus), first_seen
                    • NE-partiționat → ON CONFLICT funcționează; claim în worker
                      înainte de orice scriere; purjă >48h (jobs/cleanup_dedupe)
                    • migrare: docs/004_inbound_dedupe.sql (aplicată live)
outbox            — id, business_id, conversation_id, idempotency_key UNIQUE,
                    kind, payload jsonb, status(pending|dispatching|sent|failed|dead),
                    attempts, next_attempt_at, last_error
                    • Sender scrie aici tranzacțional; dispatcherul trimite
```

### Catalog (read-only pentru bot, scris de sync)
```
products          — id, business_id, brand_id, primary_category_id, external_id,
                    name, slug, ai_summary, price, sale_price, availability,
                    stock_total, rating, status, attributes jsonb, product_url
                    • search hibrid: filtre SQL pe products + ORDER BY embedding <=>
product_embeddings— product_id PK, business_id, model, embedding vector(1536),
                    content_hash  • HNSW cosine; re-embed DOAR la content_hash diferit
product_variants  — id, business_id, product_id, label, sku, price, sale_price, stock
product_review_summaries — product_id PK, business_id, summary, sentiment,
                    top_pros[], top_cons[]  • job offline; citit de get_product_details
brands, categories — tenant-scoped; categories are parent_id + path
reviews, product_images, product_sections, ingredients, product_ingredients,
product_badges, product_category_map — detalii produs
catalog_sync_runs, catalog_quality_alerts — ingestion monitor („alertă, nu publicare")
```

### Knowledge (straturile gratuite 40-60%)
```
faqs              — id, business_id, question, answer, locale, embedding vector(1536)
                    • lookup ÎNTOTDEAUNA: business_id + locale + cosine
intent_aliases    — id, business_id, phrase_norm, target_kind(faq|product|category|
                    route), target_id, status(candidate|approved|rejected)
                    • lookup pe status='approved'; candidates din shadow mode
semantic_cache    — id, business_id, locale, query_norm, embedding vector(1536),
                    answer, hit_count, expires_at
                    • lookup ÎNTOTDEAUNA: business_id + locale + cosine
```

### Comerț & atribuire (bucla de bani)
```
checkout_links    — id, business_id, conversation_id, contact_id, ref_code UNIQUE,
                    cart jsonb, url, clicked_at, converted_order_id, expires_at
                    • checkout_link(ref=...) scrie aici; webhook comenzi face match pe ref_code
orders            — id, business_id, contact_id, external_id, status, total,
                    attributed_checkout_link_id, attribution(none|assisted|direct_bot)
                    • PII: NU are customer_phone — telefonul vine din channel_identities
order_items, shipments (AWB → proactiv)
back_in_stock_subscriptions — UNIQUE(business_id, contact_id, product_id, variant_id)
proactive_jobs    — kind(awb_update|back_in_stock|abandoned_cart|follow_up),
                    scheduled_at, status
                    • `template_id` a fost DROPAT de 051 (referea wa_templates)
appointments      — business_id, contact_id, service_name, starts_at, ends_at,
                    status, external_ref (Google Calendar)
```

### Analytics (append-only — botul are doar INSERT)
```
analytics_events [PARTIȚIONAT] — business_id, conversation_id, event_type,
                    properties jsonb, tokens_in/out, cost_usd, turn_id (NX-122)
                    • model generic: intent_detected/route/tool_call/cache_hit/risk_detected...
                    • turn_id: corelare per-tur (emit() îl injectează; replay traiectorie)
usage_daily       — business_id, day PK, conversations, messages_in/out,
                    templates_sent, tokens, cost_usd, cache_hits, handoffs (=0, handoff scos),
                    orders_attributed, revenue_attributed, intents jsonb
                    • rollup nocturn; dashboard-ul și facturarea citesc DOAR de aici
conversation_evals, golden_tests — LLM-as-judge + gate CI
```

### GDPR & audit
```
gdpr_requests     — id, business_id, contact_id, kind(erase|export|access), status
audit_log         — business_id, actor, action, entity, entity_id, details jsonb
funcția gdpr_erase_contact(contact_id):   (security definer, în schema_v2)
    • contacts: display_name=NULL, profile='{}', rfm=NULL, erased_at=now()
    • channel_identities: DELETE (telefonul dispare)
    • messages: body=NULL, payload='{}', media_ref=NULL (păstrezi structura pt analytics)
    • audit_log: insert
Retenție: partiții vechi messages/analytics_events → drop partition (job pg_cron).
```

---

## Tool-uri agentului (cod determinist, activate per business)

```python
# toate tool-urile au semnătura: async def tool(ctx: TurnContext, **params) -> ToolResult
# MAX 3 RUNDE de model per tur (nu 3 apeluri — vezi stagiul 7 și docs/architecture/04-EVIDENCE.md)

search_products(category, filters, budget_max, concerns, suitable_for, limit=6)
  # filtre SQL dure (categories + attributes) + ranking semantic (product_embeddings) + reranker
  # returnează max 6 produse × 8 câmpuri: id, name, brand, price, product_url, ai_summary, stock, variant

get_product_details(product_id)
  # detalii complete + review summary din product_review_summaries

compare_products(product_ids: list[str])
  # diferențe structurate între 2-3 produse (tabel pros/cons)

check_order(order_number_or_contact)
  # status + tracking din orders + shipments

delivery_eta(product_id, address)
  # ETA din integrarea cu curier/ERP

reorder(contact_id)
  # ultimele comenzi ale contactului → sugestie reorder

cart_add(product_id, variant_id)
checkout_link(cart_items, ref=turn_id)
  # scrie checkout_links (ref_code) → link cu ?ref= pentru atribuire conversie

subscribe_back_in_stock(product_id, variant_id)
  # insert în back_in_stock_subscriptions; proactivul notifică la restock

faq_lookup(query)
  # căutare în faqs (filtrat pe ctx.language → faqs.locale)

book_appointment(service_name, preferred_datetime, contact_info)
  # creare în appointments + Google Calendar sync

```

---

## Roluri DB și securitate

Schema_v2 are **RLS enabled pe toate tabelele** + politici dashboard
(`auth.uid()` → membership în `business_users`). Workerii NU folosesc
`service_role` (ar fi bypass RLS total). Plasa de izolare pentru worker se
adaugă în [`docs/003_bot_runtime_role.sql`](docs/003_bot_runtime_role.sql):

```
bot_runtime  (rolul cu care se conectează workerul aplicației — FĂRĂ bypassrls)
   — SELECT pe catalog (products, variants, embeddings, faqs, ...)
   — INSERT/UPDATE semantic_cache, intent_aliases (candidates)
   — SELECT/INSERT/UPDATE/DELETE pe runtime (contacts, conversations, messages,
     outbox, orders, ...)
   — INSERT analytics_events (append-only); SELECT/INSERT/UPDATE usage_daily (rollup)
   — politici RLS: business_id = current_business_id()  (din SET app.business_id)

service_role — DOAR migrări + joburi admin (bypass RLS). NU pentru worker.
gdpr_svc     — EXECUTE gdpr_erase_contact + export + audit_log (security definer)
```

**Izolarea multi-tenant primară: `WHERE business_id = $1` în cod, FĂRĂ excepție.**
**Defense-in-depth:** workerul se conectează pe tenant path cu rolul de **LOGIN**
`bot_runtime` (NX-50, pool dedicat `bot_pool`); `tenant_conn` setează DOAR
`app.business_id` per checkout — fără `SET ROLE` (care se scurgea sub
multiplexarea poolerului). Politicile RLS pe `bot_runtime` transformă un query
greșit în „zero rezultate", nu „datele altui client". `bot_runtime` NU are
bypassrls. Control plane-ul (`admin_conn`) rulează pe un pool privilegiat separat.
Detalii: `docs/db_connections.md`.

**Excepții documentate — `admin_conn` (control plane), exact DOUĂ:**
(1) lookup-ul `(channel_kind, channel_account_id) → business_id` (db/queries/channels.py) rulează
ÎNAINTE ca tenantul să fie cunoscut — e operația care îl derivă;
(2) NX-249: `release_policies` (db/queries/release.py) — policy-ul de release e un
obiect de MEDIU, nu de tenant, iar rândul poartă allowlistul de tenanți eligibili;
citit de pe o conexiune tenant-scoped, ar scurge cine altcineva e în canary.
Migrarea 044 nici nu dă grant lui `bot_runtime` — nu e convenție, e imposibilitate.
Restul suprafeței rămâne mentenanță non-PII (cleanup inbound_dedupe). Orice
alt query pe admin_conn = bug de izolare.

**Conexiunea aparține OPERAȚIEI, nu turului (NX-231).** Stagiile și tool-urile nu
primesc un `conn` viu, ci PROVIDERUL `deps.db`:

```python
async with deps.db("search_products") as conn:   # checkout SCURT, etichetat
    ...                                          # doar muncă de DB
emb = await deps.llm.embed([q])                  # extern → ZERO conexiune ținută
```

Un tur = `load` (un checkout) → `compute` (fără conexiune) → `commit` (un checkout,
O tranzacție) → `aftercare` (checkout-uri proprii) — contractul din
`src/worker/turn_uow.py`. **Interzis:** orice await extern (LLM/embed/moderation/
media/HTTP de provider/backoff/SSE/așteptare de coadă) în interiorul unui checkout;
query-urile care trebuie atomice se grupează cu `db_tx(deps.db, "op")`, nu ținând
conexiunea între apeluri. Guard mecanic în CI: `python scripts/check_no_raw_conn.py`
(excepțiile cer motiv în `scripts/conn_allowlist.json`). Frâna de concurență nu mai
e poolul, ci `admission` (lease-uri Redis, plafon global + per-tenant, aceeași
poartă pe worker și pe `/web/chat`). Detalii: `docs/db_connections.md`.

---

## Principii — respectă-le în tot codul

1. **Pipeline liniar** — niciun stagiu nu sare înapoi, niciun loop de orchestrare
2. **LLM doar la 1 punct pe drumul sincron** — agentul. NX-297 a șters triajul nano; extracția de profil și rezumatul rulează POST-tur, pe modelul agentului. Tot restul: cod determinist
3. **Un singur proprietar per câmp** — dacă două funcții scriu același câmp din TurnContext, e o greșeală de design
4. **Buget de context impus în cod** — nu în prompturi, nu prin disciplină, în cod (state 8KB tăiat de context builder; CHECK în DB ca plasă)
5. **Un singur punct de ieșire** — Sender → outbox → dispatcher. Orice alt loc care trimite mesaje e o greșeală
6. **Niciodată tăcere** — degradare: agent → retry → fallback determinist → template
7. **business_id pe tot, SERVER-OWNED** — niciun query fără `WHERE business_id = $1`; RLS (`bot_runtime` + `app.business_id`) ca plasă, nu ca mecanism primar. `business_id` se injectează server-side: **niciodată** din output-ul modelului, niciodată parametru de tool controlabil de LLM
8. **State = ref-uri, nu obiecte** — în displayed_products: {product_id, name, price}, NU obiectul complet
9. **Promptul se generează din DB** — system prompt din `categories` (+ `intent_aliases`), nu hardcodat. (Un tabel `taxonomy` bogat se adaugă aditiv DOAR când verticalul cere filtre pe concerns — vezi schema_reference.)
10. **Observabilitate din runner** — stagiile nu știu că sunt măsurate; runner-ul scrie event-ul
11. **Limba e parte din cheie** — orice lookup în faqs / semantic_cache include locale. Un cache hit în limba greșită e un bug, nu un hit. **Pilotul e `ro-RO`, dar nucleul rămâne locale-aware (D3): nu hardcoda română** — limba activă e configurație, nu constantă
12. **PII trăiește într-un loc** — `channel_identities` (telefon E.164 / id canal, + hash). Nicăieri altundeva. Logurile nu conțin telefoane (redaction în logger)
13. **Vocea e cod, nu speranță** — un mesaj nu trebuie să „se vadă că e făcut cu AI". În textul
    către client NU există liniuță de pauză („—", „–" sau „-" între spații) și nici punct și
    virgulă. Cratima din cuvinte („să-ți", „nu-s") rămâne, e ortografie. Regula trăiește în
    [`src/agent/voice.py`](src/agent/voice.py): `VOICE_RULES` intră în TOATE prompturile de
    compunere (bucla de tool-calling, retry, rich, status comandă, MainBrain), iar
    `naturalize()` e plasa DETERMINISTĂ din `TurnContext.set_reply` + scrub-urile din `compose`
    (pură, idempotentă, atinge doar punctuația → nu poate invalida un text tocmai validat).
    Două consecințe practice: (a) prompturile se scriu ÎN vocea pe care o cer, fiindcă un exemplu
    cu liniuță în prompt îl învață pe model exact ce îi interzici (așa a picat prima încercare de
    a impune regula doar prin memorie); (b) nici textele DETERMINISTE ale codului (fallback-uri,
    lead-uri de comparație, copy localizat) n-au voie să folosească semnul interzis.

---

## Structura proiectului

```
nativx-assistant/
├── CLAUDE.md                    ← acest fișier
├── TODO-MANUAL.md               ← taskurile manuale ale lui Adi (conturi/setup extern)
├── docs/
│   ├── schema_v2_production.sql ← SURSA DE ADEVĂR a schemei (Postgres 16, seedată)
│   ├── schema_reference.md      ← mapare nume vechi → real + decizii de design
│   ├── 003_bot_runtime_role.sql ← rol bot_runtime + RLS (app.business_id) + guard 8KB
│   ├── 004_inbound_dedupe.sql   ← NX-51 layer 2 (aplicat live)
│   ├── 0NN_*.sql                ← migrări delta (003→051), aplicate ORDONAT de scripts/migrate.py
│   │                              (030/031 ARSE — vezi antetul lui 034; următorul număr liber: 052)
│   ├── 014_schema_migrations.sql← NX-123: tabel tracking migrări + backfill 003–013 (legacy)
│   ├── PROJECT_STATUS.md        ← starea proiectului (actualizat la fiecare milestone)
│   ├── DB_MIGRATION_NOTES.md    ← note migrare v1 → v2 + runner migrate.py (NX-123)
│   ├── FRONTEND-CONTRACT-IZI.md ← contractul JSON web v1 (carduri+comparison) pt randarea FE (paritate iZi)
│   ├── FRONTEND-CONTRACT-IZI-V2.md ← NX-228: contractul v2 pt FE (inert pana la NX-232/233)
│   ├── STAGE1-WEB-E2E.md        ← NX-247: gate E2E (harness real, matricea R1–R22, runbook, verdict)
│   ├── STAGE1-RELEASE-DECISIONS.md ← NX-249: inventarul care a cerut migrarea 044 + deciziile
│   ├── STAGE1-CANARY-RUNBOOK.md ← NX-249: etape, evidence packet, kill-switch, rollback
│   ├── STAGE1-CUTOVER.md        ← NX-249: închiderea rutei v1 (criteriu structural pt „v1 in-flight")
│   ├── STAGE1-QUALITY-RITUAL.md ← NX-249: zilnic/săptămânal/lunar → regresii, nu tuning online
│   ├── NX-251-CONTEXT-ORCHESTRATION.md ← NX-251: cine AFIRMĂ un fapt + triajul scos de pe sincron
│   ├── NX-241-TURN-DEADLINE.md  ← NX-241: deadline unic, manifest de bugete, SLO + runbook
│   ├── NX-240-GROUNDED-PROJECTOR.md ← NX-240: grounding + projector pur + regulile de adevăr
│   ├── WEB-VIEW-V2-DATA-READINESS.md ← NX-240: matricea de câmpuri + coverage măsurat (300 prod.)
│   ├── WEB-WIDGET-BOUNDARY-V2.md← NX-228: matricea de ownership + regula „frontend pasiv"
│   ├── WEB-CONTEXT-DATA-READINESS.md ← NX-234: field → sursă → SLA → UNKNOWN + coverage măsurat
│   ├── CONVERSATION-STATE-V2.md  ← NX-235: inventar state v1 + contract v2 + rollout/migrare lazy
│   └── *audit*                  ← audit CTO (pdf), plan v2 (xlsx), diagramă v4 (drawio)
├── tasks/                       ← cardurile de task (TXXX.md, NX-XX.md) + backlog compact
├── scripts/                     ← migrate.py (runner ordonat + poartă boot NX-123; one-shot + advisory
│   │                              lock + credential de DDL separat, NX-248); db_check.py, spot_check.py;
│   │                              archive/ = apply_0NN.py istorice (înlocuite de migrate.py)
│   ├── release/                 ← NX-248: build_manifest · preflight · verify_manifest · rollback
│   │                              (dry-run implicit) · smoke_web_v2 · migration_drill · image_contract
│   │                              · evidence · deploy.sh (digest + host key pin-uit)
│   └── dr/restore_verify.py     ← NX-248: verifică un restore IZOLAT (read-only, refuză producția)
├── db/
│   └── seed/                    ← seed.ts + embed.ts (Supabase JS client, tsx)
├── src/
│   ├── config.py                ← settings (Pydantic BaseSettings)
│   ├── models.py                ← TurnContext + toate dataclass-urile
│   ├── redis_bus.py             ← client Redis + dedupe layer 1 + XADD inbound
│   ├── db/
│   │   ├── connection.py        ← pool asyncpg, tenant_conn (RLS) + admin_conn (control plane)
│   │   ├── provider.py          ← NX-231: `deps.db("op")` = checkout SCURT tenant-scoped + db_tx
│   │   ├── op_metrics.py        ← NX-231: db_checkout_ms/db_hold_ms pe operație (+ idle-held)
│   │   └── queries/             ← SQL per domeniu (contacts, conversations, messages,
│   │                              outbox, inbound_dedupe, catalog, channels, businesses)
│   ├── webhook/
│   │   ├── app.py               ← FastAPI: health + redirect + /webhook/orders + /web/* (LIVE)
│   │   ├── signature.py         ← verificare HMAC-SHA256 peste corpul brut (comenzi)
│   │   ├── health.py            ← NX-248: live/startup/ready + vederea de operator
│   │   ├── redirect.py          ← NX-162: /r/{business_id}/{ref_code} → atribuire click
│   │   └── orders.py            ← webhook comenzi → match ref_code → atribuire
│   ├── worker/
│   │   ├── consumer.py          ← consumer group Redis (XREADGROUP + ACK) + entrypoint __main__
│   │   ├── processor.py         ← handle_turn: load → compute (fără conn) → commit → aftercare
│   │   ├── turn_uow.py          ← NX-231: TurnLoadSnapshot (imutabil) + TurnCommit (o tranzacție)
│   │   ├── turn_snapshot.py     ← NX-234: TurnSnapshot IMUABIL (tenant/actor/conv/input/suprafață)
│   │   ├── admission.py         ← frâna de concurență: lease-uri Redis, plafon global + per-tenant
│   │   ├── runner.py            ← pipeline runner (stagii în ordine, early-exit, măsoară)
│   │   ├── dispatcher.py        ← LIVE: outbox → ChannelSender (webchat), retry idempotent
│   │   ├── context.py           ← stagiul 6: istoric conversație bugetat (agent)
│   │   └── stages/             ← agent.py (RAG + validator) ✅; triage.py ȘTERS (NX-297);
│   │                             TODO: gates, free_layers; echo=fallback
│   ├── channels/                ← abstracția de canal (NX-60+); cuplajul de transport
│   │   ├── base.py              ← ChannelSender/MediaFetcher + Capability matrix (NX-115) + registre
│   │   ├── media.py             ← registru de MediaFetcher (GOL din NX-289 — vezi docstring)
│   │   └── web/                 ← sender.py (Pub/Sub + backlog SSE) · render.py (v1)
│   │                              render_v2.py (NX-240: projector PUR, zero I/O, zero ceas)
│   ├── tools/                   ← search_products, get_product_details, ... (vezi mai sus)
│   ├── domain/                  ← NX-114: DomainPack (config per-vertical din DB+seed)
│   │   ├── pack.py + loader.py + normalize.py + defaults/*.json (ecommerce/beauty_salon/...)
│   │   facets.py (NX-186: fațete tipizate) · contracts.py (NX-205: contractul de adevăr —
│   │   Facts/Evidence/Provenance/DerivedSignals + obligatorii per categorie)
│   ├── catalog/                 ← NX-234: regulile canonice de catalog (SQL-ul rămâne în db/queries)
│   │   ├── context_resolver.py  ← rehidratare batch a contextului de pagină + relații + freshness
│   │   └── product_type.py      ← tipul de produs, extras determinist din nume (2 reguli gramaticale)
│   ├── conversation/            ← NX-235: memoria conversației ca STARE REDUSĂ (totul PUR)
│   │   ├── state_v2.py          ← schema `ConversationStateV2` + caps + adapter v1↔v2 + serialize
│   │   ├── needs.py             ← vocabularul de nevoi din DomainPack (P9) + normalizare canonică
│   │   ├── state_reducer.py     ← SINGURUL scriitor de stare: propuneri typed → aplicat/respins
│   │   └── clarification_policy.py ← information gain + anti-buclă (max o întrebare/tur)
│   ├── runtime/                 ← NX-241: contractele de RUNTIME ale turului (timp + buget)
│   │   ├── deadline.py          ← `TurnDeadline`: UN buget monoton, rezervă terminală, cancel
│   │   └── turn_budget.py       ← manifest VERSIONAT pe clase de tur + ledger atomic
│   ├── release/                 ← NX-249: controllerul de release (cine primește v2, cu ce dovezi)
│   │   ├── models.py            ← ReleasePolicy (frozen, amprentat) + Assignment + STAGES 0-7
│   │   ├── assignment.py        ← bucketing HMAC + epoch sticky din ledger + fail-closed
│   │   ├── policy_store.py      ← citire validată + cache bounded + CAS auditat + kill-switch
│   │   ├── gates.py             ← hard stops, timp ȘI eșantion, non-inferioritate Wilson
│   │   └── report.py            ← evidence packet imutabil, agregat, fără identificatori
│   ├── ops/                     ← NX-248: contractele de OPERARE (identitate, health, heartbeat)
│   │   ├── build_info.py        ← ce artefact rulează + interval de schemă tolerat + config revision
│   │   ├── health.py            ← live/startup/ready: sonde mărginite, required vs optional per ROL
│   │   ├── worker_health.py     ← health pt procesele fără HTTP (freshness + PID + boot id)
│   │   └── manifest.py          ← manifestul de deploy: amprentă canonică + fezabilitatea rollbackului
│   ├── observability/           ← NX-246: contractul de telemetrie (nimic din `src/` nu vede OTel)
│   │   ├── turn_latency.py      ← NX-241: spans pe FAZE (vocabular închis) → un event/tur
│   │   ├── contract.py          ← NX-246: vocabularul ÎNCHIS (spans/atribute/metrici/bucket-uri)
│   │   ├── sanitize.py          ← NX-246: ce are voie să iasă (deleagă PII la NX-230)
│   │   ├── tracing.py           ← NX-246: trace derivat din `turn_id` + eșantionare pe coadă
│   │   ├── metrics.py           ← NX-246: registru + gardă de cardinalitate + drop-uri numărate
│   │   ├── export.py            ← NX-246: coadă MĂRGINITĂ, non-blocantă + sink de captură
│   │   ├── hooks.py             ← NX-246: hook-urile NEUTRE chemate din runner/adaptoare
│   │   ├── slo.py               ← NX-246: `slo_policy.v1` — denominatori, verdicte, burn-rate
│   │   └── otel_sink.py         ← NX-246: SINGURUL modul care importă OpenTelemetry (lazy)
│   ├── retrieval/               ← NX-238: portul de retrieval (contract stabil pt NX-239)
│   │   ├── port.py              ← `RetrievalPort` + `RetrievalBundle` (refs + verdicte + evidence)
│   │   ├── current_live.py      ← adapter peste `search_products_tool`: paritate prin construcție
│   │   ├── search_entities.py   ← CANDIDATUL (enforce hard constraints); FĂRĂ `@register`, inert
│   │   └── selector.py          ← poarta de promovare: GO semnat + amprentă + bucket stabil
│   ├── agent/
│   │   ├── brain_rich.py        ← plan → `RichReply` prin `compose.assemble` (paritate de FORMĂ
│   │   │                          cu v1, fără al doilea apel de model) + `card_refs` (id→product_id)
│   │   ├── evidence_bundle.py   ← NX-240: faptele turului (known/unknown/stale + sursă), înghețate
│   │   ├── grounding_guard.py   ← NX-240: poarta de adevăr plan→fapte (respinge vs omite)
│   │   ├── voice.py             ← vocea răspunsului: `VOICE_RULES` (în toate prompturile de
│   │   │                          compunere) + `naturalize` (plasa deterministă, principiul 13)
│   │   ├── prompt_builder.py    ← system prompt generat din categories
│   │   ├── reference_resolver.py← NX-234/235: „acesta"/„prima" → produs; precedență UNICĂ
│   │   │                          (action>named>ordinal>page>selected>single), stale = refuz
│   │   └── tool_definitions.py  ← OpenAI tool schemas
│   ├── proactive/
│   │   ├── scheduler.py         ← proactive_jobs → outbox (motor NX-70; emite DOAR type=text)
│   │   ├── initiators.py        ← PL-1: sweeper-e care CREEAZĂ proactive_jobs (coș abandonat +
│   │   │                          back-in-stock) + seam-uri awb/follow_up; rulate de jobs/scheduler
│   │   ├── builders.py          ← text per kind (`free_text`; NX-289 a scos template_name/variables)
│   │   └── templates.py         ← poarta NX-71: consent check (NX-289: doar atât a rămas)
│   ├── safety/                  ← NX-173 (P0): gate-uri DETERMINISTE, în afara deciziei de model
│   │   └── contraindications.py ← context (sarcină/alăptare) × registru curat → excludere dură
│   ├── gdpr/
│   │   └── erase.py             ← gdpr_erase_contact + export
│   ├── evals/                   ← G8-1: harness golden (regresii de pipeline)
│   │   └── golden.py            ← checker pur (evaluate_reply) + run_case (pipeline real, LLM scriptat) + load_cases
│   └── jobs/
│       ├── cleanup_dedupe.py    ← purjă inbound_dedupe >48h (admin_conn, zilnic)
│       ├── cleanup_web_turns.py ← NX-232: retenție ledger web_turns (admin_conn, bounded)
│       ├── partition_maintenance.py ← NX-218: creează partițiile lunare (analytics_events/messages)
│       │                              luna curentă + următoarea; warning dacă DEFAULT are rânduri
│       ├── lifecycle.py         ← Val3: scrie contacts.lifecycle nocturn (new/engaged/customer/repeat/churn_risk)
│       ├── rollup_usage.py      ← TODO: nocturn: analytics_events → usage_daily
│       ├── rollup_demand.py     ← NX-217: nocturn: faptele de cerere → demand_daily
│       │                          (o trecere/zi, toți tenanții; --from --to pt backfill)
│       ├── embed_products.py    ← TODO: ai_summary → product_embeddings (content_hash)
│       └── cleanup.py           ← TODO: drop partiții vechi, expire semantic_cache
├── tests/
│   ├── golden/                  ← cazuri golden (cases.json) + fixture-uri de conversație
│   ├── test_golden.py           ← G8-1: gate CI (ScriptedLLM + stub-uri DB, zero OpenAI/DB real)
│   ├── e2e/                     ← NX-247: harnessul E2E Stage 1 (TEST-ONLY, nu intră în imagine)
│   │   ├── stage1_app.py        ← app factory peste aplicația REALĂ + gărzi + garda de rețea
│   │   ├── stage1_scenarios.py  ← tenanți sintetici, embedder determinist, model fals, invarianți
│   │   ├── stage1_probes.py     ← probe READ-ONLY tenant-scoped (registru de SQL, verificat mecanic)
│   │   └── test_stage1_*.py     ← self-teste, manifest de contract, matricea R1–R22
│   ├── test_pipeline.py
│   ├── test_tools.py
│   ├── test_validator.py
│   └── test_tenant_isolation.py ← fiecare query refuză date cu alt business_id
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Client activ — proiect NOU din 2026-08-28

**Proiect Supabase: `NativexSales`** (ref `pidqzxymjhzlmoesfsba`, **eu-west-2**, **Postgres 17.6**,
plan free, **Data API STINS** → `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` nu se folosesc; singurele
secrete sunt connection stringurile). Cele **47 de migrări (003→051, cu 030/031 arse)** sunt
înregistrate în `schema_migrations` — ultima aplicată e **051** (NX-289, ștergerea canalelor
înghețate), verificat pe DB pe 2026-09-17 — deci poarta de boot NX-123 nu cere re-rularea lor.
Producția rulează pe `schema: requires 51, tolerates 52` (`/health/startup`).

**business_id**: `99fe1292-f9ed-469e-8183-f994ea5b59c0`
**Slug**: `sole-ro` (name „SOLE") · **Vertical**: `ecommerce` · locale `ro`, `Europe/Bucharest`

**Catalog REAL, importat complet (2026-08-28):** 2.758 produse (toate `active` + `published`),
183.003 recenzii, 27.931 FAQ de produs, 43.761 secțiuni (18.424 `merchant_pdp` + 25.337 `aura`),
21.182 evidence chunks, 2.758 documente de căutare, 15.487 rânduri de imagine (2.758 fișiere în
**Supabase Storage**, bucket public `product-images`, prefix `sole`). Detalii + capcanele de adevăr:
[`docs/DB-V3-SOLE-IMPORT.md`](docs/DB-V3-SOLE-IMPORT.md).

**Canal webchat**: `channel_id=ae254f0f-c60b-4ab4-b281-dd8a8490e82b`,
`public_token=pub_b738dd1aa2ff2e0535b491792cc789d9` (`data-token` în widget). Recreabil idempotent
cu `python scripts/seed_web_channel.py --business sole-ro`.

**Ce e PLIN și ce e GOL, remăsurat 2026-09-17** (cifrele de mai jos au fost verificate pe DB; lista
veche declara zero pe patru dintre ele și era depășită — vezi principiul din
`scripts/mvp_audit.py`: starea reală se citește, nu se ține minte. Remăsurarea din 17 sep a găsit
încă DOUĂ intrări depășite, deși lista purta deja avertismentul ăsta: rezumatele de recenzii și
embeddingurile de FAQ fuseseră produse între timp, iar nota rămăsese pe zero. Un tabel de stare cu
dată în titlu îmbătrânește tăcut — recitește-l, nu-l cita):
`product_embeddings` = **2.758/2.758** (`text-embedding-3-small`, scrise 2026-09-02) ⇒ **RRF-ul ARE
al doilea braț**; `product_derived_signals` = **22.434**; `attributes->'concerns'` = **2.506**, deci
filtrul de `concerns`, fațetele și boost-ul din rerank au pe ce opera; `product_relations` =
**37.082** ⇒ graful NU mai e inert, iar cele 391 de produse epuizate au substitut (NX-195).
**`product_review_summaries` = 2.557** (era 0 până pe 2026-09-16): producătorul determinist NX-279
a fost RULAT (`scripts/derive_review_summaries.py`: teme din `domain_pack.review_themes`, numărate
ca recenzii distincte, zero model; cere migrarea 050, pachetul re-aplicat și `--apply`; vechiul
`summarize_reviews.py` INVENTA rezumatele și rescria `products.rating`, e arhivat cu gardă).
Rândurile sunt SERVITE pe calea live: `db/queries/catalog.py` face `left join
product_review_summaries` și proiectează `top_pros`/`review_summary` atât pe listă, cât și pe
detaliu — deci `top_pros` NU mai iese NULL. Le mai citesc `evidence_bundle` (NX-240) și
`db/queries/carts.py` (NX-237), ambele pe flag stins.
**`faqs.embedding` = 20/20** (era 0): lookup-ul de FAQ la nivel de business are în sfârșit pe ce
opera. Cât DEVIAZĂ efectiv e altă întrebare, măsurată separat — vezi principiul 4 din
`docs/PROJECT_STATUS.md` și nota că stratul „gratuit" servea 1 din 15.
Rămân GOALE, cu consecință: `product_card_blurbs` = 0 (corect: codul refuză să cadă pe numele
produsului); `intent_aliases` (approved) = 0, deci stratul de alias nu deviază NIMIC;
`semantic_cache` = 3 rânduri, adică practic mort; **`product_category_map` = 0** (măsurat
2026-09-16) — deci catalogul e o **PARTIȚIE strictă**: acoperirea pe rafturi e 2.758/2.758, suma
pe rafturi e exact 2.758 (zero suprapuneri), iar apartenența vine EXCLUSIV din
`primary_category_id`. Consecința nu e cosmetică: rafturile de PUBLIC (`Barbati`, `Copii`) taie
transversal peste cele funcționale (`Par`, `Corp`, `Ten`), iar într-o partiție asta e
nereprezentabil — un șampon pentru bărbați e ori pe `Par`, ori pe `Barbati`, și e invizibil pe
celălalt, în ambele direcții. Nu e un import ratat (catalogul are în total 6 produse cu semnal
masculin în nume, 3 deja pe raft): e tabelul destinat apartenenței multiple, nefolosit. Card:
[`tasks/stage1/NX-294.md`](tasks/stage1/NX-294.md).
**`domain_pack` NU mai lipsește** (§13 din doc): 20 de chei canonice de
nevoie derivate din cele 12.665 de fraze reale de căutare din secțiunile `aura`, fiecare
confruntată cu catalogul, plus `skin_type` declarat SEPARAT de `concerns` (`partitioning` vs
`additive`, NX-257) și `routine_time` ca fațetă vie (86,8% acoperire). `query_expansions` rămâne
GOL **pe măsurătoare**: expandările intră în `search_text` și se leagă cu ȘI pe treapta `strict`,
deci pot doar să îngusteze (11/11 interogări înrăutățite, «am cearcane» 18→1, «crema pentru riduri»
20 strict→50 relaxed). Reparația e în scara lexicală, nu în config.

**Căutarea lexicală a fost REPARATĂ pe catalogul real (migrarea 046 + `src/catalog/query_terms.py`).**
Măsurat pe 18 fraze scrise ca de client, **13 întorceau ZERO** — nu rezultate slabe, tăcere. Trei
cauze: (a) `search_tsv` era `name || ai_summary`, iar `ai_summary` e NULL pe toate cele 2.758 de
rânduri, deci vectorul de căutare era literalmente numele — `description` (2.758/2.758) nu era
indexată nicăieri; (b) `websearch_to_tsquery` leagă TOATE cuvintele cu ȘI iar `'simple'` nu elimină
niciun cuvânt gol, deci „sampon pentru par gras" cerea ca produsul să conțină literal și „pentru"
(configurația `'romanian'` NU repară asta: lista ei de cuvinte goale are diacritice, iar noi indexăm
text trecut prin `ro_unaccent`); (c) brațul de typo compara interogarea cu numele ÎNTREG, deci
prindea zero typo-uri reale, dar costa ~220 ms pe FIECARE căutare. Acum: `search_tsv` = nume (A) +
`ai_summary` (B) + descriere (C) cu `setweight`, termenii de conținut se extrag în cod cu listă de
cuvinte goale pe **locale** (P11), iar potrivirea e o SCARĂ (strict ȘI → relaxat SAU → typo cu
`word_similarity`), treptele 2-3 rulând doar pe ratare. Rezultat măsurat pe baza live: **13 zerouri
→ 0**, iar timpul de execuție în Postgres pentru același set de rezultate **219 ms → 26 ms**.
Kill-switch `LEXICAL_QUERY_V2_ENABLED=false` → comportamentul vechi, byte-identic. Un produs servit
de pe o treaptă degradată poartă `lexical_step`, publicat în evenimentul `product_search` —
degradarea e vizibilă, nu tăcută.

**Migrarea 047** repară un defect vecin: `product_variants.stock` era `NOT NULL DEFAULT 0`, deci
UNKNOWN nu era reprezentabil la nivel de variantă (deși e la produs, `stock_total`). Importul a scris
0 pe toate cele 2.755 de variante, iar `facts_provider` tratează un stoc CUNOSCUT 0 drept epuizat —
deci **2.364 din cele 2.367 de produse în stoc se prezentau ca epuizate** coșului (NX-237) și
faptelor turului (NX-240). Codul aștepta deja NULL peste tot; doar schema forța minciuna.
Detalii + planurile de execuție: [`docs/DB-V3-SOLE-IMPORT.md`](docs/DB-V3-SOLE-IMPORT.md) §12.

**Fațeta `product_type` + migrarea 049 — categoria greșită se servea cu încredere.** Măsurat pe
traseul real de retrieval, «protectie solara spf» întorcea pe locul 1 un ser cu retinol, «ser» o
cremă de ochi, «gel de curatare» un gel de DUȘ, iar «rutina ten uscat» un ruj (de două ori). Nu
rezultate slabe: categoria greșită, cu produse și prețuri REALE — deci validatorul (stagiul 8) și
`grounding_guard` (NX-240) o lasă să treacă, fiind porți de ADEVĂR, nu de potrivire. Două cauze.
(1) **Numele nu e nume**, e nume + descriere: 191 de caractere în medie, `care contribuie` în
2.287/2.758, coadă >40 car. după ` - ` în 2.647 — deci premisa migrării 046 („A = identitatea
produsului") era falsă pe primul catalog real, iar fraza de marketing stătea în greutatea maximă.
Migrarea **049** pune în `A` doar capul numelui (40 car.) plus tipul canonic, și mută numele întreg
în `C` lângă descriere: recall identic, se schimbă ORDINEA. Tipul în `A` nu e bonus, e condiția —
capul numelui e în engleză („…Moisture Barrier Cream"), clientul scrie „crema", iar cuvântul există
DOAR în coadă. (2) **Nu exista fațetă de TIP.** Categoria e prea grosieră (`ten-ingrijirea-tenului`
= 933 de produse, cu creme și seruri la un loc). Tipul era însă deja în date, îngropat:
[`src/catalog/product_type.py`](src/catalog/product_type.py) îl extrage DETERMINIST cu două reguli
GRAMATICALE (nu de cosmetice, deci țin pe orice vertical) — un calificativ prepozițional schimbă
clasa („balsam **de buze**" ≠ „balsam **de par**"), unul alipit nu („fond de ten **cushion**"); iar
un obiect nu coordonează („hidratare si luminozitate" e beneficiu, nu produs). Rezultat: **54 de
chei canonice pe 2.088 de produse (75,7%)**, scrise cu `scripts/derive_product_type.py --apply`,
declarate `partitioning` + `provenance: structural` + **`enforce_ready: false`** (acoperirea dă
dreptul de a FILTRA, nu pe cel de a exclude candidați deja găsiți — ăla cere audit de precizie,
NX-268/271). Cele trei reguli încercate ȘI picate pe date sunt scrise în modul, ca să nu se
reintroducă. Măsurat: cu tipul cerut explicit, 6/6 seruri și 6/6 creme.

**Sonda DB → agent (2026-09-08) — ce vede modelul și ce plătește DB-ul, măsurat pe tool-urile
reale.** `python scripts/db_query_probe.py` rulează tool-urile de catalog pe tenant, cu fiecare
statement înregistrat și re-rulat cu `EXPLAIN ANALYZE` pe `bot_runtime` (planul de sub RLS, nu al
superuserului); raportul brut, cu fiecare `llm_view`, e în `reports/db-query-probe-<biz>.md`.
Constatări + ce s-a reparat: [`docs/DB-QUERY-PROBE-2026-09-08.md`](docs/DB-QUERY-PROBE-2026-09-08.md).
**Embeddings: NU se folosesc (decizie de produs).** `SEARCH_SEMANTIC_ENABLED=false` (default)
închide brațul vector și checkout-ul `has_embeddings` de pe fiecare căutare, iar jobul de embed
nu mai pornește fără el; rândurile din `product_embeddings` rămân (reversibil pe măsurătoare). Nu
era doar cost: cu embeddings prezente, sub sort explicit (preț/rating) brațul vector scana TOT
catalogul și sorta global, deci «protectie solara spf, cel mai ieftin» aducea benzi pentru nas la
3 lei și măști epuizate. Reparate pe drumul lexical: `get_substitutes` hidrata toate produsele în
stoc înainte de join-ul cu relațiile (8,5 s rece → 0,3 s; id-urile întâi, hidratarea după);
numărătoarea de categorii servabile (`list_category_names`, FIECARE tur cu agent) era un subquery
corelat per categorie (427 ms → 35 ms, o trecere: `servable_subtree_counts_sql`); vederea de
detaliu randa UNA din cele 17 secțiuni ale fișei SOLE — lista de tipuri e acum în pachet
(`detail_sections`, ecommerce.json: summary/fit/anti_fit/…), cu tăiere la graniță de propoziție;
fațetele din brief vin din `comparison_facets` în ordinea pachetului (nu un set fix care lăsa afară
`skin_type`/`spf`); pe axele de comparație produsul e numit scurt (`display_name`); badge-urile
prezente pe > 90% din catalog (`CPNP`, „Cadou") nu ajung la model (`noise_badges`, în vocabular);
recenziile se aleg pe lungime, nu pe rating (173.657 din 183.003 au 5★); relaxarea lexicală cere
PERECHI de termeni de la 3 în sus (SAU pe singulari rămâne treaptă separată), iar epuizatul
coboară 5 ranguri în fuziune fără să iasă din pool. `attributes.key_ingredients` e acum scris pe
2.577/2.758 (93,4%) din secțiunea `key_ingredients` cu `scripts/derive_key_ingredients.py`
(parser pur în `src/catalog/key_ingredients.py`), deci filtrul de ingredient are în sfârșit pe ce
opera; potrivirea rămâne EXACTĂ pe valoare («niacinamida» da, «centella» ≠ «centella asiatica»).
Rămân în date, nu în cod: 4 perechi KUNDAL cu nume și preț identice și 216 capete-de-nume repetate
(familii de nuanțe/gramaje) pe care modelul le primește ca produse distincte.

**Două defecte vecine, găsite pe drum.** (a) `compliance` = `["CPNP"]` pe 2.711/2.758 trecea ambele
teste din `_keep_dimension` (valorile se repetă, sunt scurte), deși are **o singură valoare** — iar
o dimensiune constantă nu discriminează nimic, dar `resolve_any` o încearcă și îi CONSUMĂ termenii.
Testul e acum pe informație: sub 2 valori, sau o valoare peste 98% din cheie, dimensiunea nu e
vocabular. (b) `TypedFacet.aliases` erau INERTE: `load_vocabulary` descoperă dimensiunile din
cheile reale ale lui `attributes` și nu citește pachetul, iar singurul overlay pasat în
`_resolve_search_terms` era `concern_map`. Deci `routine_time` își declara cele 9 aliasuri
(„seara" → `pm`) și niciunul nu era consultat: „seara" se rezolva pe `compliance` cu verdict
UNKNOWN, iar filtrul nu rula, deși atributul e populat pe 2.758/2.758. Aliasurile fațetei se aplică
acum DOAR fațetei lor — nu în `concern_map`, care e overlay-ul de NEVOI și are un invariant testat
(fiecare valoare trebuie purtată de `skin_type` sau `concerns`); „seara" nu e o nevoie.

> **Indexurile GIN sunt INERTE pe conexiunea de runtime, și nu e un index lipsă.** Cu RLS activ,
> predicatele non-leakproof (`@@`, `%`, `<%`) nu pot fi evaluate înaintea predicatelor de securitate,
> deci nu pot deveni condiții de index: `bot_runtime` face `Seq Scan` unde `postgres` face
> `Bitmap Index Scan`. La 2.758 de produse e suportabil; crește liniar cu catalogul. Vezi §12.2.

**FAQ, cu nuanța care contează:** `product_faqs` = **27.931**, `locale='ro'`, pe 2.750/2.758 de
produse, și SUNT servite (6 per produs, la DETALIU, [`catalog.py`](src/db/queries/catalog.py) —
nu intră în căutare, vezi 032). `faqs` (nivel business) = **20**, luate de pe paginile REALE
sole.ro, cu `source_url` per intrare în [`db/seed/faqs_sole_ro.json`](db/seed/faqs_sole_ro.json),
dar toate cu `embedding` NULL ⇒ lookup-ul (`embedding is not null`) încă nu le servește.
**Nu copia setul demo peste un client real:** cifrele lui sunt inventate pentru un magazin
fictiv și diferă de SOLE aproape peste tot (200 vs 199/149 lei prag, 14 vs 30 zile retur), iar
răspunsul demo REFUZĂ returul de cosmetice deschise pe care SOLE îl acceptă.
Restul găurilor de conținut ale sursei (stoc cantitativ, istoricul prețului) sunt în §8 din doc.

<details>
<summary>Clientul demo VECHI (proiect `xfczucwqntefethxxien`, eu-west-1) — păstrat pentru context</summary>

**business_id**: `6098812a-50fc-44bd-a1ba-bc77e6399158`
**Slug**: `nativex-demo` (name „Sole Demo")
**Vertical**: `beauty`
**Date reale în Supabase** (re-verificat 2026-07-17, NX-177): **654 produse în total, din care doar
150 `status='active'`** — restul de 504 sunt seed-ul vechi templatat, ARHIVAT. Catalogul SERVIT =
cele 150 hand-curate v3 (NX-168e). Nu asertați numere fixe în teste: catalogul crește (testele
cuplate la „500" au picat la 654 și au fost raportate ca regresie — vezi tasks/NX-177.md).
- variante: 46/150 active au variante → prețul afișat = min-variantă DOAR pentru ele, altfel
  `products.price` (contract condiționat);
- ⚠️ **78/150 active (52%) au diacritice în nume**, `unaccent` NU e instalat, iar FTS rulează pe
  config `english` → căutarea lexicală e diacritic-SENSITIVE („sampon" → 0 rezultate, „șampon" →
  5). Impact real pe RO. Vezi **tasks/NX-178.md**.
- `faqs` = 32 (RO seedate); ⚠️ 2 duplicate + typo — vezi tasks/NX-175.md.
Datele de simulare (`sim:*`, din `scripts/sim/server.py`) se curăță cu
`scripts/sim/cleanup.py` (dry-run default, `--apply` ca să șteargă).
**Canale** (re-verificat pe DB live 2026-07-17 — NX-179): **webchat = ACTIV** (64 conversații,
ultimul mesaj 2026-07-14). Proiectul avea atunci și 17 conversații Telegram de test (ultima
2026-06-18) și zero WhatsApp; toate trăiau pe acest proiect abandonat. NX-289 a șters canalele —
pe baza curentă (`NativexSales`) nu există niciun rând care să le numească. Testele integration
își creează channel throwaway (tranzacție rollback-uită).

</details>

Folosește `business_id`-ul lui `sole-ro` pentru toate testele locale. `.env` arată spre proiectul
nou din 2026-08-28; configul vechi e păstrat în `.env.bak.old-project` (gitignored).

---

## Ce NU facem

- NU n8n pentru miezul sistemului (ok pentru cron-uri și alerte periferice)
- NU LLM pentru filtrare sau routing determinist
- NU obiecte de produs complete în state — doar ID-uri + snapshot mic
- NU categorii/aliase hardcodate în prompturi — vin din `categories` / `intent_aliases`
- NU recovery agent pentru cazuri ambigue — CLARIFY ieftin
- NU scriere în catalog din worker (excepție: `semantic_cache` și `intent_aliases` candidates)
- NU tăcere la erori — întotdeauna ceva iese spre client
- **NU transfer la operator uman** — nu există consolă, inbox sau om de gardă, deci botul nu
  promite niciodată „te conectez cu un coleg". Un client care cere explicit un om, o reclamație
  sau o amenințare legală primesc răspunsul agentului: cel mai bun răspuns pe care îl avem bate
  o promisiune pe care nimeni n-o onorează, iar tăcerea ar încălca P6. Riscul se DETECTEAZĂ
  (event `risk_detected`, ca să știm cât de des se cere), dar nu schimbă turul. Singurul
  kill-switch rămas e `conversations.bot_active`, setat din DB — nu declanșat de conversație
- NU trimitere directă la un canal din stagii — totul prin `outbox` + dispatcher (ChannelSender)
- NU cod specific de canal în pipeline/worker — doar la margini (parser ingestie + ChannelSender)
- NU mesaje proactive fără consent (`contacts.consent`) — singura poartă rămasă (NX-289)
- NU telefoane/PII în loguri sau în analytics — doar în `channel_identities`
- NU `service_role` în worker — workerul folosește `bot_runtime` (RLS activ)
```
