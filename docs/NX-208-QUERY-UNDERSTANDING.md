# NX-208 — Query understanding (rescriere + concern_map) + contract QuerySpec

**Faza 5 (Quality Overhaul).** Owner: Claude (build) / Codex (verify). Model: Opus 4.8.
Sursă adevăr inițiativă: [QUALITY-OVERHAUL-2026.md](QUALITY-OVERHAUL-2026.md) (ADR D6, D7, D10, D11).

## Ce a arătat baseline-ul (NX-203)
Pe 12 interogări grele (adevăr NX-202 validat), `raw_hybrid` (text brut, zero filtre):
**Recall@20 = 0,667 · Top-6 = 0,667 · Forbidden@6 = 0,250.** 4 query-uri colocviale ratau complet
(Recall 0): anti-luciu, „ca X dar mai ieftin", „fără parfum toată rutina", machiaj discret de birou.

## Ce livrează NX-208
Stratul de **înțelegere a interogării** — determinist, ZERO LLM (P2), config-driven (P9):

1. **Contract `QuerySpec` pe 3 reprezentări (D6)** — `src/agent/query_spec.py`:
   - `RuntimeQuerySpec` (raw + normalized + `search_text` expandat + constrângeri) — `@dataclass`
     frozen, **fără nicio cale de serializare**; trăiește DOAR în memoria turului.
   - `SafeQuerySpec` (constrângeri canonice + metadate, FĂRĂ text liber) — Pydantic `extra=forbid`;
     SINGURA reprezentare persistabilă/telemetrizabilă. `to_safe()` = unica punte, dropează raw-ul.
   - **Garanție STRUCTURALĂ de confidențialitate** (nu convenție): `SafeQuerySpec` n-are câmp de text
     liber → `raw_query` n-are unde intra. Dovedit în `tests/test_query_spec.py`.

2. **Rescriere deterministă** — `src/agent/query_rewrite.py` (`build_query_spec`):
   - **Pattern-uri de limbă (RO generic):** preț plafon („sub 120", „buget 200"), referință + „mai
     ieftin/accesibil" → sort preț crescător + referință de exclus, „fără parfum" → fațetă pozitivă (D9).
   - **Vocabular (DomainPack):** `concern_map` (colocvial → concern canonic) + `query_expansions`
     (frază → termeni canonici de căutare adăugați la `search_text`, ca lexical + semantic să prindă).

3. **Vocabular extins** (`scripts/seed_demo_domain_pack.py` — override live pe tenant + parity în
   `beauty_salon.json`/`taxonomy._BEAUTY`): concern high-confidence („mă lucesc"→oily, „reactiv"→
   sensitive) + `query_expansions` (anti-luciu→matifiant/mat, rezistență→rezistent, rutină→
   curățare/ser/cremă, apă micelară→demachiant, discret→natural/lejer).

4. **Fix de date:** `ser-pentru-ten` → `seruri-pentru-ten` (categoria REALĂ din catalog) în
   `compound_truth_proposed.json` + `retrieval_qrels_compound.json`. Slug-ul greșit făcea filtrul de
   categorie să prindă zero produse → 2 query-uri cădeau la 0 sub `with_constraints`.

5. **Shadow seam + kill-switch:** `query_spec_shadow_enabled` (default **OFF**). ON → triajul emite
   `query_spec_shadow` (fără PII: intent/sort/fațete/nr. constrângeri) pe turul sales — ZERO schimbare
   de comportament. Ownership-ul ȚINTĂ al QuerySpec = agentul principal (F7, D11); triajul e provizoriu.

## Dovada cu cifre (`reports/nx208-rewrite-compound.json`, `python scripts/nx208_baseline.py`)

| Regim | Recall@20 | Top-6 | nDCG@6 | Forbidden@6 |
|---|---|---|---|---|
| `raw_hybrid` (reper NX-203) | 0,667 | 0,667 | 0,617 | 0,250 |
| **`rewritten_hybrid` (NX-208)** | **0,806** | **0,833** | **0,639** | **0,167** |
| `hybrid_with_constraints` (plafon, cu fix date) | 0,667 | 0,667 | 0,621 | 0,333 |

**Δ atribuibil înțelegerii: Recall@20 +0,139 · Top-6 +0,166 · Forbidden −0,083. ZERO regresii** pe
cele 8 query-uri care treceau deja. Fix-ul de date urcă plafonul `with_constraints` 0,5 → 0,667
(vezi și `reports/nx203-baseline-compound.json` re-rulat).

Per-query reparate:
- `alternativa-la-scump`: 0 → **1,0** (ambele alternative găsite; referința interzisă exclusă).
- `antilucire-rezistent-caldura`: 0 → **0,67** (expandarea matifiant/mat/rezistent redirecționează
  semanticul de la creme SPF către fonduri mate/long-wear).

## Cazuri reziduale — bounded de ALTE carduri (diagnosticate, NU forțate)

Două cazuri rămân — analizate, cu cauza atribuită explicit, ca să NU supra-ajustăm cele 12 query-uri
(D15). Retrieval-ul de MECANISM e scope-ul NX-209 (`search_entities`), catalogul e NX-206/207.

- **`fara-parfum-rutina-fata` (0 → 0):** query multi-categorie („toată rutina" = curățare + ser +
  cremă + SPF). O SINGURĂ căutare vectorială/FTS nu poate acoperi 4 categorii. Dovedit: chiar și un
  sub-query focalizat pe expansiuni întoarce **0/4** — NU e lacună de vocabular. Soluția = agentul
  descompune în sub-căutări (D1) SAU filtru pe fațeta `fragrance_free` + multi-field (NX-209).

- **`birou-natural-ten-normal` (0,33 → 0,33):** precizie într-o categorie largă („machiaj"). Fondurile
  potrivite EXISTĂ dar rankează în afara top-20. Dovedit: un sub-query focalizat („natural acoperire
  lejeră lejer ten normal") le recuperează **2/3 la rangurile 4 & 6** → lead concret pentru NX-209
  (multi-query / rerank). NU implementat aici: multi-query e schimbare de MECANISM (scope NX-209).

**Descoperire cheie pentru NX-209:** FTS-ul (`websearch_to_tsquery('simple', …)`) face AND pe
toate token-urile → un `search_text` lung (raw + expansiuni) prinde ZERO; un query scurt focalizat
prinde (trgm-ul e sensibil la lungime). `search_entities` (NX-209) trebuie să emită sub-query-uri
focalizate / OR-uri, nu un singur query lung.

## Ce NU intră în acest card (deferred, per DoD + matrice dispoziție)
- Pipeline vocabular VIU cu review uman săptămânal (mecanismul D10) — separat, follow-up.
- Analiza raportului de dezacord shadow (triaj vs. comportament actual) — cere comparație cu args-urile
  tool-loop-ului; se face când shadow-ul rulează pe trafic real.
- Enforcement QuerySpec (NX-188/189) — ÎNGHEȚAT până la gate-ul NX-210 (D11).
