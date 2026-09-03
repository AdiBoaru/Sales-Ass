# Calitatea recomandării pe catalogul SOLE — audit măsurat și plan (Wave H)

**Data auditului:** 2026-09-03 · **Catalog:** import SOLE 2026-08-28, migrări 003-047
**Tenant:** `sole-ro` / `99fe1292-f9ed-469e-8183-f994ea5b59c0` · **Toate cifrele: măsurate live, read-only**

Documentul ăsta ține dovezile pe care se sprijină cardurile NX-264 … NX-272. Cifrele nu sunt
estimări și nu se copiază din documentație — fiecare are comanda care o reproduce.

---

## 1. Premisa: porți de adevăr, niciuna de potrivire

Sistemul verifică peste tot că nu minte: validatorul stagiului 8 (preț, produs, link),
`grounding_guard` (NX-240), safety (NX-173), fereastra de promoție. Nimic nu verifică că produsul
**e ce a cerut clientul**.

De aceea poate recomanda onest un ser de ten uscat cuiva cu ten gras: fiecare propoziție
verificabilă, întregul greșit. Nu e halucinație, e irelevanță, iar irelevanța nu apare în niciun log
de eroare — apare doar în faptul că omul nu cumpără.

Mecanismul de potrivire există (NX-257, `RELEVANCE_MASK_ENABLED`) și e stins dintr-un singur motiv:
fațetele pe care s-ar sprijini sunt la 0%.

---

## 2. Starea măsurată a datelor

### 2.1 Ce există

| | |
|---|---|
| produse active | 2.758 (toate `published`) |
| `product_embeddings` | **2.758** (`doc_type='product'`, `text-embedding-3-small`) |
| `product_search_documents` | 2.758, medie **1.835** caractere |
| `description` | 2.758, medie 1.618 caractere |
| `product_sections` | 43.761 |
| recenzii | 183.003 |
| `product_faqs` | 27.931 |
| variante | 2.755 |

CLAUDE.md spunea `product_embeddings = 0`. **E depășit** — există toate, deci brațul semantic e viu.

### 2.2 Ce lipsește, și consecința fiecărui gol

| tabel | rânduri | consecință |
|---|---|---|
| `product_relations` | **0** | cele 391 de produse epuizate n-au niciun substitut; `routine_next` inert; complementul cade pe „același brand" |
| `product_derived_signals` | 0 | zero blurb-uri de card |
| `product_review_summaries` | 0 | `top_pros` NULL pe orice card, peste 183.003 recenzii reale |
| `intent_aliases` | 0 | fără scurtături de rutare |
| `faqs.embedding` | 0/20 | cele 20 de FAQ reale nu sunt servite (lookup-ul cere `embedding is not null`) |

### 2.3 Acoperirea fațetelor (`scripts/facet_coverage.py`)

| fațetă | acoperire | `enforce_ready` |
|---|---|---|
| price, category | 100% | da |
| routine_time | 86,8% | da |
| **skin_type, concerns, key_ingredients, fragrance_free, spf, texture** | **0%** | nu |

Șase din nouă fațete declarate sunt goale. Vocabularul există (20 de chei canonice, 87 de fraze,
derivate din 12.665 de fraze reale de căutare), dar niciun produs nu poartă valorile: rezoluția unei
nevoi rostite e `UNKNOWN(overlay_target_dead)`.

---

## 3. Semnalul e în secțiunile `aura`, nu în nume/descriere

Aceeași nevoie, două surse:

| „ten gras" | produse |
|---|---|
| regex pe `name + description` | **26** |
| `concern_map` pe `fit`/`problem`/`recommendation_trigger` | **1.078** |

De 40 de ori mai mult semnal, în același rând. Descrierea e textul de magazin; nevoia e scrisă în
secțiunile semantice. Orice derivare care citește doar `name + description` ratează catalogul.

Secțiunile disponibile, per produs: `fit` (2.535), `anti_fit` (2.535), `problem` (2.533), `purpose`,
`questions`, `recommendation_trigger`, `routine_integration`, `comparison`, `editorial`,
`key_ingredients` (2.734), `composition` (2.643).

`recommendation_trigger` conține literal frazele de căutare:
„deodorant roll-on natural pentru barbati", „deodorant cu ulei de cedru organic".

---

## 4. Derivarea deterministă: rezultat măsurat (dry-run)

`python scripts/derive_product_attributes.py --business <uuid>` — zero scrieri.

| fațetă | produse | acoperire |
|---|---|---|
| concerns | 2.354 | 85,4% |
| skin_type | 1.702 | 61,7% |
| key_ingredients | 2.734 | 99,1% |
| spf | 182 | 6,6% |
| ~~texture~~ | ~~2.173~~ | ~~78,8%~~ (nereproductibil, vezi mai jos) |
| ~~fragrance_free~~ | ~~117~~ | ~~4,2%~~ (nereproductibil, vezi mai jos) |

> **Ultimele două cifre au fost șterse de NX-264 și nu se mai pot reproduce.** Le producea o listă
> de cuvinte de beauty scrisă direct în script (`TEXTURE_TERMS`, `FRAGRANCE_FREE_RE`). Azi ambele
> fațete derivă 0%, fiindcă pachetul nu le declară valorile. Rămân notate ca **plafon orientativ**
> pentru ce ar trebui să recupereze valorile ratificate din `scripts/facet_discovery.py`.

**306 produse (11,1%) rămân fără nicio nevoie**, concentrate acolo unde nevoia nu se aplică:
Sprâncene 93%, Accesorii 79%, Ochi 70%. Multe n-au deloc secțiuni `aura` — doar
`composition`/`description`/`storage`/`usage`.

> **Consecință pentru decizia „determinist vs LLM": un model n-ar extrage nimic din ele, pentru că
> nu există text din care să extragă.** Golul e de conținut în sursă, nu de reguli. Derivarea rămâne
> deterministă; hibridul nu se justifică.

### 4.1 Trei defecte găsite în derivare

**Potrivirea pe frază exactă ratează flexiunea românească:**

| | frază exactă | stem |
|---|---|---|
| „ten gras" | 93 | **1.120** |
| „roșeață" | 266 | **1.143** |
| „hidratare" | 1.492 | **1.915** |

Falsul pozitiv de care ne temeam („acizi grași" din ingrediente ⇒ etichetă `oily`) e **12 produse** —
real, dar controlabil printr-o listă de excluderi per cheie, nu prin renunțarea la stem.

**`key_ingredients` nu e o fațetă:** 99,1% acoperire și **10.392 de valori distincte**, pentru că
liniile secțiunii sunt propoziții întregi. Ca text de căutare, excelent; ca filtru, inutilizabil.

**Scurgere de domeniu în propriul script:** `TEXTURE_TERMS` și `FRAGRANCE_FREE_RE` sunt cuvinte de
beauty în românește, hardcodate. Motivul pentru care NX-264 e primul card.

---

## 5. Un sfert din catalog n-are axa pe care se cumpără

`machiaj` = 681 de produse. Toate cele 2.755 de variante au eticheta „Standard";
`shade`, `color_hex`, `undertone` sunt 0. Nuanța trăiește în numele produsului
(„LAKA Fruity Glam Tint … 116 Candid"): 199 din 681 au un indicator, 151 un finish.

Și capcana: categoria `Buze` a ieșit cu **92% acoperire pe nevoi** la derivare — „hidratare",
„luminozitate". Corecte și irelevante.

> **Acoperirea poate fi mare și fațetele complet greșite.** Cifra măsoară prezența, nu utilitatea.
> De aici criteriul din NX-264: fațetele derivate trebuie să discrimineze **în interiorul rădăcinii**.

Catalogul are cinci forme diferite de decizie, deci testul de generalitate se poate face pe date
reale, fără catalog sintetic:

| rădăcină | produse | axa de decizie |
|---|---|---|
| `ten` | 1.461 | nevoie + tip de ten |
| `machiaj` | 681 | nuanță + finish |
| `par` | 233 | tip de păr |
| `corp` | 163 | nevoie |
| `protectie` | 137 | SPF (număr exact) |
| `electrica` | 47 | funcție + tehnologie |

---

## 6. Graful: poate recupera aproape tot ce e epuizat

| | |
|---|---|
| produse epuizate | **391** |
| au ≥1 produs în stoc în aceeași categorie | **390** |
| au unul și în ±30% preț | **381** |
| au unul și de la același brand | 339 |

Azi recuperează zero. `product_relations` e gol.

---

## 7. Poziția arhitecturală

**Trei etaje, nu unul.** Generare largă de candidați (recall) → rerankare (precizie) → strat de
business. Etajul 2 lipsește: azi ordinea o dă fuziunea RRF a două brațe care compară cuvinte cu
cuvinte. Brațul vectorial e construit din `nume + brand + categorie`, pentru că `ai_summary` e NULL
pe toate cele 2.758 de rânduri.

**Fiecare mecanism rezolvă altceva:**

| mecanism | bun la | nu e bun la |
|---|---|---|
| filtre structurate | garanții: preț, stoc, SPF, fără parfum, siguranță | relevanță |
| rerankare | relevanță, nuanță, intenție | garanții |
| graf | „ce în loc", „ce mai trebuie", „ce urmează" | găsirea produsului inițial |

**Deci nu derivăm atribute ca să căutăm mai bine.** Le derivăm ca să putem exclude cu conștiința
curată. Căutarea o rezolvă recall-ul larg plus rerankarea.

**Excludere doar când toate trei sunt adevărate:** faptul e verificat prin audit de precizie, fațeta
e `partitioning`, iar clientul a rostit-o în turul curent (`corroborated_by`, nu declarația
modelului). Orice altceva influențează ordinea, nu apartenența. `UNKNOWN` nu exclude niciodată.

**Graful e schelă.** În producție, la scară, muchia valoroasă nu e „aceste două produse se aseamănă
ca text", ci „oamenii care s-au uitat la ăsta au cumpărat pe ălălalt". Fiecare muchie poartă `source`
ca să poată fi înlocuită de comportament fără rescriere.

**Fără trafic nu se poate afirma calitatea.** Offline e gardă, nu decizie. Până atunci: rata de zero
rezultate, rata de ture surde, precizia pe capul distribuției.

---

## 8. Ce NU facem, explicit

- **Nu** reconstruim embeddings-urile acum. Se decide după NX-267, cu cifra pe masă (D15).
- **Nu** adăugăm o bază de date de grafuri. ~16.000 de muchii, CTE recursiv care există deja.
- **Nu** folosim LLM pentru extracția de atribute (§4: n-are ce extrage pe cele 306).
- **Nu** construim catalog sintetic de alt vertical (decizie owner) — testul de diversitate e §5.
- **Nu** aprindem excluderea înainte de auditul de precizie.
- **Nu** restructurăm catalogul pe produs-părinte cu variante de nuanță.

---

## 9. Rămâne pe masă, în afara Wave H

**183.003 recenzii reale, zero rezumate.** Cel mai mare activ neexploatat din bază. Nu ține de
căutare sau de graf, ține de calitatea fișei după ce ai găsit produsul — dar e ce mai aduce mult cu
efort mic.

Restul: cele 20 de FAQ fără embedding, `intent_aliases` gol, indexurile GIN inerte sub RLS
(`DB-V3-SOLE-IMPORT.md` §12.2).

---

## 10. Reproducerea cifrelor

```bash
python scripts/facet_coverage.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0 --date 2026-09-03
python scripts/derive_product_attributes.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0 --sample 6
```
