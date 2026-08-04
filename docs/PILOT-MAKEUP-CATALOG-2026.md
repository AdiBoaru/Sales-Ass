# Brief: seed catalog makeup realist — deblocarea demo-ului iZi-parity (2026-07)

> **Scop.** Brief pentru a doua părere (Codex). „Zidul de date" recurent: demo-ul e skincare, iar
> paritatea iZi pe makeup (selecție nuanță, comparație fond, mod detaliu) e blocată de LIPSA de date,
> nu de engine. Scris ca să fie citibil FĂRĂ context. Findings = verificate în `db/seed/catalog.json`
> + `src/domain/defaults/beauty_salon.json`.

---

## 0. TL;DR
- Demo-ul (Sole Demo, vertical beauty) are **500 produse, ~90% skincare + tools**. **ZERO fonduri de
  ten**, zero variante reale (doar placeholder „Standard"/produs), zero atribute makeup
  (finish/coverage/undertone/shade). DomainPack-ul beauty e 100% skincare.
- **Engine-ul suportă DEJA makeup iZi-parity** (`comparison_facets`, `searchable_facets`,
  `product_variants`, mod detaliu). Lipsește DOAR data + câteva fațete în DomainPack.
- **Fix:** (1) sub-catalog makeup realist (flagship = fonduri cu nuanțe ca VARIANTE) + (2) extindere
  DomainPack cu vocabular makeup. Ambele = DATE/CONFIG, ~zero cod nou.
- **Decizie de luat:** scope-ul (foundation-first vs gamă completă vs audit+plan întâi).

---

## 1. Context (1 minut)
Nativx = AI Sales Assistant pe WhatsApp/web pentru retail RO (beauty). Ținta de calitate = **paritate
iZi** (asistentul eMAG): carduri de produs, selecție de nuanță pe fond de ten, comparație structurată,
mod detaliu bogat. Demo-ul rulează pe „Sole Demo" (500 produse seedate). Repetat s-a lovit de faptul
că demo-ul **nu poate arăta** selecția de nuanță / comparația de fonduri — motivul real = catalogul
demo e skincare, nu makeup.

## 2. Findings — realitatea de date (verificată)

| Dimensiune | Realitate (din `catalog.json`, 500 produse) |
|---|---|
| Compoziție | ~90% skincare/tools: seruri 63, măști 52, creme-zi 49, curățare 47, pensule 36, tonere 34, SPF 24, șampoane 19 |
| Fonduri de ten | **0** — nicio categorie `fond-de-ten`, niciun produs (există doar BB/CC + cushion adiacente) |
| Variante | 500/500 au `variants`, dar = **placeholder** `{label:"Standard", ...}` — fără shade/nuanță reală |
| Atribute produs | RAW scraped: `Cod EAN`/`Cod memoX` (500), `Culoare` (doar 23), `Ingrediente` (11), ingrediente one-off (noise). **Makeup attrs (finish/coverage/undertone/shade): 0** |
| DomainPack beauty | concern_map = skin types + skincare concerns; comparison_facets = key_benefit/key_ingredients/concerns; searchable_facets = key_ingredients. **Zero makeup** |

> NB: `catalog.json` (seed brut) n-are nici `concerns`/`key_benefit` — acelea sunt îmbogățite pe DB
> LIVE de `enrich_*` scripts POST-seed. Dar enrich-ul îmbogățește skincare-ul existent, NU adaugă
> produse makeup. Deci LIVE ≈ seed pe compoziție: **~0 makeup**. (De confirmat cu audit live — vezi
> opțiunea C.)

## 3. De ce e DATĂ, nu engine
Engine-ul are deja mecanismele de care iZi-parity are nevoie, generic pe vertical (nimic hardcodat):
- **`product_variants`** — nuanțele de fond = variante (label + attributes). Randorul FE le afișează
  ca selector de nuanță (contract în `docs/FRONTEND-CONTRACT-IZI.md`).
- **`comparison_facets`** (DomainPack) — rânduri de domeniu în tabelul de comparație din
  `products.attributes`. Un atribut nou (finish/coverage) = o fațetă adăugată aici (nota din pack o
  spune explicit).
- **`searchable_facets`** — filtru la cerere pe atribute normalizate („ceva mat" → `finish=matte`).
- **mod detaliu** — `get_product_details` + review summary, deja generic.

Deci **fixul e ~90% seed + config**, cum a concluzionat auditul anterior ([[izi-parity-root-cause-catalog-domain]]).

## 4. Fixul propus (2 piese, ambele date/config)

### 4.1 Sub-catalog makeup realist
Produse makeup cu atribute REALE + variante unde e cazul. Model de date:
- **Fond de ten** (flagship): `attributes = {finish: matte|dewy|satin|natural, coverage:
  light|medium|full, skin_type: [oily|dry|combination|normal], spf?}`. **Variante = nuanțe**:
  `{label:"Ivory 100", attributes:{undertone: warm|cool|neutral, depth: fair|light|medium|tan|deep}}`.
- **Concealer**: coverage + variante nuanțe.
- **Mascara**: `{effect: volume|length|curl, waterproof: bool}`, variante culoare (negru/brun).
- **Ruj**: `{finish: matte|satin|gloss|cream}`, variante nuanțe.
- **Farduri (paletă)**: `{finish, color_family}`.
- **Pudră/fixare**: finish + coverage.
Brand-uri = brand-uri DEMO (ca `original_brand_replaced` existent, fără IP real). Prețuri RON
realiste, rating variat, imagini placeholder, descrieri RO display-ready.

### 4.2 Extindere DomainPack beauty (makeup)
- `comparison_facets` += finish, coverage, undertone (cu `value_labels` RO/EN/HU).
- `searchable_facets` += finish, coverage, undertone (match normalizat pe atribut).
- `profile_whitelist`/`fact_type_whitelist` += undertone, shade_preference, finish_preference.
- (opțional) mapări RO în vocabularul de căutare: „acoperire mare"→full, „mat"→matte, „luminos/
  dewy"→dewy, „cald"→warm.
- Categorie nouă `fond-de-ten` (+ ruj/mascara/concealer dacă lipsesc) în taxonomie.

## 5. Scope — cele 3 opțiuni (DE ALES)

### Opțiunea A — Foundation-first (flagship) ✅ recomandarea mea
~10-15 **fonduri × ~12 nuanțe reale** (variante undertone+depth) + ~8 concealere, cu finish/coverage/
undertone în DomainPack.
- **+** Țintește DIRECT killer-feature-ul iZi (selecție nuanță + comparație fond + mod detaliu) cu
  efort focusat → cel mai rapid la un demo care impresionează.
- **+** Date curate, ușor de revizuit (un domeniu îngust, bine definit).
- **−** Nu acoperă search pe alte categorii makeup (ruj/mascara) — dar alea nu-s killer-feature-ul.

### Opțiunea B — Gamă makeup completă
~40-60 produse across fond/concealer/mascara/ruj/farduri/pudră/blush + atribute + variante.
- **+** Demo complet de makeup (orice categorie).
- **−** Efort mult mai mare (mult mai multă dată curată de generat/revizuit); risc de date
  superficiale dacă se grăbește; killer-feature-ul (fond+nuanțe) e diluat în volum.

### Opțiunea C — Audit live + plan scris întâi
Rulez audit pe DB LIVE (nu doar seed.json) → doc de plan detaliat (categorii/atribute/variante exacte,
brand-uri demo, sursa datelor, legătura cu FRONTEND-CONTRACT) → aprobi → construiesc.
- **+** Zero surprize; confirmă starea LIVE (poate diferă de seed).
- **−** Mai lent la cod; dar recuperabil dacă vrem certitudine înainte de efort.

## 6. Abordare de implementare (indiferent de scope)
- Un script `scripts/seed_makeup_catalog.py` (stil cu tooling-ul existent: `enrich_catalog.py`,
  `seed_*`) care INSEREAZĂ produse + variante + atribute în DB (idempotent, marcat demo).
- Update `src/domain/defaults/beauty_salon.json` (fațete makeup) + re-seed DomainPack
  (`seed_demo_domain_pack.py`).
- Re-embed produsele noi (`embed.ts`/job embed) pentru search semantic.
- Verificare: audit coerență (`audit_catalog_coherence.py`) + un test de căutare makeup + un tur sim
  („caut un fond de ten cu acoperire mare pentru ten gras, nuanță deschisă").

## 7. Întrebări pentru Codex
1. **Scope:** A (foundation-first) vs B (gamă completă) vs C (audit+plan)? Eu recomand A —
   maximizează impact-demo/efort. De acord?
2. **Realismul datelor:** hand-authored curat (10-15 fonduri) vs generat cu LLM + revizuit? Pentru un
   demo care „vinde", cât de realist trebuie (nuanțe cu nume reale, game de undertone coerente)?
3. **Brand-uri:** demo-brands fictive (ca `original_brand_replaced`) sau nume reale de brand? (IP vs
   credibilitate demo.)
4. **Variante ca shade:** confirmi că `product_variants` (label + attributes.undertone/depth) e calea
   corectă pt selecția de nuanță (vs atribut pe produs)? FE randează variante ca selector?
5. **Ratăm ceva?** (ex. FRONTEND-CONTRACT are nevoie de un câmp nou de card pt nuanțe; sau
   `searchable_facets` pe undertone/depth are un gotcha de normalizare.)

## 8. Recomandare
**Opțiunea A (foundation-first).** Un set curat de ~12 fonduri × ~12 nuanțe + concealere + fațetele
makeup în DomainPack deblochează exact demo-ul iZi (nuanțe + comparație + detaliu) cu efort focusat și
date de calitate. Gama completă (B) = follow-up după ce flagship-ul convinge.

## 9. Referințe
`db/seed/catalog.json` (seed), `src/domain/defaults/beauty_salon.json` (DomainPack),
`docs/FRONTEND-CONTRACT-IZI.md` (contract card/comparison FE), `scripts/enrich_catalog.py` +
`scripts/seed_demo_domain_pack.py` (tooling), audit anterior iZi-parity.
