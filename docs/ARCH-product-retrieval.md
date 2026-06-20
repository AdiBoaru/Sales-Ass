# Arhitectură — retrieval & ranking de produse intent-aware (2026)

> Sinteză dintr-un workflow de cercetare + design + **review adversarial pe 3 lentile**
> (corectitudine/edge-cases, fit & simplitate, production-readiness). Acest document e
> versiunea RAFINATĂ: design-ul inițial trecut prin mustFix-urile review-ului.
> Status: **propunere**, neimplementat. Owner: TBD.

## 1. Problema (clasa de bug, nu simptomul)

Simptom live (2026-06-20): user „vreau un parfum" → 3 parfumuri (80.99 / 97.99 / 149.99);
user „ceva mai ieftin" → bot zice „cea mai ieftină = 80.99", **deși există un parfum la
18.99 lei, activ + în stoc**, în catalog.

**Trei cauze de rădăcină** (toate confirmate în cod):

1. **Sort fix, rating-first.** [`catalog.py:102`](../src/db/queries/catalog.py#L102):
   `order by p.rating desc, effective_price asc`. Un produs ieftin cu rating mic nu ajunge
   niciodată în top-6 — nici pe calea SQL, nici pe cea semantică ([`catalog.py:265`](../src/db/queries/catalog.py#L265),
   pur cosine). Deci „cel mai ieftin" nu e niciodată căutat ca atare.
2. **Follow-up reutilizează setul afișat.** [`agent.py:451-458`](../src/worker/stages/agent.py#L451-L458)
   (R3) re-hidratează `displayed_products` (cele 3 deja arătate) când modelul n-a rechemat un
   tool → „mai ieftin" răspunde din setul vechi, nu re-caută catalogul.
3. **Relax-ladder dă drumul la preț PRIMUL.** [`catalog_tools.py:90-109`](../src/tools/catalog_tools.py#L90-L109)
   relaxează `price_max` → `concerns` → `category`. Pe un tur supra-constrâns („cel mai ieftin
   sub 50 pentru ten sensibil"), ladder-ul **scoate bound-ul de preț** și re-aduce un 80.99 →
   reproduce bug-ul în alt tur. (Găsit de review, nu era în design-ul inițial.)

## 2. Decizia arhitecturală centrală: **filter-then-sort**

Constrângerile dure (preț, disponibilitate, categorie, brand) stau în `WHERE`. Sortarea e un
**mod explicit**, niciodată pliată în scorul de relevanță. „Mai ieftin" = `WHERE active +
in_stock + price<bound` apoi `ORDER BY price ASC, id` → cel mai ieftin produs real e rândul 1,
**determinist**. Asta e schimbarea care contează; restul o servește.

### Semantica „mai ieftin" (cerință de produs, explicită)

„Mai ieftin" afișează **DOAR produse efectiv mai ieftine** — nu re-arată setul vechi cu unul
ieftin în față. Regula deterministă:

- **baseline = `min(effective_price)` peste setul EXACT afișat** (din `displayed_products`).
- filtru DUR `effective_price < baseline` (strict mai mic — `<`, nu `<=`, ca să nu re-includă
  baseline-ul însuși; rezolvă și problema de epsilon/egalități semnalată de review).
- `ORDER BY effective_price asc, p.id`.
- **Fără padding**: afișezi numărul REAL de produse care califică (1 dacă e 1, până la cap-ul N).
  NU completezi la 2-3 cu produse la prețul vechi.
- set gol → „Asta e cea mai ieftină opțiune pe care o am pentru X" (P6: niciodată tăcere,
  niciodată inventezi, dar nici nu umpli).

Exemplu (cazul live): afișat 80.99/97.99/149.99 → baseline=80.99 → `< 80.99` → doar 18.99 →
**un singur card**. (`baseline` e `effective_price` = min-variant, ACELAȘI scalar pe care l-a
văzut clientul → comparația rămâne adevărată; nuanța de variante e documentată, nu ascunsă.)

Baseline-ul se calculează **determinist în cod** din `displayed_products` (ref-uri deja în state),
nu lăsat pe seama modelului — un `price_max` exclusiv pre-umplut în tool-call.

Principiile pipeline-ului rămân intacte: 9 stagii liniare, LLM doar la 2 puncte, un owner per
câmp, state = ref-uri ≤8KB, canal doar la margini, prompt din DB, prompt-cache byte-stabil.

## 3. Planul — pe faze, cu mustFix-urile review-ului încorporate

### P0 — Fix de ranking în SQL (se livrează SINGUR; ROI maxim, risc minim)

Pur SQL, fără atingere de pipeline. **Cea mai mare parte a valorii e aici.**

- `search_products` și `search_products_semantic` primesc `sort_mode` ∈
  `{relevance, price_asc, price_desc, rating_desc}` + `in_stock_only`.
- `ORDER BY` devine un switch:
  - `price_asc` → `order by effective_price asc, shrunk_rating desc, p.id`
  - `relevance` (flag ON) → `order by shrunk_rating desc, effective_price asc, p.id`
  - `rating_desc` → `order by shrunk_rating desc, p.id`
- **`shrunk_rating`** = Bayesian: `(review_count*rating + C*global_mean)/(review_count + C)`,
  C≈30. Repară cold-start (un 5.0 cu 1 recenzie nu mai îngroapă un 4.6 cu 200). Pur SQL,
  `review_count` deja selectat.
- **Tie-break determinist `p.id`** la final — omoară ordonarea instabilă (500 produse, multe
  rating-uri egale) care otrăvește `semantic_cache.retrieval_signature` și face golden-urile flaky.
- **`in_stock_only` = SOFT, nu default dur** (mustFix review): NU pune `WHERE availability in
  (...)` pe intenție *inferată* de preț — sortează `out_of_stock` ultimul. Filtru dur DOAR pe
  „în stoc" explicit. (Altfel ascunzi cel mai ieftin preorder / golești catalogul rar.)
- `sort_mode` expus în [`tool_definitions.py`](../src/agent/tool_definitions.py) ca enum,
  **adăugat ultimul** (prefix de cache stabil).
- **Kill-switch** `search_sort_mode_enabled` (default OFF). CI: cu flag OFF, `ORDER BY` e
  **byte-identic** cu cel curent (`rating desc, effective_price asc`, fără shrunk/id).

**Livrabil P0:** `sort_mode=price_asc` întoarce cel mai ieftin produs activ+în-stoc pe rândul 1,
determinist; ranking stabil; teste integration + golden pe noul `ORDER BY`.

### P1 — Închide bug-ul de set-vechi cu schimbare MINIMĂ (validează înainte de a construi mai mult)

- **O regulă în promptul agentului**: „pentru *mai ieftin/cel mai ieftin*, cheamă
  `search_products` cu `sort_mode=price_asc` și bound-ul de preț dat; arată DOAR produsele
  întoarse (efectiv mai ieftine), nu completa cu produse deja arătate; dacă e unul singur,
  arată unul singur."
- **Cod determinist mic**: calculează `baseline = min(displayed_products.price)` și pasează-l ca
  `price_max` exclusiv în tool-call (vezi §2). Asta garantează „doar mai ieftine + 1-dacă-1"
  indiferent de cum formulează modelul.
- **Gate pe R3**: fire DOAR pe follow-up-uri non-preț — **dar R3 RĂMÂNE plasa de grounding**
  (mustFix review: nu transforma un fail-safe în fail-closed; local-op e ruta deliberată,
  R3 e plasa pentru tururi neclasificate).
- **Rescrie relax-ladder-ul** (cauza #3): pe intenție de preț (`sort_mode=price_asc` sau bound
  explicit), ține `price_max` + disponibilitatea **fixate**, relaxează `concerns`/`category`
  PRIMUL.
- **Validează pe golden-ul de pinning.** Dacă asta închide bug-ul → **STOP. Nu construi
  `intent.py`/`query_plan.py`/`constraints.py`.** (Consensul review-ului: agentul e deja un LLM
  care vede constrângerile și deține tool-args; un planner determinist care pre-umple `sort_mode`
  poate fi nejustificat dacă regula de prompt + enum-ul P0 rezolvă cazul.)

### P2 — Strat determinist de intent (DOAR dacă golden-ul P1 încă pică; scope minim)

Construit *per-feature*, fiecare ancorat pe o conversație reală care pică — nu taxonomia întreagă
în avans (mustFix: nu over-engineering pe un catalog demo cu 0 embeddings / 0 orders / 0 URL-uri).

- `src/worker/intent.py`: matchers RO/HU/EN **înăspriți + ancorați + golden-testați** pentru
  preț/buget/ordinal (mustFix: un `price_max` fals devine trunchiere dură de catalog, nu fallback
  soft). Produc **hint-uri de tool-arg**, NU scriu `ctx.route` (mustFix: evită 3 writeri pe rută;
  refolosește câmpul mort `RouteDecision.filters` dacă e nevoie).
- „Mai ieftin" = re-search `price_asc` cu filtru DUR `effective_price < baseline` (vezi §2
  „Semantica «mai ieftin»": baseline = min afișat, strict `<`, fără padding, 1-dacă-1) — **NU**
  math „min(shown)−epsilon" (mustFix: epsilon-ul pică pe egalități/catalog rar → 0 rezultate →
  ladder-ul scoate prețul → bug-ul revine).
- Acumulare de constrângeri (buget/concerns/category) în `state`, cu schemă normalizată
  (`price_max` float, nu string „sub 80 lei") — **reconciliat cu writer-ul `clarify_resume`**
  (mustFix: un singur writer pe `state.constraints`).
- **`get_products_by_ids` order-preserving** (`order by array_position($2::uuid[], p.id)`) —
  **prerechizit** pentru orice rută ordinal/compare (mustFix: azi „a doua"/„compară primele două"
  hidratează produsele GREȘITE chiar dacă detecția e perfectă).
- Ordinale randate peste **setul EXACT văzut de client** — reconciliază 3-vs-6 (`state_block`
  taie la 3, `flatten` numerotează până la 6) (mustFix).
- **Răspunsurile de refine = `cacheable=False`** (sunt context-relative: „mai ieftin decât ce-ai
  văzut TU") sau cheia de cache include semnătura `sort_mode`+constrângeri (mustFix: clasa de
  cache-poisoning de care ne-am mai ars).
- **Hrănește validatorul** cu prețurile anterioare/reutilizate când reply-ul citează un număr
  dintr-un produs deja arătat + re-citește prețul live după id (mustFix: altfel `_prices_ok`
  respinge comparația legitimă → retry → fallback determinist care poate să NU fie mai ieftin).

### P3 — Hardening hibrid (opțional, gated pe aterizarea embeddings-urilor)

- `SET LOCAL hnsw.iterative_scan` — **cu probe de versiune pgvector** (mustFix: pe versiune
  nesuportată query-ul crapă; feature-detect + no-op).
- Eventual fuziune FTS(tsvector)+RRF pentru query-uri pe brand/SKU/număr.
- **Important:** pe calea semantică sub HNSW, `price_asc` întoarce *cel-mai-ieftin-din-ce-a-adus
  ANN*, NU cel-mai-ieftin global (mustFix: rutează intențiile de preț prin calea SQL pentru
  determinism, sau documentează aproximarea). Moot acum — demo are 0 `product_embeddings`, deci
  rulează doar calea SQL/ILIKE. P3 e gated pe jobul de embed.

## 4. Fișiere atinse

| Fișier | Schimbare | Fază |
|---|---|---|
| [`src/db/queries/catalog.py`](../src/db/queries/catalog.py) | `sort_mode`+`in_stock_only` params; switch `ORDER BY`; `shrunk_rating`; tie-break `p.id`; `get_products_by_ids` order-preserving | P0 (+P2) |
| [`src/tools/catalog_tools.py`](../src/tools/catalog_tools.py) | thread `sort_mode`/`in_stock`; **rescrie relax-ladder** (preț fixat) | P0/P1 |
| [`src/agent/tool_definitions.py`](../src/agent/tool_definitions.py) | enum `sort_mode` adăugat ultimul | P0 |
| [`src/agent/prompt_builder.py`](../src/agent/prompt_builder.py) | o regulă: preț → `price_asc`, nu reutiliza setul | P1 |
| [`src/worker/stages/agent.py`](../src/worker/stages/agent.py) | gate R3 pe non-preț (păstrat ca plasă); consumă hint-uri | P1/P2 |
| `src/worker/intent.py` (NOU) | matchers determiniști preț/buget/ordinal → hint-uri | P2 |
| `src/worker/query_plan.py` (NOU) | `plan()` pur → `SearchPlan` | P2 |
| `src/worker/constraints.py` (NOU) | `apply_intent` cu semantici per-slot; reconciliat cu clarify | P2 |
| [`src/worker/context.py`](../src/worker/context.py) | ordinale 1-based peste setul COMPLET | P2 |
| [`src/worker/processor.py`](../src/worker/processor.py) | persistă constrângerile acumulate (același TX) | P2 |
| [`src/models.py`](../src/models.py) | `IntentSignals`/`SearchPlan`; cap-uri liste în `from_jsonb` | P2 |

## 5. Strategie de teste

- **Unit** (fără DB/LLM): matchers `intent.py` (`mai ieftin`→price_asc, `sub 80`→price_max=80,
  `a doua`→ordinal=2, `fără parfum`→negate; variante RO/HU/EN, + cazuri NEGATIVE: „maxim de
  calitate", „am 200 de lei dar premium"); `query_plan`/`constraints` (OVERWRITE vs ACCUMULATE
  vs topic-shift; cap-uri 8KB).
- **Integration** (`test_search_products.py`, demo 500 produse): `sort_mode=price_asc` → rânduri
  în preț crescător și `row[0].price == min(effective_price WHERE active+in_stock)`; `p.id`
  tie-break face apeluri repetate byte-identice; `shrunk_rating` ține un 5.0/1-recenzie sub un
  4.x/multi-recenzii.
- **Golden** (ScriptedLLM, zero OpenAI) — **cazul de pinning**: fixture cu un parfum 18.99 în-stoc
  + unul 80.99 mai bine cotat; tur 1 arată produse, tur 2 „ceva mai ieftin" → assert `route=sales`,
  reply `must_include` 18.99, `forbidden` 80.99, ȘI assert că a tras un eveniment
  `search_products(sort_mode=price_asc)` (NU reuse R3). + golden „sub 50 lei" → doar ≤50;
  „a doua" → rezolvă shown[1] fără re-search; switch de categorie → resetează bugetul.
- **Regression guard** (CI): cu kill-switches OFF, comportamentul e byte-identic cu cel curent.

## 6. Riscuri (purtate)

- Morfologie RO/HU (ieftin/ieftine/mai ieftin/cel mai ieftin/olcsóbb) — un miss scapă tăcut
  intenția de preț → listă de tokeni curată, golden-testată per locale + fallback nano la ambiguu.
- Supra-constrângere prin acumulare → 0 rezultate pe catalog rar → ladder-ul relaxează SOFTUL
  întâi, ține prețul + disponibilitatea, apoi întreabă (niciodată tăcere, P6).
- `displayed_products` 3-vs-6 → ordinalele/„cheaper-than-shown" trebuie peste setul exact văzut.
- Cache poisoning pe „cheaper" context-relativ → `cacheable=False`.
- Pricing pe variante: `effective_price` = min-variant; „mai ieftin" e adevărat pe cel mai ieftin
  SKU — documentat, nu ascuns.
- Plasă vs autonomie agent: pre-umplerea tool-call-ului pe tururi mixte reduce libertatea
  modelului → ține-l hint puternic + validare post-hoc, nu override dur, pe intenții mixte.

## 7. Recomandare

**Livrează P0 singur, măsoară, apoi P1.** P0 (sort_mode + shrunk_rating + tie-break, behind
flag) e ~3 fișiere, respectă regula celor 2 puncte LLM, și **foarte probabil închide bug-ul**
împreună cu regula de prompt + gate-ul R3 + rescrierea relax-ladder din P1. Construiește
`intent.py`/`query_plan.py`/`constraints.py` (P2) **doar dacă** golden-ul de pinning încă pică
după P1 — și atunci, scoped per-feature, cu mustFix-urile de mai sus. Nu construi taxonomia
întreagă în avans pe un catalog demo fără embeddings/orders/URL-uri.
