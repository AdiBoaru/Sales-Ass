# Analiză: căutare, ranking și afișare produse — industrie 2026 vs. Nativx Assistant

**Data:** 2026-06-29
**Autor:** analiză de arhitectură (Claude Code), la cererea lui Adi
**Trigger:** într-o conversație live (3 seruri vitamina C sub 150 lei), produsul afișat
pe poziția 3 (4.6★ din 148 recenzii) era vizibil mai bun decât cel de pe poziția 2
(4.4★ din doar 28 recenzii), iar „Recomandarea mea" a căzut pe cel mai ieftin produs
(37.99 lei), nu pe cel mai potrivit. Plus: produsele erau seruri de *hidratare*, nu de
*vitamina C*. Scopul: benchmark vs. practica de producție 2026 + plan de remediere.

> **Status: document de analiză (referință).** Recomandarea **P0 e IMPLEMENTATĂ**
> (2026-06-29 — vezi secțiunea *Plan*); P1–P3 rămân în backlog.

---

## TL;DR

Arhitectura noastră de retrieval este **deja în forma corectă de 2026**: funnel pe 3 etaje
(retrieval hibrid → fuziune RRF → rerank), filtre dure separate de semnale soft, „shrunk
Bayesian rating" pentru cold-start, relax-ladder determinist. Nu suntem departe de standard.

Avem **trei găuri punctuale**, exact cele care au produs simptomele de mai sus:

1. **Rating/social-proof nu e feature de ranking** — pe modul `relevance`, ordinea finală e
   scorul RRF (lexical+vector); rating × volum recenzii intră *doar ca tie-break când RRF e
   egal* (≈niciodată). → produsul mai bine cotat se îngroapă sub unul mai slab.
2. **LLM-ul alege `pick`-ul liber** — „Recomandarea mea" e aleasă de modelul mini, expus la
   popularity/position bias documentat, în loc să fie top-ul unui scor determinist.
3. **Constrângerile de ingredient sunt soft** — „vitamina C" a fost tratat ca semnal `concerns`
   relaxabil, nu ca filtru dur ca prețul; fără disclosure la relaxare. (Agravat de gaură de
   date: atribute de ingredient nepopulate în catalogul demo.)

Fix-ul cu cel mai bun raport impact/efort: **un scor de relevanță blended determinist** care
include rating/review_count + alegerea `pick`-ului ca argmax al acestui scor (LLM-ul doar
narează). Rezolvă simptomele #1 și #2 deodată, rămâne 100% determinist și generic pe verticale.

---

## 1. Cum se face în producție (2026)

Consensul de producție este un **funnel pe 3 etaje**, identic ca formă fie pentru web search,
RAG sau căutare de produse:

| Etapă | Scop | Tehnică tipică | #candidați |
|---|---|---|---|
| **1. Candidate retrieval (recall)** | plasă largă, ieftin | BM25/sparse + dense/vector ANN (bi-encoder) | ~100–300 |
| **2. Fuziune / first-pass rank** | unește cele 2 liste | Reciprocal Rank Fusion (RRF) sau combinație convexă ponderată | → ~50–75 |
| **3. Rerank (precizie)** | reordonare scumpă, precisă a supraviețuitorilor | cross-encoder sau Learning-to-Rank (GBDT) | top ~10 afișate |

**De ce împărțirea:** asimetrie de cost. Bi-encoderele embed query și document *separat*
(rapid, precalculabil, scalează la milioane); cross-encoderele encodează query+document *împreună*
prin attention (mult mai precis, dar îți permiți doar pe câteva zeci de supraviețuitori).
Regulă de producție: **rerankează ≤50 candidați**; dacă rerankezi 200+, recall-ul de etapa 1 e
stricat — repari retrieval-ul, nu te bazezi pe reranker.

### Hibrid + fuziune
Cele două retrievere greșesc în direcții opuse: **BM25 câștigă** pe exact-match (SKU, coduri,
branduri rare); **dense câștigă** pe parafrază și intenție conceptuală („cadou pentru mama").
Metode de fuziune, în ordinea sofisticării:
1. **RRF** — `score = Σ 1/(k+rank)`, `k=60`. Operează pe *ranguri, nu scoruri* → scale-agnostic,
   fără date de antrenament. Lift modest (~1.3% NDCG). Punctul de pornire corect.
2. **Combinație convexă ponderată** — `score = α·dense + (1−α)·sparse`. Necesită ~40 perechi
   etichetate ca să tunezi α (~0.3 tehnic, ~0.6 mixt, ~0.7–0.8 conversațional).
3. **Tiered boosting** — boost all-term 100×, any-term 10×, vector fallback 0.1×. A bătut RRF
   semnificativ (+7.5% vs +1.3% NDCG pe datasetul Wands, 2025).

### Learning-to-Rank — unde intră semnalele de business
LTR stă la etapa de rerank și e locul unde intră semnalele de business. Features tipice:
- **Document:** preț, margine, timp de livrare, stoc, **rating, review_count**, recency.
- **Query:** lungime, tip de query.
- **Query-document:** scor BM25 per câmp (titlu/descriere), similaritate vector.
- **Comportamentale** (driverele reale de conversie): click-rate, add-to-cart, purchase-rate.

Model: **GBDT / LambdaMART rămâne calul de bătaie** (bate modelele liniare decisiv). Obiectivul
declarat al LTR în ecommerce e *explicit multi-obiectiv*: „cele mai relevante produse **și**
maximizarea șansei de venit" — relevanța e un termen într-un blend care optimizează și conversie
și margine. Semnalele de merchandising (reputație seller, promoții, stoc, margine) se țin ca un
**strat tunabil deasupra** scorului de relevanță, ca să le poți audita și A/B separat.

### Cross-encoder rerankers (2026)
Stau la etapa 3, scorează top ~50–75. Opțiuni de producție: **Cohere Rerank-3.5** (~100ms,
default comun), **ZeroEntropy zerank**, **Voyage** (instruction-following, agentic), **BGE-reranker/
BGE-M3** (open-weight). Tradeoff care delimitează designul: cross-encoder ~100–200ms pe ~30
candidați; **LLM-rerankers 1–3s+** și *„userii abandonează după 3 secunde"* → pe hot-path
interactiv, cross-encoderele sunt preferate față de LLM-rerankers.

### Specific conversațional / LLM-commerce
- **Filter-then-score:** filtrele dure rulează **PRIMUL**, în context boolean (match/no-match,
  nescorat, cacheabil). *„Prețul e constrângere dură, nu preferință soft. Similaritatea semantică
  nu poate impune un plafon de preț prin embeddings."* Produsele out-of-budget/wrong-category/
  out-of-stock **nu ajung niciodată la scoring vectorial** → nu sufocă match-urile valide.
- **Nu lăsa LLM-ul să fie ranker-ul.** Failure-modes documentate: **popularity bias**
  (supra-recomandă popularul), **position bias** (supraponderează ce apare primul în prompt),
  **social-proof/authority bias** (limbaj de autoritate în descriere schimbă alegerea),
  **recency bias**. Fix de producție: **un model determinist calculează ordinea din semnale reale
  (rating, review_count, conversie); LLM-ul doar re-rankează într-un prompt position-bias-aware
  sau, mai bine, doar narează lista deja ordonată.** Rating și review_count trebuie să fie
  **câmpuri structurate băgate în scorer-ul determinist**, niciodată lăsate LLM-ului „să observe"
  în proză.
- **Relaxare progresivă + onestitate:** când filtrele dau 0 rezultate → ordonează filtrele după
  importanță (relaxezi cosmeticul întâi: culoare; fundamentalul ultimul: ingredientul activ,
  plafonul de preț), relaxezi iterativ, și **declari** ce ai relaxat („n-am găsit ser cu vitamina C
  sub 150 lei, dar uite cele apropiate la 165"). Benchmark sobru (ShoppingComp): modelele actuale
  ratează dominant pe **attribute mismatch (35%), missing products (20%), constraint violations
  (25%)** — verificarea multi-constrângere e partea cea mai grea; aici merită investit în teste.

### Display / UX (2026)
Ierarhie vizuală strictă: **imagine > nume > preț+discount > trust signals (rating+review_count)
> UN badge**. Semnale și date din spate:
- **Rating + review_count** — afișarea recenziilor crește conversia ~270% mediu. Contraintuitiv:
  un **5.0 perfect convertește ~12% mai slab decât un 4.2–4.5** (sweet-spot-ul citește ca autentic).
- **Preț + discount specific** — „30% Off" bate „Sale" generic; arată prețul tăiat.
- **Badge** — „Best Seller" e cel mai puternic; *„Doar 3 rămase"* bate „Stoc limitat". **Doar
  15–25% din catalog cu badge, max 1 badge/produs** (altfel „badge blindness"). **Badge-ul trebuie
  să mapeze pe date reale** — fals = erodează încrederea (pt un asistent conversațional: un tag
  „bestseller" în chat trebuie susținut de semnal real în retrieval, altfel e halucinație).
- **Explainability:** trend 2026 = **LLM ca re-ranker explicabil** — jobul LLM-ului trece de la
  *a alege* la *a justifica* ranking-ul determinist („Top-rated în acest buget — 4.6★ din 2.300
  recenzii, în stoc"). Dublează ca guardrail anti-halucinație (justificarea citează câmpuri reale).

### Eval de calitate
- **NDCG@10** — metrica-titlu de ecommerce (relevanță graded: purchase > add-to-cart > click >
  view). k=10, nu k=20. **MRR** când contează doar top-1 (pick-ul highlight). **MAP** binar.
- **Offline prezice online?** Studiul Amazon pe 36 metrici: offline e de acord cu online pe ce
  model e mai bun **până la ~97%**, **NDCG are >99% putere discriminativă**, k=10 bate k=20.
  Folosește offline ca poartă tare, confirmă online.
- **Stack complet:** golden set offline (stratificat pe tip de query, Recall@K *per cluster* —
  un număr agregat ascunde catastrofa pe SKU/identifier), apoi **interleaving** (mai sensibil,
  mai puțini useri decât A/B), apoi **A/B**, plus **drift monitor** pe top-1 cosine.
- **Guardrails anti-halucinație:** grounding structural (ierarhii/variante), validare metadata la
  recommend-time (preț/stoc per variantă), **citație** (fiecare produs/preț trebuie să tragă dintr-un
  record real — RAG peste catalog verificat, nu memoria LLM), verificare de constrângeri (re-asertezi
  filtrele determinist, nu te încrezi că LLM le-a respectat), rerank determinist.

---

## 2. Ce avem noi (stare reală a codului)

Funnel-ul nostru, mapat pe cod:

- **Retrieval hibrid** — `src/tools/catalog_tools.py:240-420`. Lexical (FTS `search_tsv` +
  `pg_trgm`) ∪ semantic (pgvector HNSW cosine), pool ~50 fiecare, în paralel.
- **Fuziune RRF** — `src/db/queries/fusion.py:62-117`. `score = Σ 1/(60+rank)`, tie-break
  determinist pe boost (in-stock +1, on-sale +1, concern-overlap +N) apoi `product_id`.
- **Sort modes** — `src/db/queries/catalog.py:74-96`. `relevance` (default), `price_asc`,
  `price_desc`, `rating_desc`. Kill-switch `SEARCH_SORT_MODE_ENABLED`.
- **Shrunk Bayesian rating** — `src/db/queries/catalog.py:25-28`:
  `(review_count·rating + 30·4.0) / (review_count + 30)`. Prior C≈30, mean 4.0 — împiedică un
  5.0★ cu 1 recenzie să bată un 4.6★ cu 200. **Bine gândit** — dar vezi gaura #1: pe `relevance`
  intră doar ca tie-break.
- **Filtre dure** — `src/db/queries/catalog.py:141-256`. business_id, status='active', categorie,
  brand (nerelaxat), preț efectiv (`coalesce(vp.price, sale_price, price) ≤ max`), stoc, concerns.
- **Relax-ladder** — `src/tools/catalog_tools.py:123-157`. Cu flag ON: preț + stoc pinned;
  doar soft filters (concerns, apoi categorie) relaxate cumulativ.
- **„Mai ieftin" determinist** — `src/db/queries/catalog.py:339-371` (`search_cheaper_than`):
  aceeași categorie, doar în stoc, strict mai ieftin decât `min(displayed)`, ordonat preț↑ apoi
  shrunk_rating↓. Mesaj no-result per-locale.
- **Card display** — `src/worker/compose.py:178-245`. RichItem: toate faptele din retrieval,
  doar textul e LLM. Câmpuri: name, price, rating, review_count (doar dacă >0), list_price (doar
  dacă on-sale), badge, reason, url. Contract FE: `docs/FRONTEND-CONTRACT-IZI.md`.
- **Badge** — `src/worker/badges.py:32-65`. „Super Preț" (discount ≥20%) sau „Top Favorit"
  (shrunk_rating ≥4.7 ȘI review_count ≥50). Praguri reale — bine.
- **Pick („Recomandarea mea")** — `src/worker/compose.py:225-234`. **Ales de modelul mini**
  (`j["pick"]`), randat ca `👉 Recomandarea mea: {name} — {reason}`.

---

## 3. Comparație și găuri

| Dimensiune | Standard 2026 | Noi | Verdict |
|---|---|---|---|
| Candidate retrieval | BM25 ∪ vector ANN | FTS+trgm ∪ pgvector HNSW | ✅ Avem |
| Fuziune | RRF k=60 (+ tiered când ai date) | RRF k=60 + tie-break | ✅ Avem |
| **Rerank cu rating/social-proof** | rating, review_count, conversie ca features de ranking | rating **doar tie-break pe RRF egal** | ❌ **Gaura #1** |
| Filtre dure înainte de scoring | preț/stoc/ingredient = boolean filter | preț/stoc/categorie/brand dure ✅; **ingredient → soft, relaxabil** | ⚠️ **Gaura #3** |
| **Alegerea pick-ului** | argmax scor determinist; LLM narează | **LLM alege liber** (bias risk) | ❌ **Gaura #2** |
| Relaxare onestă | relax least-important + **disclose** | relax determinist ✅, **fără disclosure** | ⚠️ Parțial |
| Card display | imagine>nume>preț>rating+#review>1 badge | toate câmpurile + badge cu prag real | ✅ Avem |
| Eval ranking | NDCG@10 graded, golden stratificat, interleaving→A/B, drift | golden + halu-suite ✅, **fără NDCG/ranking metrics** | ⚠️ Parțial |

### Cauza exactă a celor 3 simptome
1. **Produs 3 (4.6×148) sub produs 2 (4.4×28)** → Gaura #1. Pe `relevance`, ordinea = RRF
   (lexical+vector). Rating × volum recenzii nu e feature de ranking, doar tie-break când RRF e
   egal (≈niciodată). Social-proof-ul n-are voce. Failure-mode documentat în producția 2026.
2. **„Recomandarea mea" = cel mai ieftin (37.99)** → Gaura #2. `pick`-ul e ales liber de mini;
   LLM-urile au popularity/position/social-proof bias. Standard: pick = top scor determinist,
   LLM doar justifică.
3. **Seruri de hidratare, nu vitamina C** → Gaura #3 + gaură de date. „Vitamina C" tratat ca
   `concerns` soft, relaxabil; catalogul demo probabil n-are atributul de ingredient populat.
   Standard: ingredientul activ = constrângere fundamentală, relaxată ultima, cu disclosure.
4. **Bonus — nume cu „…379/426"** = pur gaură de date catalog, nu ranking (dar foarte vizibil).

---

## 4. Plan de remediere (prioritizat după impact/efort)

**P0 — Blended relevance score (rezolvă #1 + #2 deodată). ✅ IMPLEMENTAT (2026-06-29).**
Înlocuiește „RRF pur, rating doar pe tie" cu un **scor final blended** pe top-N după fuziune:
`score = w_rel·RRF_norm + w_social·shrunk_rating_norm + w_avail·in_stock + w_deal·on_sale + w_concern·frac`.
`pick = argmax(score)` (modelul mini doar justifică, nu mai alege). Determinist, generic
(ponderi din DomainPack per vertical), respectă principiul „cod, nu LLM, pentru ranking".

Implementare:
- `src/db/queries/fusion.py` — `RANK_WEIGHTS` (default-uri agnostice), `_minmax_norm`,
  `blended_rerank` (semnale min-max-normalizate pe set, relevanță dominantă); `fuse_candidates`
  ia `weights` → blend când e dat, `deterministic_rerank` (RRF pur) când `None`.
- `src/tools/catalog_tools.py` — `_rank_weights(ctx)` (DomainPack.rank_weights / default / `None`
  când kill-switch OFF), pasat la `fuse_candidates`.
- `src/worker/compose.py` — `assemble` ordonează cardurile pe rankingul de retrieval (modelul
  curatează setul, codul ordinea) + `_select_pick` (pick = top-ul clasat afișat; modelul narează).
- `src/domain/pack.py` + `loader.py` — câmp `rank_weights` (override per-vertical).
- Kill-switch-uri FAIL-SAFE: `SEARCH_BLENDED_RANK_ENABLED`, `RICH_PICK_DETERMINISTIC_ENABLED`
  (ambele default ON; OFF → comportament byte-identic vechi).
- Teste: `tests/test_retrieval_ranking.py` (blend + minmax + `_rank_weights`),
  `tests/test_compose.py` (ordine + pick deterministe + kill-switch).

**P1 — Constrângeri de ingredient/atribut ca filtru dur + disclosure.**
„Vitamina C / SPF50 / fără parfum" → filtru dur la nivel variantă, relaxat ULTIMUL; la relaxare,
**spune-o** („n-am găsit cu vitamina C sub 150, cele mai apropiate sunt…"). Depinde și de
popularea atributelor în catalog (gaură de date).

**P2 — Eval de ranking.**
Golden set stratificat + **NDCG@10 / MRR** ca gate CI, pe lângă halu-suite. Prinde regresiile de
ranking în CI, nu live la mână.

**P3 — Igienă date catalog.**
Nume cu ID rezidual, `product_url` NULL, `ai_summary` templat. Nu e cod de ranking, dar e cel mai
vizibil pentru client.

### Ce NU facem (încă)
Cross-encoder reranker extern (Cohere/BGE) sau GBDT LTR. Sunt standardul de scală, dar:
au nevoie de date de click/conversie pe care nu le avem; adaugă 100–200ms+ pe hot-path; aduc o
dependență. Scorul blended determinist (P0) ia ~80% din câștig la ~20% din efort. Le ținem ca pas
ulterior când avem trafic real și semnale comportamentale.

---

## Surse (industrie 2026)

- [ZeroEntropy — Best Reranking Model 2026](https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/)
- [tianpan.co — Hybrid Search in Production (BM25 vs dense), 2026](https://tianpan.co/blog/2026-04-12-hybrid-search-production-bm25-dense-embeddings)
- [Digital Applied — Hybrid Search: BM25, Vector & Reranking 2026](https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026)
- [Weaviate — Hybrid Search Explained](https://weaviate.io/blog/hybrid-search-explained)
- [Cross-Encoder Reranking (EmergentMind)](https://www.emergentmind.com/topics/cross-encoder-reranking-9dd25a04-77c6-4f44-807d-cb5f2256901b)
- [A Survey on E-Commerce Learning to Rank (arXiv 2412.03581)](https://arxiv.org/html/2412.03581v1)
- [Elastic — Learning to Rank docs](https://www.elastic.co/docs/solutions/search/ranking/learning-to-rank-ltr)
- [Snowplow — Ecommerce Search Best Practices (LTR)](https://snowplow.io/blog/ecommerce-search-best-practices)
- [Fin AI — Structured Agentic RAG for E-Commerce](https://fin.ai/research/structured-agentic-rag-for-e-commerce/)
- [ShoppingComp — Are LLMs Ready for Your Shopping Cart? (arXiv 2511.22978)](https://arxiv.org/html/2511.22978v1)
- [Position Bias-Aware Reranking (arXiv 2505.04948)](https://arxiv.org/html/2505.04948v1) · [Brand Bias in LLM Recommenders (arXiv 2606.17443)](https://arxiv.org/html/2606.17443v1) · [LLM as Explainable Re-Ranker (arXiv 2512.03439)](https://arxiv.org/html/2512.03439v1)
- [FoxEcom — Product Card Design](https://foxecom.com/blogs/all/product-card-design)
- [WiserNotify — Product Badges That Convert 2026](https://wisernotify.com/blog/product-badges/)
- [FutureAGI — MRR vs MAP vs NDCG 2026](https://futureagi.com/blog/what-is-mrr-map-ndcg-2026/) · [Evidently — Ranking Metrics](https://www.evidentlyai.com/ranking-metrics/evaluating-recommender-systems)
- [Amazon Science — Do Offline Metrics Predict Online Performance?](https://www.amazon.science/publications/how-well-do-offline-metrics-predict-online-performance-of-product-ranking-models)
- [Airbnb — Interleaving & Counterfactual Evaluation (arXiv 2508.00751)](https://arxiv.org/pdf/2508.00751)
