# Agent Response Quality - brief complet pentru review Claude

> **DOCUMENT ISTORIC (brief de review). SUPERSEDED de [RESPONSE-QUALITY-EPIC.md](RESPONSE-QUALITY-EPIC.md) + `tasks/NX-180..189`.**
> Contractele de mai jos sunt DEPASITE acolo unde difera de carduri — in special: schema V2 veche
> (`message`/`product_reasons`/evidence semantice ca `texture.light` — inlocuita de
> `lead`/`answer`/evidence OPACE, vezi NX-183) si politica „soft mismatch → alternativa"
> (inlocuita de MatchSet DISJUNCT: soft = doar ranking, vezi NX-187/188). Sursa de adevar
> executabila sunt CARDURILE; acest doc ramane pentru istoricul deciziei.

**Status:** ISTORIC — superseded 2026-07-18 (vezi banner)  
**Data:** 2026-07-18  
**Canal tinta:** web widget (Telegram si WhatsApp raman inghetate conform NX-179)  
**Baza verificata:** `origin/main` + starea reala a PR-urilor din GitHub  

## 1. Cererea pentru Claude

Fa un review critic al acestei propuneri direct pe codul actual. Nu implementa si nu confirma
automat concluziile. Pentru fiecare observatie importanta, indica fisierul, linia, comportamentul
care se rupe si un input concret.

Review-ul trebuie sa separe:

1. constatari confirmate in cod;
2. riscuri plauzibile care necesita experiment;
3. decizii de produs care trebuie confirmate de Adi;
4. schimbari care pot fi eliminate sau amanate pentru reducerea scope-ului.

Nu folosi numarul NX-169 pentru acest epic: este deja ocupat de proiectia catalogului v3.

## 2. Obiectivul de produs

Agentul trebuie:

- sa raspunda direct la intrebarea curenta;
- sa foloseasca istoricul si constrangerile active;
- sa nu repete aceeasi introducere, lista sau incheiere;
- sa aleaga cantitatea de text potrivita turului;
- sa nu relisteze produsele la un follow-up simplu;
- sa recomande numai produse compatibile cu cerintele clientului;
- sa separe potrivirea exacta de alternativa si informatie necunoscuta;
- sa pastreze grounding-ul pentru produse, preturi, linkuri, stoc si safety;
- sa nu introduca PII in state, events, QuerySpec sau evaluator.

Nu urmarim "mai multe template-uri". Urmarim un raspuns cu forma determinata de intentie si
context, nu de o schema editoriala fixa.

## 3. Ce este confirmat in cod

### 3.1 Forma raspunsului

- `src/agent/finalize.py:52` defineste schema rich.
- Schema cere CHEILE `intro`, `items`, `pick`, `education`, `suggestions` — DAR permite `pick=null`,
  `education=null`, `suggestions=[]` (finalize.py:74-88). Template-ul e impus de PROMPT (`_RICH_RULES`)
  + randare, NU de schema JSON. (Corectie 2026-07-18 dupa contra-review Claude.)
- `src/agent/prompt_builder.py:164` incurajeaza pana la patru produse, ideal patru cand exista.
- Promptul cere deja naturalete si anti-repetitie, dar pastreaza contractul editorial amplu.
- `src/worker/compose.py:522` construieste framing-ul web din intro, pick si education.
- Cardurile, preturile si linkurile sunt hidratate de cod, ceea ce reduce halucinatiile comerciale.

Concluzie (revizuita 2026-07-18): forma repetitiva e impusa de `_RICH_RULES` (prompt) + calea rich
mereu-carduri, NU de schema (care permite null-uri). Promptul de pe origin/main e DEJA de-templatizat
(intro „1-2 fraze", education „optionala, mai bine gol", anti-repetitie explicita) → Prompt vNext va
PLAFONA; ramura text-only din V2 e probabil load-bearing, nu optionala.

### 3.2 Planner si context

- `src/agent/planner.py:64` are deja un `ResponsePlan` explicit.
- Modurile actuale sunt tehnice: comparison, rich, prose, order, fallback.
- Exista deja comportamente pentru attr query, cheaper follow-up, displayed products si rehidratare.
- Contextul trimite istoric recent, summary, profile, facts, state si search constraints.

Concluzie: nu este necesar un subsistem conversational complet nou. Planner-ul trebuie extins cu
moduri semantice, iar renderer-ul trebuie sa accepte raspunsuri de dimensiuni diferite.

### 3.3 Retrieval si matching

- Triajul extrage `budget_max`, `concerns`, `suitable_for` si `brand`.
- Tool-ul de search accepta categorie, brand, buget, concerns, features, stoc, sortare, produs si
  varianta.
- `src/tools/catalog_tools.py:367` defineste relaxarea progresiva a filtrelor.
- Pretul si stocul raman hard cand sort mode este activ, dar concerns, category si features se pot
  relaxa.
- `src/tools/reason_codes.py:25` produce doar cateva confirmari pozitive: concern, buget, ingredient.
- Nu exista un verdict complet MATCH/MISMATCH/UNKNOWN pentru fiecare constrangere.
- `DomainPack.searchable_facets` este o lista de chei, nu un contract tipizat.

Concluzie: retrieval-ul poate gasi produse apropiate, dar nu poate demonstra generic ca fiecare
produs respecta toate conditiile explicite.

### 3.4 Evaluare

- Golden tests sunt utile pentru rute, tool-uri, produse, constrangeri si grounding.
- Raspunsurile LLM sunt in principal scriptate.
- `must_include` si `forbidden` nu pot masura suficient naturaletea, repetitia si raspunsul direct.
- `scripts/sim/web_audit.py` foloseste calea web reala si poate deveni baza evaluatorului live.

## 4. Arhitectura propusa

Cele trei track-uri sunt complementare:

| Track | Rezolva | Nu rezolva singur |
|---|---|---|
| Prompt vNext | voce, concizie, instructiuni per tur | forma rigida si matching-ul |
| ResponseEnvelope V2 | forma flexibila si randare pe mod | adevarul selectiei |
| QuerySpec + Match Gate | constrangeri si selectie verificabila | naturaletea formularii |

Evaluatorul este Track 0 si precede schimbarile comportamentale.

## 5. Track 0 - evaluator conversational

### Prima versiune

- 10-12 conversatii reprezentative la baseline (extindere spre ~20 inainte de rollout larg);
- 2-5 tururi per conversatie;
- trei rulari reale per caz;
- aceeasi versiune de model si acelasi catalog pentru comparatii;
- cale obligatorie: `/web/chat` si contractul randat pentru widget;
- artefact baseline inainte de orice schimbare.

### Gate-uri deterministe

- ruta si response mode;
- produse permise si interzise;
- respectarea constrangerilor hard;
- preturi si linkuri grounded;
- zero raspuns gol;
- card count potrivit modului;
- zero repetare de carduri la direct follow-up;
- safety si contraindications;
- exact/alternative disclosure.

### LLM judge secundar

- raspunde la ce a intrebat clientul;
- foloseste corect contextul;
- suna natural;
- nu repeta inutil;
- este clar si suficient de scurt;
- recunoaste incertitudinea.

Judge-ul nu poate anula un failure determinist. Scorul se agrega prin mediana sau majoritate peste
cele trei rulari.

### Tintele sunt propuneri, nu rezultate masurate

- minimum 90% dintre cazuri cu naturalete si relevanta cel putin 4/5;
- minimum 95% follow-up-uri corecte pe produsele afisate;
- zero hard mismatch prezentat drept exact;
- zero pret, link sau produs inventat;
- nicio deschidere identica in doua tururi consecutive;
- p95 de latenta sa nu creasca cu mai mult de 10% fata de baseline;
- numarul normal de apeluri LLM sa nu creasca.

## 6. Track 1 - Prompt vNext

### Schimbari

- eliminam "ideal patru produse";
- eliminam obligatia implicita de education, pick, chips si intrebare finala;
- eliminam exemplele care ancoreaza aceleasi fraze;
- folosim instructiuni pozitive si scurte, nu o lista lunga de expresii interzise;
- sistemul pastreaza rolul, grounding-ul si safety;
- mesajul user contine datele dinamice:
  - response mode;
  - intrebarea curenta;
  - constrangerile active;
  - produsele/evidence permise;
  - istoricul relevant;
  - semnalul anti-repetitie.

Semnalul anti-repetitie ramane in mesajul user, nu in prefixul system, pentru a pastra prompt
caching-ul. Nu este necesara persistarea intregului raspuns; istoricul existent sau o semnatura
normalizata a deschiderii poate fi suficienta.

### Limite

- schema V1 continua sa ceara toate campurile;
- promptul nu poate valida matching-ul;
- prea multe reguli pot produce contradictii si cost de input;
- prompt-only trebuie tratat ca experiment rapid, nu solutie finala.

Claude trebuie sa decida daca Prompt vNext merita livrat separat sau numai ca politica reutilizata
de V1 si V2.

## 7. Track 2 - ResponseEnvelope V2

### Contract minim propus

```json
{
  "message": "text natural pentru turul curent",
  "product_reasons": [
    {
      "product_id": "p1",
      "evidence_ids": ["texture.light", "concern.oily_skin"],
      "text": "motiv scurt"
    }
  ],
  "follow_up": null
}
```

Campurile pot fi required tehnic pentru structured outputs, dar valorile goale nu se randeaza.
Lipsa continutului este o decizie valida, nu o eroare de schema.

### Responsabilitati

Modelul poate scrie:

- mesajul conversational;
- legatura lingvistica dintre nevoie si dovezile permise;
- cel mult o intrebare de follow-up, numai cand ajuta.

Codul continua sa controleze:

- product membership;
- evidence membership;
- nume, pret, rating, link, stoc si offer;
- ordinea cardurilor;
- numarul maxim de carduri;
- fallback-ul si no-silence;
- safety disclosure.

### Problema nerezolvata complet

Un `evidence_id` valid nu demonstreaza semantic ca `text` nu inventeaza alt atribut. Claude trebuie
sa compare cel putin trei variante:

1. text liber + evidence IDs + scrub existent;
2. modelul emite doar evidence IDs, iar codul compune motivul;
3. modelul emite o clauza limitata, iar codul elimina orice claim fara evidence mapping.

Recomandarea trebuie sa maximizeze naturaletea fara sa redeschida afirmatii factuale neverificate.

### Response modes

- `recommendation`: una pana la trei optiuni;
- `direct_answer`: una sau doua fraze, fara relistare;
- `detail`: un produs, fapte noi;
- `repeat_followup`: raspuns pe setul deja afisat;
- `no_exact`: explica lipsa potrivirii exacte;
- `compare`: pastreaza renderer-ul determinist existent;
- `commerce`: coș, checkout sau link.

Planner-ul alege modul. Modelul nu are voie sa schimbe singur modul sau sa extinda setul de produse.

### Compatibilitate V1/V2

- schema si renderer versionate;
- kill-switch per business;
- fallback V2 -> V1/prose determinist numai pe eroare tehnica;
- un failure de matching nu trebuie sa cada pe V1 si sa reintroduca produse respinse;
- cache key foloseste namespace `envelope_version`/`prompt_version` (disponibil PRE-triaj); `response_mode`
  NU intra in cheie in V1 (necunoscut la lookup — cache-ul ruleaza inainte de triaj, runner.py:256);
  direct/detail/repeat raman cacheable=False (corectie 2026-07-18);
- raspunsurile V1 nu trebuie interpretate drept V2 la rehidratare;
- telemetry separata pentru V1, V2 si fallback.

Claude trebuie sa verifice toate call-site-urile care presupun existenta `RichReply.items`,
`education`, `chips` sau produse.

## 8. Track 3 - QuerySpec

### Contract propus

```json
{
  "version": 1,
  "intent": "recommend",
  "subject": {"category": "creme"},
  "constraints": [
    {"facet": "price", "op": "lte", "value": 80, "strength": "hard", "source": "current_turn"},
    {"facet": "fragrance_free", "op": "eq", "value": true, "strength": "hard", "source": "current_turn"},
    {"facet": "texture", "op": "eq", "value": "light", "strength": "soft", "source": "current_turn"}
  ],
  "sort": "relevance",
  "reference_set": []
}
```

### Ownership

- triajul emite QuerySpec in apelul nano existent;
- un merger determinist combina turul curent cu state-ul;
- constrangerea explicita din turul curent castiga;
- schimbarea de subiect reseteaza constrangerile mostenite;
- revocarea explicita elimina o constrangere;
- tool-loop-ul poate formula query-ul semantic, dar nu poate slabi hard constraints;
- SearchArgs devine o proiectie operationala a QuerySpec, nu o a doua sursa de adevar.

Claude trebuie sa verifice riscul de owner dublu intre `RouteDecision.filters`,
`state.search_constraints`, planner si argumentele tool-ului.

### Clasificarea hard/soft

Politica initiala recomandata:

- hard: buget maxim explicit, negatie explicita, brand cerut, produs numit, varianta, stoc cerut,
  restrictie si safety;
- soft: "as prefera", "mai lejer", ranking, rating si preferinte de explorare;
- ambiguu: modelul pune confidence si planner-ul cere clarificare cand clasificarea poate schimba
  material rezultatul.

SafetyPolicy ramane o poarta separata si nu poate fi slabita de QuerySpec.

## 9. Typed facets in DomainPack

Contractul necesita cel putin:

- key canonic;
- value type: bool, enum, number, text, list;
- operatori permisi;
- valori si aliases normalizate;
- semantica any/all;
- sursa controlata din produs;
- politica pentru missing value;
- etichete per locale;
- prag minim de coverage pentru enforcement.

Configuratia nu poate contine SQL arbitrar sau JSON paths interpolate direct. O cheie validata se
mapeaza printr-un registru controlat din cod la extractor si, ulterior, la expresia SQL sigura.

## 10. Match Gate

Pentru fiecare produs si constrangere:

- `MATCH`: datele confirma cerinta;
- `MISMATCH`: datele confirma contrariul;
- `UNKNOWN`: informatia lipseste sau nu este verificabila.

Clasificarea setului:

- `exact`: toate conditiile hard sunt MATCH;
- `alternatives`: fara hard mismatch, dar cu UNKNOWN sau soft mismatch;
- `rejected`: cel putin un hard mismatch;

### Politica recomandata de afisare

| Situatie | Comportament |
|---|---|
| hard MATCH complet | poate fi recomandare exacta |
| hard MISMATCH | nu se afiseaza automat ca alternativa |
| hard UNKNOWN non-safety | nu se numeste exact; cere acord sau marcheaza explicit neverificat |
| soft mismatch | poate fi alternativa explicata |
| safety UNKNOWN/MISMATCH | SafetyPolicy decide fail-closed |
| zero exact | mesaj onest + un singur pas urmator, niciodata tacere |

Relaxarea nu mai este tacita. Doar conditiile soft pot fi relaxate automat. O cautare separata de
alternative trebuie sa pastreze distinctia fata de setul exact.

## 11. Exemplu complet

Client: `Vreau o crema fara parfum, sub 80 lei, pentru ten sensibil.`

- A: 72 lei, fara parfum confirmat, suitable for sensitive -> exact;
- B: 65 lei, parfumul lipseste din date -> UNKNOWN/alternative;
- C: 95 lei -> rejected pe buget;
- D: contine parfum -> rejected pe negatia explicita.

Raspuns tinta:

`Am gasit o varianta care respecta toate cerintele. Mai exista una mai ieftina, dar catalogul nu
confirma daca este fara parfum, asa ca nu as prezenta-o drept potrivire exacta.`

Nu este acceptabil ca ladder-ul sa elimine `fara parfum` si apoi sa prezinte rezultatul ca exact.

## 12. Data coverage inainte de enforcement

Match Gate nu trebuie activat hard pe o fateta doar pentru ca schema o suporta. Pentru fiecare
business si fateta trebuie masurate:

- procent produse cu valoare;
- procent valori verificate;
- distributia MATCH/MISMATCH/UNKNOWN pe query-uri reale;
- exact rate inainte si dupa gate;
- rata de zero exact;
- rata de alternative acceptate de clienti;
- diferente fata de selectia veche.

Pragul de coverage se decide per fateta. Lipsa datelor nu se rezolva printr-un prag global optimist.

## 13. State, cache si memorie

Claude trebuie sa verifice explicit:

- cum se serializeaza QuerySpec fara PII si fara text brut;
- ce constrangeri merita persistate si ce ramane doar per tur;
- cum se revoca o preferinta sau restrictie;
- cum se reseteaza state-ul la topic switch;
- daca response cache-ul actual poate servi un raspuns cu mode sau constraints gresite;
- daca displayed products raman referinte, nu obiecte complete;
- daca anti-repetitia poate folosi istoricul existent in loc de state nou;
- daca fallback-ul poate reintroduce produse respinse de Match Gate.

## 14. Observabilitate fara PII

Evenimente recomandate, cu valori normalizate si ID-uri curate:

- `response_mode_selected`;
- `response_v2_rendered`;
- `response_v2_fallback`;
- `query_spec_shadow`;
- `query_spec_disagreement`;
- `match_gate_shadow`;
- `match_gate_outcome`;
- `alternative_disclosed`;
- `hard_constraint_blocked`.

Nu se emit query brut, telefon, nume client, continut de istoric sau valori libere neverificate.

## 15. Rollout si kill-switch-uri

Fiecare nivel trebuie sa poata fi oprit independent:

- Prompt vNext;
- ResponseEnvelope V2;
- QuerySpec shadow;
- Match Gate shadow;
- Match Gate enforcement;
- typed facet SQL.

Ordine recomandata:

1. evaluator si baseline;
2. Prompt vNext;
3. V2 pe direct answer, recommendation si detail;
4. V2 pe repeat follow-up si no exact;
5. QuerySpec shadow;
6. Match Gate post-retrieval shadow + recall vs scan exhaustiv;
7. raport coverage/UNKNOWN;
8. typed facet SQL tri-state in SHADOW, per fateta (candidate-recall — PRECEDE enforce-ul acelei fatete);
9. enforcement per fateta pe un business pilot (numai fatetele cu SQL/recall verde);
10. rollout 5% -> 25% -> 100%.

> Corectie 2026-07-18 (runda 2 Codex): SQL-ul tipizat NU e ultimul pas de optimizare — participarea
> fatetei in retrieval e prerechizit de CORECTITUDINE pentru enforcement (MAX_SEARCH_POOL=24 →
> enforce post-pool da false-negative). Ordinea per fateta: 187 shadow → 189 tri-state shadow →
> paritate+recall OK → 188 enforce.

Nu facem rescriere big-bang a retrieval-ului. Prima versiune Match Gate poate evalua candidatii
existenti in memorie; SQL-ul tipizat vine dupa masurarea datelor.

## 16. Cost si performanta

Ipoteze care trebuie masurate, nu promise:

- Prompt vNext poate reduce modest output-ul, dar creste input-ul static;
- V2 ar trebui sa reduca output tokens prin eliminarea sectiunilor obligatorii;
- QuerySpec nu trebuie sa adauge un apel LLM daca este extras in triajul existent;
- Match Gate in-memory trebuie sa aiba cost liniar mic pe maximum pool-ul curent;
- typed SQL poate reduce pool-ul, dar adauga complexitate query planner-ului;
- standard sales trebuie sa ramana in numarul actual de apeluri LLM;
- evaluatorul trebuie sa urmareasca p50/p95, tokens si cost per turn.

Auditul live NX-176a a avut o rulare de aproximativ 6 secunde fata de bugetul declarat de 5 secunde;
o singura rulare nu demonstreaza regresie, dar confirma nevoia baseline-ului repetat.

## 17. Securitate si invariante

- orice query nou are `business_id` explicit;
- RLS ramane plasa, nu mecanismul unic;
- QuerySpec si events nu contin PII;
- product/evidence IDs sunt validate prin membership;
- modelul nu poate genera SQL paths sau operatori arbitrari;
- hard constraints nu pot fi slabite de tool-loop;
- safety ruleaza pe toate caile de expunere si mutatie;
- orice eroare degradeaza vizibil, fara tacere;
- un failure tehnic V2 nu poate ocoli Match Gate.

## 18. Test matrix minim

### Prompt si V2

- direct question dupa lista -> raspuns direct, fara patru carduri;
- detail pe un produs -> un produs, fapte noi;
- repeat follow-up -> foloseste setul afisat;
- compare -> renderer determinist;
- no exact -> disclosure, fara bait-and-switch;
- lipsa message/reasons -> fallback vizibil;
- product/evidence ID inventat -> eliminat;
- pret/link/claim inventat -> blocat.

### QuerySpec si Match Gate

- hard budget;
- negatie explicita;
- brand si produs numit inexistente;
- variant label;
- unknown facet;
- topic switch si revocare;
- constrangeri multi-tur;
- soft-only relaxation;
- zero exact cu alternative;
- safety plus QuerySpec;
- tenant isolation in toate query-urile noi.

### Conversational

- minimum doua verticale;
- trei rulari per caz;
- raspunsuri RO, EN si HU unde exista suport;
- input fara diacritice;
- mixed intent;
- inchidere fara CTA fortat;
- adversarial prompt injection.

## 19. Impartire recomandata in PR-uri

ID-urile finale se aloca dupa review.

1. **Evaluator conversational v1 - M**  
   Harness, scoruri deterministe, judge secundar, baseline.

2. **Prompt policy vNext - S**  
   Reguli reduse, fara ideal patru, mode input, anti-repetitie dinamica.

3. **ResponseEnvelope V2 contract + compose - M**  
   Schema, membership, renderer, fallback, kill-switch.

4. **Planner semantic modes - M**  
   Direct, recommendation, detail, repeat si no exact.

5. **QuerySpec shadow - M**  
   Contract, normalizare, ownership, telemetry, zero schimbare de comportament.

6. **Typed facet registry + coverage report - M/L**  
   Tipuri, operatori, aliases, missing policy, audit per business.

7. **Match Gate shadow/post-retrieval - M/L**  
   MATCH/MISMATCH/UNKNOWN, MatchSet si comparatie cu selectia veche.

8. **Typed facets in SQL tri-state (shadow, per fateta) - L**  
   MATCH/UNKNOWN pastrate, MISMATCH exclus; paritate exact/alternatives/rejected cu in-memory;
   numai fatetele cu coverage suficient; tenant-scoped. PRECEDE enforcement-ul per fateta.

9. **Match Gate enforcement + alternatives UX - M**  
   Exact/alternatives/rejected, consent/disclosure, rollout pilot — numai pe fatete cu SQL
   tri-state + recall verde (corectie 2026-07-18: ordinea 8/9 inversata fata de versiunea initiala).

## 20. Statusul verificarii pending

`docs/PENDING-VERIFICATION.md` este stale fata de GitHub:

- PR #230 / NX-177: merged, CI verde;
- PR #231 / NX-179: merged, CI verde;
- PR #232 / NX-175: merged, CI verde;
- PR #233 / NX-176a: deschis, cu finding P0 confirmat.

Finding-ul #233:

- guard-ul nou verifica safety numai pe `confidence=low` cu ruta `sales/order`;
- un output direct `route=clarify` ajunge la `set_clarify` fara `_safety_sensitive`;
- reproducere pe HEAD: safety `pregnancy`, mesaj cu sarcina, nano scriptat `clarify` -> rezultat
  `clarify_asked`, fara safety gate;
- finding-ul este publicat pe PR;
- PR-ul nu trebuie aprobat pana cand ruta directa clarify este tratata si testata.

Restul gate-ului #233:

- ruff check verde;
- ruff format verde;
- 188 teste tintite;
- 1746 teste CI non-integration;
- audit web routine live cu zero findings;
- proba OpenAI reala electronics: cererea vaga -> clarify, cererea calificata -> sales.

## 21. Intrebari obligatorii pentru Claude

1. Este Prompt vNext util separat sau trebuie livrat numai odata cu V2?
2. Care este contractul minim de reason/evidence care nu redeschide halucinatiile?
3. Ce componenta este owner unic pentru QuerySpec si persistenta lui?
4. Cum prevenim ca fallback-ul sa ocoleasca Match Gate?
5. Care fatete au date suficient de complete pentru enforcement?
6. Cand poate fi afisat un UNKNOWN fara acord explicit?
7. Ce call-site-uri presupun ca RichReply are items/education/chips?
8. Ce chei de cache trebuie versionate sau invalidate?
9. Ce parte poate fi eliminata din prima versiune fara sa pierdem obiectivul?
10. Exista un contraexemplu concret in care modurile propuse aleg forma gresita?

## 22. Formatul raspunsului Claude

Raspunsul trebuie sa contina, in ordine:

1. findings CONFIRMED, P0/P1/P2, cu fisier, linie si repro;
2. findings PLAUSIBLE si experimentul care le poate confirma;
3. presupuneri Codex nesustinute de cod;
4. probleme de ownership si contract;
5. riscuri de grounding, data completeness, recall, cache, latency si cost;
6. scope de eliminat sau amanat;
7. arhitectura finala recomandata;
8. taskuri/PR-uri mici cu dependente si DoD;
9. verdict: aproba, aproba cu modificari sau respinge.

Nu implementa inainte de acest verdict.
