# NX-238 — artefactele deciziei de promovare `search_entities`

**Verdict măsurat: `NOT_READY`.** Candidatul rămâne OFF, traseul live canonic rămâne singurul
activ. Cardul e complet pe această ramură — un NO-GO/NOT-READY documentat este un rezultat
legitim, nu un eșec de livrare.

## Ce e aici

| Fișier | Ce conține |
|---|---|
| `decision.json` | artefactul de decizie: verdict, manifest (hashes), blockers, amprentă, semnătură |
| `readiness-h3.json` | proiecția readiness NX-210 H3 la momentul verdictului |
| `readiness-qrels.json` | readiness NX-207/209/210 pe qrels (splituri H1/H2/H3) |

## Cum se reproduce

```bash
PYTHONPATH=. PYTHONIOENCODING=utf-8 python scripts/search_entities_release_gate.py evaluate \
    --retrieval-qrels tests/golden/qrels_confirmed.json \
    --decided-at 2026-08-13 \
    --out reports/nx238/decision.json
```

Ieșire `2` = blocat (starea de azi). Ce ar spune runtime-ul despre artefact:

```bash
PYTHONPATH=. python scripts/search_entities_release_gate.py verify
```

## De ce NOT-READY (blockers măsurați, 2026-08-13)

Pe `origin/main@3cffbf5`, cu `tests/golden/qrels_confirmed.json`:

**Calitate (NX-210 H3)** — nu există dataset sigilat:

| Cerut | Măsurat |
|---|---|
| 50 cazuri human-verified/sealed | **0** |
| 30 cazuri `hard` | **0** |
| 10 cazuri `simple_fact` | **0** |
| `DecisionPolicy` înghețată + fingerprint | **absentă** (`policy_fingerprint: null`) |

**Retrieval (NX-203/NX-209 qrels)** — corpusul e pauzat înainte de etichetare:

| Cerut | Măsurat |
|---|---|
| 100 familii integral human-verified | **18** |
| holdout H1 ≥ 20 query-uri | **0** |
| holdout H2 ≥ 20 query-uri | **3** |
| holdout H3 ≥ 20 query-uri | **4** |
| un query `real_sanitized` per categorie | lipsesc `bodycare`, `haircare`, `makeup` |

Coduri de blocare în artefact: `quality_h3_sample_too_small`,
`quality_h3_hard_slice_too_small`, `quality_h3_simple_fact_slice_too_small`,
`quality_holdout_unavailable`, `decision_policy_unavailable`, `nx209_retrieval_gate_blocked`,
`retrieval_qrels_not_ready`.

**Nu „aproape ready".** Deficitul nu se acoperă cu parafraze: metrica agregă pe FAMILIE de
intenție, deci 18 → 100 înseamnă intenții noi etichetate de om, nu variante ale acelorași.

## Ce NU s-a făcut, intenționat

- **Nu s-a rulat protocolul blind NX-210 pe H3.** Se rulează O SINGURĂ DATĂ, pe dataset sigilat.
  A-l consuma acum pe 4 query-uri ar arde holdoutul și ar face orice măsurătoare viitoare
  necredibilă — exact contaminarea pe care preînregistrarea o previne.
- **Nu s-au coborât pragurile și nu s-a re-sigilat nimic.** Pragurile trăiesc în cod
  (`scripts/search_entities_release_gate.py::THRESHOLDS`), tocmai ca să nu poată fi „relaxate"
  editând un raport.
- **Nu s-a rulat nimic care consumă credite OpenAI** (embed/eval). Readiness-ul e pur offline:
  citește JSON, nu cheamă niciun furnizor.

## Ce ar debloca un GO

1. NX-203 duce corpusul la ≥100 familii human-verified, cu splituri necontaminate și seed sigilat.
2. NX-202 produce `QualityH3Set` sigilat: ≥50 cazuri, din care ≥30 `hard` și ≥10 `simple_fact`.
3. NX-201 fixează SLO-ul de latență și plafonul de cost; `DecisionPolicy` se îngheață și se
   amprentează ÎNAINTE de deschiderea ratingurilor.
4. Rulare pereche baseline↔candidate pe același manifest, o singură dată, blind.
5. Decizia semnată a lui Adi → `search_entities_release_gate.py sign`.

Abia după toate cinci poate `RETRIEVAL_CANDIDATE_ENABLED=true` să însemne ceva: fără artefact
semnat, selectorul întoarce `current_live` indiferent de flag.

## Integritatea artefactului

`fingerprint` = SHA-256 peste conținutul canonic (chei sortate, fără `fingerprint`/`signature`).
`signature` = HMAC-SHA256 peste amprentă, cu `RETRIEVAL_DECISION_KEY` (secret de operare, nu în
repo).

Cine editează `verdict` la `"GO"` rupe amprenta → `decision_fingerprint_mismatch`. Cine
recalculează amprenta n-are cheia → `decision_signature_invalid`. Cine șterge fișierul →
`decision_artifact_missing`. Toate trei duc în același loc: **traseul live curent**.
