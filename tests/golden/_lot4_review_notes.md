# NX-203 lot 4 — note pentru review (înainte de etichetare)

Generat cu filtrele din `_lot4_filters.json`, candidații în `_lot4_candidates.json`.
Catalog: `demo-2026-07-22`, 300 produse active+published.

**Nimic nu e etichetat.** `provenance` și `category` sunt `pending`, niciun `relevance` nu e
completat, `human_verified` rămâne fals peste tot. Mașina a produs doar *mulțimea de examinat*.

## Ce e nou față de loturile 1-3

Loturile anterioare stăteau pe creme hidratante / fond de ten / protecție solară. Lot 4 intră pe
teren neacoperit: **rujuri** (0 qrels până acum), îngrijirea părului dincolo de „păr gras", creme
de ochi / cearcăne, demachiere, seruri pe ingredient activ.

E și primul lot generat **după** fixul prețului efectiv, deci filtrele de buget văd promoțiile.
`Rhea Organics Glow Ruj` intră în pool-ul „sub 60" la 30.39 (listă 37.99) — sub vechea regulă ar fi
intrat oricum, dar mecanismul e acum cel corect.

## Două lucruri de decis ÎNAINTE de etichetare

### lot4-03 nu e un query de buget — e un duplicat al lui lot4-02

„vreau rujuri mate sub 60 d eleu" produce **exact același pool** ca „vreau rujuri mate": toate cele
4 rujuri mate costă sub 60 (42.99 / 44.99 / 30.39 / 39.99). Constrângerea de preț nu discriminează
nimic.

Consecința nu e cosmetică: dacă lot4-03 primește familie proprie, benchmark-ul numără de două ori
același contract de adevăr și headline-ul se mută fără ca ceva real să se fi schimbat — exact
distorsiunea pe care agregarea pe familie o elimină.

Propunere: **aceeași `family_id` ca lot4-02**, păstrat ca variantă de formă (typo real din trafic,
„d eleu"). Alternativa — să-l ținem ca query de buget separat — cere un prag care chiar taie ceva,
deci alt query, nu ăsta.

### lot4-13 nu are niciun candidat din catalog — și nu din vina filtrului

„ser cu acid hialuronic pentru ten gras" → **0 produse**. Cauza, verificată produs cu produs: în
catalog există 6 seruri cu acid hialuronic, dar **niciunul nu e etichetat `oily`**. Toate poartă
`hydration` / `dry` / `combination` / `normal`.

Nu am lărgit filtrul. Ar fi fost ușor (scot `oily` și rămân 6 candidați), dar aș fi transformat o
problemă de date într-un gold inventat: aș fi declarat relevante produse pe care catalogul nu le
declară potrivite pentru tenul gras.

Cele două citiri posibile, și amândouă cer decizia ta:

- **catalog_gap** — etichetarea e incompletă. Serurile cu acid hialuronic *sunt* potrivite pentru
  ten gras (formulele apoase sunt exact recomandarea uzuală), doar că nimeni n-a pus `oily` pe ele.
  Atunci fixul e în catalog, iar query-ul intră în qrels după.
- **abstention** — cererea n-are răspuns bun în stocul curent, iar comportamentul corect al botului
  e să spună asta. Atunci query-ul aparține suitei de abstenție, nu celor 200.

Până se decide, lot4-13 rămâne **în afara** lotului de etichetat.

## Gold mic, de confirmat ca realitate de catalog

Aceeași situație ca `finish=matte` în lotul 3, unde s-a confirmat că 2 produse e corect:

| query | candidați din catalog | de ce |
|---|---|---|
| lot4-06 „șampon pentru păr vopsit" | 1 | un singur produs cu `suitable_for=colored` |
| lot4-09 „cremă de ochi cu cofeină sub 120" | 2 | 2 produse cu cofeină în interval |
| lot4-12 „ser cu niacinamidă pentru ten gras" | 2 | intersecția ingredient × tip de ten |
| lot4-18 „cremă hidratantă pentru ten gras" | 2 | |
| lot4-20 „fond de ten acoperire medie" | 2 | |

Un gold de 1-2 produse e legitim, dar face metricile per-query zgomotoase (un singur miss duce
Recall@20 de la 1.0 la 0.0). De semnalat, nu de ascuns.

## Traduceri pe care le-am făcut și pot fi contestate

Fiecare e o opinie în SQL, nu o măsurătoare:

- **lot4-08** „șampon pentru volum" → `suitable_for=fine`. „Volum" nu e valoare de catalog; „păr
  subțire" e cea mai apropiată. Dacă traducerea nu ține, query-ul iese din lot.
- **lot4-13** „să nu fie foarte scump" → **niciun prag**. Deliberat. Un prag inventat ar deveni
  adevăr de gold fără ca userul să-l fi spus.
- **lot4-18** „să nu lase luciu" → **nicio constrângere**. Ar fi `finish=matte`, dar
  `creme-hidratante` n-are `finish` în catalog. Rămâne semnal pentru om, nu filtru.
- **lot4-05** „nuanță de roșu" și **lot4-20** „ten deschis" → nuanța trăiește pe **variantă**, nu pe
  produs, deci filtrul nu o poate exprima. Omul decide dacă un produs fără nuanța cerută e 2 sau 1.
- **lot4-14** „aveți cu acid hialuronic ȘI cu niacinamidă?" → citit ca întrebare de
  **disponibilitate**, nu ca intersecție. Userul întreabă ce există, nu cere ambele în același
  produs. Dacă citirea e greșită, gold-ul se schimbă complet.

## Volum de etichetat

18 query-uri (fără lot4-13, blocat mai sus; lot4-03 fuzionat cu lot4-02 dacă se acceptă
propunerea). **lot4-17** singur are 27 de candidați — cross-categorie, ca `q-con-05`.

Familii eligibile rămase în manifest după acest lot: ~65.
