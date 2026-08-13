# NX-234 — Data readiness al contextului de pagină

**Ce e documentul ăsta.** Contextul de pagină poate ancora un produs. Nu garantează că faptele
necesare unei experiențe de nivel iZi *există*. Matricea de mai jos spune, pentru fiecare fapt
comercial, de unde vine, cât de proaspăt e și **ce se întâmplă când lipsește** — pentru că
răspunsul „ce se întâmplă când lipsește" e singurul care decide dacă botul minte sau nu.

Regula care traversează tot tabelul: **`UNKNOWN` nu e `0` și nu e `MISMATCH`.** Un produs fără
recenzii nu are „0 stele"; un `delivery_class` absent nu înseamnă „nu livrăm". Downstream
(NX-240) omite claimul *și* CTA-ul care depinde de el. Nici modelul, nici frontendul nu umple
golul.

Sursa de adevăr a implementării: [`src/catalog/context_resolver.py`](../src/catalog/context_resolver.py)
(`_product_evidence` / `_variant_evidence`), query-ul în
[`src/db/queries/catalog.py`](../src/db/queries/catalog.py) (`load_context_entities`).

---

## 1. Matricea `field → source → freshness → UNKNOWN`

| Fapt | Sursă canonică (query/adaptor) | Semnal de prospețime | SLA | Comportament `UNKNOWN` |
|---|---|---|---|---|
| `price` (efectiv) | `coalesce(min(product_variants.price\|sale_price), products.price\|sale_price)`, cu fereastra `sale_start/sale_end` | `products.synced_at` → `updated_at` | `WEB_CONTEXT_FRESHNESS_SLA_S` (24h) | `price=None` + `unknown={"price"}`; fără preț nu se afirmă niciun cost |
| `list_price` | `products.price` **doar** când promoția e activă în fereastră | idem `price` | 24h | `None` = **nicio promoție activă** (fapt cunoscut negativ), nu „necunoscut" |
| `currency` | `products.currency` (NOT NULL, default `RON`) | idem `price` | 24h | nu poate lipsi structural |
| `price_source` | derivat: `variant_min` \| `product` | — | — | spune CARE preț s-a folosit; fără el, „89 lei" al contextului și al validatorului pot diverge tăcut |
| stoc / disponibilitate | `products.availability` (NOT NULL, enum) | idem `price` | 24h | clasa e mereu cunoscută |
| `stock_total` | `products.stock_total` (nullable) | idem `price` | 24h | `unknown={"stock_total"}` — „câte bucăți" nu se afirmă |
| stoc per variantă | `product_variants.stock` (NOT NULL) | `product_variants.updated_at` | 24h | cunoscut; `0` **este** un fapt (epuizat), nu o absență |
| `rating` | `products.rating` **numai dacă** `review_count > 0` | idem `price` | 24h | `rating=None` + `unknown={"rating"}`. `products.rating` are `default 0`: fără gardul ăsta, „fără recenzii" ar arăta identic cu „evaluat cu zero" |
| `review_count` | `products.review_count` | idem `price` | 24h | `0` → tratat ca absență de recenzii, nu ca „0 recenzii afișabile" |
| `review_summary` | `product_review_summaries.summary` (job offline) | rândul propriu | 24h | `unknown={"review_summary"}` |
| promisiune de livrare | **nu există feed canonic** | — | — | `STRUCTURALLY_UNKNOWN`. Ce ținem e `delivery_class` (fapt stabil). Promisiunea depinde de CEAS (lecția NX-191: „mai ai 2 ore" servit a doua zi) → nu intră niciodată într-un snapshot |
| `delivery_class` | `products.delivery_class` (migrarea 032, nullable) | idem `price` | 24h | `unknown={"delivery_class"}` |
| promo / voucher eligibility | **nu există feed canonic** | — | — | `STRUCTURALLY_UNKNOWN`, mereu. Un `updated_at` recent nu dovedește că un voucher e valabil azi |
| `url` | `products.product_url` | idem `price` | 24h | `unknown={"url"}` → nu se emite CTA de navigare (PP-F4: mesaj onest, nu link inventat) |
| `image` | `product_images` (prima după `position`) | — | — | `unknown={"image"}` → card fără poză, nu placeholder inventat |
| categorie | `categories` prin `products.primary_category_id` | `categories.updated_at` | 24h | absentă → fără context de categorie; NU devine filtru |
| coș | **nu există sursă canonică până la NX-237** | — | — | `CartSnapshot(status="unavailable", reason="no_canonical_cart_source")`. `ConversationState.cart` e ce a pus botul în conversație, **nu** coșul magazinului |

### Ce înseamnă `stale`

`stale` **marchează**, nu ascunde. Un catalog care se sincronizează o dată pe zi ar deveni
inutilizabil dacă „vechi" ar însemna „aruncă". Valoarea rămâne în snapshot, cu
`freshness.bucket` (`<1h` / `1-24h` / `1-7d` / `>7d` / `unknown`) și `freshness.stale=True`;
politica de disclosure/refresh e a consumatorului, dar ca s-o poată aplica trebuie să știe.

Un rând **fără** timestamp e tratat conservator ca `stale`: prospețimea se dovedește, nu se
presupune din lipsa de dovadă.

---

## 2. Măsurarea acoperirii (obligatorie înainte de pasul 2 al rolloutului)

Fixture-urile de frontend, textul modelului și câmpurile vechi din `conversations.state` **nu**
sunt surse canonice și nu contează ca acoperire. Se măsoară pe catalogul REAL al tenantului:

```bash
python scripts/web_context_coverage.py --business-id 6098812a-50fc-44bd-a1ba-bc77e6399158
```

Scriptul e READ-ONLY (fără OpenAI, fără scrieri) și raportează, per categorie rădăcină, procentul
de produse active care au `price`, `url`, `image`, `rating` (cu recenzii reale), `review_summary`
și `delivery_class`, plus distribuția de prospețime pe buckets.

**Interpretare:** o acoperire mică nu blochează cardul — blochează *claimul* dependent. Dacă
`delivery_class` e sub prag pe o categorie, NX-240 nu are voie să promită livrare pe acea
categorie; nu se „completează" cu o valoare implicită.

### Măsurătoarea pe tenantul demo (2026-08-13)

```
categorie rădăcină           n     price       url     image    rating  review_s  delivery  stock_tot  variant
ingrijirea-tenului         104    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
machiaj                    101    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
ingrijirea-parului          48    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
ingrijire-corp              34    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
buze                         7    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
protectie-solara             6    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%
TOTAL                      300    100.0%    100.0%    100.0%    100.0%    100.0%    100.0%     100.0%   100.0%

Prospețime (coalesce(synced_at, updated_at)):
  fresh_lt_24h        0    0.0%
  stale_1_7d          0    0.0%
  stale_gt_7d       300  100.0%
  no_timestamp        0    0.0%
```

**Acoperirea de conținut e completă; PROSPEȚIMEA nu e.** Toate cele 300 de produse active au
`coalesce(synced_at, updated_at)` mai vechi de 7 zile — catalogul demo e hand-curated (NX-168e),
nu sincronizat dintr-un feed, deci nimic nu-i mai atinge rândurile.

Consecința practică: **cu SLA-ul de 24h, orice context de pagină pe tenantul demo iese `stale`.**
Exact de asta `stale` marchează în loc să arunce — altfel feature-ul ar fi mort din prima zi pe
singurul tenant pe care rulează. Ce trebuie decis înainte de pasul 2 al rolloutului nu e „cum
facem datele proaspete", ci **ce înseamnă `stale` pentru client**: pentru preț și stoc, un produs
neatins de 30 de zile pe un catalog fără sync e la fel de adevărat ca unul atins ieri, fiindcă
sursa nu s-a schimbat. Opțiuni, în ordinea preferinței:

1. `WEB_CONTEXT_FRESHNESS_SLA_S` per-tenant, aliniat la cadența REALĂ de sync a tenantului (un
   catalog fără feed nu are de ce să fie judecat cu SLA-ul unuia care se sincronizează orar);
2. NX-240 tratează `stale` ca disclosure („prețul afișat pe pagină e cel valabil"), nu ca omisiune;
3. la conectarea unui feed real (`catalog_sync_runs`), `synced_at` devine semnalul primar și
   pragul de 24h redevine semnificativ.

Ce NU e o opțiune: să ignorăm `stale` sau să-l stingem global — atunci semnalul dispare și pentru
tenantul care chiar are date vechi.

---

## 3. Bugetul de query-uri

Rehidratarea unui context complet (produs + variantă + categorie) e **un** round-trip
(`load_context_entities`, `UNION ALL`). Același număr pentru 1, 6 sau 10 referințe de produs —
verificat cu contor de `fetch` în
[`tests/test_context_resolver_db.py`](../tests/test_context_resolver_db.py) (`1/6/10`) și în
[`tests/test_turn_snapshot.py`](../tests/test_turn_snapshot.py).

Indexuri folosite (existente, fără migrare nouă): `products_pkey`,
`products_business_id_external_id_key`, `idx_variants_product`, `categories_pkey`,
`categories_business_id_slug_key`. Dacă un `EXPLAIN` pe catalogul real arată altceva, măsurătoarea
se documentează aici și abia atunci se discută un index nou.

---

## 4. Ce NU e acoperit de cardul ăsta

- coș canonic + mutații cu receipt → **NX-237**;
- modelul complet de nevoi/revocări/referințe conversaționale → **NX-235**;
- ranking/search live → **NX-238**, AnswerPlan → **NX-239**, ViewModel → **NX-240**;
- promovarea faptelor în răspuns: aici se calculează evidence-ul, nu se compune text.
