# Baza de date v3 — proiect Supabase nou, catalog SOLE complet

**Statut:** import făcut și verificat; sistemul e LEGAT de baza nouă (vezi §11), dar nu a purtat
încă niciun tur real pe ea.
**Context:** se creează un proiect Supabase NOU. Din cel actual se păstrează **structura**
(engine conversațional, multi-tenant, contractele NX-2xx), **zero date**. Catalogul devine
importul complet din `D:\Work\SOLE SCRIPT\sole_data.db`, plus o imagine per produs.

**Principiul importului, decis de owner:** *nimic nu se exclude la scriere.* Proveniența intră
în SCHEMĂ (`source`, `voice`, `kind`, `storage`), iar filtrarea devine politică la CITIRE.
Consecința practică: „nu rosti proza AURA" e un `where`, nu un re-import.

---

## Rezultatul importului (2026-08-28, proiect `NativexSales`, eu-west-2, Postgres 17.6)

Import complet, **93 de secunde**. Fiecare cifră se potrivește cu sursa.

| tabel | rânduri | notă |
|---|---|---|
| `products` | **2.758** | 2.767 minus cele 9 scrape-uri eșuate |
| `product_variants` | 2.758 | varianta implicită, ca `price_per_unit` să existe |
| `product_sections` | **43.761** | 18.424 `merchant_pdp` + 25.337 `aura` |
| `product_evidence_chunks` | 18.424 | doar F1, roluri din vocabularul NX-205 |
| `product_faqs` | **27.931** | toate |
| `product_images` | **15.487** | toate rândurile, `storage='source'` |
| `product_badges` | 11.440 | 5.872 `merchant_marketing`, 2.711 `compliance`, 2.526 `fact`, 331 `claim` |
| `ingredients` / `product_ingredients` | 5.818 / 93.913 | |
| **`reviews`** | **183.003** | toate |
| `source_products_raw` | 2.758 | ancora verbatim |
| `catalog_quality_alerts` | 53 | 41 volume neparsabile, 8 cupoane fără reducere, 4 promoții incoerente |

**Capcanele de adevăr au ținut, măsurat pe baza reală:**

| verificare | rezultat |
|---|---|
| produse cu `sale_price` | **0** (nu există nicio reducere reală în catalog) |
| produse cu `pao_months` | **0** (nu s-a fabricat niciodată) |
| produse cu `stock_total` | **0** (UNKNOWN, nu 0) |
| produse cu cupon | 2.123 |
| `out_of_stock` | 391 |
| cu dată de durabilitate / temperatură | 2.251 / 2.713 |
| badge-uri căzute pe `other` | **0** (clasificatorul acoperă toate cele 111) |

**NX-178 e rezolvat pe date reale.** „sampon" și „șampon" întorc ambele 74 de produse;
„masca de fata" și „mască de față" ambele 258.

**Izolarea, verificată cu o conexiune `bot_runtime` reală:** 0 produse fără `app.business_id`,
2.758 cu el.

**Mărimea reală: 247 MB** înainte de embeddinguri, față de 242 MB estimați pentru TOT. Estimarea a
fost cu ~22% optimistă pe tabelele de text: presupusesem compresie TOAST pe mai multe rânduri decât
în realitate, iar majoritatea rândurilor stau sub pragul de 2 kB, deci se stochează necomprimate.
Cu embeddingurile (48 MB) și documentele de căutare, proiecția finală e **~305 MB, 61% din planul
free**. Încape, cu marjă de 1,6x în loc de 2x.

**Un defect găsit și reparat în cod existent.** `src/jobs/build_search_documents.py` (NX-207)
scria în coloana `content_hash` a fragmentelor de evidence **payloadul JSON întreg**, nu un hash.
Coloana intră în constrângerea unică, iar un index btree nu acceptă chei peste ~2.704 octeți, deci
pe descrieri lungi jobul crapă cu `index row size 3072 exceeds btree version 4 maximum 2704`.
N-a explodat până acum fiindcă descrierile catalogului demo erau scurte; prima rulare pe catalogul
real a picat pe produsul 1.648. Fixul folosește aceeași rețetă de hash ca restul modulului.

---

**Livrat deja (verificabil):**

| artefact | ce e |
|---|---|
| [`scripts/dump_schema_ddl.py`](../scripts/dump_schema_ddl.py) | generează DDL din baza LIVE (60 tabele, 181 constrângeri, 99 FK, 86 indexuri, 74 politici RLS) |
| [`docs/schema_v3_generated.sql`](schema_v3_generated.sql) | ieșirea lui: 643 statements |
| [`docs/schema_v3_delta.sql`](schema_v3_delta.sql) | cele 7 defecte reparate + coloanele pentru import lossless |
| [`scripts/check_sql_syntax.py`](../scripts/check_sql_syntax.py) | gardă cu parserul real Postgres (`pglast`); 44 fișiere, 1.057 statements, zero erori |
| [`src/catalog/sole_source.py`](../src/catalog/sole_source.py) | regulile de citire a sursei, PUR (zero I/O) |
| [`tests/test_sole_source.py`](../tests/test_sole_source.py) | 52 de teste pe date REALE, nu pe exemple plauzibile |

---

## 0. Concluzia care schimbă planul

Am inventariat baza LIVE, nu documentația: **62 de tabele, 896 de coloane, 42 de migrări
aplicate (003→045)**, RLS pe tot, roluri `bot_runtime`/`gdpr_svc`/`service_role`, funcții proprii
(`current_business_id`, `ro_unaccent`, `in_24h_window`, `gdpr_erase_contact`).

Schema **nu trebuie regândită**. Trebuie consolidată într-un singur fișier și umplută. Motivul:
șase tabele proiectate exact pentru acest import există deja și au **0 rânduri**.

| tabel | rânduri | pentru ce a fost construit |
|---|---|---|
| `source_products_raw` | 0 | payload BRUT de scraping (`source_site`, `source_url`, `scraped_at`, `payload jsonb`) |
| `product_evidence_chunks` | 0 | text citabil cu `role`, `source`, `content_hash`, `locale`, `schema_version` |
| `product_derived_signals` | 0 | semnal derivat + `derived_from[]` + `rule_id` + `schema_version` |
| `product_search_documents` | 0 | `positive_search_document` + `fts_document` + `document_version` |
| `product_card_blurbs` | 0 | textul scurt de card, derivat și versionat |
| `catalog_sync_runs` / `catalog_quality_alerts` | 0 | monitorul de ingestie („alertă, nu publicare") |

Astea sunt exact casele cerute de D4/D5 (structura e adevărul; textul AI e artefact derivat,
versionat, regenerabil). Au fost construite de NX-205/NX-036/NX-171 și n-au primit niciodată date.
**Ce lipsea nu era schema, era catalogul.**

Ce cumpără proiectul nou, concret:
1. un singur `schema_v3.sql` în loc de `schema_v2` + 42 de migrări reluate;
2. ocazia de a repara cele 7 defecte reale din §5, care azi ar cere migrări noi;
3. zero balast: 95 de tenanți de test rămași din rulările NX-236/246/247, 504 produse `archived`
   din seedul vechi, 1.038 de embeddinguri ale unor produse fictive.

---

## 1. Sursa: ce conține scraperul

`sole_data.db`, SQLite, 89 MB. 2.767 produse, 100 branduri, 38 categorii.

| tabel | rânduri | text brut |
|---|---|---|
| `products` | 2.767 | 31,8 MB (din care `sections_json` 23,5 MB) |
| `reviews` | 183.003 | 35,6 MB |
| `faq` | 27.931 | 5,0 MB |
| `images` | 15.487 | 4,78 GB pe disc |

Completitudine: ~99% pe aproape toate coloanele. Cele 9 rânduri cu `name`/`price`/`brand` NULL sunt
scrape-uri eșuate și sunt **exact** cele 9 produse fără nicio imagine. Se exclud din import, deci
după import nu rămâne niciun produs fără poză.

### 1.1 `sections_json` — două familii, separabile mecanic

17 chei. Se despart curat pe diacritice, iar despărțirea e semantică, nu cosmetică:

| familie | chei | secțiuni | diacritice | ce e |
|---|---|---|---|---|
| **F1 — fapte** | Descriere, Compozitie, Ingrediente-cheie, Cum se foloseste, Depozitare si valabilitate, Cand se utilizeaza, Cantitate Recomandata | 18.424 | 0–5% | conținut de PDP, scris de magazin/producător |
| **F2 — proză AURA** | Cui i se potrivește, Când s-ar putea să nu fie alegerea potrivită, Ce problemă rezolvă, Cum se compară cu alte produse, Când apare ca recomandare, Întrebări la care răspunde, Recomandare AURA, Pe scurt, Pentru ce este, Cum se integrează în rutină | 25.337 | 99–100% | ieșirea asistentului SOLE, pe 2.533 produse |

F2 conține **12.667 formulări de întrebare, 10.237 distincte**, în română reală cu diacritice.

---

## 2. Ce păstrăm din baza actuală (structura, zero date)

Cele 62 de tabele, pe familii. Toate migrează ca **schemă**, niciunul ca date.

- **Engine conversațional:** `conversations`, `messages` (partiționat), `contacts`,
  `channel_identities`, `conversation_summaries`, `conversation_facts`, `conversation_traces`,
  `inbound_dedupe`, `outbox`, `message_status_events`, `semantic_cache`, `intent_aliases`, `faqs`
- **Catalog:** `products`, `product_variants`, `product_embeddings`, `product_images`,
  `product_sections`, `product_faqs`, `product_badges`, `product_relations`, `product_ingredients`,
  `ingredients`, `reviews`, `product_review_summaries`, `categories`, `brands`,
  `product_category_map`
- **Catalog derivat (gol azi):** `source_products_raw`, `product_evidence_chunks`,
  `product_derived_signals`, `product_search_documents`, `product_card_blurbs`,
  `catalog_sync_runs`, `catalog_quality_alerts`
- **Comerț:** `orders`, `order_items`, `checkout_links`, `shipments`, `conversation_carts`,
  `conversation_cart_items`, `commerce_action_receipts`, `back_in_stock_subscriptions`,
  `appointments`, `proactive_jobs`
- **Web v2:** `web_turns`, `web_feedback`
- **Release / ops:** `release_policies`, `schema_migrations`
- **Analytics:** `analytics_events` (partiționat), `usage_daily`, `demand_daily`,
  `conversation_evals`, `golden_tests`
- **Tenanți / GDPR:** `businesses`, `business_users`, `channels`, `wa_templates`,
  `gdpr_requests`, `audit_log`

Plus, obligatoriu, în afara tabelelor: extensiile (`vector`, `pg_trgm`, `pgcrypto`, `uuid-ossp`),
rolul `bot_runtime` fără `bypassrls`, politicile RLS pe `current_business_id()`, funcția
`ro_unaccent`, partițiile lunare pentru `messages` și `analytics_events`, și seedul
`db/seed/safety_rules.json` (poarta de boot NX-173 refuză să pornească fără el).

**Canalele WhatsApp/Telegram rămân în schemă.** Sunt înghețate ca investiție, dar abstracția e
motivul pentru care pipeline-ul e agnostic de canal, iar WhatsApp e modelul de business pentru
clienții români. Îngheț nu înseamnă ștergere.

---

## 3. Maparea completă scraper → schemă

| sursă | destinație | note |
|---|---|---|
| `url` | `products.product_url` | validatorul verifică aici linkurile din răspuns |
| `sku` / `memox_code` | `products.external_id`, `product_variants.sku` | cheia de upsert idempotent |
| `ean` | `product_variants.gtin` | validat mod-10 (026); invalid → NULL |
| `mpn` | `products.attributes.mpn` | identificator de producător, nu e cross-vertical |
| `name` | `products.name` + `slug` | 0/2767 au diacritice, vezi §5.7 |
| `brand` | `brands` (100 rânduri) → `products.brand_id` | |
| `category` | `categories` ierarhic + `primary_category_id` + `product_category_map` | 2 niveluri pe 2.707, 1 nivel pe 60, NULL pe 9 |
| `price_regular` | `products.price`, `product_variants.price` | |
| `price_promo` + `promo_code` | `coupon_price` + `coupon_code`, **nu** `sale_price` — §5.4 | 2.123 de cupoane, **0** reduceri reale, 8 anomalii |
| `price_per_unit` | nimic; 026 îl **calculează** din `net_content` | nu importa stringul |
| `volume` | `product_variants.net_content_value` + `_unit` | 2.112 parsabile, 41 compuse („4 gr x 40 gr"), 614 lipsă |
| `currency` | `products.currency` | RON pe tot |
| `availability` | `in stoc`→`in_stock`, `stoc epuizat`→`out_of_stock` | **391 out_of_stock** |
| — | `products.stock_total` = **NULL**, nu 0 | SOLE nu dă cantitate; UNKNOWN ≠ 0 |
| `rating` / `review_count` | `products.rating` / `.review_count` | 4,13–5,00 |
| `description` | `products.description` + evidence chunk `role='description'` | |
| `ingredients` (INCI) | `ingredients` + `product_ingredients` | 96.254 legături, 7.051 distincte brut |
| `how_to_use` | `product_sections` kind=`usage`, voice=`brand` | |
| `storage` | evidence chunk + fapte extrase, vezi §5.5 | expirare, PAO, temperatură |
| `badges` | vezi §5.3 | 111 distincte, se filtrează |
| `sections_json` **F1** | `product_evidence_chunks` (`role` per cheie) + `product_sections` | `content_hash` per chunk |
| `sections_json` **F2** | `product_derived_signals` + corpus NX-203, vezi §4 | |
| `faq` (27.931) | `product_faqs` (source=`brand`, derived=false) | fără embedding, vezi §7 |
| `reviews` | `reviews` + `product_review_summaries` | vezi §5.6 |
| `images` | `product_images` (o poză/produs) | vezi §6 |
| tot rândul brut | `source_products_raw.payload` | trasabilitate: se poate reconstrui orice |

---

## 4. Ce facem cu F2 (proza AURA)

**Proza se importă integral**, în `product_sections` cu `voice='assistant'` și `source='aura'`,
lângă F1 care intră cu `voice='brand'` și `source='merchant_pdp'`. Nimic nu se aruncă.

Ce NU primește F2, și e singura restricție: **nu devine `product_evidence_chunks`**
(`classify_section(...).factual is False` pe toate cele 10 chei). Evidence chunks sunt textul
CITABIL, adică sursa pe care `grounding_guard` (NX-240) confruntă fiecare cifră și afirmație din
răspuns. Proza Aura e derivată de altcineva din fapte pe care noi nu le avem, deci ca evidence ar
transforma poarta de adevăr într-o ștampilă: ar confirma afirmații pe care nu le poate verifica.
Ca `product_sections` cu sursa scrisă, e informație păstrată, filtrabilă printr-un `where`.

Separat de păstrare, se extrage **structura**, care devine a noastră:

| cheie F2 | devine | unde |
|---|---|---|
| Cui i se potrivește | semnale `suitable_for:*` | `product_derived_signals` |
| Când NU e alegerea potrivită | semnale `not_for:*` | `product_derived_signals` |
| Ce problemă rezolvă | semnale `concern:*` | `product_derived_signals` |
| Cum se compară cu alte produse | axe de comparație + `product_relations` | NX-331 / 027 |
| Cum se integrează în rutină | `routine_step`, `routine_time` (AM/PM) | `product_derived_signals` |
| Întrebări la care răspunde | **corpus de query-uri** (10.237 distincte) | `tests/golden/`, plus secțiunea în DB |
| Când apare ca recomandare | idem corpus | idem |
| Recomandare AURA, Pe scurt, Pentru ce este | niciun semnal extras | rămân ca secțiuni `source='aura'` |

Fiecare semnal scris poartă `derived_from[]` (ce evidence chunk l-a produs) și `rule_id` (ce
extractor). Adică e regenerabil și trasabil, exact contractul lui `product_derived_signals`.

Textul afișat clientului se generează **din semnale**, în `product_card_blurbs` /
`positive_search_document`, versionat prin `document_version`. Nescris de mână, nepreluat.

**De ce nu importăm proza ca text de produs:** `grounding_guard` (NX-240) confruntă fiecare
cifră, procent și afirmație din proză cu evidence bundle-ul turului. Proza Aura n-are evidence în
baza noastră, deci ori pică validarea, ori trece pentru că am pus-o noi în DB ca „fapt", ceea ce e
mai rău. Plus că e prima decizie din §9.

**Corpusul de 10.237 de întrebări** e livrabilul lateral cel mai valoros: NX-203 (corpus, ≥100
familii) e blocat de luni, iar el blochează NX-238, NX-246 felia 3 și deci NX-249. Nu ca adevăr de
referință (sunt întrebările pe care Aura *crede* că le răspunde produsul), ci ca pool din care se
eșantionează și se etichetează familii.

---

## 5. Defecte de reparat în v3

Toate există azi și ar cere migrări. Într-o schemă nouă se rezolvă din start.

**5.1 `product_badges` n-are `business_id`.** `(id, product_id, label)`. Toate celelalte tabele de
catalog sunt tenant-scoped; ăsta nu. E o gaură de izolare, nu o scăpare cosmetică.

**5.2 `product_images` n-are `business_id`.** Aceeași problemă. `product_sections` a primit-o în
032; astea două au fost uitate.

**5.3 `product_badges` n-are `kind` și n-are `locale`.** Cele 111 valori distincte amestecă patru
lucruri, iar clasificatorul (`classify_badge`) le acoperă pe toate, testat pe vocabularul complet:
- `fact` — „AM PM dimineata si seara" (1.992), „Protectie UV Daily/Outdoor/Tinted" (118),
  „Aprobat pentru copii" (15). Afirmații verificabile despre produs, utilizabile în vânzare.
- `claim` — „Eficienta demonstrata stintific" (331). **Categorie separată**, adăugată la
  implementare: n-are studiu citabil. Ca `fact` ar deveni argument de vânzare pe care validatorul
  nu-l poate verifica; ca `claim` rămâne informație păstrată și nerostită ca dovadă.
- `compliance` — „CPNP" (2.711), notificarea cosmetică UE.
- `merchant_marketing` — „SOLE Exclusiv" (747), „Cadou" (2.367), „SOLE.ro este magazin oficial al
  brandului X" (~1.500 pe ~100 de branduri). Afirmații despre magazinul SOLE, nu despre produs.
  **Se importă** (import lossless), dar un bot care le rostește vorbește despre alt magazin.

**5.4 Cuponul n-are casă. Cel mai periculos defect din listă.** `promo_code=WELCOME15` pe 2.131 de
produse. Mapat naiv în `sale_price`, widgetul afișează o reducere, `grounding_guard` o confirmă
(e în DB, deci „e adevărată"), iar clientul aude un preț pe care nu-l poate obține. Rezolvat cu
`coupon_code` + `coupon_price` și un CHECK care le leagă; `sale_price` rămâne strict necondiționat.

> **Corecție față de prima analiză, găsită la implementare.** Spusesem „102 au reducere reală
> necondiționată". Sunt **zero**. Cele 102 aveau `price_promo < price_regular` în SQLite, dar
> diferența e sub un ban pe toate (30,0 vs 29,99953): reziduu de virgulă mobilă dintr-un calcul
> procentual la scraping. Rotunjite la ce chiar stocăm (`numeric(12,2)`), sunt egale.
>
> Consecința depășește parserul: **`sale_price` va fi NULL pe tot catalogul**, deci întreaga
> mecanică de reducere (`sale_start`/`sale_end` din 032, afișarea „-15%" din `web-view.v2`,
> rotunjirea reducerii din `src/web/localization.py`) n-are pe ce rula. Singurul avantaj de preț
> real din catalog e cuponul, și de asta are nevoie de coloane proprii: fără ele, singurul mod de
> a arăta un preț mai mic ar fi să minți. Păzit de `test_sursa_nu_are_nicio_reducere_neconditionata`.

**5.4b Opt produse au preț „promoțional" mai MARE decât cel normal** (90 lei → 216,66; 50 → 127,50),
plus patru cu cupon fără reducere. Una dintre cele două cifre e greșită și nu se poate ști care.
Promoția se ignoră, prețul normal rămâne, iar cazul se raportează în `catalog_quality_alerts`
(`PriceFacts.anomalies`). A alege tăcut una dintre cifre ar însemna să inventăm care e adevărată.

**5.5 Expirare și temperatură n-au casă. PAO nu are sursă deloc.** Din `Depozitare si valabilitate`
(2.757 produse) se extrag `min_durability_date` (2.251 valori reale, dd.mm.yyyy) și temperatura de
păstrare (2.713 reale, 44 stricate: „între °C și °C").

> **A doua capcană găsită la implementare: PAO.** Linia apare pe 2.251 de produse și pare perfect
> parsabilă, dar textul e IDENTIC peste tot: „conform simbolului PAO înscris pe ambalaj — de
> exemplu, 12M, 24M sau 36M". Sunt valori **de exemplu**, nu valoarea produsului. Un parser care
> extrage „12" de acolo fabrică un fapt pentru 2.251 de produse, iar botul ar spune „se folosește
> 12 luni după deschidere" despre oricare, cu validatorul mulțumit fiindcă cifra e în baza noastră.
> Coloana `pao_months` se creează (e locul corect când apare o sursă reală), importerul o lasă
> NULL, iar `test_pao_ramane_null_pe_intreaga_sursa` o ține așa pe toate cele 2.251.

**5.6 `reviews.rating` e `integer check between 1 and 5`**, iar sursa are o recenzie cu rating 0.
Se elimină la import. Mai important: **173.657 din 183.003 sunt 5 stele (95%)**, iar 4+5 acoperă
99,9%. Recenziile dau dovadă socială, **nu diferențiere** — `top_cons` va ieși aproape gol. Al
doilea argument pentru care „Când NU e alegerea potrivită" din F2 trebuie extras ca semnal: e
singura sursă de contra-argument onest din tot datasetul.

**5.7 Numele n-au diacritice** (0/2767: „masca de fata"), iar clienții scriu cu diacritice.
Căutarea e acoperită de `ro_unaccent` (033) în ambele sensuri, deci NX-178 nu se întoarce. Dar
numele se **afișează** în carduri. Re-diacritizarea automată nu e sigură fără dicționar. Gap
cunoscut, de decis separat.

**5.8 `ingredients` are doar `(id, business_id, name, slug)`.** Pentru „conține alcool?", „e fără
parfum?", „are parfum?" trebuie `inci_name`, `aliases text[]`, `flags text[]`. Cele 7.051 distincte
brute cer și normalizare (majuscule, sinonime, sufixe).

**5.9 Variantele.** SOLE n-are variante: un produs = un preț = un volum. Dar `net_content_value/unit`
și `price_per_unit` (generated) stau pe **variantă**, iar 026 declară varianta ca sursă de adevăr
comercială. Deci creăm **o variantă „default" per produs**, altfel pierdem prețul per unitate pe care
sursa îl are (`91.07 lei/100ml`). Nuanțele de machiaj (298 Buze, 274 Față, 67 Ochi) apar ca produse
separate cu URL propriu; rămân produse separate, fidel sursei. Gruparea lor în variante ar cere
inferență și ar strica idempotența pe `external_id`.

---

## 6. Imaginile — LIVRAT

**Decizie finală (owner): Supabase Storage, nu VPS.** Bucket public `product-images`, prefix
`sole`, **2.758 de imagini urcate, 311 MB, 30% din cei 1.024 MB ai planului free.**

URL-ul: `https://<ref>.supabase.co/storage/v1/object/public/product-images/sole/<sku>/1.<ext>`
Verificat: 200, `content-type` corect, `Cache-Control: public, max-age=31536000, immutable`.

Bucketul e PUBLIC deliberat: widgetul randează `<img src>` fără sesiune, iar unul privat ar cere
URL-uri semnate cu expirare, adică un round-trip pe fiecare card afișat.

Cele 12.729 de rânduri rămase (galeria) au `storage='source'` și URL-ul magazinului. Nu sunt
găzduite de noi și baza n-o pretinde.

**Cele 10 GIF-uri au fost convertite la primul cadru.** Patru depășeau limita de 10 MB a
bucketului, dar problema reală era alta: un GIF animat de 18 MB pe un card de produs costă 18 MB
de egress la fiecare afișare, iar cei 5 GB lunari s-ar epuiza în 280 de vizualizări. Cardul
afișează oricum o imagine statică. **88,1 MB → 488 KB**, iar cele 10 produse arată acum ca
celelalte 2.748.

**Un bug de idempotență, găsit pentru că prima reluare a re-urcat tot.** API-ul
`/storage/v1/object/list` e **nerecursiv**: cu `prefix='sole'` întoarce pseudo-foldere
(`{"name": "F26146", "id": null, "metadata": null}`), nu fișiere. Inventarul ieșea gol, iar
scriptul re-urca liniștit toate cele 403 MB. `upsert` făcea rezultatul corect, deci defectul nu se
vedea în date, doar în trafic — genul de risipă care nu declanșează nimic. Inventarul citește acum
direct `storage.objects` din Postgres, la care oricum aveam acces: un query în loc de zeci de
pagini de API, și exact. Verificat: a doua rulare urcă **0**.

Arhiva completă (15.487 fișiere, 4,78 GB) rămâne locală. Rândurile ei sunt deja în DB, deci un al
doilea val de upload e un `update`, nu un re-import.

<details>
<summary>Decizia anterioară (VPS), păstrată pentru context</summary>

Propusesem VPS-ul, fiindcă la Supabase egressul de imagini se împarte cu cel al bazei de date:
dacă îl termini, rămâi fără bot, nu doar fără poze. La 143 KB per afișare, cei 5 GB acoperă
~35.000 de vizualizări de card, iar cache-ul de un an face reîncărcările gratuite. Owner-ul a ales
Supabase; nota rămâne ca lucru de urmărit, nu ca obiecție.

</details>

---

### Varianta VPS (nefolosită)

Toate cele 15.487 de RÂNDURI intră în `product_images` (import lossless), dar se găzduiesc doar
imaginile principale. Coloana `storage` (`self` | `source` | `archived_only`) spune despre fiecare
rând unde e de fapt fișierul, iar `source_url` păstrează întotdeauna adresa originală. Un al doilea
val de upload devine un `update`, nu un re-import, iar widgetul nu află niciodată.

- 2.758 de fișiere găzduite, **403 MB**, toate prezente pe disc
- **nu** în Supabase Storage: încap în 1 GB (40%), dar la 143 KB per afișare cei 5 GB de egress
  lunar se termină în ~35.000 de vizualizări de card, iar egressul e partajat cu baza de date —
  adică rămâi fără bot, nu doar fără poze
- pe VPS: serviciu `nginx:alpine` în `docker-compose.prod.yml`, atașat la `shared_network`, router
  Traefik pe `img.nativextech.com` cu `certresolver=letsencrypt`, ca `webhook`
- **volum montat, nu baked în imaginea Docker.** NX-248 a făcut imaginea imutabilă și pin-uită pe
  digest; conținutul în build ar cupla pozele de release-urile de cod, iar `.dockerignore` ne-a
  mușcat deja exact așa (`safety_rules.json` lipsea și pica poarta de boot)
- `product_images.url` = `https://img.nativextech.com/sole/<sku>/1.<ext>`
- `Cache-Control: public, max-age=31536000, immutable`
- arhiva de 4,78 GB (galeria completă, 5,6 poze/produs) rămâne locală

---

## 7. Bugetul de spațiu

Cost unitar **măsurat** pe baza actuală: un embedding de 1536 de dimensiuni cu partea lui de index
HNSW = **17,3 kB** (18 MB pentru 1.038 de rânduri: heap 224 kB, TOAST ~9,5 MB, HNSW 8 MB).
Comprimabilitatea textului românesc de cosmetice, măsurată: **13–23%**.

Bugetul de mai jos e cu **TOT inclus**: toate recenziile, toate FAQ-urile, toate secțiunile
(inclusiv F2), toate badge-urile, toate rândurile de imagine.

| | rânduri | estimare |
|---|---|---|
| baseline (conversații, analytics, infra) | | ~25 MB |
| `products` | 2.767 | ~12 MB |
| `source_products_raw` (payload brut, ancora lossless) | 2.767 | ~7 MB |
| `product_sections` (F1 **și** F2) | 43.761 | ~14 MB |
| `product_evidence_chunks` (doar F1, citabil) | 18.424 | ~8 MB |
| `product_faqs` | 27.931 | ~13 MB |
| `ingredients` + `product_ingredients` | 7.051 + 96.254 | ~14 MB |
| `product_images` (toate rândurile, doar URL) | 15.487 | ~6 MB |
| `product_badges` (toate, tipizate) | ~12.000 | ~2 MB |
| `product_variants` | 2.767 | ~1 MB |
| **`reviews` (TOATE)** | 183.003 | ~80 MB |
| `product_derived_signals` | ~40.000 | ~6 MB |
| `product_review_summaries` | 2.767 | ~2 MB |
| `product_search_documents` + `product_card_blurbs` | 5.534 | ~4 MB |
| **`product_embeddings`, 1 vector/produs** | 2.767 | **~48 MB** |
| **TOTAL** | | **~242 MB** |

**48% din planul free (500 MB).** Importul complet încape, cu marjă de 2x.

Notă despre duplicare, ca să fie o decizie și nu o scăpare: același text stă în trei locuri —
verbatim în `source_products_raw` (ancora din care se poate reconstrui orice), ca text de afișare
în `product_sections`, și tăiat în atomi citabili în `product_evidence_chunks`. Costă ~25 MB și
cumpără posibilitatea de a re-extrage altfel fără să rulezi scraperul din nou.

Ce **sparge** planul free, și de ce embeddingul se face doar la nivel de produs:

| granularitate greșită | cost |
|---|---|
| un vector per FAQ (27.931) | **483 MB** |
| un vector per recenzie plafonată (52.786) | **913 MB** |
| un vector per recenzie completă (183.003) | **3,2 GB** |

Costul de generare pentru cele 2.767 de embedduri: **~$0,03**. Comanda se pregătește, o rulează Adi.

**Capcană a planului free:** proiectul se suspendă după 7 zile de inactivitate. Cu botul live nu se
întâmplă; merită un ping în cron ca plasă înainte de orice demo programat.

**Regulă de egress:** vectorii nu părăsesc baza. Căutarea face `ORDER BY embedding <=>` în Postgres
și întoarce id-uri și scoruri. Un `SELECT embedding` din Python ar însemna 17 MB pe apel.

---

## 8. Găurile pe care scraperul NU le acoperă

Se semnalează acum, nu la demo:

- **Livrare, retur, plată** — ~~niciun câmp~~ **REZOLVAT altfel, vezi §8.1.** Scraperul nu le are,
  dar sole.ro le publică, iar `faqs` e casa lor corectă. `delivery_class` (032) rămâne fără sursă
  structurată: răspunsul e text de politică, nu un câmp per produs.
- **Garanție** — nu apare ca termen explicit pe paginile publice. `grounding_guard` respinge din
  construcție orice afirmație despre garanție fără sursă, deci botul refuză onest.
- **Stoc cantitativ** — doar binar. `stock_total` rămâne NULL peste tot, la fel `variants.stock`
  (2.755/2.755). **Decizie de owner, 2026-09-04: iese din scop, nu e gaură de închis.** Autoritatea
  e `availability` (2.367 `in_stock` / 391 `out_of_stock`), iar codul o tratează deja așa: filtrul
  `in_stock_only` e pe `availability`, nu pe un număr; `_stock_facts` emite `availability` cunoscut
  lângă `stock` UNKNOWN; `format_availability` cere `0 < stock <= 5` ca să scrie „Ultimele N bucăți",
  deci fără cifră cade pe eticheta de disponibilitate; poarta de CTA și validatorul cer
  `in_stock`/`low_stock`, nu cantitate. Două consecințe asumate: (1) coșul NU plafonează cantitatea
  (`cart_service` compară doar când stocul e cunoscut) — reconcilierea e a magazinului, noi n-avem
  API de storefront; (2) urgența pe raritate („mai sunt 3") nu se poate folosi ca pârghie de
  vânzare. `low_stock` rămâne în vocabular, dar importul SOLE nu-l produce niciodată.
- **Politica de preț / istoricul prețului** — `sale_start`/`sale_end` rămân NULL.

### 8.1 FAQ: ce e acoperit și ce nu

**FAQ pe produs e complet și LIVE:** 27.931 de rânduri, `locale='ro'`, pe 2.750 din 2.758 de
produse. `get_product_details` le servește (6 per produs, ordonate după `position`). Nu intră în
căutare, deliberat (032) — sunt răspunsuri despre UN produs, nu semnal de potrivire.

**FAQ de magazin: 20 de intrări, luate de pe paginile REALE sole.ro** (`db/seed/faqs_sole_ro.json`,
cu `source_url` pe fiecare), scrise cu `embedding` NULL ⇒ lookup-ul încă nu le servește.

Setul demo **nu se putea copia**, și e o capcană care merită numită: cifrele lui erau plauzibile,
nu adevărate.

| | demo | SOLE (verificat) |
|---|---|---|
| prag transport gratuit | 200 lei | **199 lei** client nou / **149 lei** recurent |
| cost livrare | 19,99 lei | **19,9 până la 24,90 lei** |
| curier | „curier rapid" | **Fan Courier, Sameday, Cargus** |
| retur | 14 zile | **30 zile**, prin **service-return.com** |
| cost retur | fără cifră | **49,9 lei**, reținuți din rambursare |
| cosmetic deschis | **REFUZAT** (Art. 16 lit. e) | **acceptat** dacă n-are urme de utilizare |
| plată | card + ramburs | + **PayPal** + **rate 0%** la 6 bănci |

Penultimul rând e cel care contează: răspunsul demo REFUZĂ, citând legea, un retur pe care
magazinul îl acceptă. Un client ar fi plecat convins că nu are ce face. Iar „200 lei" i-ar spune
unuia cu coșul la 199,50 că mai are de adăugat.

Ce n-are sursă verificată **nu e în set**: anularea comenzii, confirmarea prin e-mail, contul și
parola, comisionul de ramburs, termenul exact de rambursare. Un răspuns plauzibil e mai periculos
decât o lipsă: lipsa produce un refuz onest, plauzibilul produce o minciună cu formă de fapt.

---

## 9. Decizii deschise

**Închise de owner (2026-08-28):**

- ~~Recenzii: plafon sau toate?~~ → **toate 183.003.** Import lossless.
- ~~Ce excludem la import?~~ → **nimic.** Proveniența în schemă, filtrarea la citire.
- ~~Imaginile: redimensionate sau originale?~~ → **originale, o poză per produs, pe VPS.**
- ~~Schemă consolidată sau replay al migrărilor?~~ → **consolidată**, generată din baza live
  (`dump_schema_ddl.py`), cu `schema_migrations` seedat ca poarta de boot NX-123 să nu ceară
  re-rularea celor 42.

**Rămâne deschisă una singură:**

1. **SOLE e clientul, sau sunt date de piață?** Importul e lossless în ambele cazuri, deci nu
   blochează scrierea nici un pic. Ce decide e **politica de citire**: dacă SOLE e clientul,
   secțiunile `source='aura'` pot fi servite ca text curated cu proveniență; dacă sunt date de
   piață, rămân stocate dar nerostite, iar textul afișat se generează din semnale în
   `product_card_blurbs`. Aceeași întrebare se aplică fotografiilor găzduite pe domeniul nostru.

   Fiindcă e o poartă de citire și nu una de scriere, **importul poate porni înainte de răspuns.**

2. **Re-diacritizarea numelor** (§5.7): le lăsăm ca în sursă și acceptăm „masca de fata" în
   carduri, sau construim un dicționar? Nu blochează nimic.

---

## 10. Ordinea de execuție (după decizii)

1. `schema_v3.sql` consolidat + roluri + RLS + extensii + partiții + seed safety → proiect nou
2. Importer: `sole_data.db` → `source_products_raw` → tabele canonice, idempotent pe `external_id`,
   cu `catalog_sync_runs` + `catalog_quality_alerts` pornite din prima rulare
3. Extracția F1 → `product_evidence_chunks`; F2 → `product_derived_signals`
4. Redimensionare (nu) + upload imagini pe VPS + `product_images`
5. Recenzii + `product_review_summaries`
6. `product_search_documents` + `product_card_blurbs` (derivate, versionate)
7. Embeddings, o singură trecere
8. Corpusul NX-203 din pool-ul de 10.237 de întrebări

---

## 11. Legarea sistemului de baza nouă (2026-08-28)

Importul umpluse baza, dar nimic din cod nu arăta spre ea. Cinci lucruri lipseau; toate sunt acum
făcute, iar ce nu s-a putut face aici e numit exact mai jos, nu presupus.

**Făcut și verificat:**

| pas | rezultat |
|---|---|
| `.env` mutat pe proiectul nou | `SUPABASE_DB_URL` (rol `postgres`) + `DATABASE_URL_BOT` (rol `bot_runtime`, fără `bypassrls`) + `DB_ISOLATION_ASSERT=strict`. Configul vechi rămâne în `.env.bak.old-project` (gitignored) |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | COMENTATE, nu șterse: Data API e stins pe proiectul nou, iar valorile vechi refolosite din reflex ar scrie în bucketul ALTUI proiect |
| `SEED_BUSINESS_SLUG` | `nativex-demo` → `sole-ro` |
| `WEB_ENABLED=true` | lipsea din `.env`, deci routerul `/web/*` nu era montat deloc: `GET /web/bootstrap` întorcea **404**, nu „flag stins" |
| canal `webchat` creat | `channel_id=ae254f0f-…`, `public_token=pub_b738dd1a…`. Fără el, `resolve_channel` întoarce `None` și nicio conversație nu se poate deschide |
| partiții lunare | `messages_2026_08/09`, `analytics_events_2026_08/09` — `partition_maintenance` rulat pe baza nouă: `created=0` (existau), `default_rows=0` |

**Retrieval, măsurat pe calea reală (`search_products_lexical`, conexiune `bot_runtime` + RLS):**

| query | rezultate |
|---|---|
| `sampon` / `șampon` | 50 / 50 — identice, deci `ro_unaccent` ține în ambele capete |
| `mască de față` | 50 |
| `ser cu vitamina C` | 26 |
| `crema hidratanta` | 8 |
| `parfum barbati` | **0** — gol de CONȚINUT, nu de cod: catalogul SOLE n-are parfumerie |
| același query, alt `business_id` pe conexiunea `sole-ro` | **0** (izolarea ține) |

> **Atenție la funcția pe care o testezi.** `search_products` (numele evident) filtrează cu
> `p.name ilike '%q%'` și dă **0** pe „șampon" — e calea veche. Calea reală a tool-ului e
> `search_products_lexical` (FTS + trgm, ambele prin `ro_unaccent`) plus brațul semantic, unite
> prin RRF. O măsurătoare pe funcția greșită arată ca o regresie NX-178 care nu există.

**Două lucruri găsite rulând suita de teste pe baza nouă** (n-ar fi ieșit altfel):

1. **`set role bot_runtime` era REFUZAT.** Supabase creează singur apartenența
   `postgres → bot_runtime`, dar cu `set_option = false`; pe PG16+ opțiunile apartenenței sunt
   separate, iar `WITH ADMIN` nu dă dreptul de a INTRA în rol. Un `grant bot_runtime to postgres`
   simplu se contopește cu rândul existent și nu repară nimic — trebuie
   `with inherit true, set true`. Proiectul VECHI avea rândul corect (două rânduri, doi grantori);
   diferența a ieșit doar comparându-le. Contează fiindcă exact testele care verifică izolarea RLS
   coboară la rol așa, deci fără grant plasa de izolare rămâne NEVERIFICATĂ, iar eșecul seamănă cu
   „testul e stricat".
2. **Tenantul-fixtură al testelor lipsea.** ~80 de fișiere din `tests/` au UUID-ul demo scris în
   clar și îl folosesc drept ancoră pentru rândurile pe care și le creează. Fără el cad pe
   `channels_business_id_fkey`. `scripts/seed_test_tenant.py` îl creează explicit ca artefact de
   test (`status='paused'`, fără catalog).

**NU s-a putut face aici, cu motiv:**

- **Un tur real end-to-end.** `/web/bootstrap` întoarce acum **429 `rate limited`**, nu 404: ruta e
  montată și ajunge la limitator, dar limitatorul e `fail_closed` și Redis nu e accesibil de pe
  mașina asta (`REDIS_URL` arată spre hostul `redis` din compose, iar Docker nu e instalat).
  Turul cere Redis (admission + rate limit + dedupe) ȘI OpenAI. Se rulează pe VPS.
- **Embeddinguri** (`python -m src.jobs.embed_products`, ~$0.03) — le rulează owner-ul.

**Rămâne de CONSTRUIT (nu de rulat), fiindcă fiecare cere o decizie, nu o comandă:**

1. **F2 → `product_derived_signals`.** Proza AURA e text liber, cu bullet-uri în română naturală.
   Un extractor pe liste de cuvinte ar contrazice regula proiectului („model + context, nu
   wordlists") și, mai grav, vocabularul de nevoi vine din `DomainPack` (P9), iar `sole-ro` are
   `settings = {}`: nu există încă vocabularul în care s-ar scrie semnalele. Ordinea corectă e
   domain pack întâi, extractor după.
2. **`product_review_summaries` din recenzii REALE.** `scripts/summarize_reviews.py` există, dar e
   scris pentru recenziile FICTIVE ale demoului: inventează rezumatul cu LLM și **variază ratingul**
   ca să nu fie toate 5★. Pe 183.003 recenzii adevărate, varierea ratingului ar fi falsificare.
   Cere un job nou, cu agregare determinist ancorată în rânduri.
3. ~~**`faqs` la nivel de business.**~~ **FĂCUT** (§8.1): 20 de intrări de pe paginile reale
   sole.ro, în așteptarea embeddingului. `intent_aliases` rămâne gol - se umple din shadow mode,
   pe trafic real, nu prin seed.

### 11.1 Ce se schimbă în suita de teste

Mutarea a spart o confuzie veche: UUID-ul demo era simultan **cuiul** de care testele își agață
rândurile (canal throwaway, conversație, evenimente) și **tenantul cu catalog**. Pe baza nouă
catalogul e sub `sole-ro`, iar rândul demo a rămas doar un cui. `tests/tenants.py` le separă
(`CATALOG_BIZ` / `FIXTURE_BIZ`, ambele suprascriptibile din mediu).

Măsurat pe suită, cu flagurile ca în CI (`--ignore=tests/e2e`): **21 → 0 eșecuri** legate de baza
nouă. Cu flagurile locale APRINSE (`.env` are 16 `*_ENABLED=true`) numărul de roșii e alt ordin de
mărime (208) — e diferența cunoscută dintre local și CI, nu o regresie; vezi nota din
`docs/PROJECT_STATUS.md` despre stiva aprinsă local.

Un test a devenit `skip`, deliberat: `test_price_is_min_variant_when_product_has_variants`.
Contractul „prețul afișat = min-variantă" e intact, dar SOLE n-are variante (§5.9), deci catalogul
nu-l poate exercita. Un roșu permanent ar fi învățat pe toată lumea să ignore fișierul.

### 11.2 Ce rulează owner-ul, în ordine

```bash
# 1. embeddinguri pe cele 2.758 de produse (~$0,03) — bratul semantic al RRF
python -m src.jobs.embed_products

# 2. embed pe cele 20 de FAQ de magazin (aceeasi rulare, cost neglijabil). FARA `--no-embed`,
#    fiindca lookup-ul cere `embedding is not null` — pana atunci randurile exista si TAC.
python -m src.jobs.seed_faqs --business sole-ro --source db/seed/faqs_sole_ro.json

# 3. verificare rapida ca s-au scris
python scripts/db_check.py            # sau: select count(*) from product_embeddings

# 4. audit conversational pe calea /web/chat REALA, LOCAL: `web_audit.py` injecteaza
#    `fakeredis`, deci nu cere Docker. Cere OpenAI. Isi curata singur vizitatorii.
PYTHONPATH=. python scripts/sim/web_audit.py --only discovery
PYTHONPATH=. python scripts/sim/web_audit.py            # toate cele 9 scenarii
```

Auditul își ia tenantul din `SEED_BUSINESS_SLUG` (sau `--business sole-ro`) și întoarce exit code
non-zero la orice finding P0 — e gate de regresie, nu doar o privire.

Pasul 4 e primul moment în care sistemul chiar VORBEȘTE pe catalogul SOLE. Până atunci, tot ce e
verificat mai sus e infrastructură: că poate ajunge la date, nu că știe ce să spună despre ele.

---

## 12. Auditul de retrieval pe catalogul real (2026-08-28)

§11 a verificat că sistemul AJUNGE la date. Secțiunea asta verifică ce se întâmplă când chiar
CAUTĂ în ele, cu fraze scrise ca de client, nu cu cuvinte-cheie. Toate cifrele sunt măsurate pe
baza live, pe conexiunea `bot_runtime` — deci cu RLS activ, ca în producție.

### 12.1 Defectul principal: căutarea întorcea tăcere pe majoritatea frazelor

Din 18 formulări realiste, **13 întorceau ZERO rezultate**. Nu rezultate slabe: zero, adică exact
ce interzice P6.

| interogare | înainte | după |
|---|---|---|
| `produse pentru acnee` | **0** | 50 |
| `ce imi recomanzi pentru riduri` | **0** | 42 |
| `ceva pentru cearcane` | **0** | 18 |
| `sampon pentru par gras` | **0** | 5 |
| `fond de ten pentru ten gras` | **0** | 13 |
| `crema hidratanta pentru ten uscat` | 1 | 21 |
| `sampoon` (typo) | **0** | 50 |
| **zerouri, total** | **13/18** | **0/18** |
| **latență medie (wall, prin pooler)** | 289 ms | **186 ms** |
| **timp de execuție în Postgres, același set** | 219 ms | **26 ms** |

Trei cauze independente, toate reparate:

1. **`search_tsv` conținea doar numele produsului.** Coloana generată (015, re-generată în 033) e
   `name || ai_summary`, iar pe catalogul SOLE **`ai_summary` e NULL pe toate cele 2.758 de
   rânduri**. `description` există pe 2.758/2.758 și nu era indexată nicăieri. Numele unui produs
   de cosmetice nu conține „acnee" sau „riduri" — descrierea da. Reparat de
   [`046_search_tsv_description.sql`](046_search_tsv_description.sql), care adaugă descrierea și
   pune **ponderi pe câmp** (nume A / `ai_summary` B / descriere C), ca o mențiune în descriere să
   nu rangueze la fel cu numele produsului.
2. **`websearch_to_tsquery` leagă toate cuvintele cu ȘI**, iar configurația `'simple'` nu elimină
   niciun cuvânt gol: „sampon pentru par gras" cerea ca produsul să conțină literal și „pentru".
   Configurația `'romanian'` NU repară asta — lista ei de cuvinte goale e scrisă CU diacritice, iar
   noi indexăm text trecut prin `ro_unaccent` (verificat cu `ts_debug`: „pentru", „si", „ceva" trec
   neatinse prin ea). Reparat în read-path:
   [`src/catalog/query_terms.py`](../src/catalog/query_terms.py), pur, cu lista indexată pe
   **locale** (P11), plus o scară strict → relaxat → typo în `search_products_lexical`.
3. **Brațul de typo nu funcționa și era cel mai scump lucru din căutare.** `similarity()` compara
   interogarea cu numele ÎNTREG, iar numele SOLE au 60-90 de caractere: un typo de 6 litere are
   similaritate ~0,05 cu un nume de 80. Măsurat, `%` prindea **zero** din typo-urile testate, dar
   costa ~220 ms pe FIECARE căutare (evaluat pe toate cele 2.758 de rânduri). Înlocuit cu
   `word_similarity` (`<%`, compară cu cel mai bun CUVÂNT), mutat pe treapta 3 — se plătește doar
   când altfel am fi întors tăcere. De aici vine câștigul de viteză: **219 ms → 26 ms** timp de
   execuție în Postgres pentru ACELAȘI set de 21 de rezultate. Diferența dintre asta și cifra
   wall-clock de mai sus e latența de rețea către eu-west-2, care nu ține de query.

### 12.2 Indexurile GIN sunt INERTE pe conexiunea de runtime

Același query, aceeași bază, două roluri:

| rol | plan | timp |
|---|---|---|
| `postgres` (fără RLS) | `Bitmap Index Scan on idx_products_name_ro_trgm` | 109 ms |
| `bot_runtime` (cu RLS) | **`Seq Scan`, `Rows Removed by Filter: 2758`** | 186 ms |

Cauza e o regulă de Postgres, nu un index lipsă: cu RLS activ, predicatele **non-leakproof** ale
interogării (`@@` pentru FTS, `%`/`<%` pentru trigrame) nu pot fi evaluate înaintea predicatelor de
securitate, deci **nu pot deveni condiții de index**. Rămân filtre pe rând.

E o proprietate STRUCTURALĂ a alegerii „RLS ca plasă", nu un defect de scriere. La 2.758 de produse
costul e suportabil — și e exact motivul pentru care 046 câștigă atât de mult prin simpla SCOATERE
a funcției scumpe de pe drumul obișnuit. Consecința de urmărit: costul crește liniar cu numărul de
produse, deci un tenant cu 20.000 de produse va simți asta. Cele două ieșiri —
`ALTER FUNCTION ... LEAKPROOF` pe operatorii de căutare (decizie de SECURITATE, globală pe bază) sau
un drum de citire fără RLS — sunt amândouă decizii de arhitectură, nu ajustări. **Nu se iau aici.**

### 12.3 Ce e GOL și ce consecință are, măsurat

Fiecare gol dezactivează TĂCUT o capacitate:

| tabel / coloană | rânduri | ce nu funcționează din cauza asta |
|---|---|---|
| `product_embeddings` | **0** | brațul semantic al RRF; `has_embeddings` → fals, deci fuziunea rulează cu un singur braț |
| `products.ai_summary` | **0/2.758** | greutatea B din `search_tsv`; textul scurt de card |
| `products.attributes->'concerns'` | **0** | filtrul `concerns`, `_facet_filter_clause`, `_feature_clause` și boost-ul de concern din rerank — toate au 0 valori pe care să opereze. `attributes` are doar 6 chei, toate tehnice: `sku`, `mpn`, `compliance`, `volume_raw`, `routine_time`, `price_per_unit_source` |
| `product_derived_signals` | **0** | sursa din care s-ar scrie fațetele de mai sus (F2 → semnale, §11) |
| `product_review_summaries` | **0** | `top_pros` / `review_pro` ies NULL pe orice card, deși există **183.003** recenzii reale |
| `product_card_blurbs` | **0** | textul scurt derivat; codul refuză corect să cadă pe numele produsului |
| `product_relations` | **0** | `traverse_relations` → 0 noduri. **Cele 391 de produse `out_of_stock` n-au niciun substitut**, deci „nu mai avem" e răspunsul final pentru toate — exact situația pentru care s-a construit NX-195 |
| `product_category_map` | **0** | a doua ramură din `_category_clause` (arborele). Prima ramură (`primary_category_id` + descendenți pe `path`) funcționează: 45 de categorii, `path` completat pe toate |
| `faqs` | 20, **0 cu embedding** | lookup-ul cere `embedding is not null`, deci rândurile există și TAC până la rularea de embed |
| `intent_aliases` | **0** | ruta gratuită pe alias (stagiul 4) |

Ce FUNCȚIONEAZĂ pe date reale, verificat: cross-sell-ul (`get_complementary_products` cade pe
euristica same-brand și întoarce 4 produse coerente ale aceluiași brand, din categorii diferite),
arborele de categorii, izolarea pe tenant, și agregarea recenziilor per produs (bitmap index,
0,24 ms pe un produs cu 309 recenzii).

### 12.4 Un risc de PRECIZIE introdus deliberat, cu marcaj

Treapta de relaxare (ȘI → SAU) transformă tăcerea în „cel mai apropiat lucru". Pe cele 18
interogări măsurate, singura pe care a servit-o a fost `parfum de dama` — un raft pe care SOLE nu
îl are — iar rezultatul a fost un spray parfumat de păr. Înainte, răspunsul ar fi fost onest
(„n-am găsit"); acum e un produs vag înrudit.

De aceea fiecare produs servit de pe o treaptă degradată poartă `lexical_step`
(`relaxed` | `fuzzy`), iar evenimentul `product_search` îl publică. Marcajul e SEPARAT de `relaxed`
/ `relax_depth`, care descriu relaxarea FILTRELOR: confundarea celor două ar ascunde exact cazul
periculos — o cerere fără niciun filtru, servită din plasa de typo.

Poarta care ar respinge un rezultat off-target nu există încă:
`search_offcategory_guard_enabled` se declanșează doar când scara a renunțat la o CATEGORIE cerută
explicit, iar NX-257 a livrat porți de adevăr, nu de potrivire. **Deci semnalul se măsoară acum și
se decide pe măsurători** (D15), nu se presupune.

### 12.5 Al doilea defect găsit: `UNKNOWN nu e 0` respectat la produs, încălcat la variantă

`import_sole.py` scria literal `0` în `product_variants.stock`, deși la nivel de produs scria
corect `NULL` cu comentariul „sursa da doar binar. UNKNOWN nu e 0". Măsurat pe baza live:

| coloană | valoare | rânduri |
|---|---|---|
| `products.stock_total` | `NULL` (corect) | 2.758 / 2.758 |
| `product_variants.stock` | **`0`** | 2.755 / 2.755 |

Consecința nu e cosmetică. `src/commerce/facts_provider.py` tratează un stoc **cunoscut** 0 pe
variantă drept `out_of_stock` pentru acea variantă — „faptul mai specific bate faptul produsului",
ceea ce e regula corectă. Cu zero scris peste tot, **2.364 din cele 2.367 de produse `in_stock` se
prezentau ca epuizate** revalidării de coș (NX-237) și faptelor turului (NX-240), iar modelul
primea `stoc 0` pe marfă care există. Cu `CONVERSATION_CART_ENABLED` aprins, fiecare adăugare în coș
ar fi fost respinsă ca „fără stoc" — pe aproape tot catalogul.

De ce n-a ieșit la niciun test: toate suitele care ating stocul folosesc fixture-uri, iar
importatorul n-avea niciun test pe SQL-ul pe care îl scrie. Verificările de import (§ „capcanele de
adevăr") au controlat `products.stock_total` și au trecut — nivelul variantei n-a fost întrebat.

Reparat: importatorul scrie `null` și include `stock` în `do update` (o re-rulare REPARĂ rândurile
vechi, altfel importul idempotent ar păstra zeroul la infinit), plus un `UPDATE` unic pe rândurile
deja scrise. Regresie: `test_importul_scrie_stoc_UNKNOWN_pe_varianta_nu_zero`.

**Al treilea gol, de alt fel: datele EXISTĂ, dar calea de citire le filtrează pe toate.**
`ingredients_db` (ce vede modelul despre compoziție) se hidratează dintr-un lateral cu
`where pi.is_key`, iar importul a scris `is_key = false` pe **toate cele 93.887** de legături. Deci
ingredientele ies goale pe orice produs, deși există 5.818 ingrediente normalizate cu `inci_name`
completat 5.818/5.818. La „conține alcool?" sau „are parfum?" nu există fapt structurat de citit.

Filtrul NU e greșit: „key ingredients" înseamnă ingredientele-erou, nu lista INCI completă (35 de
rânduri per produs în vederea modelului ar fi zgomot, nu informație). Ce lipsește e legătura.
Sursa o are — secțiunea `Ingrediente-cheie` (familia F1) e importată și conține exact asta
(„Ulei de argan 100% pur / Acizi grasi omega / Vitamina E"). Dar potrivirea ei cu rândurile din
`ingredients` cere o decizie, nu un `UPDATE`: textul e comercial („Ulei de argan"), iar `inci_name`
e chimic („Argania Spinosa Kernel Oil"). De aceea rămâne aici ca finding, nu ca reparație:
`is_key` pus pe `true` fără potrivire ar umple vederea modelului cu lista INCI întreagă, ceea ce e
mai rău decât gol.

### 12.6 Ordinea în care se închid golurile

Ordonată după cât cumpără fiecare pas, nu după cât e de ușor:

1. **`python -m src.jobs.embed_products`** (~$0,03, îl rulează owner-ul) — al doilea braț al RRF.
   Singurul pas care e o comandă, nu o construcție.
2. ~~**`domain_pack` pentru `sole-ro`**~~ — **LIVRAT 2026-09-02, vezi §13.** Fără vocabularul de
   nevoi nu existau chei canonice în care să se scrie semnalele, deci extractorul F2 n-avea unde
   scrie. 20 de chei derivate din 12.665 de fraze reale de căutare, fiecare confruntată cu marfa.
3. **F2 → `product_derived_signals`** → de aici se umplu `concerns` și fațetele, care deblochează
   filtrarea, boost-ul de rerank și `product_card_blurbs`.
4. **`product_review_summaries`** din cele 183.003 recenzii, cu agregare DETERMINISTĂ (§11 explică
   de ce scriptul existent nu se poate refolosi: variază ratingul).
5. **`product_relations`** — substitute pentru cele 391 de produse epuizate, apoi complement/rutină.
6. **`intent_aliases`** — ruta gratuită, după ce traficul real arată ce se cere des.

---

## 13. Vocabularul de domeniu al lui `sole-ro` (2026-09-02)

Pasul 2 din §12.6. Până acum `businesses.settings` era `{}`, iar `vertical='ecommerce'` încărca
[`src/domain/defaults/ecommerce.json`](../src/domain/defaults/ecommerce.json), care are
`concern_map` **gol**, zero fațete, zero `query_expansions` și zero `relation_kinds`. Consecința nu
era o eroare, ci absența tăcută a unui vocabular: o nevoie rostită de client n-avea cheie canonică
în care să fie ținută minte (`src/conversation/needs.py`, P9), tabelul de comparație n-avea rânduri
de domeniu, iar extractorul F2 n-avea unde să scrie semnalele.

Pachetul e [`db/seed/domain_pack_sole_ro.json`](../db/seed/domain_pack_sole_ro.json), scris cu
`python scripts/set_domain_pack.py --business sole-ro --pack <fișier> --apply` (dry-run implicit).
**Aplicat pe tenant, 2026-09-02.**

### 13.1 Derivat din catalog, nu copiat din pachetul demo

`beauty_salon.json` descrie catalogul DEMO. Confruntat cu marfa reală, vocabularul lui e în urmă:
n-are pori, barieră cutanată, roșeață, luminozitate, fermitate, cearcăne sau scalp, deși toate sunt
printre cele mai cerute lucruri din catalogul SOLE.

Sursa de derivare există în baza importată: `product_sections` cu `source='aura'` conține
`recommendation_trigger` — **12.665 de fraze de căutare reale**, ~5 per produs, plus tot atâtea
bulleturi `fit` („cui i se potrivește"). Din ele au ieșit 20 de chei canonice și 87 de fraze
colocviale. Fiecare cheie a fost apoi CONFRUNTATĂ cu catalogul: numărul de produse care chiar
servesc nevoia. O nevoie fără marfă nu intră în vocabular, fiindcă e o promisiune care întoarce zero.

| cheie | produse | | cheie | produse |
|---|---|---|---|---|
| `hydration` | 2.139 | | `acne` | 516 |
| `dry` | 1.464 | | `pores` | 370 |
| `oily` | 1.266 | | `combination` | 244 |
| `sensitive` | 1.207 | | `hair_dryness` | 157 |
| `dullness` | 1.171 | | `normal` | 141 |
| `barrier` | 1.117 | | `hair_damage` | 110 |
| `redness` | 1.093 | | `scalp_care` | 109 |
| `sun_protection` | 1.092 | | `dark_circles` | 103 |
| `anti_aging` | 811 | | `hair_loss` | 57 |
| `hyperpigmentation` | 805 | | `dandruff` | **12** |

`dandruff` la 12 produse e cel mai subțire rând și rămâne în vocabular tocmai fiindcă e adevărat:
pe catalogul demo, „mătreață" avea **zero** produse și botul nu putea nici măcar să spună onest ce
are. Douăsprezece e puțin, dar e marfă.

### 13.2 Tipul de ten e o fațetă SEPARATĂ de nevoi, și asta e decizia centrală

Pachetul demo le amestecă într-o singură listă `concerns` cu valori
`[oily, dry, combination, sensitive, normal, acne, anti_aging, ...]`. Codul are însă de mult
distincția care contează (NX-257, `domain/facets._BINDING_KINDS`):

- `partitioning` — cumpărătorul are exact UNA dintre valori (tipul de ten). Un produs cu altă
  valoare îl CONTRAZICE, deci excluderea e corectă.
- `additive` — valoarea e un obiectiv (riduri, luminozitate). Un produs care nu-l țintește nu
  contrazice nimic, deci nu se exclude niciodată, doar se depunctează la ranking.

Într-o listă amestecată binding-ul nu se poate declara: ori tratezi „riduri" ca pe un tip de ten și
excluzi produse care nu contrazic nimic, ori tratezi tipul de ten ca pe un obiectiv și nu excluzi
niciodată. Pachetul SOLE declară `skin_type` (`partitioning`, 5 valori) separat de `concerns`
(`additive`, 15 valori). Contractul e scris acum, ca extractorul F2 să aibă unde scrie.

### 13.3 Ce e VIU azi și ce se aprinde singur

Sub `min_coverage` nu se aplică nimic ([`src/agent/relevance_gate.py`](../src/agent/relevance_gate.py)),
deci fațetele a căror sursă e goală sunt inerte, nu stricate. Ele se aprind fără schimbare de cod
când F2 scrie `attributes`.

| ce | stare azi |
|---|---|
| `concern_map` (vocabular de nevoi, `needs.py`) | **viu** |
| `response_style` (intră în prompturi) | **viu** |
| fațeta `routine_time` (`am` / `pm` / `am_pm`) | **vie**, 2.393/2.758 = 86,8% |
| `price`, `category` | vii |
| `skin_type`, `concerns`, `key_ingredients`, `spf`, `texture` | inerte până la F2 |
| `relation_kinds` | inert până la `product_relations` |
| `required_attributes` | citit doar de auditul de catalog |

Adăugarea `concern_map`-ului **nu produce nicio regresie de căutare**, verificat pe baza live: un
termen ca „ten gras" era `UNKNOWN(not_in_vocabulary)` înainte și e `UNKNOWN(overlay_target_dead)`
acum. În ambele cazuri zero filtru și aceeași dezvăluire onestă către model, dar al doilea motiv e
alarma de drift de configurare pe care codul o aștepta.

### 13.4 `query_expansions` rămâne GOL, pe măsurătoare

Asta era partea de la care așteptam cel mai mult, fiindcă hrănește direct căutarea lexicală
reparată de 046. Un set de 27 de expandări a fost construit din vocabularul real și rulat pe baza
live. **Niciuna n-a îmbunătățit vreo interogare. Toate au înrăutățit-o.**

Cauza e în consumator, nu în vocabular: `query_rewrite` concatenează termenii în `search_text`, iar
`search_products_lexical` îi trece prin `content_terms` și leagă TOT cu ȘI pe treapta `strict`.
Deci o expandare poate doar să îngusteze.

| interogare | brut | expandat |
|---|---|---|
| `am cearcane` | 18 `strict` | **1** `strict` |
| `pete pigmentare pe fata` | 41 `strict` | **3** `strict` |
| `bariera afectata` | 29 `strict` | **1** `strict` |
| `pori dilatati` | 26 `strict` | **2** `strict` |
| `puncte negre` | 22 `strict` | **1** `strict` |
| `ten tern fara stralucire` | 12 `strict` | **1** `strict` |
| `crema pentru riduri` | 20 `strict` | 50 **`relaxed`** |
| `ten iritat si cu roseata` | 5 `strict` | 50 **`relaxed`** |
| `caderea parului` | 17 `strict` | 50 **`relaxed`** |
| `toata rutina` | 17 `strict` | 50 **`relaxed`** |

Numărul de rezultate minte aici, treapta nu: toate cele 11 interogări erau servite `strict` înainte.
Unde strictul încă potrivește, expandarea sufocă. Unde nu mai potrivește, turul cade pe `relaxed`,
adică pe SAU, și primește 50 de rezultate vagi — aceeași degradare de precizie descrisă în §12.4,
declanșată acum de propria noastră configurare.

**Reparația nu e în pachet.** Expandarea trebuie să lărgească ramura SAU și ranking-ul, NICIODATĂ
conjuncția strictă, adică o schimbare în scara lexicală, cu măsurătoarea ei. Până atunci orice
intrare în `query_expansions` degradează retrievalul, iar asta e valabil și pentru cele 13 expandări
pe care `beauty_salon.json` le trimite azi tenantului demo: defectul nu e nou, doar n-a fost măsurat.

Decizia e pinuită de
`tests/test_domain_pack_sole.py::test_expandarile_raman_goale_pana_cand_scara_lexicala_se_repara`,
care cere și explicația: o valoare goală fără motiv se reumple.

### 13.5 Două lucruri găsite pe drum

- **`purpose: "recovery"` nu există.** Prima versiune a pachetului declara muchia `substitute` cu
  un `purpose` inventat. `RelationPurpose` are exact două valori (`upsell` / `requirement`), iar
  distincția e despre SUPRIMARE, nu despre motiv comercial. Loader-ul a respins muchia fail-closed
  și a logat, comportament corect, dar în producție diferența dintre „am declarat" și „rulează" e
  invizibilă. Exact muchia care salvează un produs epuizat ar fi căzut tăcut. Prins de
  `test_loaderul_nu_arunca_nimic_din_pachet`.
- **Vocabularul descoperit conține chei tehnice.** `load_vocabulary` întoarce ca dimensiuni
  filtrabile `sku`, `compliance`, `volume_raw`, `price_per_unit_source` — tot ce e cheie în
  `attributes`. Nu e periculos azi (rezolvarea cere potrivire pe VALOARE), dar spațiul de fațete al
  unui tenant nu ar trebui să conțină identificatori interni. Rămâne finding, nu reparație.

### 13.6 Cum se verifică, oricând

```bash
python scripts/set_domain_pack.py --business sole-ro --check     # confruntă ce e ÎN DB cu catalogul
python -m pytest tests/test_domain_pack_sole.py -q               # invarianții, fără DB
```

`--check` iese non-zero dacă vreo nevoie a scăzut sub prag sau dacă loader-ul aruncă ceva. Pachetul
nu îmbătrânește singur, dar catalogul se mișcă sub el.
