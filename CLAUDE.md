# Nativx Assistant — context complet pentru Claude Code

## Ce e acest proiect
Platformă multi-tenant de AI Sales Assistant pentru ecommerce.
**Canalul de lucru ACUM: WEB WIDGET, exclusiv (NX-179).** WhatsApp/Telegram sunt ÎNGHEȚATE — cod
păstrat, zero investiție, nimic nu rulează pe ele. Orice task nou se măsoară pe web sau nu se face.
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
| LLM sales | OpenAI **`gpt-5.6-luna`** (`MODEL_AGENT`; era `gpt-5.4-mini` până pe 2026-08-24). Escaladarea `MODEL_AGENT_COMPLEX` e GOALĂ implicit |
| LLM triaj + simple | OpenAI GPT-5.4-nano |
| Embeddings | text-embedding-3-small (pgvector în Supabase) |
| **Web widget** | **SINGURUL canal de lucru (NX-179)** — `/web/chat` sincron + `/web/stream` SSE; widgetul e în repo FE separat (`docs/FRONTEND-CONTRACT-IZI.md`) |
| WhatsApp | Meta Cloud API direct (NU Twilio) — cod LIVE, dar **niciodată conectat** (0 conversații reale; lipsește phone_number_id, T013). **ÎNGHEȚAT** |
| Telegram | Bot API (long polling) — a fost canal de TEST. **ÎNGHEȚAT** (ultimul mesaj real: 2026-06-18). Poller OFF by default: `docker compose --profile telegram up` ca să-l repornești |
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
pentru o acțiune, deci `text` ar fi o minciună în ledger, iar `interactive` e termen Meta pentru
mesaje de PROVIDER — l-am împrumuta pentru un concept web-only și analytics-ul n-ar mai putea
distinge „a scris" de „a apăsat"). (2) `load_execution_refs` citea `payload` din Record, dar
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

---

## Arhitectura — pipeline liniar (12 stagii)

Fiecare mesaj inbound parcurge stagiile în ordine fixă.
Un singur obiect `TurnContext` curge prin toate stagiile.
Orice stagiu poate seta `reply` → early exit direct la Sender (stagiul 9).

```
[1] WEBHOOK SVC  (implementat: src/webhook/ — subțire, FĂRĂ DB)
    • validare semnătură Meta X-Hub-Signature-256 peste corpul BRUT (signature.py)
    • dedupe LAYER 1 (NX-51): Redis SET NX EX pe (phone_number_id, wamid).
      NB: unique-ul de pe messages include cheia de partiționare (created_at) →
      retry-ul Meta vine cu alt created_at, ON CONFLICT nu prinde. De aceea
      dedupe-ul e în 2 straturi, NU pe messages.
    • push pe stream-ul Redis unic `inbound` (conversation_id nu e cunoscut
      la webhook fără round-trip în DB; ordinea per conversație = în worker)
    • ACK 200 în < 50ms (Meta face retry agresiv la timeout)
    • update conversations.last_inbound_at s-a mutat în worker (processor)

[2] REDIS BACKBONE + WORKER  (implementat: redis_bus.py, worker/consumer.py + processor.py)
    • stream unic `inbound` + consumer group `workers` (XREADGROUP + ACK)
    • worker: resolve phone_number_id → business (admin_conn, control plane)
      → tenant_conn → dedupe LAYER 2 durabil (inbound_dedupe, claim ÎNAINTE
      de orice scriere — prinde retry scăpat de Redis după restart/FLUSHALL)
      → contact/conversație → last_inbound_at → pipeline
    • TODO: lock per conversație (multi-consumer), debounce adaptiv 2-3s,
      rate limit per user + abuse blocklist (contacts.is_blocked),
      cost guard zilnic per business (contor Redis; sursa de adevăr
      pentru facturare = usage_daily, rollup nocturn), XAUTOCLAIM

[3] GATES (cod pur, fără LLM)
    • bot_active check (conversations.bot_active) → early exit cu handoff dacă false
    • handoff_until check → dacă în viitor, tăcere (om preia)
    • risc detection (pattern-uri) → request_human dacă necesar — DOAR pe canale cu
      handoff activ (config.handoff_enabled_channels). Web exclus by default: fără
      operator → nu escaladăm/nu tăcem, mesajul curge normal (botul asistă singur)
    • media routing: vocale → STT (Whisper), poze → Vision (match catalog)
    • language detect → RO / HU / EN (setează ctx.language; TOATE
      lookup-urile în faqs / semantic_cache / wa_templates includ locale)
    • identity resolution: lookup în channel_identities →
      același user pe 2 canale = un singur contact

[4] STRATURI GRATUITE (fără LLM, țintă 40-60% din trafic opresc aici)
    • alias lookup: phrase_norm(text) → match în intent_aliases
      (status='approved', filtrat pe business_id)
    • cache semantic: embedding → cosine search în semantic_cache
      (filtrat pe business_id + locale)
    • clarificare: dacă state are pending_question → formulare din cod/prompt
    • oricare produce reply → early exit la Sender

[5] TRIAJ (GPT-5.4-nano, ~300 tokens input)
    • clasificare: simple | sales | order | handoff | clarify
    • output JSON validat cu Pydantic: {route, category_key, filters, missing_field}
    • category_key validat contra categories (dacă inventează → CLARIFY)
    • «simple»: nano compune și răspunsul → early exit la Sender
    • incertitudinea = CLARIFY, NU recovery agent

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
      `agent_stage` îl înghite, iar triajul (nano) rămâne intact deasupra — deci sistemul pare
      sănătos. S-a întâmplat pe 2026-08-24 (bbb77b3, ambele schimbări deodată)
    • buying stages framework: browsing → narrowing → comparing → ready_to_buy
    • AGENT decide mutarea de vânzare (NU routerul)
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
      DRUMURILE DIN AFARA PIPELINE-ului au poarta lor (n-au TurnContext → `SafetyPolicy
      .from_state`): caruselul (worker/callback.py, ◀/▶ e inbound NON-LLM) și PROACTIVUL
      (back_in_stock/abandoned_cart — un job vechi ar promova produsul zile mai târziu;
      awb_update/follow_up NU se gate-uiesc, sunt tranzacționale).
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
    • typing indicator trimis instant la primire (Meta API)
    • răspuns spart în 2 mesaje scurte dacă > 200 caractere
    • scriere tranzacțională în aceeași TX: reply în outbox +
      patch conversations.state (cu state_version) + insert messages
    • dispatcher separat citește outbox → trimite la Meta →
      salvează provider_msg_id pe messages → retry cu backoff la fail
    • statusurile delivered/read/failed (webhook status) intră în
      message_status_events → update messages.status pe provider_msg_id
    • POST-TUR async (nu blochează): extractor profil nano + lead_score update

PROACTIV (în afara pipeline-ului, scheduler separat — proactive_jobs)
    • AWB la expediere (shipments) · back-in-stock · follow-up coș abandonat
    • verifică opt-in: contacts.consent
    • verifică 24h window: in_24h_window(conversation) →
      mesaj normal; altfel → DOAR template cu status='approved'
      din wa_templates
```

---

## Canale (multi-channel) — cuplajul stă DOAR la margini

Pipeline-ul (stagiile 3-9) și worker-ul sunt **agnostice de canal**: operează pe
`TurnContext` (contact, conversație, mesaj, reply). Cuplajul de canal trăiește la
exact DOUĂ margini, izolat prin contracte (NX-60):

- **Ingestie** (stagiul 1): fiecare canal are parser-ul + verificarea lui →
  produc un **envelope NEUTRU** pe stream-ul unic `inbound`:
  `channel_kind`, `channel_account_id` (id-ul canalului RECEPTOR — phone_number_id
  la WhatsApp, bot id la Telegram), `sender_external_id` (id-ul userului — wa_id /
  chat.id), `provider_msg_id`, `body`, ... Worker-ul rezolvă tenantul cu
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
  Fără fereastră 24h, fără template-uri. Handoff dezactivat by default (fără operator). Identitate:
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
  > Execuția e `src/agent/action_kernel.py`, stagiu ÎNAINTEA triajului (o acțiune e o decizie, nu
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
- **WhatsApp** — Meta Cloud API, webhook semnat. Codul e LIVE și testat, dar canalul **n-a fost
  niciodată conectat** (0 conversații reale; lipsește `phone_number_id` — T013). **ÎNGHEȚAT.**
  Fereastră 24h + template-uri (proactiv) — relevant doar când se reia.
- **Telegram** — Bot API prin long polling. A fost canal de TEST pe VPS fără HTTPS.
  **ÎNGHEȚAT** (17 conversații, ultimul mesaj 2026-06-18). Poller OFF by default în ambele
  compose-uri (`profiles: ["telegram"]`) → `docker compose --profile telegram up` ca să-l repornești.

**De ce rămâne codul de canal:** abstracția (NX-60) NU e o dependență de Telegram/WhatsApp — e
motivul pentru care pipeline-ul (stagiile 3-9) e agnostic. A o scoate ar cupla engine-ul la web și
ar arunca seam-ul care face WhatsApp posibil pentru clienții români (modelul de business). Îngheț ≠
ștergere: nu se investește, nu rulează, dar nici nu blochează.

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
channels          — id, business_id, kind(whatsapp|telegram|...),
                    provider_account_id, credentials_ref (secret manager, NU secrete în DB)
wa_templates      — id, business_id, channel_id, name, language, category,
                    version, body, variables jsonb, status(draft|submitted|
                    approved|rejected|paused|deprecated), provider_template_id
                    • proactivul în afara ferestrei 24h folosește DOAR status='approved'
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
                    bot_active, handoff_until, last_inbound_at (24h window),
                    last_outbound_at, locale, state jsonb (≤8KB), state_version
                    (optimistic lock), risk_flags[], shadow_mode
                    • in_24h_window(conv) = funcție SQL (derivat, nu flag stocat)
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
message_status_events — provider_msg_id, status, occurred_at  (delivered/read/failed)
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
                    scheduled_at, status, template_id
appointments      — business_id, contact_id, service_name, starts_at, ends_at,
                    status, external_ref (Google Calendar)
```

### Analytics (append-only — botul are doar INSERT)
```
analytics_events [PARTIȚIONAT] — business_id, conversation_id, event_type,
                    properties jsonb, tokens_in/out, cost_usd, turn_id (NX-122)
                    • model generic: intent_detected/route/tool_call/cache_hit/handoff...
                    • turn_id: corelare per-tur (emit() îl injectează; replay traiectorie)
usage_daily       — business_id, day PK, conversations, messages_in/out,
                    templates_sent, tokens, cost_usd, cache_hits, handoffs,
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

request_human(reason)
  # setează conversations.handoff_until, notifică operatorul
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
(1) lookup-ul `phone_number_id → business_id` (db/queries/channels.py) rulează
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
2. **LLM doar la 2 puncte** — triaj (nano) și agent (mini). Tot restul: cod determinist
3. **Un singur proprietar per câmp** — dacă două funcții scriu același câmp din TurnContext, e o greșeală de design
4. **Buget de context impus în cod** — nu în prompturi, nu prin disciplină, în cod (state 8KB tăiat de context builder; CHECK în DB ca plasă)
5. **Un singur punct de ieșire** — Sender → outbox → dispatcher. Orice alt loc care trimite mesaje e o greșeală
6. **Niciodată tăcere** — degradare: mini → retry → nano → template → om notificat
7. **business_id pe tot, SERVER-OWNED** — niciun query fără `WHERE business_id = $1`; RLS (`bot_runtime` + `app.business_id`) ca plasă, nu ca mecanism primar. `business_id` se injectează server-side: **niciodată** din output-ul modelului, niciodată parametru de tool controlabil de LLM
8. **State = ref-uri, nu obiecte** — în displayed_products: {product_id, name, price}, NU obiectul complet
9. **Promptul se generează din DB** — system prompt din `categories` (+ `intent_aliases`), nu hardcodat. (Un tabel `taxonomy` bogat se adaugă aditiv DOAR când verticalul cere filtre pe concerns — vezi schema_reference.)
10. **Observabilitate din runner** — stagiile nu știu că sunt măsurate; runner-ul scrie event-ul
11. **Limba e parte din cheie** — orice lookup în faqs / semantic_cache / wa_templates include locale. Un cache hit în limba greșită e un bug, nu un hit. **Pilotul e `ro-RO`, dar nucleul rămâne locale-aware (D3): nu hardcoda română** — limba activă e configurație, nu constantă
12. **PII trăiește într-un loc** — `channel_identities` (telefon E.164 / id canal, + hash). Nicăieri altundeva. Logurile nu conțin telefoane (redaction în logger)
13. **Vocea e cod, nu speranță** — un mesaj nu trebuie să „se vadă că e făcut cu AI". În textul
    către client NU există liniuță de pauză („—", „–" sau „-" între spații) și nici punct și
    virgulă. Cratima din cuvinte („să-ți", „nu-s") rămâne, e ortografie. Regula trăiește în
    [`src/agent/voice.py`](src/agent/voice.py): `VOICE_RULES` intră în TOATE prompturile de
    compunere (bucla de tool-calling, retry, rich, status comandă, triaj, MainBrain), iar
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
│   ├── 0NN_*.sql                ← migrări delta (003→044), aplicate ORDONAT de scripts/migrate.py
│   │                              (030/031 ARSE — vezi antetul lui 034; următorul număr liber: 045)
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
│   │   ├── app.py               ← FastAPI: GET verify + POST inbound (ambele LIVE)
│   │   ├── signature.py         ← verificare X-Hub-Signature-256 (corp brut)
│   │   ├── meta.py              ← parser payload Meta → InboundEvent
│   │   ├── status.py            ← LIVE: delivered/read/failed → messages.status (#26)
│   │   └── orders.py            ← TODO: webhook comenzi → match ref_code → atribuire
│   ├── worker/
│   │   ├── consumer.py          ← consumer group Redis (XREADGROUP + ACK) + entrypoint __main__
│   │   ├── processor.py         ← handle_turn: load → compute (fără conn) → commit → aftercare
│   │   ├── turn_uow.py          ← NX-231: TurnLoadSnapshot (imutabil) + TurnCommit (o tranzacție)
│   │   ├── turn_snapshot.py     ← NX-234: TurnSnapshot IMUABIL (tenant/actor/conv/input/suprafață)
│   │   ├── admission.py         ← frâna de concurență: lease-uri Redis, plafon global + per-tenant
│   │   ├── runner.py            ← pipeline runner (stagii în ordine, early-exit, măsoară)
│   │   ├── dispatcher.py        ← LIVE: outbox → ChannelSender (Meta/Telegram), retry idempotent
│   │   ├── context.py           ← stagiul 6: istoric conversație bugetat (triaj+agent)
│   │   └── stages/             ← triage.py (nano) ✅ + agent.py (mini, RAG+validator) ✅;
│   │                             TODO: gates, free_layers; echo=fallback
│   ├── channels/                ← abstracția de canal (NX-60+); cuplajul de transport
│   │   └── web/render_v2.py     ← NX-240: projectorul PUR `web-view.v2` (zero I/O, zero ceas)
│   │   ├── base.py              ← ChannelSender Protocol + Capability matrix (NX-115) + registry
│   │   └── telegram/            ← client.py (Bot API) + poller.py (long polling, TEST)
│   ├── meta_client.py           ← MetaClient (WhatsApp Cloud API send); implementează ChannelSender
│   ├── tools/                   ← search_products, get_product_details, ... (vezi mai sus)
│   ├── domain/                  ← NX-114: DomainPack (config per-vertical din DB+seed)
│   │   ├── pack.py + loader.py + normalize.py + defaults/*.json (ecommerce/beauty_salon/...)
│   │   facets.py (NX-186: fațete tipizate) · contracts.py (NX-205: contractul de adevăr —
│   │   Facts/Evidence/Provenance/DerivedSignals + obligatorii per categorie)
│   ├── catalog/                 ← NX-234: regulile canonice de catalog (SQL-ul rămâne în db/queries)
│   │   └── context_resolver.py  ← rehidratare batch a contextului de pagină + relații + freshness
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
│   │   ├── evidence_bundle.py   ← NX-240: faptele turului (known/unknown/stale + sursă), înghețate
│   │   ├── grounding_guard.py   ← NX-240: poarta de adevăr plan→fapte (respinge vs omite)
│   │   ├── voice.py             ← vocea răspunsului: `VOICE_RULES` (în toate prompturile de
│   │   │                          compunere) + `naturalize` (plasa deterministă, principiul 13)
│   │   ├── prompt_builder.py    ← system prompt generat din categories
│   │   ├── reference_resolver.py← NX-234/235: „acesta"/„prima" → produs; precedență UNICĂ
│   │   │                          (action>named>ordinal>page>selected>single), stale = refuz
│   │   └── tool_definitions.py  ← OpenAI tool schemas
│   ├── proactive/
│   │   ├── scheduler.py         ← proactive_jobs → outbox (motor NX-70; calea template LIVE, PR #142)
│   │   ├── initiators.py        ← PL-1: sweeper-e care CREEAZĂ proactive_jobs (coș abandonat +
│   │   │                          back-in-stock) + seam-uri awb/follow_up; rulate de jobs/scheduler
│   │   ├── builders.py          ← text per kind (free_text + template_name + variables)
│   │   └── templates.py         ← wa_templates + 24h window + consent check (poartă NX-71)
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
secrete sunt connection stringurile). Cele 41 de migrări (003→045, cu 030/031 arse) sunt înregistrate
în `schema_migrations`, deci poarta de boot NX-123 nu cere re-rularea lor.

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

**GOL azi, și fiecare gol are consecință:** `product_embeddings` = 0 (căutarea e DOAR lexicală, deci
RRF-ul n-are al doilea braț); `product_derived_signals` = 0 → `product_card_blurbs` = 0 (corect:
codul refuză să cadă pe numele produsului) și `attributes->'concerns'` = 0, deci filtrul de
`concerns`, fațetele și boost-ul de concern din rerank n-au pe ce opera; `product_review_summaries`
= 0 (183.003 recenzii reale, nerezumate → `top_pros` iese NULL pe orice card); `product_relations`
= 0 (graful e inert: `traverse_relations` → 0 noduri, iar cele **391 de produse epuizate n-au
niciun substitut**, deci „nu mai avem" e răspunsul final — situația pentru care s-a construit
NX-195); `intent_aliases` = 0. **`domain_pack` NU mai lipsește** (§13 din doc): 20 de chei canonice de
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
ultimul mesaj 2026-07-14) → SINGURUL pe care se lucrează. Telegram ÎNGHEȚAT (17 conv, ultimul
2026-06-18; poller OFF: `profiles: ["telegram"]`). WhatsApp ÎNGHEȚAT (0 conversații reale; canalul
din DB e `SIM-DRIVER`, harness-ul de test). Testele integration își creează channel throwaway
(tranzacție rollback-uită).

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
- NU trimitere directă la Meta/Telegram din stagii — totul prin `outbox` + dispatcher (ChannelSender)
- NU cod specific de canal în pipeline/worker — doar la margini (parser ingestie + ChannelSender)
- NU mesaje proactive fără consent + (24h window SAU template approved)
- NU telefoane/PII în loguri sau în analytics — doar în `channel_identities`
- NU `service_role` în worker — workerul folosește `bot_runtime` (RLS activ)
```
