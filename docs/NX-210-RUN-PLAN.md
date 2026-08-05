# NX-210 — plan de rulare a gate-ului (stare reală, comenzi, cost)

> Scris 2026-08-05, după ce codul NX-210 a intrat în main (#258). Scopul documentului:
> ce mai trebuie ca gate-ul să poată fi RULAT, comenzile exacte, și cât costă.

## TL;DR — gate-ul NU e rulabil azi

Codul e livrat și testat, dar `readiness` întoarce **`ready: false`**. Ultimul raport
([reports/nx210-h3-readiness.json](../reports/nx210-h3-readiness.json)):

```json
"sealed_case_count": 0, "hard_case_count": 0, "simple_fact_case_count": 0,
"required_pairs": 50, "required_hard_cases": 30, "required_simple_fact_cases": 10,
"blocking_codes": ["quality_h3_sample_too_small", "quality_h3_hard_slice_too_small",
                   "quality_h3_simple_fact_slice_too_small", "nx209_retrieval_gate_blocked"],
"unavailable_codes": ["quality_holdout_unavailable", "decision_policy_unavailable"]
```

Adică: **nu lipsesc creditele, lipsește corpusul.** Avem 0 din 50 de cazuri sigilate în
holdout-ul H3, nu există fișierul de politică de decizie, iar gate-ul de retrieval NX-209
e încă blocat. Costul OpenAI al rulării e neglijabil (vezi mai jos) — costul real e
**etichetarea**, care e exact ce e pauzat la NX-202b/NX-203.

Consecință în lanț: `reports/nx211-preflight.json` listează `nx210_h3_not_run` +
`adi_signature_unavailable` ca blocante de activare → **NX-211 rămâne dormant**
(`answer_plan_default_enabled: false`), iar NX-212/213/214 și enforcement-ul NX-188/189
stau după el.

## Ce trebuie construit ca să devină rulabil

| # | Artefact | Cerință | Cine | Stare |
|---|---|---|---|---|
| 1 | `quality_h3.json` (`QualityH3Set`) | ≥50 cazuri, din care ≥30 `hard` și ≥10 `simple_fact`, zero PII (validat de Pydantic) | Claude construiește · Adi/Codex etichetează | ❌ 0 cazuri |
| 2 | `policy.json` (`DecisionPolicy`) | praguri **preînregistrate** + `fingerprint` — se îngheață ÎNAINTE de a vedea rezultatele | Adi semnează | ❌ lipsă |
| 3 | Gate NX-209 deblocat | `retrieval_qrels` cu holdout-ul de retrieval satisfăcut | ține de NX-203 | ❌ blocat |
| 4 | `baseline.json` + `candidate.json` (`RunArtifact`) | rularea efectivă a celor două variante peste cazurile din (1) | rulare cu credite | ⏸ după 1-3 |

Punctele 1 și 3 **sunt** NX-202b + NX-203 pauzate. Nu există scurtătură: gate-ul e
proiectat să refuze un eșantion prea mic, tocmai ca să nu producă o decizie falsă.

## Comenzile (în ordine, când artefactele există)

```bash
# 0) verifică dacă se poate rula (exit 0 = ready, 2 = not ready)
python scripts/nx210_h3.py readiness \
  --quality-h3 tests/golden/quality_h3.json \
  --retrieval-qrels tests/golden/retrieval_qrels_compound.json \
  --policy tests/golden/nx210_policy.json \
  --output reports/nx210-h3-readiness.json

# 1) sigilează pachetele oarbe (A/B randomizat pe seed; reveal-ul rămâne separat)
python scripts/nx210_h3.py pack \
  --quality-h3 tests/golden/quality_h3.json \
  --retrieval-qrels tests/golden/retrieval_qrels_compound.json \
  --policy tests/golden/nx210_policy.json \
  --baseline reports/nx210-baseline.json \
  --candidate reports/nx210-candidate.json \
  --seed <seed-fix-notat-în-ADR> \
  --packets-output reports/nx210-packets.json \
  --reveal-output reports/nx210-reveal.json

# 2) evaluarea oarbă = OM. Adi notează pachetele din nx210-packets.json → ratings.json.
#    NU deschide reveal-ul până ratings e complet (asta e tot rostul gate-ului).

# 3) decizia, după regula preînregistrată (exit 0 = candidate_for_adi_review, 3 = altfel)
python scripts/nx210_h3.py evaluate \
  --policy tests/golden/nx210_policy.json \
  --ratings reports/nx210-ratings.json \
  --reveal reports/nx210-reveal.json \
  --output reports/nx210-decision.json
```

`evaluate` întoarce una din trei: `insufficient_data` · `no_go` · `candidate_for_adi_review`.
**Niciuna nu e „GO"** — GO-ul e semnătura lui Adi în ADR, unealta doar spune dacă propunerea
e eligibilă de discutat.

## Cost estimat (credite OpenAI)

Singura felie care consumă credite e pasul de generare a celor două `RunArtifact` (baseline
+ candidat) peste cele 50 de cazuri. Cu tarifele reconciliate din
[NX-201-PRICING](NX-201-PRICING.md) (`gpt-5.4-mini`: 0,75 / 0,075 cached / 4,50 USD per 1M):

| Ipoteză | Valoare |
|---|---|
| Perechi | 50 (30 hard multi-tur + 20 simple) |
| Tururi totale, ambele variante | ~250 |
| Cost per tur (mini + nano triaj + embedding, ~60% prompt cached) | ~0,005 USD |
| **Total rulare** | **~1,3 USD** (marjă largă: sub 5 USD chiar și cu retry-uri și tururi mai lungi) |

**Costul e neglijabil. Costul adevărat e uman**: notarea oarbă a 50 de perechi de către Adi
(estimat 1,5-2,5h), plus etichetarea corpusului dinainte. De aia gate-ul nu e „o comandă de
rulat", ci o sesiune de lucru care trebuie programată.

## Recomandare

Nu porni rularea acum. Ordinea corectă:
1. Deblochează corpusul: **NX-202b** (etichete de adevăr pe cazurile compuse) → **NX-203**
   (familiile rămase) — asta ridică și `nx209_retrieval_gate_blocked`.
2. Construiește `quality_h3.json` din corpusul etichetat (≥30 hard / ≥10 simple_fact).
3. Îngheață `policy.json` + seed-ul în ADR, semnat, ÎNAINTE de orice rulare.
4. Rulează pașii 0-3 de mai sus într-o singură sesiune.

Între timp, **NX-165 (Analytics Faza 3)** nu depinde de nimic din lanțul ăsta și poate merge
în paralel.
