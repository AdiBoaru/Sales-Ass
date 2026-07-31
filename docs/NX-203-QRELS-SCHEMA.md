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
| `src/evals/retrieval/constraints.py` | Evaluarea `hard_constraints` contra unui produs, in TREI stari (`satisfies`/`violates`/`unknown`). Sursa UNICA: o importa si scriptul de derivare a exceptiilor, si metrica. Absenta unui atribut NU e incompatibilitate; exceptia se declara per constrangere (`unknown_is_violation`, praguri de siguranta). |
| `src/evals/retrieval/catalog.py` | `load_catalog(conn, business_id)` → `CatalogSnapshot`: filtru comun `status='active'` + `content_status='published'`, prețul EFECTIV proiectat cu chiar `_EFFECTIVE_PRICE` din calea de producție (promoție în fereastră + minimul variantelor), decodare a `jsonb`-ului întors ca string de asyncpg. |
| `src/evals/retrieval/harness.py` | `run_benchmark(qset, retrieve_fn, config, catalog=None)` → `BenchmarkReport` (metrici agregate + config-ul complet al rularii: embeddings, document_version, reranker, ponderi, split). `retrieve_fn` injectat → aceeasi masurare compara orice configuratie. |
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
- **Catalogul e input pentru orice afirmație despre constrângeri.** `hard_constraints` se evaluează
  contra produselor, nu contra qrels-ului. Fără `catalog=`, `forbidden_violation_rate` e `None` și
  raportul iese marcat `constraint_validation_unavailable` — **niciodată 0**, fiindcă un zero ar fi
  indistinct de o rulare curată. Metricile de relevanță (recall/nDCG/MRR) rămân valide.
- **Prețul din snapshot = prețul pe care îl vede clientul.** `load_catalog` proiectează
  `_EFFECTIVE_PRICE` (promoție în fereastră + minimul variantelor), importat din calea de
  producție, nu rescris. Pe `p.price`, un produs de 100 lei vândut cu 60 apărea ca încălcând un
  prag de 90 — un fals-pozitiv raportat ca `verified`, deci mai rău decât o stare neverificată: un
  răspuns greșit dat cu încredere. Aceeași corecție în `nx203_derive_forbidden.py` (folosește acum
  încărcătorul) și în filtrul de buget din `nx203_propose_qrels_candidates.py` (unde 3-7 produse
  per prag lipseau din candidați — o gaură de recall în GOLD, pe care benchmarkul n-o poate
  detecta). `list_price` rămâne în snapshot ca diagnostic, exclus din amprentă.
  **Consecință:** prețul efectiv depinde de ziua rulării (ferestre de promoție), deci baseline și
  candidat se rulează în aceeași zi — altfel `compare_reports` refuză, corect.
- **Un singur numărător de încălcări.** `metrics.violations_at_k` = REUNIUNEA dintre excepțiile
  explicite din qrels și violările derivate din constrângeri. Reuniune, nu sumă: un produs prins de
  ambele e o singură încălcare, altfel rata ar crește cu cât un caz e mai bine acoperit de
  constrângeri, nu cu câte produse greșite au ieșit.
- **Rapoartele își poartă catalogul.** `catalog_fingerprint` = versiune + hash peste **conținutul
  canonic** (id + preț + categorie + `attributes`, chei sortate), nu peste setul de id-uri:
  `version` vine din `updated_at` trunchiat la secundă, deci două modificări de preț în aceeași
  secundă produceau amprente identice pe cataloage diferite. `compare_reports` refuză rapoarte cu
  cataloage diferite sau cu verificarea indisponibilă: altfel deltele ar fi schimbări de DATE
  prezentate ca schimbări de calitate, iar un `None` tratat ca 0 ar raporta „fără regresie de
  siguranță" pe o rulare în care nimic n-a fost măsurat.
- **Acoperire, în două momente.** Înainte de rulare, snapshotul trebuie să conțină **toate**
  id-urile la care se referă qrels-ul — suprapunerea parțială nu ajunge (un snapshot cu doar
  produsul judecat trecea drept compatibil). După rulare, orice produs returnat în top-k care
  lipsește din snapshot face raportul neverificat, cu id-urile în `unverifiable_products` —
  altfel un retrieval care întoarce exact produse din afara catalogului raporta zero încălcări
  și `verified`, adică cel mai prost rezultat posibil prezentat drept cel mai bun.

## Ce NU e aici (rămâne la popularea NX-203, după validarea NX-202)
- Datasetul real de 200-500 query-uri ro cu qrels (din etichetele NX-202 + trafic sanitizat).
- Adaptor-ul care leagă `retrieve_fn` de retrieval-ul real (`search_products_lexical/semantic` +
  `fuse_candidates`) și baseline-ul configurației actuale pe holdout.
- Cross-check-ul efectiv cu `ir-measures` pe date reale.

## Următorul pas
După ce etichetele NX-202 (produse acceptate/interzise + constrângeri) sunt validate de Adi:
popularea qrels (truth din NX-202 → `QrelsQuery`), adaptor la retrieval real, baseline pe tuning +
raport machine-readable cu config + intervale de încredere.
