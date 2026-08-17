# NX-246 (felia 3) — Gate de calitate conversațională „personal shopper"

> **Verdict măsurat azi: `NOT-READY`.** Harness-ul e complet și funcțional; corpusul nu e.
> Deblocarea e o muncă de DATE (60 dev + 40 holdout sigilat), nu de cod — exact tiparul NX-238.

```
$ PYTHONPATH=. python scripts/web_quality_eval.py gate --suite tests/golden/web_journeys
VERDICT: NOT-READY
policy:  df1b430d959672b0
  - holdout nesigilat: manifest nesigilat (fără content_sha256 sau count=0)
  - dev: journeys 10/60
  - acoperirea holdoutului necunoscută
exit=2
```

---

## 1. De ce nu ajung golden tests

Un golden test verifică un RĂSPUNS. Produsul vinde o CONVERSAȚIE, iar diferența e chiar lucrul
greu: *„și ceva mai ieftin?"* nu are sens fără turul dinainte; *„nu, fără parfum"* trebuie să
**șteargă** un criteriu vechi, nu să-l adune peste cel nou; *„compară-le pe primele două"* se
referă la o listă pe care userul o vede acum.

Harness-ul NX-210 (`nx210_blind`, `nx210_h3`) rămâne sursa pentru grounding, hard constraints și
pairwise-ul orb — nu s-a rescris nimic din el. Felia asta adaugă exact ce el nu are: mai multe
ture, context de pagină/coș, corecții, referințe ordinale, și o rubrică despre cum SUNĂ răspunsul.

---

## 2. Ordinea: determinist ÎNAINTE de stil

```
sigiliu + acoperire  →  verificări deterministe  →  rubrici + pairwise
   (am dreptul?)           (e adevărat?)              (sună bine?)
```

Ordinea e întregul design. Dacă rubricile ar veni primele, un text fluent care inventează un preț
ar bate un text onest care spune „nu știu" — fiindcă se citește mai bine. Cardul o cere explicit
(*„un judge nu poate compensa o halucinație cu un scor bun de ton"*), iar aici e **impusă de cod**:
`deterministic.passed=False` ⇒ verdict `FAIL`, indiferent de rubrici, indiferent de pairwise.

Cele 10 verificări deterministe (`DETERMINISTIC_CHECKS`, vocabular închis, toleranță zero):
`viewmodel_schema`, `non_empty_terminal`, `grounding`, `hard_constraints`, `safety`, `state_delta`,
`reference_resolution`, `action_scope`, `receipt`, `no_pii`.

---

## 3. Patru verdicte, nu două

| verdict | ce înseamnă | exit |
|---|---|---|
| `PASS` | toate pragurile trecute | 0 |
| `FAIL` | am măsurat și candidateul a picat | 1 |
| `INSUFFICIENT` | eșantionul nu susține o decizie (< 40 perechi) | 2 |
| **`NOT-READY`** | **n-am măsurat**: holdout nesigilat sau acoperire incompletă | 2 |

`NOT-READY` e distinct de `FAIL`, deliberat. Un `FAIL` ar sugera că cineva a pierdut o comparație;
adevărul e că nu există comparație. Același vocabular ca verdictul NX-238 pentru portul de
retrieval, din același motiv: a numi corect starea „nu știu" e jumătate din valoarea unui gate.

**Lipsa datelor nu produce niciodată `PASS`.** Un gate care trece fiindcă n-a găsit holdoutul e
fail-open, adică opusul unui gate. Testul care apără asta e cel mai important din fișier.

---

## 4. Corpusul: 10 familii, vocabular închis

Familiile sunt un `Literal`, nu etichete libere. Motivul e acoperirea: un gate care raportează
„92% pass" fără să spună pe ce familii a măsurat ascunde exact cazul pe care nu l-a testat. Cu
familii închise, `coverage()` poate afirma *„no_results n-a fost testat niciodată"* — ceea ce e o
informație, nu o absență.

| familie | ce prinde |
|---|---|
| `typo_diacritics` | „sampon" fără diacritice, formulări colocviale |
| `elliptical_followup` | „și ceva mai ieftin?" |
| `correction` | „nu, fără parfum" — ȘTERGE criteriul vechi |
| `useful_clarification` | întreabă când chiar lipsește informația, o singură dată |
| `ordinal_reference` | „primul", „acesta", „compară-le" |
| `page_context` | ancora de pe PDP + schimbare de pagină între ture |
| `hard_constraint` | buget/safety, `UNKNOWN ≠ MISMATCH` |
| `no_results` | relaxare onestă, fără produse inventate |
| `cart_mutation` | snapshot, mutație cu receipt, stale/conflict |
| `mixed_conversation` | greeting → recomandare → comparație → acțiune |

**Eticheta trebuie să descrie conținutul.** `family="page_context"` fără niciun `page_context` e
respins la validare — altfel acoperirea ar minți (raportezi 4 cazuri dintr-o familie care de fapt
n-o testează).

**Duplicatele sunt respinse pe DOUĂ chei**: `journey_id` și amprenta de CONȚINUT (care exclude
id-ul, deliberat). Copiat-lipit cu alt id e același caz de test — exact ce ar face cineva grăbit
să atingă 60.

Corpusul refuză și PII în texte, și fapte comerciale în `page_context` (aceeași regulă ca la
runtime: nu testăm cu date pe care serverul le respinge).

### Ce e în repo azi

10 journey-uri **seed**, câte unul per familie, în `tests/golden/web_journeys/dev/`. Rolul lor e
să dovedească că schema funcționează pe toate cele 10 familii și să dea `coverage()` ceva real de
măsurat. **Nu sunt suita cerută** — și gate-ul o spune: `journeys 10/60`.

---

## 5. Holdoutul: sigilat, nu în repo

În repo intră doar `holdout_manifest.json`: `suite_id`, numărul, distribuția pe familii și
SHA-256. Conținutul stă într-un store restricționat.

```
content_sha256 = SHA-256( amprentele ORDONATE ale journey-urilor )
```

Peste amprente, nu peste fișiere: un holdout re-serializat cu altă indentare rămâne același
holdout, dar unul căruia i s-a schimbat un `must_ground` **nu** — și exact asta trebuie prins.

Un manifest **fără hash nu e sigilat**. `journey_count: 40` fără `content_sha256` e o afirmație pe
care nimeni nu o poate verifica, iar `sealed` întoarce `False`. Toate ramurile sunt fail-closed:
manifest absent, hash diferit, conținut indisponibil, număr care nu se potrivește ⇒ runner-ul iese
non-zero **înainte** de eval.

Minime pentru gate (cardul, literal): **≥60 dev**, **≥40 holdout**, fiecare familie **≥4×** în
holdout, **≥30% adversarial**.

**Starea de azi:** `journey_count: 0`, `content_sha256: ""`, `location: NEALOCAT`. Holdoutul nu a
fost construit. Când va fi, `web_quality_eval.py seal --content <dir>` calculează hashul de pus în
manifest, iar comanda refuză să raporteze „complet" dacă minimele nu sunt atinse.

---

## 6. Pairwise orb

**Statistică diferită de NX-210, deliberat.** Acolo e „delta medie pe rubrică"; aici e o
PROPORȚIE: `win + 0,5×tie ≥ 55%`, cu limita inferioară bootstrap 95% ≥ 50%. Nu sunt
interschimbabile — se poate câștiga la medii pierzând majoritatea journey-urilor, dacă victoriile
sunt mari și înfrângerile multe.

Rubrica (1-5, ancore publicate): `naturalness`, `helpfulness`, `trust`, `no_overtalk`,
`context_handling`.

Trei capcane tratate explicit:

- **scurgere de etichetă** — `assert_blind` refuză să emită un pachet care conține „candidate",
  „champion", „release", un nume de model. Dacă evaluatorul poate deduce care variantă e
  candidate, tot exercițiul e teatru;
- **order bias** — dacă „A" câștigă sistematic indiferent ce e în A, rezultatul măsoară poziția.
  Se raportează întotdeauna, și peste ±0,10 **blochează**;
- **acord între evaluatori** — câștigători diferiți SAU >1 punct pe o dimensiune ⇒ adjudecare.
  Fără al treilea evaluator, perechea **nu intră în scor**: a o include cu media a doi oameni care
  nu sunt de acord ar fabrica o observație.

Randomizarea laturii e **deterministă din seed** (ca la NX-210): rularea se poate reproduce și
contesta. Un `random()` real ar face imposibil de verificat că ordinea n-a fost aleasă după ce s-au
văzut rezultatele.

Dezvăluirea se face **după** ratings, în `aggregate()`. Ordinea nu e o formalitate.

---

## 7. Pragurile: preînregistrate, cu amprentă

`GatePolicy` e înghețată și amprentată (SHA-256) în fiecare raport:

| prag | valoare |
|---|---|
| media per rubrică | ≥ 4,0 |
| niciun cohort sub | 3,8 |
| pairwise `win + 0,5×tie` | ≥ 55% |
| limita inferioară bootstrap 95% | ≥ 50% |
| overtalk sever | ≤ 5% |
| regresie per cohort vs champion | ≤ 5pp |
| perechi minime | 40 |
| order bias tolerat | ±0,10 |
| dezacord tolerat | ≤ 30% |

Se schimbă **doar printr-un PR de policy separat, înainte de rularea candidateului**. Un prag mutat
după ce s-au văzut cifrele nu e un prag, e o justificare — iar amprenta din raport face mutarea
vizibilă.

---

## 8. Rulare

```bash
# schema + duplicate + coerență familie/conținut
PYTHONPATH=. python scripts/web_quality_eval.py validate --suite tests/golden/web_journeys

# acoperire față de minimele cardului (exit 2 dacă e incompletă)
PYTHONPATH=. python scripts/web_quality_eval.py coverage --suite tests/golden/web_journeys

# SHA-256 de pus în manifest, după ce holdoutul există
PYTHONPATH=. python scripts/web_quality_eval.py seal --content <store-restricționat>

# verdictul
PYTHONPATH=. python scripts/web_quality_eval.py gate --suite tests/golden/web_journeys \
    --holdout <store> --ratings ratings.json --keys keys.json --deterministic det.json --json
```

**CLI-ul nu rulează modele.** Nici măcar `gate`: el consumă artefacte deja produse. Separarea e
deliberată — un gate care ar chema el însuși modelul ar amesteca „a măsura" cu „a decide", iar
recitirea unui verdict ar costa bani de fiecare dată.

Artefactul conține **doar agregate și coduri de motiv**; niciun transcript (cardul: „runnerul nu
printează transcriptul în CI artifacts"). Un test verifică asta pe payload-ul serializat.

---

## 9. Ce blochează `PASS`, concret

| ce lipsește | cine deblochează |
|---|---|
| 50 de journey-uri de development (10/60) | muncă de scriere, pe catalogul demo |
| holdout de 40, sigilat, ≥4 per familie, ≥30% adversarial | NX-203 (corpus) — **pauzat** |
| ratings umane blind de la ≥2 evaluatori | proces, nu cod |
| rularea candidate vs champion | credite OpenAI + un champion imutabil identificat prin release SHA |

Primele două sunt aceeași datorie ca la NX-238 (H3 sigilat: 0/50 cazuri, qrels 18/100 familii).
Codul e gata și o spune singur, de fiecare dată când e rulat.
