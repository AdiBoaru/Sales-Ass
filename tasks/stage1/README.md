# STAGE 1 — WebWidget backend-driven, fiabil și pregătit pentru calitate de producție

**Status:** plan executabil, derivat din auditul read-only din 2026-08-11  
**Backend auditat:** `Sales Ass` la `origin/main@cca5760`  
**Frontend canonic auditat:** `D:\Work\Sales MVP Frontend Final` la `main@e706d27`, inclusiv modificările locale existente  
**Canal activ:** exclusiv WebWidget  
**Owner build:** Claude · **Owner verify:** Codex · **Merge:** numai userul

Acest folder este backlogul complet pentru prima etapă de reconstrucție. Cardurile nu cer o
rescriere vizuală a widgetului. Ele transformă `Sales Ass` în singurul proprietar al logicii de
conversație și comerț, iar frontendul existent într-un renderer pasiv al unui ViewModel versionat.

Invocare Claude Code din repo-ul backend:

```text
/task stage1/NX-228
```

Pentru cardurile cu repo țintă frontend, Claude creează un worktree separat din repo-ul
`Sales MVP Frontend Final`; cardul rămâne documentat aici, dar codul și PR-ul aparțin repo-ului FE.
Nu se amestecă două repo-uri într-un singur PR și nu se lucrează în directoarele principale dirty.

---

## 1. Decizia nenegociabilă: frontend pasiv

Frontendul WebWidget **nu este un al doilea motor conversațional**. El nu decide, nu deduce și nu
repară semantic răspunsurile backendului.

### Permis în frontend

- stare strict tehnică/UI: deschis/închis, focus, scroll, draft, expanded/collapsed;
- lifecycle de transport: bootstrap, `client_turn_id`, `turn_id`, pending, reconnect, recovery;
- validarea structurală a JSON-ului primit și escape/sanitizare pentru randare;
- maparea unui set finit de `block.type`, `tone`, `appearance` și `icon` pe componente/CSS;
- trimiterea textului brut al utilizatorului;
- retrimiterea **neschimbată** a unui `action_token` opac;
- forward opac pentru `id_token`, `context_token` și identificatori tehnici prevăzuți în contract;
- accesibilitate, responsive layout și navigare către un `href` deja validat de backend.

### Interzis în frontend

- intent detection, routing, retrieval, ranking, filtrare sau relaxarea constrângerilor;
- calcul de preț, reducere, total, scor, confidence, stock status sau freshness;
- inventarea greetingului, disclaimerului, progresului, fallbackului ori microcopy-ului comercial;
- acumularea criteriilor/memoriei conversaționale;
- rezolvarea lui „acesta”, „primul”, „compară-le” sau construirea unui mesaj din numele produsului;
- transformarea labelului unui chip în comandă semantică;
- mutații de coș/wishlist/comandă în `localStorage` din interiorul asistentului;
- alegerea unui CTA pe baza `kind`, a stocului sau a altor fapte;
- acceptarea HTML/CSS/JS/SVG arbitrar venit de la backend.

Backend-driven nu înseamnă remote-code UI. Contractul permite numai un discriminated union finit de
blocuri și tokenuri semantice allowlisted. Backendul decide semantica și ordinea; frontendul deține
layoutul, tema, accesibilitatea și siguranța randării.

Până există un commerce adapter server-side canonic, asistentul poate emite numai `navigate` către o
pagină validată sau poate omite CTA-ul. Nu are voie să pretindă că a adăugat ceva în coș printr-un
bridge ascuns către `localStorage`.

---

## 2. Topologia țintă Stage 1

```text
React WebWidget singleton (renderer pasiv)
  │ POST text brut sau action_token opac + identificatori de context
  ▼
Web edge: tenant server-owned + sesiune/auth + DLP
  ▼
Durable web_turns ledger ── replay/recovery/status
  ▼
Executor serial per conversație + lease/fencing + admission/deadline
  ▼
TurnSnapshot canonic: context pagină/coș rehidratat + ConversationStateV2
  ▼
fast path determinist exact SAU agent principal unic
  ▼
QuerySpec → search_entities → EvidenceBundle → AnswerPlan
  ▼
validator determinist + critic selectiv + WebViewModel projector
  ▼
commit atomic: rezultat + state + receipts + status completed
  │ GET/SSE status/result; fără streaming de tokeni nevalidați
  ▼
React afișează blocurile exact în ordinea primită
```

SSE este opțional pentru stări reale (`accepted`, `working`, `validating`, `completed`). Răspunsul
comercial este afișat atomic numai după validare. Frontendul nu afișează chain-of-thought și nu
simulează etape prin timere locale.

---

## 3. Modelele Claude Code pentru implementare

Acestea sunt modelele folosite de **Claude ca implementer**, nu modelele runtime ale Sales
Assistantului. Schimbarea modelului runtime rămâne sub D15 și cere eval blind; niciun card nu poate
schimba modelul de producție doar pentru că folosește Fable/Opus/Sonnet la coding.

| Nivel | Comandă recomandată | Utilizare în Stage 1 |
|---|---|---|
| Fable 5 | `claude --model claude-fable-5` | concurență, idempotency, tranzacții și execuții cross-cutting foarte lungi |
| Opus 5 xhigh | `claude --model claude-opus-5 --effort xhigh` | securitate, privacy, contracte, orchestrator, grounding, rollout |
| Opus 5 high | `claude --model claude-opus-5 --effort high` | backend production bine delimitat, performanță și observabilitate |
| Sonnet 5 high | `claude --model claude-sonnet-5 --effort high` | renderer pasiv, decoder, accesibilitate și teste mecanice |

Referințe oficiale: [model overview](https://platform.claude.com/docs/en/about-claude/models/overview)
și [model selection](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model).
Nu folosi Haiku pentru niciun card Stage 1. Dacă modelul recomandat nu este disponibil în cont,
folosește nivelul imediat inferior și notează explicit abaterea în PR.

---

## 4. Indexul complet și dependențele

| Card | Titlu scurt | Repo | Model | Dependențe hard | Wave |
|---|---|---|---|---|---|
| [NX-228](NX-228.md) | Contract `web-view.v2` și ownership | Backend | Opus 5 xhigh | — | A |
| [NX-229](NX-229.md) | Edge web: tenant, sesiune, auth, CORS | Backend | Opus 5 xhigh | NX-228 | A |
| [NX-230](NX-230.md) | Privacy/DLP înainte de storage și prompt | Backend | Opus 5 xhigh | NX-228 | A |
| [NX-231](NX-231.md) | Unit-of-work, conexiuni DB scurte, admission | Backend | Opus 5 xhigh | NX-228 | B |
| [NX-232](NX-232.md) | Ledger durabil, idempotency și result replay | Backend | Fable 5 | NX-228, NX-229, NX-231 | B |
| [NX-233](NX-233.md) | Executor asincron serial, lease și recovery | Backend | Fable 5 | NX-232 | B |
| [NX-234](NX-234.md) | Context pagină/coș ca ID-uri + rehidratare | Backend | Opus 5 xhigh | NX-228, NX-229, NX-230 | B |
| [NX-235](NX-235.md) | ConversationStateV2, nevoi și referințe | Backend | Opus 5 xhigh | NX-230, NX-234 | C |
| [NX-236](NX-236.md) | Acțiuni opace semnate end-to-end | Backend | Opus 5 xhigh | NX-228, NX-232, NX-235 | C |
| [NX-237](NX-237.md) | Coș/comerț canonic + receipts | Backend | Fable 5 | NX-234, NX-236 | C |
| [NX-238](NX-238.md) | Promovare controlată `search_entities` | Backend | Opus 5 xhigh | NX-203, NX-209, NX-210 decision gate, NX-234 | C |
| [NX-239](NX-239.md) | Agent principal unic în producție | Backend | Fable 5 | NX-211, NX-233, NX-235, NX-238 (adapter ales) | C |
| [NX-240](NX-240.md) | Grounding + projector WebViewModel complet | Backend | Opus 5 xhigh | NX-228, NX-234, NX-236, NX-239 | C |
| [NX-241](NX-241.md) | Deadline total, tool budget, batch și aftercare | Backend | Opus 5 high | NX-231, NX-233, NX-238, NX-240 | D |
| [NX-242](NX-242.md) | Decoder FE strict, fără normalizare semantică | Frontend | Sonnet 5 high | NX-228 | D |
| [NX-243](NX-243.md) | Widget singleton + transport/recovery | Frontend | Opus 5 high | NX-229, NX-232, NX-233, NX-242 | D |
| [NX-244](NX-244.md) | Renderer FE pasiv pe registry de blocuri | Frontend | Sonnet 5 high | NX-236, NX-240, NX-242 | D |
| [NX-245](NX-245.md) | A11y și single-flight UX | Frontend | Sonnet 5 high | NX-243, NX-244 | D |
| [NX-246](NX-246.md) | OTel, feedback real și metrici SLO | Backend | Opus 5 high | NX-228, NX-232 | B→E |
| [NX-247](NX-247.md) | Gate E2E cross-repo și failure matrix | Ambele, PR-uri separate | Opus 5 high | NX-233–246 | E |
| [NX-248](NX-248.md) | Deploy, secrete, readiness și supply chain | Backend/infra | Opus 5 xhigh | NX-232, NX-233, NX-246 | E |
| [NX-249](NX-249.md) | Canary, cutover v2, rollback și ritual calitate | Backend | Opus 5 xhigh | NX-241, NX-247, NX-248 | F |
| [NX-250](NX-250.md) | Diagrame 04a–04c și runbookuri sincronizate as-built | Backend/docs | Sonnet 5 high | NX-247, NX-248, NX-249 | G |

### Critical path

```text
NX-228 → NX-231 → NX-232 → NX-233 → NX-238 → NX-239 → NX-240
       → NX-242 → NX-243 → NX-244 → NX-245 → NX-247 → NX-249 → NX-250
```

NX-229, NX-230 și NX-246 pot porni devreme după contract. NX-234/235 pot evolua în paralel cu
ledgerul numai dacă nu ating aceleași fișiere. NX-238 și NX-239 nu au voie să activeze producția
cu candidate search dacă gate-ul NX-210/H3 nu este GO. NX-238 înregistrează
`GO|NO_GO|NOT_READY`; la `NO_GO|NOT_READY`, candidate search rămâne OFF, iar NX-239 continuă prin
`CurrentLiveRetrievalAdapter`. NX-250 este strict docs-only și începe numai după cutover, pe
SHA-urile/digesturile as-built; nu documentează anticipat o topologie încă neimplementată.

---

## 5. Relația cu taskurile existente

- NX-221 este **REUSED**, nu refăcut: lockul Redis existent rămâne temporar, dar NX-232/233 adaugă
  ledger, replay și single-flight durabil. Fail-open nu mai este o garanție de corectitudine.
- NX-225/226/227 sunt **REUSED** ca deadline de embedding, ranking lexical și telemetrie pentru
  nevoi nemapate. NX-238 le consumă; nu implementează încă o soluție paralelă.
- NX-209 este **REUSED**, dar în main este încă shadow. NX-238 este promovarea măsurată, nu rescriere.
- NX-210 este prototip offline și gate-ul poate fi `NOT_READY`. NX-238 închide decizia
  `GO|NO_GO|NOT_READY`: numai promovarea candidate search cere `GO`; NX-239 nu se oprește, ci folosește
  `CurrentLiveRetrievalAdapter` la `NO_GO|NOT_READY`.
- NX-211 este **REUSED**, implementat dar dormant prin flags; NX-239/240 îl întăresc și îl activează
  controlat, nu scriu un al doilea AnswerPlan.
- NX-212/213 sunt **ABSORBED** de NX-235/239 și NX-241.
- NX-214/215 sunt **ABSORBED** de NX-249 și NX-246.
- NX-24 vechi este **SUPERSEDED** pentru web: presupunea widgetul minimal/omnichannel și persista
  context insuficient. NX-234 este contractul web-first server-rehidratat.
- NX-188/189 rămân **FROZEN** până la GO NX-210; NX-238 le poate consuma numai după gate.
- Branchul vechi NX-181–184 nu se cherry-pick-uiește automat; nu este strămoș al `origin/main`.

`docs/FRONTEND-CONTRACT-IZI.md` este v1 implicit. Nu se modifică shape-ul in-place. NX-228 adaugă
v2 versionat și o perioadă de compatibilitate; eliminarea v1 se face numai prin NX-249.

---

## 6. Protocol obligatoriu pentru fiecare card

1. Claude citește integral `CLAUDE.md`, cardul, dependențele și fișierele indicate.
2. Verifică dependențele în **cod și git log**, nu doar în headerul unor carduri vechi.
3. Creează branch nou din `origin/main`, într-un worktree separat. Fără stacked branch implicit.
4. Implementează exclusiv scope-ul cardului. Orice decizie nouă se notează în PR.
5. Migrațiile se aplică numai prin `scripts/migrate.py`; înainte se verifică numărul liber real.
6. Rulează testele unit, integration, adversarial și E2E cerute de card.
7. Rulează gate-ul backend: `ruff check .`, `ruff format --check .`, `pytest -x -q`.
8. Pe frontend rulează toate scripturile definite de NX-247, inclusiv typecheck/build/E2E.
9. PR-ul conține link spre `tasks/stage1/NX-XXX.md`, checklist DoD, fișiere și riscuri de verificat.
10. Codex verifică read-only în alt worktree. Numai userul face merge.

### Reguli DB comune

- fiecare query tenant-scoped are `WHERE business_id = $1` explicit;
- `business_id` este derivat server-side, niciodată din browser/model/action token;
- RLS rămâne defense-in-depth, nu înlocuiește filtrul explicit;
- nicio conexiune nu rămâne checkout-uită peste apel LLM/HTTP sau așteptare de coadă;
- migrarea are up/down/rollback operațional documentat, backfill bounded și test pe Postgres real;
- PII nu intră în chei de idempotency, traces, logs, analytics sau result payload.

### Reguli de failure comune

- P6: nicio cale terminală nu produce payload gol sau tăcere;
- retry folosește același ID și nu repetă LLM-ul/acțiunea;
- orice serviciu opțional are deadline, circuit breaker și fallback onest;
- răspunsul final se persistă înainte de a fi considerat livrat;
- un rezultat vechi nu se poate atașa unei conversații/resetări noi;
- unknown/stale/tampered input produce un ViewModel terminal randabil, nu 500 brut.

---

## 7. Registru migrații Stage 1

Snapshotul curat se termină la `038_demand_daily.sql`, însă tree-ul principal al userului conține
un `039_public_read_categories.sql` local/necomis. Prin urmare **niciun card nu rezervă orb 039**.

| Card | Migrare logică | Număr |
|---|---|---|
| NX-232 | `web_turns` / result ledger / indexes / RLS | alocat la start după fetch, minimum 040 dacă 039 intră în main |
| NX-235 | state schema/version helpers, numai dacă DDL e necesar | următorul liber după NX-232 |
| NX-237 | cart/action receipts, dacă adaptorul Postgres este ales | următorul liber după NX-235 |
| NX-246 | feedback/telemetry tables, numai dacă tabelele existente nu ajung | următorul liber după NX-237 |

Cardurile cu migrare nu se implementează în paralel fără rezervarea numărului în acest tabel printr-un
PR docs foarte mic sau coordonare explicită. Nu se redenumește o migrare deja aplicată.

---

## 8. Definition of Done global pentru Stage 1

Stage 1 este închis numai dacă toate condițiile sunt demonstrate, nu doar declarate:

- un singur turn activ per conversație, inclusiv două taburi și restart worker;
- același `client_turn_id` întoarce exact același rezultat, fără al doilea apel LLM;
- refresh/navigare după accept recuperează rezultatul;
- frontendul nu conține logică de produs, preț, stoc, ranking, memorie, cart sau microcopy;
- toate chipsurile/acțiunile folosesc token opac și nu pierd referințele produselor;
- contextul „produsul acesta” este rezolvat din snapshot server-side;
- coșul nu confirmă nicio mutație fără receipt și reverificare canonică;
- toate blocurile UI sunt grounded, localizate și display-ready înainte să ajungă în browser;
- 0 încălcări hard constraints și 0 preț/link/stoc inventat pe suitele de release;
- inputul și toate controalele care creează ture sunt inactive până la status terminal;
- nicio etapă fictivă de „thinking” și niciun disclaimer repetat client-side;
- p90 end-to-end sub pragul ratificat în NX-241 și zero conexiuni DB ținute peste LLM/HTTP;
- trace complet `request → turn → model/tool → result`, fără PII;
- CI, migrations, readiness, smoke, canary și rollback sunt executabile;
- v1 poate fi reactivat în timpul canary fără pierderea turelor deja acceptate.
- diagramele 04a/04b1/04b2/04b3/04c și runbookurile descriu exact releaseul as-built, separă v1
  legacy și leagă fiecare nod/muchie/invariant de cod, test și semantica reală de livrare.

---

## 9. Lucruri intenționat în afara Stage 1

- WhatsApp și Telegram: rămân înghețate; nu reparăm Redis stream/outbox pentru ele în această etapă;
- voice/image input, plăți în chat și autentificare completă de storefront;
- framework agentic nou, fine-tuning, vector DB separat sau model runtime swap nemăsurat;
- HTML remote, design system nou sau rescrierea vizuală a storefrontului;
- migrarea tuturor paginilor admin/localStorage din demo store; invariantul pasiv se aplică
  WebWidgetului. Coșul global poate rămâne temporar demo, dar asistentul nu îl tratează drept adevăr.
