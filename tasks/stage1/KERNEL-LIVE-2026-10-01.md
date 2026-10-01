# Kernelul pe producție: de ce nu a mers setul `kernel-live-2026-10-01`

**Pentru:** Codex (verificare, read-only). **Scris de:** Claude, 2026-10-01, după rularea setului pe
producție. **Release rulat:** `b4e7cdb` (= `main` la data rulării), kernelul servește 100% pe `sole-ro`.

**Ce îți cerem:** confirmă sau infirmă fiecare mecanism de mai jos pe cod (`fișier:linie`), răspunde
la întrebările din §6 și spune unde ordinea de reparație propusă e greșită. Mecanismele vin din
citirea codului făcută de Claude + traceurile reale; cele marcate PLAUSIBIL au un pas nedovedit.
Nu scrie cod: raportul tău decide cardurile.

---

## 1. Ce s-a rulat și cum reproduci

- Setul: [`tests/golden/prod_sets/kernel-live-2026-10-01.json`](../../tests/golden/prod_sets/kernel-live-2026-10-01.json),
  14 conversații, 54 de ture, câte un mecanism al contractului `kernel.v6.0` pe conversație; fiecare tur
  are `expect` (verdictul așteptat, scris ÎNAINTEA rulării, pe fapte citite din catalog).
- Rulat cu `PYTHONPATH=. python scripts/sim/prod_set_run.py --set tests/golden/prod_sets/kernel-live-2026-10-01.json --yes`
  pe `/web/chat`, ca widgetul (fereastra 2026-10-01 12:20:06 → 12:23:20 UTC, ~0,10 $).
- Fiecare tur e în `conversation_traces` cu `diagnostics.kernel` (traceul `kernel.v1.2`) și
  `diagnostics.model_io` (ieșirile modelului, NX-366). Citire:
  `python scripts/kernel_trace.py --business sole-ro <turn_id>`; replay la 0 $:
  `PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --conversation <conversation_id>`.
- Dump-ul tur cu tur (interpretare, schimbări verificate, referințe rezolvate, stare, plan,
  `SearchArgs`, carduri, textul botului): [`KERNEL-LIVE-2026-10-01-ture.txt`](KERNEL-LIVE-2026-10-01-ture.txt).

| Conversație | `conversation_id` | Mecanism țintit |
|---|---|---|
| k1_park_resume | `7efdd409-4967-4d11-9d6e-9484334c7c02` | parcare/reluare (I19/I6), excludere NX-364 |
| k2_retract_budget | `36e63653-3e3f-4b36-a31c-d1571f13c9e5` | retragere (I6), extrem pe rating, fațetă da/nu |
| k3_ordinal_zoom | `9a829e44-57dc-467c-9ab3-21d19a73ae00` | ordinal după detaliu (NX-364 r1), coș I10 |
| k4_price_band | `f19efbbe-377e-442d-9a44-c2be70776722` | «mai ieftin», bandă de preț, plafon |
| k5_umbrella | `af5d6950-fc24-4d92-8e0b-20e60650c8a4` | umbrela NX-350 |
| k6_named_cart | `9d759c3f-f794-4716-8464-19e12a0b12ec` | resolver pe nume, coș |
| k7_multi_act | `23520277-db02-4141-b130-563ba62c3fa1` | două acte într-un tur |
| k8_correction | `e333f0a7-8be7-4ee3-945b-8a55c41ad66c` | corecție (I21) |
| k9_aside_pagination | `feccc96a-92e3-4fae-ae0a-a5a8b4861c5d` | aside (I5) + paginare |
| k10_routine | `62131d73-2924-47db-bfc6-0745e921978c` | rutina (`bundle`) |
| k11_compare_verdict | `e7f0c062-01b8-4b6c-84db-78e6c51fab22` | comparație pe nume, I12 |
| k12_vague_clarify | `e70d6d1e-4482-42cd-825d-d4a101f4c9f5` | poarta de ambiguitate |
| k13_typos_eye | `456d7bec-0131-4744-bfac-f9021a78306a` | typo-uri, flexiune |
| k14_pregnancy_kernel | `2660ea0b-0d21-4d01-804c-6b153e486b07` | siguranța pe calea kernelului |

## 2. Rezultatul

- HTTP: 54/54 `200`, 2,1–18,4 s pe tur.
- Kernelul a SERVIT 49 de ture; 3 au mers pe scurtăturile exacte (corecte), 2 au căzut `dark` pe
  golurile cunoscute (două acte de citire; chitchat).
- Față de `expect`: **18 corecte, 10 parțiale, 26 greșite.**

Ce merge (nu pierde timp aici): parcarea și reluarea păstrează nevoile subiectului; excluderea pe
ingredient se aplică și se spune onest; `aside` păstrează sesiunea și paginarea continuă; `replace`
pe buget; coș pe ordinal exact; coș + căutare în același tur; fraza de siguranță pe fiecare tur cu
context de sarcină; «cea mai ieftină» cu o singură ancoră pe ecran; typo-urile din «vreu o crme pt ochi».

## 3. Clasele de defect, ordonate după gravitate

Fiecare clasă: **simptom** (ce a văzut clientul), **dovada** (traceul), **mecanism** (cod),
**strat**, **încredere**, **direcția de reparație**. „Strat" urmează regula de reparație a
contractului: se repară în stratul pe care îl arată traceul.

### A. Afirmații FALSE către client (cele mai grave)

#### A1. Un răspuns de magazin onest e înlocuit cu „n-am găsit produse"
- **Simptom:** k9 T2 «livrati si in Republica Moldova?» și k7 T3 «cat fac toate in cos?» primesc
  „Momentan n-am găsit produse potrivite. Îmi spui mai exact ce cauți (tip de produs, buget)?".
- **Dovada (`model_io`):** pe k9 T2 executorul `faq` a chemat `faq_lookup`, iar modelul a scris
  „Nu am informații confirmate despre livrarea în Republica Moldova. Pot verifica acest lucru cu un
  coleg." Pe k7 T3 (`delegate`) modelul a chemat `faq_lookup` de TREI ori (nu are unealtă de coș),
  apoi a scris corect din istoric „În coș este momentan primul TIRTIR Mask Fit Red Cushion, la 135 lei…".
- **Mecanism:** executorii `faq`/`delegate` trec prin `build_plan(kernel=True)` + `render` v1
  (`src/agent/kernel_executors.py:314-330`). Fără produse, `render` validează textul cu
  `_valid(final, [], …)` (`src/agent/finalize.py:1426`); tiparul de afirmație din
  `src/worker/text_scrub.py:25-26` (`…|livrare|…`) respinge orice frază despre livrare care nu e
  citat literal (NX-346), iar pe coș prețul e „neîntemeiat" (nu există produs în tur).
  `store_rules.match_rules` (NX-369, `src/agent/store_rules.py:37,101`) nu leagă fraza de nicio
  regulă, deci se ajunge la `_no_result_msg(is_order=False)` (`finalize.py:1475`, fraza la `:381`).
  `render` nu știe că actul era `store_info`: singura rezervă a unui tur de vânzare e cea de produse.
- **Notă:** respingerea pe k9 T2 e corectă în sine (fraza promite un coleg, iar transferul la om e
  scos din produs); greșită e REZERVA.
- **Strat:** executor (rezerva) + validator (o frază care spune „nu știu" nu e o afirmație).
- **Încredere:** CONFIRMAT (textul modelului citit din `model_io`; ramura din cod).
- **Direcție:** pe `faq`/`delegate`, rezerva e o frază de pachet „nu am informația" (`kernel_sentences`),
  niciodată `_no_result_msg`; coșul are nevoie de un act/executor de citire (A1 + D5).

#### A2. Compunerea bogată rescrie proza corectă și neagă regula magazinului (calea v1)
- **Simptom:** k7 T1 «arata-mi un cushion pentru ten gras si spune-mi cat costa livrarea» (două
  acte de citire ⇒ `dark` ⇒ v1): „Nu am informații despre costul livrării".
- **Dovada:** runda 1 a chemat `search_products` ȘI `faq_lookup` în paralel; runda 2 (proza) conținea
  regula exactă: „Livrarea costă între 19,90 și 24,90 lei… E gratuită peste 199 lei, sau peste 149 lei
  dacă ai mai comandat la SOLE." Apelul 3 (`sales_recommendation`, compunerea bogată) a scris
  „Nu am informații despre costul livrării", iar asta a ajuns la client.
- **Mecanism (PLAUSIBIL, de verificat):** compunerea bogată primește produsele, nu și sursele FAQ
  ale turului (`ResponsePlan.grounded_sources`, NX-346), iar proza rundei 2 se aruncă pe calea bogată
  reușită. Partea non-produs a răspunsului nu are unde să supraviețuiască.
- **Strat:** compunerea v1 (afectează kernelul prin orice tur `dark` cu două acte).
- **Încredere:** simptomul și dovada CONFIRMATE; mecanismul PLAUSIBIL.
- **Direcție:** compunerea bogată primește regulile citite în tur (sau păstrează propozițiile de
  regulă din proză); pe kernel, două planuri de citire `search` + `faq` rulate în ordine (golul
  `kernel_executors.py:791-792`, `if len(plans) != 1: return None`).

#### A3. Un produs numit, în stoc, primește „nu apare"
- **Simptom:** k6 T1 «aveti ANUA Heartleaf 77 toner?» ⇒ „ANUA Heartleaf nu apare în produsele
  disponibile" + șase tonere de alte mărci. k11 T1 «compara SKIN1004 Madagascar Centella Ampoule cu
  Probio-Cica Intensive Ampoule» ⇒ „apare Probio-Cica…, nu varianta Madagascar Centella Ampoule
  simplă, așa că nu le pot compara".
- **Adevărul din catalog:** ANUA Heartleaf 77% Toner Calmant (30 lei, în stoc, `product_type` NULL),
  plus Toner Pad 125 lei și Soothing 110 lei; SKIN1004 Madagascar Centella Ampoule 55/100/120 lei, în stoc.
- **Dovada:** k6 T1 `resolved: name not_found (source shown_now, name_not_found)`, plan `search`
  cu `query="toner"`, `product_name=null`. k11 T1: prima referință `catalog_tie` pe 3 id-uri (mărimile
  aceluiași produs), a doua `name_not_found`; planul caută doar `product_name="Probio-Cica Intensive Ampoule"`.
- **Mecanism:**
  1. `find_products_named` caută NUMELE DISTINCTIV al catalogului ÎN cuvintele clientului
     (`src/db/queries/catalog.py:2549`, `position(lower(ro_unaccent(<nume distinctiv>)) in lower(ro_unaccent($2))) > 0`):
     un nume scris parțial («ANUA Heartleaf 77 toner» vs „ANUA Heartleaf 77% Toner Calmant Anti-Roseata")
     nu se găsește niciodată. `source: shown_now` e o etichetă înșelătoare (`references.py:739,745`
     întoarce mereu sursa focusului), lookup-ul A mers în catalog.
  2. Actul `find` nu preia un nume negăsit: `_missing_name` citește doar țintele actului
     (`src/agent/turn_planner.py:805`, `_refs_of` la `:394-395`), iar referința de nume nu e în
     `find.targets`.
  3. La `compare`, `_read` caută după PRIMUL nume pierdut și aruncă referința ambiguă
     (`turn_planner.py:709-714`). `catalog_tie` = egalitate pe numele distinctiv: mărimile aceluiași
     produs au același nume (`references.py:482-486`).
- **Strat:** catalog/resolver (direcția potrivirii), planner (find și compare).
- **Încredere:** CONFIRMAT pentru direcția potrivirii, `catalog_tie` și compare; PLAUSIBIL că
  referința nu era în `find.targets` (în trace `targets: []`, deci probabil CONFIRMAT).
- **Direcție:** potrivire și în sens invers (cuvintele clientului ⊆ tokenii numelui distinctiv,
  `77%`≈`77`, cifrele rămân obligatorii ca la NX-329); o egalitate între mărimi = o familie (un
  reprezentant); `find` preia numele negăsite; `compare` păstrează ținta rezolvată.

#### A4. Raftul omonim „Corp" și refuzul „sunt creme pentru față"
- **Simptom:** k5 T2 «pentru maini» (după «vreau o crema hidratanta») ⇒ zero carduri și „Pentru mâini,
  produsele din lista aceasta nu se potrivesc: sunt creme pentru față". k5 T3 «si una de corp pentru
  piele foarte uscata» ⇒ șase creme de FAȚĂ („Ți-am ales cremă de față.").
- **Adevărul:** 12 creme de mâini în stoc, toate pe `corp-ingrijirea-corpului`; raftul ales de kernel,
  `ingrijire-personala-corp`, are UN singur produs (o loțiune).
- **Dovada:** k5 T2 `category corp → ingrijire-personala-corp (implicit)`; căutarea a întors pe
  treapta `strict` aceleași creme de față (`lexical_pool` 50), compunerea le-a refuzat
  (`rich_downgraded: no-items-selected`). k5 T3: modelul a emis doar `set category corp` +
  `set concerns hydration`, fără tip; `delta_counters.unchanged=2` ⇒ raftul rezolvat e ACELAȘI
  subraft, umbrela rămâne `['crema de maini']`.
- **Mecanism:**
  1. Vocabularul indexează cheia ȘI eticheta categoriei (`src/catalog/vocabulary.py:447`), iar
     `_best` alege cel mai ADÂNC (`vocabulary.py:476`, `sorted(… key=lambda e: (-e.depth, …))`):
     rădăcina `corp` (potrivire pe CHEIE) pierde în fața subraftului etichetat „Corp" al lui
     „Ingrijire personala". `_from_hits` (`:770`) declară ambiguitate doar la adâncime egală.
  2. Turul 3 propune același raft, fără tip ⇒ rafinare, umbrela păstrată
     (`state_reducer.py:894-912`), deci `prefer` rămâne pe creme de mâini.
  3. Căutarea efectivă e clasa C1 (interogarea „crema" + `prefer`, care nu poate urca cele 12 creme
     de mâini într-un pool de 50).
- **Latent, același tur:** un raft `implicit` ajunge în `SearchArgs.category` necondiționat
  (`turn_planner.py:937`; `Topic` nu ține proveniența raftului, `delta.py:128-132`). Pe k5 T2 garda
  NX-313 l-a scos, dar nimic din planner nu o garantează.
- **Strat:** vocabular (omonimia), reducer (umbrela), planner (C1).
- **Încredere:** CONFIRMAT (rezolvarea „corp", umbrela păstrată); PLAUSIBIL cine a scos raftul pe T2.
- **Direcție:** potrivirea pe cheie bate potrivirea pe etichetă (sau rădăcină-pe-cheie vs
  subraft-pe-etichetă = `AMBIGUOUS`); raftul ține proveniența, iar planner-ul trimite `category`
  doar pentru un raft `explicit`; un raft nou numit care nu se potrivește cu umbrela o resetează.

### B. Mișcările frecvente ale conversației

#### B1. Întrebările despre tot ecranul devin „La care te referi dintre…?"
- **Simptom:** k5 T4 «care e cea mai ieftina dintre astea?», k9 T4 «care din astea are BHA?»,
  k6 T2 «cat costa si ce volum are?» ⇒ `must_ask` cu 4 nume, deși ecranul are 6 carduri (și le
  re-arată pe toate 6).
- **Adevărul (k9 T4):** 4 din cele 6 spume de pe ecran au BHA/acid salicilic în `key_ingredients`
  (IUNIK, ABIB, EVY, JUMISO); răspunsul era calculabil.
- **Dovada:** modelul a emis act `detail` cu `{"kind":"deictic","text":"astea"}`; resolverul
  `deictic ambiguous no_anchor` pe 6 id-uri; poarta `must_ask ambiguous_too_many`.
- **Mecanism:** promptul descrie `extreme` generic (`src/conversation/turn_interpreter.py:295`), fără
  „X-ul cel mai … dintre astea"; nu există o referință „filtru pe ecran" pe orice fațetă
  (`attribute` doar pe `reference_dimensions`, implicit marcă + tip: `src/domain/pack.py:25`,
  altfel `denotes_property`, `references.py:580-581`). Poarta: `_ambiguous_reads`
  (`ambiguity_gate.py:776-792`), `MAX_ACT_BOTH = 3` (`:129`): peste 3 candidați se întreabă
  ÎNTOTDEAUNA, și pe un act de citire. Opțiunile sunt tăiate la 4 (`:780`, `:396`;
  `MAX_OPTIONS = NARROWING_MAX_VALUES = 4`), dar cheia întrebării și cardurile acoperă toate id-urile
  (`turn_planner.py:572-574`, `kernel_executors.py:465-471`).
- **Strat:** interpretare (prompt), apoi poarta.
- **Încredere:** CONFIRMAT.
- **Direcție:** un deictic plural pe un act de CITIRE = tot ecranul (răspuns pe toți, nu întrebare);
  «care din astea are X» = filtru pe ecran pe orice fațetă; întrebarea, când rămâne, arată exact
  cardurile pe care le numește.

#### B2. «Cel mai bine cotat» nu sortează după rating
- **Simptom:** k12 T4 «cel mai bine cotat» ⇒ căutare pe relevanță; k2 T2 «…vreau cea mai bine
  cotata» ⇒ un singur card.
- **Dovada:** k12 T4 modelul a emis `{"kind":"extreme","dimension":"unmapped","direction":"max"}` ⇒
  `not_orderable`; k2 T2 `set unmapped "cea mai bine cotata"` ⇒ `rank_terms`.
- **Mecanism:** modelul NU POATE numi `rating`: `dimension` pe referință e
  `{*UNIVERSAL_DIMENSIONS, *fațetele pachetului}` (`src/conversation/interpretation.py:286-290`), iar
  `UNIVERSAL_DIMENSIONS = ("category","price","unmapped")` (`:103`). Resolverul ar ordona pe rating
  (`_COLUMN_DIMENSIONS = {price, rating}`, `references.py:104`; `_numeric` citește `facts.rating`),
  iar `_sort_mode` (`turn_planner.py:961-967`) mapează `rating`. Lanțul e întreg, lipsește doar
  valoarea din enum.
- **Strat:** schema/promptul interpretării.
- **Încredere:** CONFIRMAT.
- **Direcție:** `rating` în enumul de dimensiuni al referinței `extreme` (schimbare de schemă scrisă
  de model ⇒ bump de versiune + snapshot); o regulă de prompt: „cel mai X" e referință `extreme`,
  niciodată schimbare.

#### B3. Retragerea și corecția sunt respinse
- **Simptom:** k2 T2 «de fapt bugetul nu conteaza» ⇒ plafonul de 100 lei rămâne. k8 T2
  «nu, nu rosu, nude am zis» ⇒ „Ai dreptate, nude" + ACELEAȘI rujuri.
- **Dovada:** k2 T2 modelul a emis `{"op":"clear","target":"c1","dimension":"price"}` cu handle-ul
  CORECT (`c1` = `budget_max lte 100`); validatorul: `unknown_handle`; `corrects_previous_turn=true`
  ⇒ `correction_unconfirmed`. k8 T2 `{"op":"replace","target":"c2","dimension":"key_ingredients","value":"nude am zis"}`
  (ținta corectă, valoarea = citatul); reducerul: `revoke unsupported_revoke`.
- **Mecanism:**
  1. `clear` acceptă doar `topic`/`all` (`src/conversation/provenance.py:404-407`), deci
     `clear` + `cN` iese `unknown_handle` (etichetă înșelătoare). Promptul (`turn_interpreter.py:272-273`)
     nu spune că „nu mai contează X" = `remove cN`.
  2. La `replace`, proveniența retragerii vine din valoarea NOUĂ: „nude am zis" nu se rezolvă ⇒
     `unmapped` ⇒ `implicit` ⇒ revoke cu sursa `user_implicit` (`delta.py:265,288-292,357`); reducerul
     refuză ca o sursă ne-explicită să șteargă un fapt `user_explicit`
     (`state_reducer.py:656-659`, `REVIVE_CAPABLE_SOURCES`, `state_v2.py:93`). `_apply_correction`
     (`state_reducer.py:1611-1618`) retrage doar nevoi `user_implicit`/`model_inferred`.
  3. Valoarea = citatul fiindcă promptul cere „cuvintele clientului" când nu există cod
     (`turn_interpreter.py:283`).
- **Strat:** validator + prompt (1), validator/delta (2).
- **Încredere:** CONFIRMAT.
- **Direcție:** `clear`+`cN` citit ca `remove cN` (sau `target` constrâns per op în schemă) cu motiv
  propriu; jumătatea de retragere a lui `replace` ia proveniența din CITAT (cum face
  `_structural`), iar valoarea nouă se rezolvă pe n-gramele citatului («nude»).

#### B4. O nuanță devine ingredient
- **Simptom:** k8 T1 «caut un ruj mat rosu» ⇒ `features=["rosu"]` (filtru de ingredient); un card
  „Peach Pudding".
- **Dovada:** modelul `set shade eq rosu`; validatorul `key_ingredients rosu explicit`.
- **Mecanism:** `_canonical` (`provenance.py:362-372`) nu găsește «rosu» pe `shade` și îl PROMOVEAZĂ pe
  orice dimensiune care îl are (`_resolve_anywhere`); „roșu" apare în `key_ingredients` la 39 de
  produse (ginseng roșu, ardei roșu). Citatul se verifică apoi pe dimensiunea promovată
  (`:632-635`), deci iese `explicit`; dimensiunea propusă de model nu se mai compară.
- **Gaură de date, separată:** pe rujuri `shade_group` e un hash opac (`c3ef89cb5e72`), iar `shade`
  ține codurile producătorului («116 Candid»): culoarea nu e reprezentabilă în catalog.
- **Strat:** validator (+ date).
- **Încredere:** CONFIRMAT pentru promovare; PLAUSIBIL că `shade` e scos din vocabular de
  `_keep_dimension` (`vocabulary.py:364`).
- **Direcție:** promovarea pe altă dimensiune cere ca și CITATUL să se rezolve acolo, sau e
  `semantic_mismatch` când dimensiunea propusă e o fațetă declarată a pachetului.

#### B5. «Ceva mai ieftin» re-servește produsul de pe ecran
- **Simptom:** k4 T2, ecran 80/204/204 lei ⇒ un singur card, cel de 80 lei, deja arătat.
- **Adevărul:** 17 măști de păr pentru păr deteriorat sub 204 lei (de la 35 lei).
- **Mecanism:** fără ancoră unică, delta ia MEDIANA candidaților (`delta.py:159-172`):
  median(80,204,204) = 204 = maximul; legătura e inclusivă (`lte` ⇒ `price <= $`,
  `catalog.py:757`) și persistată ca nevoie dură; pe kernel cardurile arătate se exclud doar pe
  `show_more` (`catalog_tools.py:1894-1895`, `turn_planner.py:536-540`). Referința era «ceva»
  (`relative_to: r1`, deictic „ceva"), nu un produs.
- **Strat:** delta + planner.
- **Încredere:** CONFIRMAT.
- **Direcție:** „mai ieftin" e STRICT; fără ancoră unică, sub cel mai ieftin de pe ecran (sau
  întrebare), cu cele arătate excluse; un preț relativ dintr-o ancoră ambiguă nu se persistă ca
  `budget_max` dur.

#### B6. «Al patrulea» citit ca `earlier`
- **Simptom:** k3 T3 «compara-l cu al patrulea» (după detaliu pe al doilea) ⇒ întrebare cu 4 tonere.
- **Dovada:** modelul a emis `{"kind":"earlier","text":"al patrulea","ordinal":null}`.
- **Mecanism:** promptul definește ordinalul ca „poziția #i pe ecran" și `earlier` ca „un articol de
  MAI DEVREME" (`turn_interpreter.py:293-296`), fără să spună că `earlier` poartă ordinal; blocul
  EARLIER al vederii nu are poziții (`render_view`, `:202-205`). Cu `ordinal` corect, codul NX-364 ar
  fi mers: `zoom_ordinals=True` în producție (`references.py:891`, `interpreted_turn.py:525`) ⇒
  `ordinal_in_zoomed_list`.
- **Strat:** interpretare (prompt + vedere).
- **Încredere:** CONFIRMAT.
- **Direcție:** pozițiile numerotate în EARLIER/PARKED; ordinalul numără lista pe care clientul o
  parcurge, inclusiv lista din care s-a intrat în detaliu.

#### B7. «Și pentru cearcăne?» cu un card pe ecran devine detaliu
- **Simptom:** k13 T2 ⇒ fișa cremei de ochi de 480 lei (fermitate), nu creme pentru cearcăne
  (34 de creme de ochi în stoc).
- **Dovada:** modelul: act `detail` cu `{"kind":"deictic","text":"pentru cearcane"}`; resolverul leagă
  orice deictic de cardul unic (`references.py:460-462`, `single_in_set`).
- **Strat:** interpretare (promptul îndeamnă: „detail also covers a question about an item on
  screen…", `turn_interpreter.py:262-264`).
- **Încredere:** CONFIRMAT. (Citirea „și asta e pentru cearcăne?" e legitimă; ambiguitatea există.)
- **Direcție:** o referință cere cuvinte care arată spre un articol; „și pentru X?" cu o cerință
  nouă = `find` + schimbare; opțional poarta tratează `single_in_set` + fațetă nouă ca ambiguitate.

#### B8. `set` pe o fațetă listă adaugă în loc să înlocuiască
- **Simptom:** k14 T3 «dar unul cu retinal?» după «si un ser cu vitamina C?» ⇒ `features=["vitamina c","retinal"]`.
- **Mecanism:** delta tratează `set` și `add` identic (`delta.py:241-246`); reducerul înlocuiește doar
  pe chei non-listă (`state_reducer.py:455,469-472`).
- **Strat:** delta/reducer. **Încredere:** CONFIRMAT.
- **Direcție:** `set` pe o cheie listă înlocuiește valorile active ale cheii pe același subiect;
  `add` rămâne aditiv.

### C. Căutarea planificată

#### C1. Interogarea e capul umbrelei („crema"), iar pool-ul e greșit
- **Simptom:** k13 T1 «crme pt ochi» ⇒ 1 card (480 lei, „Salut!" ca introducere); k14 T1 ⇒ 1 card;
  k5 T1 ⇒ 2 carduri; k5 T2 ⇒ 0. Căutarea întoarce 6, compunerea păstrează 1-2.
- **Dovada:** `SearchArgs.query="crema"`, `prefer.product_type` = 8 coduri de cremă; `product_search`
  pe `strict`, `lexical_pool` 50 de creme generice (aceleași COSRX/AXIS-Y pe k5 T1, k5 T2, k13 T1).
- **Mecanism:** `_read_act_query` ia cuvintele clientului pentru tip doar când tipul e `explicit`
  (`turn_planner.py:243`); altfel `_subject_label` = primul cuvânt al primului cod
  (`turn_planner.py:416-423`). «crema pt ochi» nu scrie „crema contur ochi" în ordine
  (`provenance.py:726`), deci umbrella cuprinde toate cremele; `preference_level` dă 1,0 oricărei
  creme (`fusion.py:224`), deci cremele de ochi nu primesc niciun avantaj (limita de ~7 poziții din
  NX-333 nici nu mai contează). Pe calea planificată scutirea de cotă NX-358 e oprită
  (`catalog_tools.py:2723`).
- **Strat:** planner (text) + validator (umbrella).
- **Încredere:** CONFIRMAT.
- **Direcție:** cuvintele de conținut ale citatului pentru tip (`crema ochi`); codul presupus
  ordonat peste restul umbrelei, nu la egalitate.

#### C2. Interogarea e compusă din cuvinte rămase
- **Simptom:** k10 T2 «fara spf, am deja unul» ⇒ `query="am deja unul"` ⇒ 0 rezultate, „Nu am găsit
  în catalog ce ai cerut". k10 T3 `query="ceva sub 60 de lei"` ⇒ o mască și dischete, „nu apare un
  ser" (există 6 seruri pentru roșeață ≤ 60 lei, de la 30 lei). k12 T1 `query="vreau ceva pentru fata"`.
- **Mecanism:** ultima rezervă `whole` = `Act.query` minus doar cuvintele schimbărilor `avoid` și ale
  fațetelor da/nu (`turn_planner.py:290-293`); „deja"/„unul" nu sunt cuvinte goale
  (`query_terms.py:53-66`); prețul nu intră în `named_filters`, iar `strip_price_mentions` e sărit
  pe calea planificată (`catalog_tools.py:1047`, `if planned: return`); ținta rezolvată „serul" nu
  dă tipul interogării.
- **Strat:** planner. **Încredere:** CONFIRMAT (a, b); PLAUSIBIL (c).
- **Direcție:** ultima rezervă păstrează doar cuvinte de conținut; un tur care doar neagă sau
  pune preț reia textul căutării anterioare cu filtrul nou; o țintă rezolvată dă tipul.

#### C3. Pool-ul e strangulat de un filtru pe o fațetă cu acoperire de 40%
- **Simptom:** toată conversația k1 (seruri pentru pete, ten gras) a rulat pe 5 produse; T4
  «inapoi la seruri, mai arata-mi altele» ⇒ 0 carduri, „nu văd un alt ser".
- **Dovada:** `product_search` pe T1, T2, T4: `lexical_step: filters_only`, `lexical_pool: 5`,
  `unmet_query text_unmatched`. În catalog: 68 de seruri de față pentru hiperpigmentare fără acid
  hialuronic în ingrediente, dar doar 2 au `skin_type` ∋ `oily`; `skin_type` e completat pe 1.098 din
  2.758 de produse.
- **Mecanism (PLAUSIBIL, central):** `skin_type=oily` spus ⇒ filtru relaxabil (NX-352, trimis prin
  `concerns`), dar scara se oprește la PRIMA treaptă cu orice rezultat, deci nu relaxează niciodată
  când intersecția are 5 produse. Dacă filtrul de fațetă exclude produsele FĂRĂ atribut, e o
  încălcare a D7 (`UNKNOWN ≠ MISMATCH`): 60% din catalog e scos pentru că nu are câmpul completat.
  În plus, textul „ser de față" nu se potrivește strict în acel pool (de aceea `filters_only`).
- **Strat:** unealta de căutare (scara + semantica filtrului pe fațete parțiale).
- **Încredere:** dovada CONFIRMATĂ; tratarea atributului lipsă de verificat (§6, Q5).
- **Direcție:** pe o fațetă cu acoperire parțială, un filtru relaxabil ordonează (sau păstrează
  necunoscutele după potriviri), iar o pagină sub plafon declanșează relaxarea, nu doar zero rezultate.

#### C4. Reluarea + «mai arată-mi» exclude ecranul greșit
- **Mecanism:** pe `resume`, `changed` e forțat (`turn_planner.py:364`), deci `_show_more` face o
  căutare nouă (`:689-695`); `excludes_shown` exclude `ctx.state.displayed_products` = ecranul de
  ȘAMPOANE (`catalog_tools.py:1643-1649,1895`), nu serurile parcate și nici `recent_sets`;
  `active_search` nu se restaurează la reluare (`interpreted_turn.py:260-262`), iar slotul parcat nu
  ține pool și cursor. Pe k1 T4 căutarea a reîntors aceleași 4 seruri, compunerea le-a refuzat
  (`no-items-selected`) ⇒ 0 carduri.
- **Strat:** planner/orchestrator. **Încredere:** CONFIRMAT (ramura și setul exclus), combinat cu C3.
- **Direcție:** la reluare se exclud setul parcat + `recent_sets`, sau se parchează sesiunea de
  căutare (pool + cursor) și se continuă.

#### C5. Rutina fără subiect devine o căutare
- **Simptom:** k10 T1 «fa-mi o rutina de dimineata pentru ten sensibil cu roseata» ⇒ 4 creme cu SPF,
  „acestea acoperă pasul de protecție solară, nu o rutină completă".
- **Mecanism:** `bundle_executor` întoarce `None` fără subiect (`turn_planner.py:1031`), deci actul
  cade pe `_search` (`:633-635`); `routine_family` (`:1044-1073`) cere tip sau raft. Nevoile de fațetă
  (`routine_time am`, `skin_type sensitive`) pleacă în `SearchArgs.concerns` (`:889-894`) și sunt
  re-ghicite de unealtă (`catalog_tools.py:1331`).
- **Strat:** planner. **Încredere:** CONFIRMAT.
- **Direcție:** familia rutinei din scope-ul nevoilor (`skin_type` ⇒ rutina de față) sau întrebarea
  porții; fațetele trimise pe dimensiune, nu prin `concerns`.

### D. Dezvăluiri, formă, confidențialitate

#### D1. Golul nu e spus clientului
- k2 T3 «sa fie si fara parfum» ⇒ `gaps: ['unsupported_need']`, `disclosures: []`; răspunsul a fost
  „Ți-am ales cremă de față." + 6 carduri (două creme cu SPF), fără nicio mențiune că „fără parfum" nu
  se poate verifica. `unsupported_need` e în `GAPS`, nu în `DISCLOSURES` (`turn_planner.py:87-101`);
  executorii rostesc doar dezvăluirile (`kernel_executors.py:173-186`), iar compunerea nu primește
  golurile (`kernel_executors.py:253`). Fraza „Ți-am ales {tip}." e șablonul de încadrare
  (`src/domain/defaults/ecommerce.json:5`), singurul text când introducerea modelului lipsește.
  CONFIRMAT. Direcție: `unsupported_need` pe o nevoie SPUSĂ e dezvăluire cu frază de pachet.

#### D2. Banda de preț nu se emite
- k4 T3 «ceva ieftin dar care chiar functioneaza» ⇒ zero schimbări. Doar validatorul produce
  `band:low`, și doar dacă modelul scrie `price lte` fără cifre (`provenance.py:491-510`); promptul nu
  descrie dorința vagă de preț. CONFIRMAT (mecanismul), PLAUSIBIL (cauza tăcerii modelului).

#### D3. Sarcina devine termen de ordonare persistat
- k14 T1 ⇒ `set unmapped "sunt insarcinata"` ⇒ nevoie în stare + `rank_terms`. Excluderea de
  siguranță rulează separat (corect), dar o condiție medicală stă în starea conversației și ordonează
  căutarea textuală. `delta.py:232-235`; nimic nu marchează `sensitive_class` pe calea kernelului.
  CONFIRMAT. Direcție: când detectorul de siguranță a revendicat contextul turului, schimbarea
  `unmapped` care se suprapune nu se persistă.

#### D4. Coșul nu se poate citi
- k6 T5, k7 T3: niciun act de citire a coșului; `DELEGATE_TOOLS` = `{faq_lookup, clarify_options,
  check_order}` (`turn_planner.py:121-123`), niciun bloc de coș în context. CONFIRMAT. Direcție: act
  sau executor `cart_view` cu totalul calculat de server.

## 4. Lanțurile (de ce o greșeală costă mai multe ture)

- k6: A3 pe T1 ⇒ ecranul are 6 tonere greșite ⇒ T2 și T3 cad în B1 (întrebare) ⇒ T4 «crema din aceeași
  gamă» n-are ancoră (modelul n-a emis nicio schimbare) ⇒ tot tonere.
- k8: B4 pe T1 ⇒ B3 pe T2 nu poate retrage ⇒ T3 un card ⇒ conversația e pierdută.
- k1: C3 pe T1 (pool 5) ⇒ C4 pe T4 (0 carduri).
- k10: C5 pe T1 ⇒ C2 pe T2 și T3.

## 5. Distribuția pe straturi

| Strat | Clase |
|---|---|
| Interpretare (prompt/schemă/vedere) | B1, B2, B6, B7, D2, parțial B3 |
| Validator (proveniență) | B3, B4, D3 |
| Delta / reducer | B5, B8, A4 (umbrela) |
| Vocabular / catalog | A3 (potrivirea numelui), A4 (omonimia „Corp") |
| Planner | A3 (find/compare), C1, C2, C4, C5, A4 (raft implicit latent) |
| Unealta de căutare | C3 |
| Executori / compunere | A1, A2, D1, D4, B1 (cardurile întrebării) |

## 6. Întrebările pentru tine

1. **A1:** confirmi că pe `faq`/`delegate` singura rezervă după un text respins e `_no_result_msg`
   (`finalize.py:1475`, `:1490`)? Există vreo cale prin care un tur `store_info` primește altceva?
2. **A2:** pe calea v1 bogată reușită, sursele FAQ ale turului ajung la apelul `sales_recommendation`?
   Unde se pierde regula care era în proza rundei 2?
3. **A3:** `find_products_named` (`catalog.py:2549`) poate găsi vreodată un nume scris PARȚIAL de client?
   Câte nume distinctive din catalogul SOLE conțin `%` sau cifre lipite de text?
4. **A4:** câte etichete de categorie SOLE sunt identice cu cheia unei rădăcini (ca „Corp")? Regula
   „cel mai adânc câștigă" din `_best` are un motiv documentat pe care îl rupe „cheia bate eticheta"?
5. **C3:** un filtru de fațetă (`skin_type`, `concerns`, `features`) exclude produsele care NU au
   atributul? Arată SQL-ul exact. Dacă da, e o încălcare a D7 pe calea live, nu doar pe kernel.
6. **C3:** de ce textul „ser de față" nu se potrivește strict în pool-ul de 5 (unde sunt seruri)? E
   `ro_unaccent` pe „față" sau termenul „ser" pe ponderile 049?
7. **B2:** adăugarea `rating` în enumul referinței e o schimbare minoră sau majoră a contractului (I-uri
   afectate, snapshot de schemă)?
8. **B3:** e vreun motiv de contract pentru care `clear` nu acceptă un handle `cN`?
9. **B5:** mediana NX-352 (`relative_price_median`) a fost aleasă pe o măsurătoare; care ar fi rupt
   „sub cel mai ieftin de pe ecran"?
10. **Ordinea:** propunem A1 → A3 → A2 → B1 → B2 → B3 → C3 → C1/C2 → restul. Unde greșim? Ce clasă
    lipsește din listă, după ce citești dump-ul turelor?

## 7. Ce NU e în raport

- Nu am reparat nimic; nu există PR de cod.
- Cifrele de calitate sunt verdictele lui Claude față de `expect`, nu un scor automat; dump-ul e acolo
  ca să le poți contesta tur cu tur.
- Setul nevăzut (`heldout-2026-10-01`) NU a fost citit sau rulat.
