# NX-240 — Data readiness pentru `web-view.v2`: ce putem afirma, ce omitem, ce lipsește

**Status:** măsurat pe tenantul demo `6098812a-50fc-44bd-a1ba-bc77e6399158` la 2026-08-14 ·
**Sursa cifrelor:** `python scripts/nx240_data_readiness.py --json reports/nx240/coverage.json`
(citește DOAR catalogul, tenant-scoped; zero OpenAI, zero scriere)

Documentul ăsta nu descrie ce *ar trebui* să afișeze widgetul. Descrie ce **poate** afișa pe
datele care există azi, și ce anume ar trebui să se schimbe în date ca să afișeze mai mult. E
ordinea corectă: paritatea vizuală cu iZi nu autorizează date inexistente.

---

## 1. Matricea de câmpuri

Matricea trăiește ca **date**, în `src/agent/evidence_bundle.py::FIELD_POLICY`, nu în tabelul de
mai jos. Tabelul e o redare; `tests/test_web_data_readiness.py` verifică faptul că politica e
completă și coerentă cu implementarea (`sellable`, `STRUCTURALLY_UNSOURCED`, formatterele).
Motivul e experiența v1: contractul scris într-un `.md` a divergent tăcut de cod, iar frontendul
a ajuns să derive stocul dintr-un `0/null` pentru că nicăieri nu scria că `0` înseamnă „nu știm".

| câmp | owner | sursă canonică | SLA | formatter | când lipsește | blochează CTA |
|---|---|---|---|---|---|---|
| `identity` | catalog | `products.id` | — | — | produs omis | da |
| `title` | catalog | `products.name` | — | — | produs omis | da |
| `brand` | catalog | `brands.name` | — | — | subtitlu fără brand | nu |
| `url` | catalog | `products.product_url` | — | — | fără buton „Vezi produsul" | nu |
| `image` | catalog | `product_images.url` | — | — | card fără poză | nu |
| `currency` | catalog | `products.currency` | — | — | **preț omis** | da |
| `price` | catalog | `products.price/sale_price` + min(variante) | da | `format_money` | preț și reducere omise | da |
| `list_price` | catalog | `products.price` în fereastra de sale | da | `format_discount` | fără preț tăiat, fără procent | nu |
| `availability` | catalog | `products.availability` | da | `format_availability` | fără etichetă (NU „indisponibil") | da |
| `stock` | catalog | `products.stock_total` / `variants.stock` | da | `format_availability` | etichetă generică în loc de „ultimele N" | nu |
| `variant` | catalog | `product_variants.label` | — | — | subtitlu fără variantă | nu |
| `rating` | catalog | `products.rating` (cu `review_count > 0`) | — | `format_rating` | fără rating (NU „0 stele") | nu |
| `review_count` | catalog | `products.review_count` | — | `format_rating` | rating fără paranteză | nu |
| `review_summary` | content | `product_review_summaries.summary` | — | — | fără rezumat | nu |
| `delivery_promise` | fulfillment | **niciuna** | da | — | fără ETA, fără notice | nu |
| `promotion` | promotions | **niciuna** | da | — | fără promoție | nu |
| `voucher` | promotions | **niciuna** | da | — | fără voucher | nu |

---

## 2. Coverage măsurat (300 produse active, demo)

| câmp | known | stale | unknown | verificat (`synced_at`) |
|---|---|---|---|---|
| identity, title, brand, url, image | 100% | 0% | 0% | **0%** |
| currency, price, availability, stock | 100% | 0% | 0% | **0%** |
| rating, review_count, review_summary | 100% | 0% | 0% | **0%** |
| `list_price` | 30% | 0% | 70% | 0% |
| `variant` | n/a¹ | — | — | — |
| `delivery_promise`, `promotion`, `voucher` | **0%** | 0% | **100%** | — |

¹ `variant` are sens doar când turul selectează o variantă anume; scriptul măsoară produse, nu
perechi (produs, variantă), deci coloana e goală prin construcție, nu prin lipsă de date.

**Ce înseamnă în practică:** cardurile pot afișa nume, brand, poză, preț, reducere (unde există),
stoc, rating și recenzii. Livrarea, promoțiile și voucherele **nu apar deloc** — iar `GroundingGuard`
respinge întregul răspuns dacă modelul le afirmă în proză (`unsourced_delivery_claim`,
`unsourced_promo_claim`, `unsourced_warranty_claim`). Nu e o degradare de UI, e refuzul de a
susține o afirmație pe care nu o putem verifica.

---

## 3. `verified_at` ≠ `updated_at` — și de ce contează mai puțin decât pare

`updated_at` spune când s-a **atins** rândul (poate fi o corectură de descriere). `synced_at`
spune când sincronizarea l-a **confruntat cu sursa**. Doar al doilea e o verificare, iar în
mediul curent el e NULL pe 300/300 produse: catalogul demo e curat manual, fără pipeline de sync.

Consecința e mai îngustă decât ar sugera intuiția, și asta e deliberat:

- un fapt **verificat** care depășește `COMMERCE_FACTS_SLA_S` devine `stale` ⇒ nu se afișează, iar
  CTA-ul care depindea de el dispare;
- un fapt **neverificat** nu poate deveni `stale` — nu avem de unde ști că s-a stricat — deci se
  afișează ca valoare de catalog și poate purta un CTA.

**De ce CTA-ul nu cere `verified`.** Butonul „Adaugă în coș" nu e o garanție de inventar:
`CartService` (NX-237) rehidratează și revalidează preț/stoc/siguranță **înainte de fiecare
mutație**, în tranzacție. Un click pe un buton învechit produce un refuz onest, nu un coș greșit.
A cere `verified` pe buton ar fi însemnat zero comerț pentru orice tenant fără sync (măsurat:
0/300), plătind cu toată funcționalitatea pentru o siguranță pe care o avem deja acolo unde
contează. Butonul e o ofertă de a încerca; garanția e la mutație.

**Ce ar aduce un sync real:** prospețime afișabilă („verificat acum 2 minute") *și* capacitatea de
a detecta expirarea. Ambele sunt blocate azi de aceeași absență, nu de cod.

---

## 4. Ce NU e suportat, explicit

| lipsă | efect în ViewModel | ce ar debloca |
|---|---|---|
| adaptor de livrare | fără ETA; orice promisiune în proză = răspuns respins | card separat (adaptor fulfillment) |
| motor de promoții | fără voucher/cupon; orice mențiune = răspuns respins | card separat (promotion engine) |
| garanție/retur ca dată structurată | orice afirmație = răspuns respins | fapt juridic per tenant, nu coloană de catalog |
| `products.synced_at` populat | nimic nu poate fi declarat expirat; fără text de prospețime | pipeline de sync catalog |
| vocabular `need_key → etichetă` | blocul `memory` afișează doar `budget_max` și `brand` | DomainPack cu etichete afișabile |
| `badges` cu sursă | zero badge-uri emise | o sursă canonică de badge (nu text AI) |

Niciuna nu e completată cu valori fabricate, nici în schemă, nici în prompt, nici în fixture.

---

## 5. Bugetul de query-uri (anti-N+1)

`EvidenceBundle` se construiește din rândurile **deja retrievate** — zero I/O propriu. Projectorul
e pur prin construcție (`tests/test_web_render_v2.py` verifică prin AST că nu conține apeluri de
ceas/config/await). Testele rulează scenariile la 1 / 6 / 10 produse plus comparație și cer
explicit zero atingeri de DB; `query_count` transportă câte **căutări** au alimentat faptele, ca
bugetul să fie asertabil, nu presupus.

---

## 6. Cum se reproduce

```bash
python scripts/nx240_data_readiness.py                                  # raport în consolă
python scripts/nx240_data_readiness.py --json reports/nx240/coverage.json
python -m pytest tests/test_web_data_readiness.py -q                    # matricea + bugetul
```
