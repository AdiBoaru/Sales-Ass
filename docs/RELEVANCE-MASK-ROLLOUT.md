# Jurnalul aprinderii excluderii (NX-271)

**Stare azi (2026-09-03): NICIO fațetă aprinsă.** `RELEVANCE_MASK_ENABLED=false`,
`RELEVANCE_MASK_FACETS=""`, zero fațete cu `enforce_ready: true` în pachet.

Documentul ăsta e un jurnal, nu un plan. Fiecare aprindere primește un rând cu cifrele de dinainte
și de după, iar o stingere primește la fel. Dacă un rând n-are cifre, aprinderea n-a avut loc.

---

## De ce cardul ăsta e cel mai riscant din val

Toate celelalte carduri din Wave H **adaugă**: fapte, muchii, constrângeri, exemple. Ăsta **scoate
produse din rezultate**.

Un filtru dur peste un atribut derivat cu precizie 70% șterge tăcut produse corecte. **Tăcut** e
cuvântul care contează: clientul nu vede un mesaj de eroare, vede mai puține opțiuni și pleacă. Nu
există nicio poartă în aval care să prindă asta — validatorul verifică dacă ce spunem e ADEVĂRAT,
nu dacă am arătat tot ce trebuia.

---

## Cele trei condiții, toate obligatorii

O fațetă exclude un produs doar dacă **toate** sunt adevărate în același tur:

| condiție | unde e declarată | ce oprește |
|---|---|---|
| `binding: partitioning` | pachetul tenantului (NX-257) | un obiectiv („luminozitate") nu contrazice pe nimeni, deci nu poate exclude |
| `enforce_ready: true` | pachetul tenantului, dat de auditul NX-268 | un fapt derivat cu precizie mică nu capătă drept de excludere pentru că are acoperire mare |
| fațeta e în `RELEVANCE_MASK_FACETS` | env, decizie de operare | aprinderea a tot ce a fost vreodată auditat, deodată |

Plus cele două care erau deja în NX-257: constrângerea trebuie să fie `hard` (deci `user_explicit`,
confirmat de `corroborated_by` — rostit de client, nu dedus de model), iar acoperirea printre
candidați trebuie să atingă `min_coverage`.

**De ce sunt trei declarații și nu una.** `enforce_ready` răspunde la „e faptul destul de precis ca
să excludă?"; lista activă răspunde la „vrem să-l aprindem ACUM, pe el, singur?". Sunt întrebări
diferite și se pot răspunde în momente diferite. Fără a doua, o fațetă auditată acum trei luni s-ar
aprinde odată cu flagul, iar dacă rezultatul se strică n-ai ști care dintre ele a stricat.

Aceeași formă ca la promovarea retrievalului (NX-238): **kill-switch-ul aprins nu e suficient**.

---

## Ordinea de aprindere

**1. `skin_type`.** Singura fațetă `partitioning` cu acoperire reală măsurată (68,6% după NX-268) și
exact cazul din findingul NX-257: un ser de ten uscat recomandat cuiva cu ten gras. Restul de 31%
rămâne `UNKNOWN` și **trece** (D7), deci aprinderea nu poate goli raftul.

**2. `spf`** — dar ca **constrângere numerică** (NX-266), nu ca fațetă de potrivire. E aici doar ca
să nu fie uitată; drumul ei e `TYPED_CONSTRAINTS_ENABLED`, nu lista de mai sus.

**3. `shade`** (NX-269) — `partitioning` prin natură: cumpărătorul cere o nuanță ANUME, iar alta nu e
o potrivire mai slabă, e produsul greșit. Acoperire măsurată 85,3% pe `machiaj`. Aprinderea ei cere
auditul de precizie la pragul de PROMISIUNE (95%), fiindcă o nuanță greșită e vizibilă imediat.

**4. `fragrance_free`** — promisiune, deci pragul e precizia, nu acoperirea: un client care cere
„fără parfum" și primește un produs parfumat nu se mai întoarce. Acoperirea ei e **0%** azi (cele
4,2% de la prima măsurătoare veneau dintr-un regex hardcodat pe care NX-264 l-a șters, iar pachetul
nu declară încă frazele care o derivă).

**5. Restul rămân `additive`, permanent.** `concerns` **nu** devine partiționant: o nevoie e un
obiectiv, nu o exclusivitate — un produs care nu tratează acneea nu contrazice pe cineva care are
acnee.

---

## Criteriul de oprire, preînregistrat

Pe setul NX-265, după fiecare aprindere:

- **top-3 nu scade.** Dacă scade, se stinge fațeta. **Nu se ajustează pragul** — ajustarea pragului
  ca reacție la un rezultat prost e exact cum se pierde un instrument de măsură.
- **rata de contradicție scade.** Ăsta e motivul pentru care aprindem; dacă nu scade, mecanismul nu
  face ce credem că face.
- **rata de zero rezultate nu crește** peste toleranța declarată înainte.

---

## Jurnalul

| data | fațetă | acoperire | precizie auditată | top-3 înainte → după | contradicție înainte → după | zero-rezultate | decizie |
|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | nicio aprindere încă |

**Ce blochează prima aprindere:** auditul de precizie NX-268 n-a rulat (verdictul e `INSUFFICIENT`
pe toate fațetele) și baseline-ul NX-265 nu există. Fără primul, `enforce_ready` n-are cum să
devină `true` onest; fără al doilea, n-ar exista cifra față de care să compari după aprindere.

Ordinea e deci: adnotarea NX-265 → auditul NX-268 pe `skin_type` → `enforce_ready` în pachet →
`RELEVANCE_MASK_FACETS=skin_type` → rerulare NX-265 → un rând în tabelul de mai sus.
