# Catalog Produse v3 — plan reconciliat (sursă de adevăr pentru recomandări)

**Status:** plan aprobat (2026-07-16) · **Track:** Demo Data Overhaul / IZI-parity · **Limbă:** RO-only (localizări RO/HU/EN = out of scope)
**Origine:** analiză arhitecturală comună (Claude build + Codex verify), reconciliată cu starea reală din repo.

---

## Obiectiv

Transformăm catalogul dintr-un rând de e-commerce într-o **sursă de adevăr pentru recomandare conversațională**. Chatbotul trebuie să poată:
- găsi produse relevante și **elimina** produsele incompatibile;
- **explica concret** de ce recomandă un produs (motiv grounded, nu proză tautologică);
- **compara** produse pe diferențe reale (nu „ambele sunt bune");
- răspunde despre ingrediente, utilizare, gramaj, variante, avertizări;
- **evita** afirmații inventate sau medicale.

Problema NU e schema (e matură). E combinația: **date artificiale + câmpuri nevalidate + informație care există în DB dar nu ajunge la chatbot.**

---

## Starea reală (verificată 2026-07-16) — nu repornim de la zero

| Componentă | Stare |
|---|---|
| **NX-167** (filtre pe arbore + coerență categorie retrieval/compare) | ✅ în `main` (#215) |
| **NX-168a** (schema atribute canonice v2 + audit static, regulile 1-6) | ✅ în `main` (#216) |
| **NX-168b** (catalog v2, batch 1-3) | ✅ `main` = **117 produse** (PR #217 merged) |
| **Batch 4** (+33 → **150 produse**, audit static + dry-run verzi) | **PR #218** (din `origin/main`, un commit, diff exclusiv `catalog_v2.json`) — gata de merge → `main`=150 |
| Calea **rich** (`finalize._rich_bundle`) | proiectează deja `comparison_facets` (DomainPack) la model, gated `rich_facets_enabled` |
| Embedding (`embed_products._embed_text`) | = `name \| brand \| ai_summary \| "Potrivit pentru: {concerns}"` |

**Corecție onestă la diagnoza inițială:** atributele NU sunt complet „moarte" — calea rich le surfacețuiește (behind kill-switch) și embedding-ul include `concerns`. Gap-ul PRECIS:
1. `ai_summary` subțire (= `shortDescription`, o linie) → embedding slab + blurb slab;
2. view-urile text de bază `_brief`/`_detail_view`/`_compare_view` NU proiectează faptele structurate (doar calea rich o face);
3. faptele nu-s **canonice/comparabile** între produse + lipsesc `not_recommended_for`/`usage`/`best_for`;
4. tabelele de adâncime PDP (`product_sections`, `ingredients`, `product_badges`, `reviews`) sunt **nepopulate pentru catalogul v2 prin seed-ul v2** (schema le suportă; nu-s „global goale").

---

## Decizii arhitecturale

1. **`description`** = descriere lungă pentru PDP. **`ai_summary`** = rezumat DERIVAT pentru retrieval + conversație. **`attributes`** = faptele canonice ale verticalei.
2. **`ai_summary` NU e sursă de adevăr** și nu poate introduce fapte noi — se generează DIN faptele canonice validate.
3. Ingredientele, badge-urile, avertizările, claims = acceptate **doar cu sursă** (`source`/`verified_at`). LLM redactează drafturi offline, dar **nu inventează** ingrediente, certificări, compatibilități, contraindicații.
4. Produsul comun rămâne separat de **variantele vandabile** (model Schema.org ProductGroup / Shopify ProductVariant).
5. **Faptele de vertical rămân în `attributes` jsonb**, validate prin schema verticalei (DomainPack). **NU** adăugăm coloane `skin_type`/`finish` în `products` (ar sparge multi-vertical). DDL doar pentru capabilități structurale, cross-vertical.
6. **Scop = toate cele 150 de produse** duse la contract complet (decizie 2026-07-16). NU un set „gold" de 30 separat; validarea conversațională se face pe scenariile golden peste cele 150.
7. Catalogul vechi de 500 (templatat) rămâne **arhivat**, nu se repară incremental.
8. **Max 6 produse** ajung la agent; informația transmisă trebuie compactă (fără obiecte complete).
9. **RO-only.** Fără tabel de traduceri, fără embedding per-limbă. Embedding-urile versionate după `product_id` + tip document + model (nu limbă).

---

## Contractul Produsului (v3)

Fiecare produs **activ** trebuie să conțină, pe zone:

| Zonă | Câmpuri |
|---|---|
| Identitate | `name`, `brand`, `category`, `external_id`, `url` |
| Comerț | `currency`, `price`, `sale_price`, `availability`, `stock` |
| Conținut | `short_description`, `description`, `ai_summary` (derivat) |
| Recomandare | `concerns`, `suitable_for`, `not_recommended_for`, `texture`, `routine_step`, `usage` |
| Diferențiere | `key_benefit`, `differentiators`, `finish`, `coverage`, `spf` (unde se aplică) |
| Compoziție | INCI brut, ingrediente normalizate, `key_ingredients` verificate |
| Siguranță | `warnings`, PAO/durabilitate, zonă de aplicare |
| Trust | `rating`, `review_count`, `pros`, `cons`, badge-uri verificate |
| Variante | SKU, GTIN, `label`, shade/size, `price`, `stock`, `color_hex`, cantitate netă |
| Proveniență | `source`, `source_ref`, `verified_at`, `schema_version` |

**Câmpurile obligatorii diferă pe categorie:** fond de ten cere `finish`+`coverage`+variante de nuanță; skincare cere `concerns`+`texture`+`usage`+compoziție; accesoriile NU cer ingrediente.
**`not_recommended_for`** trebuie să conțină **motiv + sursă**. NU se deduce automat din INCI.

**Disciplina anti-„date moarte": fiecare câmp are un consumator.**

| Strat | Câmpuri | Consumator |
|---|---|---|
| Filtru dur (canonic) | `concerns`, `finish`, `coverage`, `key_ingredients`, `spf`; `not_recommended_for` **level=hard** | SQL WHERE / excludere (search) |
| Agent-view (proiectat) | `suitable_for`, `not_recommended_for` (soft→atenționare), `texture`, `usage`, `routine_step`, `best_for` | `_brief`/`_detail_view`/`_compare_view` (NX-169) |
| Runtime (compus) | `reason_codes` → motivul CONTEXTUAL de recomandare | NX-170 produce, NX-169 compune |
| Embedding | doc determinist din fapte | `embed_products._embed_text` (NX-170) |
| PDP depth | sections, INCI, badges, reviews | `get_product_details` extins (NX-168e) |

> **`best_for` vs motiv contextual (corecție review Codex):** `best_for` e STATIC, intrinsec produsului („bun pentru X"). Motivul de recomandare depinde de CEREREA clientului → se compune la runtime din `reason_codes` (NX-170→NX-169), NU se stochează ca propoziție universală pe produs.
> **`not_recommended_for` are SEVERITATE:** `{value, reason, level(hard|soft), source, source_ref, verified_at}`. Doar `hard`+verificat exclude dur; `soft` = penalizare scor + atenționare. Nicio inferență (ex. „acid pe ten sensibil") nu devine excludere dură automată.

---

## Cele 6 pachete → carduri NX

| # Pachet | Card | Scop | DDL |
|---|---|---|---|
| 1. Contract + audit static ✅ | **NX-168d** | contract v3 per-categorie + audit **versionat** (`v2` neschimbat pt seed, `v3` = R1-R13) + tier `warnings` (negații non-fatale). NU blochează seed-ul existent. **Implementat:** `catalog_v3.schema.json` + audit v2/v3 cu `{violations,warnings}` machine-readable + **`evaluate()`** (schemă+reguli, sursă unică seed+171c, schema-first, fail-closed); v2 pe cele 150 = 0 violations, v3 = raportează golurile | nu |
| 2. Catalog complet la contract | **NX-168e** | **toate 150** duse la contract; seed populează DOAR tabele existente (variante, imagini, sections, ingredients, badges, reviews); `ai_summary` DERIVAT; **comută atomic** poarta seed pe `v3`; `net_content`/relații rămân în JSON | nu |
| 3. Agent Product Surface | **NX-169** | proiecție fapte în `_brief`/`_detail_view`/`_compare_view`; compune motivul CONTEXTUAL din reason_codes; compare doar axele care DIFERĂ; teste token-budget | nu |
| 4. Retrieval + reason codes | **NX-170** | doc embedding determinist din fapte; `not_recommended_for` cu **severitate** (hard=excludere, soft=penalizare+atenționare); `reason_codes` | nu |
| 5a. DDL variante | **NX-171a** | `gtin`/`net_content`+imagine pe variante + preț/unitate; backfill din JSON. Varianta = sursă de adevăr pt net_content | **da (026)** |
| 5b. DDL relații | **NX-171b** | `product_relations` (substitute/complement/accessory/routine_next) + **integritate tenant** (FK compus/trigger, test cross-tenant); backfill din JSON | **da (027)** |
| 5c. DDL content quality | **NX-171c** | `content_status`/`schema_version`/`verified_at` cu **backfill sigur** (nullable→backfill→default→NOT NULL→filtru→test count≠0) | **da (028)** |
| 5d. DDL embeddings | **NX-171d** | **fix cheie** `product_embeddings` (PK compus `product_id,doc_type,model`) + query fără duplicate (filtru doc_type+model activ, test dedup) | **da (029)** |
| 6. Validare e2e | **NX-172** | golden pe 12 scenarii + lanț live (audit static→dry-run→audit DB→re-embed→retrieval→sim). **STRICT validare pe 150** (fără autoring). Înghite golden-ul din NX-168c | nu |
| (opțional) extindere | **NX-173** | 150→250, DOAR dacă date reale de demo justifică | nu |

**Ordinea de valoare:** 168d → 168e → 169 → 170 → 171a → 171b → 171c → 171d → 172.

> **DDL nu blochează 168d-170:** feliile 171a-d aduc capabilități noi (preț/ml, rutine curate, quality-gate DB, versiuni embeddings) DEASUPRA recomandării de bază, care e reparată în 168d-170 fără DDL.

---

## Quality gate obligatoriu (un produs NU devine `active` dacă)

- lipsesc câmpurile obligatorii categoriei;
- are valori în afara vocabularului canonic;
- descrierea contrazice atributele;
- variantele n-au preț/stoc/SKU;
- ingredientele/badge-urile n-au sursă;
- nume+descriere duplicate/generice;
- SKU/GTIN duplicate sau invalide;
- nu poate produce un **motiv concret de recomandare**;
- golden conversations întorc categorii sau justificări greșite.

---

## Retrieval (Pachet 4) — două trepte, deasupra NX-167

1. **Filtre dure:** tenant, status, categorie, buget, stoc explicit, incompatibilități (`not_recommended_for`).
2. **Candidați** lexicali + semantici → **reranking** după potrivire, diferențiatori, rating ajustat.
3. LLM-ul **doar explică** selecția folosind numai faptele returnate; nu decide singur compatibilitatea.
Fiecare recomandare produce intern `reason_codes`.

> NX-167 a livrat deja filtrarea pe arbore + coerența de categorie — Pachet 4 e reason_codes + excluderi DEASUPRA, nu reconstrucție.

---

## Reguli de PR (fiecare pachet)

- `ruff check .` + `ruff format --check .` + `pytest -x -q` verzi;
- rollback documentat;
- **NU** combina autoring masiv cu modificări de retrieval în același PR;
- fiecare tabel nou populat are un consumator clar în bot (altfel = date moarte).

---

## Decizie de baseline — REZOLVATĂ (review Codex)

**Batch 4 → PR #218** (nou, din `origin/main`, un singur commit `8c71892`, diff exclusiv `catalog_v2.json`, 117→150). Merge acum → `main`=150.
Re-seed live `--archive-old` + audit live (regula 7) + re-embed = **DUPĂ NX-168e** (când catalogul e la contract v3), nu acum.

## Reconciliere review Codex (2026-07-16)

Cele 7 HIGH + 3 MEDIUM + LOW reconciliate în carduri:
- **HIGH-1** audit versionat (`v2` pt seed, `v3` gate) — 168d nu blochează seed-ul; 168e comută atomic → NX-168d.
- **HIGH-2** `recommendation_reason` → `best_for` (static); motivul contextual compus din reason_codes → NX-168d/169/170.
- **HIGH-3** `not_recommended_for` cu `level(hard|soft)`+provenance; doar hard exclude → NX-168d/170.
- **HIGH-4** dependență circulară 168e↔171 ruptă: 168e populează doar tabele existente, net_content/relații în JSON → NX-168e/171a/171b.
- **HIGH-5** `content_status` cu secvență backfill sigură (test count≠0) → NX-171c.
- **HIGH-6** cheie embeddings PK compus + query fără duplicate → NX-171d.
- **HIGH-7** integritate tenant pe `product_relations` (FK compus/trigger + test cross-tenant) → NX-171b.
- **MEDIUM-1** R7/R12 fatal doar pe afirmații pozitive high-confidence; negații → warnings → NX-168d.
- **MEDIUM-2** NX-171 spart în 171a/b/c/d.
- **MEDIUM-3** NX-172 strict validare pe 150; extinderea 150→250 → NX-173 condiționat.
- **LOW** wording „tabele PDP goale" → „nepopulate pt v2 prin seed-ul v2".

**Runda 2 (reconciliată):** schema v3 fișier SEPARAT (`catalog_v3.schema.json`) + `_validate_schema(data,contract)`; `audit()` întoarce `{violations,warnings}` (seed numără doar violations); `net_content`/`gtin` pe VARIANTE (produs=fallback); `claim_provenance[]` implementabil; content_status = JOB Python per-tenant + flag default off; NX-172 depinde și de 171a/b; `product_relations` UNIQUE+no-self+CHECK; `price_per_unit` bază+unități (kW≠net content). Docs versionate în **PR #219**.

**Runda 3 (reconciliată):** `seed_catalog_v2.py` ADAPTAT la `{violations,warnings}` (numără doar violations; test warnings-only→exit0, violation→exit≠0) — parte din 168d; R8 simplificat determinist (TOATE key_ingredients+badges cer `claim_provenance`; contraindicația hard = provenance INLINE, fără duplicare); NX-171c job rulează audit O DATĂ pe catalog complet per tenant + mapează violations→produse (auditul cere snapshot complet); NX-172 depinde de TOATE 171a-d (închide epicul → validează published + embeddings versionate).

**Runda 4 (reconciliată):** `badges` = `string[]` la nivel de PRODUS, definit explicit în schema v3, fiecare badge cere `claim_provenance` (kind=badge) + teste R8 ingredient/badge cu&fără proveniență (168d); violations = **machine-readable** `{message, product_slugs:[...]}` (nu string) → NX-171c citește `product_slugs`, NU parsează text CLI, duplicatele marchează toate slug-urile; editorial 171c: backfill = job Python `src/jobs/` (NU în migrare, path consistent).
