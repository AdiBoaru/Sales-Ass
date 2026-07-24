# NX-186 — Registru de fațete tipizate + raport de coverage

**Faza P2 (Selection Correctness).** Owner: Claude (build) / Codex (verify). Depinde de NX-208 (QuerySpec).
Precede NX-187 (Match Gate) / NX-188 (enforce) / NX-189 (SQL tri-state) — care **consumă** acest registru.

## Ce livrează
Fațetele nu mai sunt `tuple[str]` (`searchable_facets`) — devin **tipizate**: fiecare fațetă are tip,
operatori permiși, valori canonice, politică `missing_value`, provenance și **prag de coverage pentru
enforcement**. Plus raportul care măsoară, pe catalogul real, ce fațetă e gata de enforce și ce fațetă
ar prăbuși recall-ul dacă e aplicată hard.

### Registrul — `src/domain/facets.py`
`TypedFacet` (imutabil) + `build_facets(config)` validat la load. **Invariant de siguranță central:**
config-ul NU poate injecta SQL / JSON paths. Sursa e validată contra unui **allowlist din COD**:
- `column` → doar `{price, sale_price, rating, stock_total}`;
- `category` → fix (`primary_category_id` → `categories.slug`), niciodată din config;
- `attribute` → orice cheie, dar citită **parametrizat** `attributes->$key`, iar cheia trebuie să
  respecte `^[a-z_][a-z0-9_]*$` (blochează `a->b`, `; drop table`, spații, ghilimele).

**Fail-closed per fațetă:** orice intrare cu sursă/tip/operator/cheie/prag invalid e **respinsă la
load** (logată, nu încărcată) → nu devine `TypedFacet`, deci nu poate ajunge la enforcement. O fațetă
stricată nu dărâmă restul registrului. (NX-182 nu a fost construit → nu există registru minimal de
absorbit; labels-urile intră direct în registrul complet, un singur loc.)

Aditiv: `searchable_facets` (string-uri) rămân până sunt migrate. Registrul e per-vertical
(`beauty_salon.json` → 10 fațete) + override per-tenant, generic (P9).

### Raportul de coverage — `scripts/facet_coverage.py`
Per **business × category × facet** (DoD): denominator explicit (produse publicate din categorie),
prag minim de produse (< 5 → „date insuficiente", nu 100% fals), **3 stări DISTINCTE de provenance**
(`present` ≠ `valid` în registru ≠ `verified` prin `claim_provenance`), `unknown_rate` = 1 − coverage
(fracția UNKNOWN sub enforcement, D7), `value_distribution` (enum/list → NX-188 estimează MATCH vs
MISMATCH per valoare de query), `enforce_ready` (coverage ≥ prag PER fațetă ȘI date suficiente).
`compute_coverage(...)` e PUR (testabil fără DB). Ieșire: `reports/facet-coverage-<biz>-<data>.json`.

## Praguri per fațetă + coverage măsurat (pilot demo, 300 produse publicate, 38 categorii)

| Fațetă | Tip | Provenance | `min_coverage` | Coverage real | Verified% | Enforce-ready |
|---|---|---|---|---|---|---|
| `price` | number | structural | 0,98 | **1,00** | 100% | 38/38 |
| `category` | enum | structural | 0,98 | **1,00** | 100% | 38/38 |
| `suitable_for` | list | claim | 0,50 | 0,71 | 0% | 26/38 |
| `key_ingredients` | list | claim | 0,50 | 0,64 | **100%** | 23/38 |
| `concerns` | list | claim | 0,50 | 0,55 | 0% | 20/38 |
| `texture` | text | structural | 0,30 | 0,46 | 100% | 17/38 |
| `fragrance_free` | bool | claim | 0,40 | 0,40 | **0%** | 20/38 |
| `finish` | enum | structural | 0,15 | 0,23 | 100% | 11/38 |
| `coverage` | enum | structural | 0,10 | 0,07 | 100% | 3/38 |
| `spf` | number | structural | 0,05 | 0,02 | 100% | 3/38 |

## Ce spun cifrele pentru NX-188 (enforce)
- **`price`, `category`:** universale + verified → **hard-enforce oriunde**, fără risc de UNKNOWN.
- **`fragrance_free`:** 40% coverage și **0% merchant-verified** (claim AUTORAT, fără proveniență) —
  un `unknown_rate` de 60% + zero confirmare. **NU enforce hard ca fapt confirmat** → soft/disclosure
  (exact tensiunea D5: confirmat ≠ derivat). Semnalul e vizibil DOAR pentru că cele 3 stări sunt separate.
- **`concerns`/`suitable_for`:** coverage decentă dar 0% verified → utile la ranking (soft), nu ca gate dur.
- **`key_ingredients`:** singura fațetă-claim cu proveniență (100% verified, `kind=ingredient`) → poate fi confirmată.
- **`finish`/`coverage`/`spf`:** structurale dar SPARSE (coverage 2-23%) → enforce DOAR per-categorie
  unde `enforce_ready`, altfel UNKNOWN masiv (D7) prăbușește recall-ul.

## Out of scope (per card)
Aplicarea în Match Gate (NX-187) și mutarea în SQL tri-state (NX-189) — acele carduri consumă acest
registru + praguri, nu le reimplementează.
