# NX-203 loturile 5, 6, 7 — note pentru review

Generate cu `_lot5_filters.json` / `_lot6_filters.json` / `_lot7_filters.json`.
Catalog: `demo-2026-07-22`, 300 produse active+published.
**Nimic nu e etichetat.** Zero `relevance` completat, `human_verified` fals peste tot.

| lot | temă | query-uri | perechi de etichetat |
|---|---|---|---|
| 5 | seruri (vitamina C, hidratare, ten gras) + creme hidratante pe tip de ten | 20 | 363 |
| 6 | protecție solară, fond de ten, rujuri pe nuanță, păr, mâini | 17 | 151 |
| 7 | disponibilitate / stoc | 7 | 62 |

---

## Înainte de orice: manifestul nu ajunge la 200

Cu tot ce a rămas etichetat, proiecția e **115 query-uri**, nu 200:

```
qrels confirmate azi                18
lot 4                               20
lot 5                               20
lot 6                               17
lot 7                                7
                                  ----
după etichetare                     82
variante rămase în manifest         33   (din care ~14 rutine/cadouri/comparații)
                                  ----
maxim din manifest                 115
```

Și cifra care contează e mai mică. Scorurile headline se agregă **macro pe familie**, nu pe query
— tocmai ca o întrebare culeasă de două ori să nu cântărească dublu. După fuziunile propuse mai
jos, cele 82 de query-uri sunt **~60-65 de familii distincte**.

Consecința: a adăuga variante de formă (diacritice, typo, parafrază) urcă numărul de *query-uri*
spre 200, dar **nu adaugă nicio putere de măsurare** — aceleași familii, aceeași rezoluție. Ținta
de „≥200" exprimată în query-uri e, în parte, cosmetică față de cum se calculează metrica.

Trei ieșiri, și e o decizie de-a ta:

1. **Reformulăm ținta în familii** (ex. ≥100 familii) și recoltăm intenții *distincte* — cere trafic
   real în plus sau intenții noi scrise de la zero, ancorate în catalog.
2. **Ținem 200 de query-uri** știind că ~100 sunt variante de formă ale acelorași ~65 de contracte.
   Onest doar dacă e scris explicit în DoD că numărul e de acoperire lingvistică, nu de rezoluție.
3. **Coborâm ținta** la ce susține manifestul (~115 query-uri / ~65 familii) și mărim mai târziu,
   când vine trafic nou.

Recomandarea mea: **1**, cu 3 ca pas intermediar. Rezoluția vine din intenții diferite, nu din
rescrierea acelorași întrebări.

---

## Ce am reparat în propriile filtre

**lot6-10** „arată niște rujuri roșii ieftine" — tradusesem „ieftine" într-un prag de 45 lei. L-am
scos: toate cele 6 rujuri costă sub 45, deci pragul nu tăia nimic, iar în gold ar fi rămas o
constrângere pe care userul n-a spus-o. Rămâne același contract ca lot6-09, cu „ieftine" semnalat
ca vag.

E a doua oară când se întâmplă (prima: lot4-03, „sub 60" pe rujuri mate). Tiparul merită reținut:
**un prag pe care nu-l cere explicit userul e o constrângere inventată**, iar când catalogul e
ieftin peste tot, pragul nici măcar nu se vede că nu face nimic.

---

## Trei query-uri care NU intră în cele 200 așa cum sunt

### lot6-17 — produsul cerut nu există

„spune-mi mai multe despre calm theory glow cremă pentru protecție". În catalog nu există niciun
**Calm Theory Glow** și nicio protecție solară de la brandul ăsta — există mască, loțiune tonică,
ser, spray de fixare, fard, balsam de buze. Filtrul meu a prins **brandul**, nu produsul, deci
întoarce 6 produse care n-au legătură cu cererea.

Două citiri, amândouă scot query-ul din retrieval pur:
- **contextual** — userul se referea la ceva spus într-un tur anterior;
- **abstention / red-team** — răspunsul corect e „nu avem asta", iar un retrieval care întoarce
  cele mai apropiate 6 produse de la același brand e exact comportamentul periculos.

### lot6-06 — „fond de ten pentru ten gras, cu acoperire medie" → 0

Verificat produs cu produs: singurul fond pentru `oily` e *NudeLab Matte 24H*, cu
`coverage=full`, nu `medium`. Intersecția e goală.

N-am lărgit filtrul. Ca la lot4-13, e ori **catalog_gap** (acoperirea e etichetată prea rigid), ori
**abstention** (cererea n-are răspuns exact în stoc).

### lot6-15 — „cât e șamponul anti-mătreață" → 0, cum am și anticipat

Catalogul n-are niciun produs anti-mătreață; cel mai apropiat e „Șampon pentru scalp gras". L-am
pus în lot **tocmai ca să iasă zero** — un query pentru care botul trebuie să spună că nu are, nu
să livreze cel mai apropiat șampon.

---

## Volumul lotului 5 e prea mare pentru o ședință

363 de perechi, cu 7 query-uri peste 20 de candidați: `lot5-11` (49), `lot5-20` (36), `lot5-16`
(35), `lot5-17` (20), `lot5-18`/`lot5-19` (15 fiecare), `lot5-15` (17).

Cauza e legitimă — sunt cereri **cross-categorie** („ceva pt ten uscat", „ce ai pentru ten gras?",
„vreau ceva sub 50 de lei pentru față"), unde userul nu numește o categorie, deci pool-ul e mare
prin natura întrebării. Dar loturile de 20 au fost alese ca fiind cât poate cineva eticheta atent.

Propunere: **lot 5 se sparge în 5a (13 query-uri, ~150 perechi) și 5b (7 cross-categorie, ~210)**.
5b se etichetează separat, cu răbdare, fiindcă acolo apar deciziile grele.

---

## Fuziuni de familie propuse (altfel benchmark-ul numără dublu)

Fiecare pereche de mai jos are, după judecata mea, **același contract de adevăr** — deci aceeași
`family_id`. De confirmat sau respins una câte una:

| se contopește | cu | de ce |
|---|---|---|
| lot5-02, lot5-03 | lot5-01 | „aveți seruri cu vitamina C?" / „cât costă" — aceeași căutare, altă formulare |
| lot5-04, lot5-05 | lot5-01? | „fără strălucire" / „să lumineze" — efect așteptat, nu atribut. **Doar dacă** omul zice că nu schimbă gold-ul |
| lot5-14 | lot5-13 | „cremă" vs „cremă hidratantă" pentru ten gras |
| lot5-07 | q-self-01 | „recomandă-mi un ser pentru ten gras" = „am tenul gras, ce ser îmi recomanzi?" |
| lot5-10 | q-cat-01 / q-cat-02 | „cremă de față" vs „cremă hidratantă" pentru ten uscat |
| lot6-02, lot6-03 | lot6-01 | trei formulări ale „cremă cu SPF pentru ten sensibil" |
| lot6-10 | lot6-09 | „ieftine" nu adaugă constrângere (vezi mai sus) |
| lot6-11 | lot4-02 / lot4-03 | rujuri mate, prag care nu discriminează |
| lot6-13 | lot6-12, lot4-07 | trei formulări ale „șampon pentru păr uscat și deteriorat" |
| lot6-16 | q-con-03, lot4-16 | cremă de mâini pentru mâini uscate, cu typos grele |
| lot6-05 | q-con-01 | „aveți cremă SPF 50 disponibilă?" pe pool-ul SPF 50 |

Dacă toate se confirmă, cele 82 de query-uri devin **~64 de familii**.

---

## Traduceri contestabile (opinia mea în SQL)

- **lot5-04 / lot5-05** — „ten fără strălucire", „să-mi lumineze pielea" → **nicio** constrângere.
  Cel mai apropiat ar fi `hyperpigmentation`, dar nu e același lucru.
- **lot5-12** — „cremă mai bogată" → `texture=cremă` (nu `gel`). Susținut de catalog: cremele pentru
  ten uscat sunt `cremă`, cele pentru ten gras `gel`.
- **lot5-16** — „ten mixt, mai degrabă gras în zona T" → `combination`. Userul descrie **tenul**, nu
  cere un produs; răspunsul corect ar putea fi o întrebare de clarificare. Semnal pentru suita de
  clarify, nu doar pentru qrels.
- **lot5-18 / lot5-19** — „după duș" ar putea trimite spre corp, nu față. Am inclus și
  `lotiuni-de-corp`. Ambiguitatea e reală, n-am curățat-o.
- **lot7-02** — „concentrație mare" nu e atribut de catalog. Fără constrângere.
- **lot7-03 / lot7-05** — formatul (30ml, 50ml) trăiește pe **variantă**, în `net_content_value`.
  Filtrul nu-l exprimă, deliberat.
- **lot7-06** — „nuanța Ruby Red" nu există ca atare; în catalog sunt „Ruby" și „Classic Red".
  Cazul cel mai clar în care răspunsul bun e „avem ceva apropiat", nu o potrivire exactă.

## Gold mic (1-2 produse), de confirmat ca realitate de catalog

lot5-06, lot5-07, lot5-13, lot5-14 · lot6-01, lot6-02, lot6-03, lot6-04, lot6-05, lot6-07,
lot6-14 · lot7-04, lot7-05.

Legitim, dar face metricile per-query zgomotoase: cu gold de 1, un singur miss duce Recall@20 de la
1.0 la 0.0. La lot 6 sunt **7 din 17** — merită discutat dacă protecția solară și fondul de ten au
destule produse ca să suporte query-uri cu trei constrângeri.

---

# Reluare 2026-07-31 — cele trei blocaje din nota de pauză

Catalog re-verificat: `live:300@2026-07-22T19:49:18+00:00` — **neschimbat** față de
`demo-2026-07-22`, deci etichetele existente NU au expirat. (Cele 804 produse din DB includ
nepublicate; snapshot-ul de evaluare filtrează `active`+`published` = 300.)

## ✅ REZOLVAT — `q-con-06`, constrângerea inertă „vitamina c"

Nu era o problemă de etichetare, ci un **bug în comparator**: `key_ingredients` se testa exact,
case-sensitive, iar listele tolerante întorc `unknown` la nepotrivire → constrângerea nu
satisfăcea și nu încălca niciodată nimic. Arăta în qrels exact ca una validă.

Reparat în `src/evals/retrieval/constraints.py` (normalizare de registru la comparație, nu la
afișare). Efect măsurat:

- „vitamina c" satisface acum **13 produse** (înainte: 0);
- pe toate cele 3 constrângeri ale query-ului: **5 produse**, exact cele 5 deja etichetate;
- **0 candidați neetichetați** → fixul *validează* etichetele existente, nu le invalidează.

`_q-con-06_amendment_draft.json` devine inutil: amendarea presupunea schimbarea valorii din qrels,
dar valoarea era corectă — comparația era greșită.

## ⚠️ DE CONFIRMAT — verdict 11: `q-con-01` ⟷ `lot6-05` (SPF 50)

`q-con-01` („ai protecție solară spf 50?") poartă `texture in [cremă, fluid]`; `lot6-05`
(„Aveți crema SPF 50 pentru față disponibilă?") nu.

**Dovadă din catalog:** constrângerea `texture` exclude **0 produse** — mulțimea e aceeași (2
produse) cu sau fără ea. E **inertă**, iar textul lui `q-con-01` nu cere nicio formă („protecție
solară spf 50", fără „cremă"). Exact tiparul semnalat de două ori în notele de mai sus:
*o constrângere pe care userul n-o cere e o constrângere inventată, iar când nu discriminează
nici măcar nu se vede că nu face nimic.*

**Propunere:** scoate `texture` din `q-con-01` → fuziunea cu `lot6-05` devine validă.
Amendează o intrare `human_verified`, dar **nu poate schimba nicio etichetă** (mulțimea de
răspunsuri e identică). Ironia: `lot6-05` chiar spune „crema", deci dacă am ține constrângerea,
ea ar aparține lui `lot6-05`, nu lui `q-con-01`.

## ⚠️ DE CONFIRMAT — verdict 10: `q-con-03` ⟷ `lot6-14` (creme de mâini)

`lot6-14` („mi se usucă mâinele des") poartă `suitable_for=dry`; `q-con-03` („vreau o cremă de
mâini, le am cam uscate") nu. Ambele texte spun același lucru: mâini uscate.

**Dovadă din catalog:** `dry` exclude **exact 1 produs** — *Solora Sun Cremă de mâini SPF 30*, care
are `suitable_for = None`. Adică e exclus pentru **date lipsă**, nu pentru o incompatibilitate
declarată. Exact eșecul contra căruia modulul își declară doctrina celor trei stări: *absenţa unui
atribut NU e incompatibilitate.*

**Propunere:** scoate `suitable_for=dry` din `lot6-14` (aliniere la `q-con-03`), păstrând
uscăciunea ca semnal de **relevanță gradată**, nu ca hard constraint. Motiv: întreaga categorie
„creme de mâini" există pentru mâini uscate — ridicat la hard, filtrul pedepsește un produs
pentru metadate incomplete și introduce un fals-negativ în gold.

> Ambele cer confirmarea ta: sunt amendări de contract, iar `human_verified` nu se ridică din
> script — nici direct, nici indirect. Vezi antetul lui `scripts/nx203_draft_qrels.py`.
