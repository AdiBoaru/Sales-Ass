# Sondă DB → agent (SOLE, 2026-09-08): ce primește modelul și ce SQL plătim pentru asta

Instrument: `python scripts/db_query_probe.py` (read-only, zero OpenAI). Rulează tool-urile REALE
prin providerul tenant-scoped de producție, înregistrează fiecare statement și îl re-rulează cu
`EXPLAIN (ANALYZE, BUFFERS)` pe conexiunea `bot_runtime` (RLS activ). Raportul brut, cu fiecare
`llm_view` exact așa cum îl vede modelul: `reports/db-query-probe-99fe1292.md` (+ `.json`).
Brațul semantic a fost măsurat separat, cu vectorul STOCAT al unui produs drept vector de
interogare (același drum SQL, fără apel de embedding).

Context de măsurare: client Windows din România → Supabase eu-west-2, RTT `select 1` = **50 ms**.
Timpii „exec" sunt ai Postgres-ului (independenți de RTT); timpii „wall" includ rețeaua.

## 0. Stare: REPARATE (același branch, 2026-09-08), cu o decizie de produs

**Embeddings-urile nu se mai folosesc** (decizie Adi, 2026-09-08). `SEARCH_SEMANTIC_ENABLED=false`
(default) închide brațul vector, checkout-ul `has_embeddings` și jobul de embed; rândurile din
`product_embeddings` rămân. P0-ul de mai jos despre sortare rămâne documentat ca motiv suplimentar
și ca avertisment pentru ziua în care flagul s-ar aprinde din nou: atunci trebuie reparat ÎNTÂI.

| constatare | reparație | măsurat după |
|---|---|---|
| P0 sort pe preț + vector | brațul vector OFF prin flag; bug-ul rămâne în cod, marcat în `search_products_semantic` | n/a (stins) |
| P0 `get_substitutes` 8,5 s | id-uri din `product_relations` întâi, apoi `get_products_by_ids` | 594 ms wall, 7,5 ms exec |
| P1 categorii 427 ms/tur | `servable_subtree_counts_sql`: o trecere, join + group | 35 ms exec |
| P1 `has_embeddings`/căutare | dispare cu flagul OFF; cache 5 min per tenant când e ON | 0 checkouts |
| P1 secțiuni care nu ajung la model | `DomainPack.detail_sections` (ecommerce.json), tăiere la propoziție | detaliu 1.515 → 2.822 car., cu summary/fit/anti_fit/comparison |
| P1 `key_ingredients` gol | `scripts/derive_key_ingredients.py --apply` (parser pur) | 2.577/2.758 produse (93,4%), 3.901 valori |
| P2 nume repetate în comparație | `display_name` pe axe | 1.436 → 860 car. pentru 2 produse |
| P2 fațete hardcodate în brief | primele 3 din `comparison_facets` cu valoare | `routine_time`/`skin_type`/`concerns` vizibile |
| P2 recenzii arbitrare, tăiate | alese pe lungime (≥ 120 car.), `cut_at_sentence` | citat întreg, la propoziție |
| P2 badge-uri zgomot | `noise_badges` (> 90% din catalog) calculate în vocabular | `CPNP`/„Cadou" omise |
| P2 „anti" pe treapta relaxată | perechi de termeni de la 3 în sus + `relaxed_any` ca treaptă separată; epuizatul coboară 5 ranguri | S07: aparatul epuizat nu mai e pe locul 2 |
| P3 dubluri | nereparat: e date (4 perechi KUNDAL identice, 216 capete-de-nume repetate) | — |

Teste: `tests/test_agent_data_path.py` (+ actualizări în `test_query_terms`,
`test_retrieval_ranking`, `test_catalog_queries`). Suita rulează cu brațul semantic APRINS
(`conftest`), fiindcă codul lui rămâne; comportamentul implicit are testele lui explicite.

## 1. Constatări, în ordinea gravității

### P0 — sortarea pe preț cu embeddings PORNITE aduce produse fără nicio legătură cu cererea

`search_products_semantic` cu `sort_mode != relevance` NU folosește vecinătatea vectorială: planul
scanează TOATE embeddings-urile (`Index Scan using product_embeddings_pkey`, 2.758 rânduri) și
sortează global pe preț, iar `fuse_candidates` → `_merge_by_sort` face UNIUNE lexical ∪ vector și
re-sortează pe preț. Cele 50 de produse cele mai ieftine din tot catalogul intră în pool și câștigă.

Măsurat prin tool-ul real (`search_products`, vector de interogare = embedding-ul unui SPF):

| cerere | rezultat top-6 |
|---|---|
| `protectie solara spf`, `price_asc` | benzi pentru puncte negre 3 lei, 4 măști EPUIZATE la 8-10 lei; **0/6 cu SPF**, cosine 0,48-0,69 |
| aceeași + `concerns=["protectie solara"]` | 6 SPF-uri reale, 30-41 lei (filtrul dur salvează cazul) |

Comentariul din `_order_clause` („sub HNSW = cel-mai-ieftin-din-recall, nu global") descrie un
comportament care NU există: fără `ORDER BY embedding <=> q`, planificatorul nu are de ce să
folosească HNSW. Fără embeddings (cum a rulat sonda) bug-ul nu apare, ceea ce explică de ce a
supraviețuit: embeddings-urile SOLE există abia din 2026-09-02.

**Fix propus:** pe sort explicit, brațul semantic ia întâi top-`pool` pe cosine (subquery cu
`ORDER BY pe.embedding <=> $q LIMIT pool`), apoi sortează pe preț/rating DOAR acel subset. Sau, mai
simplu și mai onest: pe sort explicit brațul vector nu intră deloc în uniune (lexicalul deja
sortează pe subsetul potrivit textual). Se decide pe măsurătoare (D15), dar starea de azi nu poate
rămâne.

### P0 — produs epuizat: `get_substitutes` costă 580 ms cald și **8,5 s rece** pentru 2 rânduri

Planul pornește de la `products` (2.367 rânduri în stoc), hidratează TOATE lateralele din
`_DETAIL_SELECT` (imagini, secțiuni, badge-uri, ingrediente, variante: ~60.000 de buffere) și abia
la sfârșit face join cu `product_relations`. Pentru un produs cu 2 substitute.

**Fix propus:** inversează ordinea — întâi id-urile din `product_relations` (`product_id`, `kind`,
`position`, index `product_relations_anchor_idx`, `limit` mic), apoi `get_products_by_ids` pe ele,
cu filtrul de disponibilitate aplicat în Python sau în subquery. Același tipar există deja în
`related_products_tool` (traversal → ids → hidratare).

### P1 — `list_category_names` costă **430 ms pe FIECARE tur cu agent** și repetă un query deja cache-uit

`_load_prompt_inputs` (stagiul agent, fiecare tur) rulează `servable_count_sql` corelat: 45 de
categorii × un index scan pe `products` cu `exists` pe subarbore = 427 ms exec, 21.249 buffere.
Vocabularul (`load_vocabulary`, cache 5 min) rulează ACELAȘI subquery (`_CATEGORY_SQL`) și ține
deja `slug`, `name`, `path`, `count` pentru fiecare categorie.

**Fix propus:** `list_category_names` se derivă din `CatalogVocabulary.categories` (aceeași
definiție de „servabil", prin construcție — exact argumentul din docstring-ul lui
`servable_count_sql`). Zero SQL nou, 430 ms → 0 pe drumul fierbinte.
Independent de asta, `servable_count_sql` merită rescris ca o singură agregare (count per categorie
+ rollup pe `path`), fiindcă îl plătește și vocabularul la fiecare 5 minute.

### P1 — `has_embeddings` = un checkout întreg (3 round-trip-uri) per căutare, pentru un boolean care se schimbă la job

Fiecare `search_products` face `deps.db("has_embeddings")` → `set_config` + `select 1 … limit 1`
+ reset. Rezultatul e stabil între rulările jobului de embed. **Fix:** cache per tenant cu același
TTL ca vocabularul (sau chiar în `CatalogVocabulary`).

### P1 — structura round-trip-urilor: fiecare checkout costă 2 RT în plus

`tenant_conn` face `set_config` (cu verificare, 1 RT) și reset (1 RT) la fiecare checkout. Un tur
cu o singură căutare = `prompt_inputs` (2 stmt) + `has_embeddings` (1) + `search_products_ladder`
(1-3) = 3 checkouts ⇒ 6 RT de overhead + 4-6 RT de statement. La 50 ms RTT asta e un prag de
~500-600 ms înainte de orice muncă. Pe VPS RTT-ul e mai mic, dar raportul rămâne. Nu propun să
scoatem izolarea; propun să scădem NUMĂRUL de checkouts (P1-urile de mai sus scot două din trei).

### P1 — conținutul autorat NU ajunge la model: `_detail_view` randează doar `kind` ∈ {usage, warnings, features, benefits, scenarios}

SOLE are 43.761 de secțiuni, în 17 tipuri, iar dintre ele modelul vede UNA (`usage`). Cad tăcut:
`summary` („Pe scurt despre acest produs", 2.534), `fit` („Cui i se potrivește", 2.535),
`anti_fit` („Când s-ar putea să nu fie alegerea potrivită", 2.535), `problem`, `purpose`,
`comparison` („Cum se compară cu alte produse", 2.533), `routine_integration`, `key_ingredients`
(2.734), `dosage`, `routine_time`. Exact conținutul de care are nevoie un „personal shopper" ca să
argumenteze potrivirea. Lista de tipuri e hardcodată în cod (P9: ar trebui declarată în
`DomainPack`, cu plafon de caractere per tip).

Vecin: `ingredients_db` e mereu gol fiindcă `product_ingredients.is_key` e `false` pe toate cele
93.887 de rânduri, iar `attributes.key_ingredients` nu există deloc — deci fațeta `key_ingredients`
(declarată `searchable`) nu poate potrivi NIMIC. Consecința măsurată (S08, „ser cu niacinamida"):
filtrul întoarce 0, scara relaxează, iar modelul primește nota „au fost relaxate doar criterii
secundare" și 6 seruri NEfiltrate pe ingredient. Ingredientele-cheie există în
`product_sections.kind='key_ingredients'`; trebuie derivate în atribut (job, ca `product_type`).

### P2 — `llm_view` e umflat de numele-descriere; comparația repetă numele de 4 ori

Numele SOLE au ~190 de caractere (nume + frază de marketing). Consecințe: un search de 6 produse
= ~2.700 de caractere, din care ~1.200 sunt nume; `_compare_view` pentru 2 produse = 1.436 de
caractere, din care numele complet apare de 4 ori pe fiecare axă („preț: <190 car.>=155 lei vs
<190 car.>=179 lei"). `_DISTINCTIVE_NAME` (capul numelui, ~39 car.) există deja în SQL pentru fast
path; același decupaj în `_compare_view` și în axele de diferență ar tăia ~60% din view fără să
piardă un fapt.

Tot aici: `ai_summary` e NULL pe 2.758/2.758, deci fiecare linie de brief se termină cu un `| ` gol;
`description` (medie 1.618 car., 100% acoperire) nu e selectată NICĂIERI în `_SELECT`/
`_DETAIL_SELECT` — iar secțiunea `summary` (mai scurtă) ar fi înlocuitorul natural.

### P2 — fațetele din brief sunt hardcodate și lasă pe dinafară exact ce a derivat NX-257/268

`_BRIEF_FACET_KEYS = {concerns, suitable_for, finish, coverage, texture, key_ingredients}`, în timp
ce pachetul SOLE declară `comparison_facets = (routine_time, skin_type, concerns, key_ingredients,
texture, spf)`. Deci pe „crema pentru ten uscat" modelul vede `Potrivit pentru: hidratare…`
(concerns) dar NU vede `skin_type` (populat pe 1.098 de produse), nici `spf` (182), nici
`routine_time` (2.758). Brief-ul ar trebui să ia primele N fațete din pachet, nu dintr-un set fix.

### P2 — recenziile individuale: „top 2 după rating" = arbitrar, tăiate la 90 de caractere, uneori despre site

173.657 din 183.003 recenzii au 5★, deci `order by rating desc limit 2` alege practic la
întâmplare; tăierea la 90 de caractere rupe propoziția („…mai luminoasa dupa doar"), iar în S17 una
din cele două recenzii servite e despre navigarea pe site, nu despre produs. Rezolvarea reală e
NX-279 (`product_review_summaries`, încă 0 rânduri — de rulat cu `--apply`). Până atunci: alege
după lungime (≥ 120 car.) și taie la graniță de propoziție.

### P2 — badge-urile sunt zgomot pentru model

`CPNP` (2.711/2.758, o notificare de reglementare), `Cadou` (2.367), „SOLE.ro este magazin oficial
al brandului X în România" (per brand). Testul de informație din `_keep_dimension` (o valoare pe
> 98% din rânduri nu discriminează) se aplică și aici.

### P2 — treapta relaxată a scării lexicale promovează termeni-prefix („anti")

„sampoon anti matreata" → strict 0 → relaxat `sampoon | anti | matreata` → locul 2: un aparat
anti-aging EPUIZAT (`anti` în greutatea A). Rangul `ts_rank_cd` nu răsplătește suficient numărul
de termeni potriviți, iar rerank-ul determinist folosește stocul doar ca departajator pe scor egal.
Opțiuni de măsurat: (a) pe relaxat, cere ≥ 2 termeni când există ≥ 3; (b) demotează (nu exclude)
`out_of_stock` în rerank.

### P3 — dubluri în catalog

8 produse active au numele IDENTIC cu alt produs (4 perechi; ex. „SOME BY MI Retinol Intense
Reactivating Serum" la 150 și 220 lei) și 216 capete-de-nume se repetă (familii de nuanțe/gramaje).
Modelul primește ambele ca produse distincte (S01 semantic: locurile 3 și 4). Nu e bug de cod, dar
merită un `variant_of` sau un dedup pe cap-de-nume la prima pagină.

### Ce e OK

- Paginarea de sesiune („mai arată-mi") servește pagina 2 dintr-un singur checkout (106 ms), fără
  re-căutare, fără dubluri față de pagina 1.
- Query-urile de graf (`related_products`, recursive CTE) sunt pe index și ieftine (~100 ms wall,
  aproape tot RTT).
- Brațul semantic pe `relevance` e corect și rapid: HNSW, 5-7 ms exec.
- Hidratarea per pagină (`get_products_by_ids`, 6 produse cu 8 laterale) e ~80-190 ms wall.

## 2. Costul per operație (măsurat, `bot_runtime`, RLS activ)

| operație | checkouts | stmt | exec Postgres | wall (RTT 50 ms) | observație |
|---|---:|---:|---:|---:|---|
| `load_vocabulary` | 1 | 2 | 362 + 223 ms | 1.027 ms | cache 5 min |
| `prompt_inputs` | 1 | 2 | 427 + 0 ms | 714 ms | **FIECARE tur cu agent** |
| `search_products` (o treaptă) | 2 | 2 | ~30-45 ms | 260-450 ms | seq scan pe `products` (RLS; GIN inert) |
| `search_products` (3 trepte, S08) | 2 | 4 | 31+276+92 ms | 991 ms | clauza de `features` costă 276 ms pe gol |
| `get_product_details` | 1 | 1 | ~190 ms | 591 ms | 8 laterale |
| `get_product_details` pe EPUIZAT | 2 | 2 | 190 + 580 ms (8.543 rece) | 8.923 ms | **P0** |
| `compare_products` | 1 | 1 | ~80 ms | 231 ms | |
| `related_products` | 1 | 2 | ~10 ms | 434 ms | aproape tot RTT |
| pagina 2 | 1 | 1 | ~50 ms | 328 ms | |

Seq scan-ul pe `products` (2.758 rânduri, 27-33 ms) e cauza cunoscută din `DB-V3-SOLE-IMPORT.md`
§12.2: sub RLS, `@@`/`<%` nu sunt leakproof și nu devin condiții de index. La 2.758 de produse e
suportabil; crește liniar. Rezolvarea structurală (când va conta) e o funcție `security definer`
care primește `business_id` explicit și întoarce doar id-urile candidate.

## 3. Ce NU s-a măsurat aici

- Brațul semantic cu vectorul REAL al interogării (cere OpenAI) — dar planul SQL e același, iar
  bug-ul P0 nu depinde de vector.
- `faq_lookup` (cere embedding) și drumurile deterministe din `deterministic.py`/`planner.py`
  (rehidratare, cross-sell): folosesc aceleași query-uri de hidratare măsurate mai sus.
- Latența de pe VPS (RTT-ul real de producție). Structura round-trip-urilor e aceeași.
