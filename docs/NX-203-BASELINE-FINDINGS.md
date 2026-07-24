# NX-203 — Baseline retrieval pe query-urile grele (primele cifre reale)

**Data:** 2026-07-24 · **Sursa:** `tests/golden/retrieval_qrels_compound.json` (12 interogări grele,
adevăr NX-202 validat) · **Raport:** `reports/nx203-baseline-compound.json` · **Catalog:** demo 300 produse, read-only

Prima măsurătoare a retrieval-ului ACTUAL (lexical + semantic + RRF, calea de producție) pe
query-urile compuse. De aici plecăm, înainte de `search_entities` (NX-209).

## Cifre agregate

| Metrică | raw_hybrid (text brut) | with_constraints (price+categorie) |
|---|---|---|
| Recall@20 | **0,667** | 0,500 |
| nDCG@6 | 0,617 | 0,461 |
| Top-6 hit | **0,667** | 0,500 |
| MRR | 0,677 | 0,510 |
| Forbidden@6 | **0,250** | 0,250 |

## Ce ne spune (4 findings concrete)

### 1. Retrieval-ul actual e decent pe query clar + comparații, SLAB pe colocvial/compus
8/12 găsesc produsul în top-6. Funcționează pe: potrivire clară (`fond mat, ten gras`), produse
numite (toate 4 comparațiile — se găsesc după nume), ingredient explicit (`niacinamidă`).
**Ratează 4/12**, exact clasa grea:
- `antilucire-rezistent-caldura` („să nu mă lucesc, rezistă pe căldură") — colocvial, zero potrivire.
- `alternativa-la-scump` („ca Coral Theory dar mai ieftin") — cere identificarea referinței apoi
  alternative; retrieval-ul brut nu poate „ca X dar mai ieftin".
- `fara-parfum-rutina-fata` — set multi-produs de rutină, nu o căutare simplă.
- `birou-natural-ten-normal` — colocvial + multi-produs.

→ Exact ce țintesc **query rewriting (NX-208)** + **search_entities cu constraint coverage (NX-209)**.

### 2. Scurgere de produse INTERZISE: 25% din query-uri (3/12)
Retrieval-ul actual scoate în top-6 produse care încalcă cererea:
- `vitc-ten-reactiv` → un ser interzis (retinol/HA, nu vit C) în top-6.
- `alternativa-la-scump` → chiar produsul-referință scump (Coral Theory) apare primul.
- `compare-doua-creme-uscat` → o cremă pentru ten gras (tip greșit) în top-6.

→ Gap concret de calitate pe care îl închide **Match Gate / enforcement de constrângeri (NX-187/188)**.

### 3. Constrângerile aplicate NAIV SCAD recall-ul (0,667 → 0,500)
Contraintuitiv, dar diagnostic: filtrarea pe `category` din constrângeri a exclus produse relevante.
**Cauza:** vocabular nealiniat. Constrângerea cerea `ser-pentru-ten` (singular), dar categoria REALĂ
din catalog e `seruri-pentru-ten` (plural) → filtru pe categorie inexistentă → zero rezultate pe acea
cale. `fond-de-ten` există și merge; `ser-pentru-ten` nu.

→ Exact motivul pentru **registrul de fațete tipizate cu valori validate contra catalogului (NX-209)**
+ canonicalizarea QuerySpec (NX-208). Constrângerile NU se aplică pe valori neverificate.
→ **Follow-up de date:** corectează `ser-pentru-ten` → `seruri-pentru-ten` în adevărul NX-202.

### 4. Comparațiile funcționează deja bine (4/4)
Când produsele sunt numite explicit, retrieval-ul le găsește. Comparația e o capacitate matură;
efortul se duce în discovery-ul greu, nu în compare.

## Concluzie
Baseline pe query-uri grele: **~67% găsesc produsul, 25% scurg un produs interzis**. Nu e catastrofă,
dar e departe de ținta ≥90% Recall@20 / 0 încălcări. Cele 4 findings mapează 1:1 pe fazele planului
(NX-208 query rewriting, NX-209 search tool + fațete tipizate, NX-187/188 match gate). Baseline-ul
NU e „un număr" — e harta a ce trebuie reparat, cu dovezi per-query.

**NB (onestitate):** 12 query-uri = eșantion mic, interval de încredere larg. E baseline-ul de
PORNIRE pe cazurile grele, nu verdictul final; setul se extinde la popularea completă (200-500).
