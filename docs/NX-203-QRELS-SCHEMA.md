# NX-203 — Benchmark de retrieval: schema qrels + harness + splituri (SCHELET)

**Status:** SCHELET livrat — schema + metrici + splituri + harness rulabil pe exemplu. **Datasetul
real (200-500) NU e populat** (așteaptă etichetele NX-202 validate de Adi). · 2026-07-23
**Card:** [tasks/NX-203.md](../tasks/NX-203.md) · **ADR:** D3, D13, D15

Codex: „în paralel cu etichetarea NX-202, Claude construiește doar scheletul NX-203 — schema qrels,
harness-ul și spliturile, fără generarea masivă a datasetului." Exact asta e aici.

## Ce e livrat (cod)

| Fișier | Rol |
|---|---|
| `src/evals/retrieval/schema.py` | `QrelsQuery`/`QrelsSet` (Pydantic): relevanță GRADUALĂ (0-3), `hard_constraints`, `forbidden_products`, `provenance` (real_sanitized/synthetic/paraphrase), `catalog_version`, validare de integritate (relevant∩interzis=∅, fără duplicate). |
| `src/evals/retrieval/metrics.py` | Recall@k, nDCG@k (grade reale), Top-k hit, MRR, forbidden-violations. Pure-Python, determinist, testat cu valori de mână. |
| `src/evals/retrieval/splits.py` | Felii SINGLE-USE per gate: tuning + H1(NX-207)/H2(NX-209)/H3(NX-210). Atribuire deterministă (hash pe id, fără random) + stratificată pe categorie. `holdout_slice_for_gate` impune că fiecare gate folosește felie distinctă. |
| `src/evals/retrieval/harness.py` | `run_benchmark(qset, retrieve_fn, config)` → `BenchmarkReport` (metrici agregate + config-ul complet al rulării: embeddings, document_version, reranker, ponderi, split). `retrieve_fn` injectat → aceeași măsurare compară orice configurație. |
| `tests/golden/retrieval_qrels_example.json` | EXEMPLU minuscul (3 query-uri fictive) ca harness-ul să ruleze — NU e datasetul. |
| `tests/test_retrieval_harness.py` | 13 teste: corectitudinea metricilor, integritatea qrels, spliturile single-use, harness pe exemplu. |
| `requirements-dev.txt` | `ir-measures` pinned (cross-check la rularea completă; harness-ul nu depinde de el la runtime). |

## Decizii de design (de ce așa)

- **`retrieve_fn` injectat.** Harness-ul nu știe de `search_products`/`search_entities` → aceeași
  măsurare compară lexical vs +semantic vs +reranker, embeddings A vs B — fără să schimbe codul.
- **Metrici pure-Python, `ir-measures` doar cross-check.** Scheletul rulează oriunde (Windows/CI)
  fără dependența grea; la rularea completă, valorile se cross-validează cu `ir-measures` (standardul).
- **Splituri single-use, deterministe.** Atribuirea din hash pe id (nu random — reproductibil,
  stabil la re-rulare, consistent cu regula „fără Date.now/random"). O felie deschisă la un gate nu
  se refolosește la altul (anti-contaminare, D13).
- **Truth-first.** `hard_constraints`/`forbidden_products` din qrels sunt același adevăr de business
  ca în NX-202 — se alimentează din etichetele Adi, nu se re-inventează.

## Ce NU e aici (rămâne la popularea NX-203, după validarea NX-202)
- Datasetul real de 200-500 query-uri ro cu qrels (din etichetele NX-202 + trafic sanitizat).
- Adaptor-ul care leagă `retrieve_fn` de retrieval-ul real (`search_products_lexical/semantic` +
  `fuse_candidates`) și baseline-ul configurației actuale pe holdout.
- Cross-check-ul efectiv cu `ir-measures` pe date reale.

## Următorul pas
După ce etichetele NX-202 (produse acceptate/interzise + constrângeri) sunt validate de Adi:
popularea qrels (truth din NX-202 → `QrelsQuery`), adaptor la retrieval real, baseline pe tuning +
raport machine-readable cu config + intervale de încredere.
