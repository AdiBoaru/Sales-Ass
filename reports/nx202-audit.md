# NX-202 — auditul setului golden

Inventar: **58 cazuri + 12 conversații**.

## Clasificare

| clasă | n |
| --- | ---: |
| KEEP | 55 |
| LEGACY | 1 |
| UNIT | 14 |

`KEEP` nu e o opinie: `tests/test_golden.py` rulează FIECARE caz pe ambele contracte
(v1 și creier unic, parametrizat). Un caz care trece pe profilul `brain` e valid pe
arhitectura țintă. `LEGACY` sunt exact cele pe care harnessul le declară incapabile.

## Compoziție față de ținta cardului

Numărată pe tot ce nu e `LEGACY`. `UNIT` e un ROL (pin de regresie), nu o excludere din
acoperire: un caz de clarificare acoperă tipul simplu chiar dacă servește și ca pin.

| dimensiune | are | ținta | gol |
| --- | ---: | ---: | ---: |
| simple | 16 | 10 | 0 |
| recommendation | 27 | 15 | 0 |
| hard_query | 10 | 15 | 5 |
| comparison | 0 | 5 | 5 |
| action_followup | 16 | 5 | 0 |

Set de calitate (KEEP + HOLDOUT): **55** din 50-100 ceruți · acoperire totală: 69.

## Ce a ieșit altfel decât presupunea cardul

- **Cazurile NU sunt cuplate la catalog.** Fixturile poartă un catalog sintetic,
  self-contained, deci nu au expirat la schimbarea tenantului — spre deosebire de
  qrels-ul NX-203, care chiar a murit așa.
- **`ai_summary` apare în 47 fixturi**, dar nu e datorie tehnică: codul tratează deja
  absența lui. Ce rămâne e o divergență de FORMĂ: fixtura dă modelului un rezumat pe
  care catalogul real nu-l are pe niciun rând, deci testul e mai UȘOR decât producția.

## Ce lipsește, măsurat

- **Proveniență:** 70/70
  sunt sintetice. Cardul cere ca query-urile grele să fie REALE (sanitizate), nu
  inventate la birou. Sursa există: cele 12.665 de fraze din `recommendation_trigger`.
- **Holdout:** 0 cazuri marcate. Un holdout nemarcat nu e holdout, e set de tuning.
- **PII:** 0 cazuri semnalate — curat
