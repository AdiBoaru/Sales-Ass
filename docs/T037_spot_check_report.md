# T037 — Raport spot-check calitate date produse demo

_Data: 2026-06-12 · Business: `nativex-demo` (6098812a-...) · 500 produse, 500 variante_
_Tool: `scripts/spot_check.py` (scan sistemic pe TOT catalogul + 20 eșantioane)_

## Concluzie

Datele sunt **structural curate** dar **sintetice** (generate, nu catalog real sole.ro —
ai_summary spune literal „Produs fictiv de tip..."). Bune pentru testarea pipeline-ului.
**1 problemă de COD** (prețul) + **1 gap de date** (linkuri) de tratat.

## Scan sistemic (500 produse) — tot OK structural

| Verificare | Rezultat |
|---|---|
| Nume cu entități HTML (`&amp;` etc.) | ✅ 0 |
| Nume cu spații la capete | ✅ 0 |
| Fără brand / categorie mapată | ✅ 0 / 0 |
| Preț lipsă/≤0 · sale_price > price | ✅ 0 / 0 |
| Fără ai_summary · ai_summary <40 char · duplicate | ✅ 0 / 0 / 0 |
| Variante fără sku / fără stock | ✅ 0 / 0 |

## Probleme găsite

### 🔴 1. Prețul afișat e GREȘIT — `search_products` ignoră varianta (problemă de COD)
200/500 produse au `product.price` ≠ prețul real al variantei. Structura reală:
fiecare produs are **1 variantă**, iar **varianta are `sale_price` mai mic**.
Exemplu: produs `137.99` RON, dar varianta se vinde cu `113.99` RON.

`search_products` întoarce acum `coalesce(product.sale_price, product.price)` = **137.99**,
deci botul ar cota un preț **mai mare** decât cel real (113.99). Validatorul NU prinde asta
(compară cu retrieval-ul, adică tot cu prețul greșit) — exact riscul din cardul T037.

**Fix recomandat:** `search_products` (și tool-urile de preț) să surseze prețul din
`product_variants` (min `coalesce(v.sale_price, v.price)`), nu din `products.price`.
→ task de cod separat (propus).

### 🟡 2. `product_url` NULL pe toate cele 500 (gap de date seed)
Botul nu poate trimite linkuri către produse, iar `checkout_link` n-are URL de bază.
Nu e fixabil acum (n-avem URL-uri reale). Vine la sync-ul cu un magazin real.
Validatorul de linkuri (principiul 8) va trebui să tolereze lipsa URL în demo.

### ⚪ 3. ai_summary templat/fictiv (acceptabil pt demo)
Format: `nume + breadcrumb categorie + „Produs fictiv de tip..."`. Structural ok
(unic, populat), dar copy-ul de vânzare al agentului va fi subțire pe date fictive.
Doar de notat — la client real, ai_summary vine din sync + LLM pe descrieri reale.

## Acțiuni
- [x] Raport (acest fișier) — output T037
- [ ] **Task nou propus:** `search_products` + tool-uri preț → sursează din variante, nu din products.price
- [ ] La integrarea unui client real: populează `product_url` + ai_summary din date reale
- Fără `docs/013_data_fixes.sql` — nu există typo-uri/entități de corectat (date curate)
