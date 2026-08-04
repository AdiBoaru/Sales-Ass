# Brief: NX-166 — Selecția de nuanță (shade selection) makeup, iZi-parity

> **Scop.** A doua părere (Codex) pe spargerea NX-166 în a/b/c. Descoperit prin „pipeline-first" pe
> eșantionul makeup (4 fonduri, 42 variante): comparația merge, dar **selecția de nuanță — killer-
> feature-ul iZi — are gap de ENGINE/CONTRACT, nu de date**. Scris ca să fie citibil fără context.
> Grounding = citit în cod (căi exacte mai jos), nu presupus.

---

## 0. TL;DR
- Datele sunt seedate corect (42 variante cu `label` / `color_hex` / `undertone` / `depth` / `stock`,
  incl. 1 OOS: „NudeLab Tan Warm 08" stock=0). Dar **variantele nu ajung în NICIUN payload de output**
  → FE nu poate randa selectorul de nuanțe, OOS-ul e invizibil, agentul nu recomandă nuanță.
- Fix spart în 3: **NX-166a** (backend + contract — ACUM, „C-first"), **NX-166b** (FE, repo separat),
  **NX-166c** (recomandare de nuanță — după ce payload-ul există).
- **NU scalăm catalogul** (12-15 fonduri) până cardurile poartă nuanțe — densitatea nu rezolvă un gap
  de engine.

## 1. Ce am dovedit (pipeline-first, pe eșantion)

| DoD (tur sim) | Status | Notă |
|---|---|---|
| Velora găsit | ✅ | search-ul găsește fondurile (count=3) |
| Comparație Finish/Acoperire | ✅ | folosește atribute la nivel de **produs** (comparison_facets DomainPack) |
| Nuanță 07 recomandată | ⚠️ nu | agentul tratează întrebarea de nuanță ca SEARCH → recomandă produsul, nu nuanța |
| OOS vizibil | ❌ nu | nuanța epuizată (Tan Warm 08) invizibilă peste tot |

## 2. Starea curentă în cod (grounding exact)
- **Read-path hidratează variante, DAR incomplet.** `_VARIANTS_AGG`
  ([catalog.py:40-54](src/db/queries/catalog.py#L40)) face `jsonb_agg` per variantă doar cu:
  `id, label, sku, price(efectiv=coalesce(sale_price,price)), stock`. **Lipsesc: `color_hex`,
  `sale_price` (anchor), `attributes` (shade/undertone/depth).** Cap deja `limit 12`.
- **Card payload nu poartă variante.** `_card_products`
  ([fallbacks.py:198](src/agent/fallbacks.py#L198)) emite doar `product_id, name, price, url, image`.
  Nicio cale în `finalize`/`planner`/`compose`/`web` nu serializează variante în output.
- **Contractul FE n-are câmp de variantă/shade** (`docs/FRONTEND-CONTRACT-IZI.md`).
- **Agentul (text) vede etichete fără stoc.** `_detail_view`
  ([catalog_tools.py:143](src/tools/catalog_tools.py#L143)) formatează `[id] label (preț)` — fără
  stoc per-variantă → nu poate spune „08 e epuizat".
- NX-135 (#179, merged) = „fallback pe search variant_label" (căutare), NU randare de variante.
  NX-118 (#127) = hidratare + grounding availability-aware în validator (validator citește variante),
  dar tot nu le EMITE în payload.

## 3. NX-166a — backend + contract (ACUM, izolat, cu teste)
**Scope (strict):**
1. Extinde `_VARIANTS_AGG` `jsonb_build_object`: `+ color_hex`, `+ sale_price` (raw, pt anchor „de la X"),
   `+ attributes` (shade/undertone/depth din `v.attributes`). Cap `limit 12 → 16`.
2. `_card_variants(p)` (mapper compact) + include `variants[]` în payload-ul de card. Câmpuri minime
   per variantă: `variant_id, label, price, sale_price, stock, color_hex, attributes.shade,
   attributes.undertone, attributes.depth`.
3. **OOS explicit prin `stock: 0`** — nu se filtrează/ascunde (FE îl marchează disabled).
4. Update `docs/FRONTEND-CONTRACT-IZI.md` cu câmpul `variants`.
5. Teste: „NudeLab Tan Warm 08" apare cu `stock=0`; „Medium Warm 07" (Velora) apare în payload cu
   `undertone=warm, depth=medium`.
6. **FĂRĂ flow LLM nou** (166c e separat).

**Decizie de design (întrebare cheie pt Codex):** emit `variants[]` pe TOATE cardurile de fond (rezultate
search) SAU doar în mod-detaliu (`get_product_details` / card single)? iZi arată selectorul pe cardul de
fond, dar 16 variante × 6 produse = payload greu pe listă. Opțiune intermediară: pe listă doar
`has_shades + shade_count + swatch_preview[3]`, variantele complete doar pe detaliu.

## 4. NX-166b — FE (repo separat „Sales MVP Frontend")
- Shade selector + swatches (din `color_hex`); grid de nuanțe.
- OOS: nuanța cu `stock:0` = disabled/marcată vizual (nu ascunsă).
- Nuanța selectată → add-to-cart / checkout (poartă `variant_id`).
- Update spec în `FRONTEND-CONTRACT-IZI.md` (partajat cu 166a).

## 5. NX-166c — recomandare de nuanță (DUPĂ ce payload-ul există)
- Agentul sugerează nuanța pe `undertone`/`depth` („ten mediu cald" → „Medium Warm 07").
- Doar după ce UI + backend pot EXPRIMA nuanța — evităm să cerem modelului ceva ce interfața nu poate
  arăta (altfel proză despre o nuanță pe care userul n-o poate selecta).
- Probabil: `get_product_details` capătă în `_detail_view` și stoc + undertone/depth per variantă, ca
  agentul să aibă pe ce recomanda/gata; sau un tool dedicat de shade-match.

## 6. Întrebări pentru Codex
1. **Câmpuri payload** — minimul (variant_id, label, price, sale_price, stock, color_hex, shade,
   undertone, depth) e ok? Ceva de adăugat (sku?) / scos?
2. **Cap 16 variante/card** — rezonabil? (fondurile ~10-12 nuanțe; concealer mai puține.)
3. **Variants pe listă vs detaliu** (vezi §3): pe toate cardurile de fond, sau doar detaliu, sau hibrid
   (preview pe listă + full pe detaliu)? Care e cel mai aproape de iZi fără payload greu?
4. **OOS**: `stock:0` explicit în payload (FE marchează) — de acord, vs a-l filtra din selector?
5. **Ratăm ceva** structural pt 166b (FE) sau 166c (recomandare)? (ex. `color_hex` NULL pe unele
   variante → FE fallback; sau `variant_id` trebuie stabil pt cart membership.)

## 7. Recomandare
NX-166a acum (backend + contract — schimbare mică, izolată, cu teste; deblochează exact killer-feature-ul).
NX-166b în repo-ul FE după. NX-166c după ce payload-ul există. **Scale catalog (12-15 fonduri) DUPĂ 166a** —
atunci densitatea de nuanțe are sens (cardurile o pot transporta).

## 8. Referințe
`db/seed/makeup_catalog.json` (variante seedate), `scripts/seed_makeup_catalog.py`,
`src/db/queries/catalog.py` (`_VARIANTS_AGG`), `src/agent/fallbacks.py` (`_card_products`),
`docs/FRONTEND-CONTRACT-IZI.md` (contract card FE), `docs/PILOT-MAKEUP-CATALOG-2026.md` (brief-ul care a
dus la seed). [[web-render-contract-fe-separate]], [[izi-parity-root-cause-catalog-domain]].
