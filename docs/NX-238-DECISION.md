# ADR NX-238 — promovarea candidatului `search_entities`

**Status:** DECIS · **Data:** 2026-08-13 · **Baseline:** `origin/main@3cffbf5`
**Verdict: `NOT-READY`** · **Owner al verdictului final: Adi** (un GO cere semnătura lui)

## Decizie

Candidatul `search_entities` **NU se promovează**. Toate flag-urile de candidate rămân OFF,
producția continuă pe retrievalul canonic actual, iar artefactele de măsurare se păstrează.

Asta **nu blochează Stage 1**: NX-239 poate construi agentul unic peste `RetrievalPort`, prin
`CurrentLiveRetrievalAdapter`. Contractul e stabil indiferent de verdict — asta a fost tot rostul
portului.

## Context

Cardul cere o promovare MĂSURATĂ, cu două rezultate legitime. Ce s-a măsurat, pe commit-ul și
hash-urile curente (reproductibil: `scripts/search_entities_release_gate.py evaluate`):

### Calitate — gate NX-210 H3

| Cerut de protocol | Măsurat |
|---|---|
| ≥ 50 cazuri human-verified, sigilate | **0** |
| ≥ 30 cazuri `hard` | **0** |
| ≥ 10 cazuri `simple_fact` | **0** |
| `DecisionPolicy` înghețată + amprentă | **absentă** |

### Retrieval — qrels NX-203/NX-209

| Cerut | Măsurat |
|---|---|
| 100 familii integral human-verified | **18** |
| holdout H1 / H2 / H3 ≥ 20 query-uri | **0 / 3 / 4** |
| un query `real_sanitized` per categorie | lipsesc `bodycare`, `haircare`, `makeup` |

Nu există niciun set de date pe care pragurile cardului (Recall@20 ≥ 90%, nDCG@6 ≥ 0,85, ≥ 90%
query-uri cu rezultat relevant în top 6, zero hard violations, zero simple-fact regressions) să
poată fi evaluate. **`NOT-READY` nu e „aproape ready"** — e absența instrumentului de măsură.

## Ce s-a construit oricum (și de ce)

Un verdict negativ nu anulează munca de arhitectură; o face mai valoroasă, fiindcă ea e ce permite
măsurarea de mâine fără să atingi producția.

| Componentă | Rol |
|---|---|
| `src/retrieval/port.py` | `RetrievalPort` + `RetrievalBundle` — contractul pe care îl consumă NX-239 |
| `src/retrieval/current_live.py` | traseul canonic prin port, **paritate prin construcție** |
| `src/retrieval/search_entities.py` | candidatul, cu enforcement real; fără decorator, inert |
| `src/retrieval/selector.py` | poarta: bucket stabil, pipeline version, kill switch, verificarea GO |
| `scripts/search_entities_release_gate.py` | readiness → artefact de decizie amprentat |
| `reports/nx238/` | verdictul, blockerii și readiness-ul, versionate |

### Paritatea nu e o speranță

`CurrentLiveRetrievalAdapter` **apelează** `search_products_tool` — nu re-implementează căutarea.
Nu există o a doua implementare care s-ar putea desincroniza. Testul verifică identitatea de
obiect (`is`), nu egalitatea: dacă adaptorul ar reconstrui produsele, egalitatea ar putea trece,
iar diferența ar apărea abia în producție.

### Adnotare pe live, enforcement pe candidat

NX-188/189 rămân **ÎNGHEȚATE** până la GO. Traseul live calculează verdictele tri-state și le
atașează, dar **nu exclude nimic** — și spune asta explicit prin `constraints_enforced=False` +
degradarea `retrieval_constraints_not_enforced`. Un consumator nu poate confunda „am verdicte" cu
„am aplicat verdictele".

Candidatul (OFF) execută: masca de `rejected` se aplică **înainte** de rerank (providerul nu vede
ce nu are voie să întoarcă) **și după** (un provider extern poate întoarce orice). Invariantul e
verificat structural în `RetrievalBundle.__post_init__`: `constraints_enforced=True` cu un
`rejected` în rezultat ridică `RetrievalError`.

### `UNKNOWN ≠ MISMATCH`, peste tot

O fațetă fără acoperire produce `alternative` + intrare în `missing_information` — niciodată
`rejected`, niciodată `exact`. Un produs despre care nu știm nu e un produs care contrazice.

## Cum e imposibilă promovarea accidentală

`RETRIEVAL_CANDIDATE_ENABLED=true` **nu e suficient**. Selectorul cere un artefact de decizie cu:

1. `verdict == "GO"`;
2. `decided_by` completat (software-ul nu poate emite GO — cel mult `candidate_for_adi_review`);
3. amprentă SHA-256 care corespunde conținutului canonic;
4. semnătură HMAC verificabilă cu `RETRIEVAL_DECISION_KEY`;
5. manifest care nu a driftat (catalog/qrels/policy).

Fiecare eșec are un cod fix și duce în același loc — traseul live curent:

| Atac | Cod | Rezultat |
|---|---|---|
| ștergi artefactul | `decision_artifact_missing` | current live |
| editezi `verdict` la GO | `decision_fingerprint_mismatch` | current live |
| recalculezi amprenta fără cheie | `decision_signature_invalid` | current live |
| semnezi cu altă cheie | `decision_signature_invalid` | current live |
| GO fără om care semnează | `decision_unsigned` | current live |
| cheie absentă în runtime | `decision_no_signing_key` | current live |
| catalog/qrels mutate sub verdict | `decision_manifest_drift` | current live |

În plus, `Settings` respinge la BOOT un `RETRIEVAL_CANDIDATE_ROLLOUT_PCT > 0` fără cheie: un ramp
care nu poate porni niciodată nu are voie să arate în config ca și cum rulează.

## Consecințe

**Pozitive:** NX-239 are un contract stabil azi; măsurarea de mâine nu cere refactor; verdictul e
reproductibil și legat de hash-uri; promovarea greșită e imposibilă, nu doar improbabilă.

**Negative acceptate:** candidatul rămâne cod nefolosit (drift posibil — mitigat de testele de
lanț care îl exersează la fiecare rulare de suită); portul adaugă un strat de indirecție pe care
nimeni nu-l consumă încă (NX-239 îl consumă).

## Ce ar schimba verdictul

1. **NX-203** duce corpusul la ≥100 familii human-verified, splituri necontaminate, seed sigilat.
2. **NX-202** produce `QualityH3Set` sigilat: ≥50 cazuri, ≥30 `hard`, ≥10 `simple_fact`.
3. **NX-201** fixează SLO-ul de latență și plafonul de cost; `DecisionPolicy` se îngheață și se
   amprentează **înainte** de deschiderea ratingurilor.
4. Rulare pereche baseline↔candidate pe același manifest, **o singură dată**, blind.
5. Decizia semnată a lui Adi → `search_entities_release_gate.py sign`.
6. Rollout: dark → shadow → 1% canary pe bucket stabil → ramp aprobat. Kill switch = un flag,
   sub cinci minute, fără migrare și fără pierdere de date.

Follow-up-uri deschise de acest verdict: **NX-203** (corpus, deblocarea măsurării) și **NX-202**
(H3 sigilat). Fără ele, orice altă rundă de NX-238 va reproduce același `NOT-READY`.
