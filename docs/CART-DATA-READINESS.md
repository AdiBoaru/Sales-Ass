# NX-237 — Data-readiness: coșul canonic și faptele comerciale

**Status:** publicat 2026-08-13 · **Sursa de cod:** `src/commerce/` (`cart_models.py`,
`facts_provider.py`, `cart_service.py`, `adapters/base.py`) · **Migrare:** `docs/041_conversation_carts.sql`
**Gate în teste:** `tests/test_cart_data_readiness.py` verifică mecanic că matricea de mai jos
acoperă toate câmpurile și că niciun default comercial nu se inventează.

## Decizia sistemului canonic (Definition of Ready, pct. 1)

În mediul curent **nu există API de storefront** (catalogul demo trăiește în Supabase; checkout-ul
e linkul canonic `checkout_links` cu `?ref=`). Sistemul canonic ales este **coșul asistentului**
(al conversației): tabelele `conversation_carts` / `conversation_cart_items`, mutate EXCLUSIV prin
`CartService`. Consecințe de contract:

- UI-ul și copy-ul numesc coșul onest („coșul conversației") și **nu pretind** că au modificat
  coșul global al magazinului (boundary-ul nenegociabil din card);
- WebWidgetul nu citește și nu scrie coșul demo din site (localStorage) — integrarea cu un coș
  real cere adaptorul din `src/commerce/adapters/base.py` (port definit, **neimplementat
  deliberat**: `configured_adapter()` întoarce `None`; contractul exact-once — receipt `pending`
  înainte de call, `unknown_reconcile` la răspuns pierdut, `lookup` înainte de retry — e
  construit și testat cu adaptor fake în `tests/test_cart_service.py`);
- checkout-ul folosește exclusiv linkul canonic existent (`ref_code = turn_id`, atribuire F2-2).

## Matricea `field → sursă canonică → query/adapter → freshness SLA → politica UNKNOWN → impact CTA`

| Câmp | Sursă canonică | Query/adapter | Freshness / SLA | Politica UNKNOWN | Impact CTA |
|---|---|---|---|---|---|
| `price.current` | `products.price` + `sale_price` în fereastră, min-variantă (`_EFFECTIVE_PRICE`) | `load_cart_facts_rows` (batch, 1 query) | `synced_at`→`updated_at`; SLA `COMMERCE_FACTS_SLA_S` (86400s) | NULL ⇒ `price` UNKNOWN; **add refuzat** (`price_unknown`), total UNKNOWN | checkout omis (suma e necesară) |
| `price.list_price` | `products.price` DOAR când `_SALE_ACTIVE` (sale în fereastră) | idem | idem | absent ⇒ fără preț tăiat; **nu se derivă discount** | fără claim de reducere |
| `currency` | `products.currency` | idem | idem | NULL ⇒ UNKNOWN; monede mixte ⇒ `currency_mismatch` | checkout refuzat, nu sumă greșită |
| `availability` | `products.availability` (enum întreținut de catalog) | idem | idem | NULL ⇒ `availability_unknown`; **nu se presupune in-stock** | add/checkout omis |
| `stock` | `product_variants.stock` (variantă) → `products.stock_total` | idem | idem | NULL ⇒ cap necunoscut (cantitatea NU se plafonează pe o cifră inventată); stoc CUNOSCUT < qty ⇒ `insufficient_stock` explicat | reject explicat, nu ajustare tăcută |
| `variant` (relație + label + preț) | `product_variants` (membership verificat pe produs) | idem (lateral `_VARIANTS_AGG`) | idem | variantă străină ⇒ **reject înainte de mutație** (`variant_not_found`) | mutația refuzată |
| `rating` | `products.rating` DOAR cu `review_count > 0` | idem | idem | `review_count=0` ⇒ rating UNKNOWN („0 stele" e o afirmație) | câmp omis; nu afectează mutația |
| `review_count` | `products.review_count` | idem | idem | 0 ⇒ afișat ca absent | idem |
| `review_summary` | `product_review_summaries.summary` (job offline aprobat) | idem | idem | gol ⇒ omis | idem |
| `delivery` (promise/ETA) | **fără sursă canonică de feed**; clasa `delivery_class` + config `businesses.settings.shipping` (NX-191, calcul pe ceasul magazinului) | `src/commerce/delivery.py`, la compunere | n/a (calcul determinist) | promisiunea NU intră în facts/receipt; fără config ⇒ tăcere | fără ETA inventat |
| `promotion` | DOAR `sale_price` în fereastra `sale_start/sale_end` (regula `_SALE_ACTIVE`) | idem cu prețul | idem | orice altă „promoție" e structural UNKNOWN | fără claim de promo |
| `voucher` | **fără sursă** în mediul curent | — | — | permanent UNKNOWN; nu se generează din prompt/fixture/heuristici | CTA de voucher inexistent |

Prospețimea urmează filozofia NX-234 (`context_resolver.Freshness`): `stale` **marchează, nu
aruncă** — valoarea din catalog rămâne sursa canonică a acestui mediu (nu există una mai
proaspătă), dar snapshotul se declară `facts_status="stale"` ca downstream (NX-240) să poată
face disclosure. UNKNOWN, în schimb, **blochează claimul și CTA-ul dependent**.

## Coverage măsurat pe tenantul demo (`6098812a…`, 2026-08-13, read-only)

| Sursă | Acoperire |
|---|---|
| produse active | 300 |
| `price` / `currency` / `availability` / `stock_total` | 300/300 (100%) |
| reducere reală (`sale_price < price`) | 90/300 (30%) |
| `review_count > 0` + `product_review_summaries` | 300/300 (100%) |
| `delivery_class` | 300/300 (100%) — dar promisiunea cere config `shipping` per tenant |
| variante (`price` + `stock`) | 383/383 (100%) |
| `synced_at` | **0/300** — catalog hand-seeded, fără sync |
| `updated_at` < 7 zile | **0/300** — tot catalogul e STALE față de SLA de 24h |

Concluzia onestă (nu se umple cu defaults): faptele structurate există integral, dar **nu există
dovadă de prospețime** — snapshotul de coș pe demo va raporta `facts_status="stale"` până când un
sync real scrie `synced_at`. Asta e exact comportamentul proiectat: valoarea se afișează, vechimea
se declară, nimic nu se blochează pe stale (doar pe UNKNOWN).

## Politici aprobate (Definition of Ready, pct. 3)

- **UNKNOWN:** preț/monedă necunoscute ⇒ add refuzat, total UNKNOWN, checkout omis. Stoc
  necunoscut ⇒ nu blochează cantitatea, dar disponibilitatea trebuie să fie cunoscută.
  `availability` necunoscută ⇒ add refuzat (nu presupunem in-stock).
- **Stale:** disclosure (`facts_status`), fără blocaj — catalogul e sursa canonică a mediului.
- **Out-of-stock / discontinued:** add/checkout refuzate cu cod explicat; varianta cu stoc
  CUNOSCUT 0 e out-of-stock chiar dacă produsul-părinte e in-stock.
- **Mixed currency:** refuz (`currency_mismatch`) — niciodată o sumă însumată greșit.
- **Cap cantitate:** 10 per linie (`CART_MAX_LINE_QUANTITY`, CHECK în DB) și 10 linii per coș
  (`CART_MAX_LINES`). Depășirea = reject explicat, nu tăiere tăcută.
- **Legacy `state.cart` (v1):** cu `CONVERSATION_CART_ENABLED` aprins, liniile vechi NU se
  importă (ar căra prețuri stale drept fapte); coșul canonic pornește curat. Fără dual-write.

## Idempotency și receipts (runbook)

- Cheia mutației: `t:<turn_id>:<op>:<fingerprint>` (calea LLM) sau `a:<action_id>` (calea de
  acțiuni NX-236). UNIQUE în DB (`commerce_action_receipts`), replay la orice retry.
- **`pending` / `unknown_reconcile`** apar DOAR pe calea cu adaptor extern (azi inexistent).
  Runbook când va exista: (1) alarma `cart_receipt_reconcile` / `pending_receipts(older_than_s)`;
  (2) `CartService.reconcile(key)` — întreabă providerul după cheie; provider „n-a văzut cheia" ⇒
  `failed` (safe de reîncercat cu cheie NOUĂ); provider confirmă ⇒ `succeeded` cu `external_ref`.
  **Niciodată retry orb** al unei operații incerte.
- Un receipt terminal nu se redeschide (`finalize_receipt` face UPDATE doar din
  `pending|unknown_reconcile`).

## Retenție și GDPR

- Tabelele de coș nu conțin PII (refs + cantități + coduri). Legătura cu persoana e prin
  conversație, ca la metadata de mesaje; `gdpr_erase_contact` nu are ce anonimiza aici.
- Retenție: coșuri `checked_out`/`expired` + receipts terminale > 90 zile pot fi purjate de un
  job admin bounded (de adăugat la `jobs/cleanup.py` când se activează în prod; receipts nu se
  șterg de worker — `bot_runtime` nu are DELETE pe ele).

## Observabilitate (low-cardinality)

`cart_command{operation,outcome,reason}` · `cart_version_conflict{operation}` ·
`cart_receipt{operation,status}` · `cart_receipt_reconcile{outcome}` ·
`cart_hydration_ms{outcome}` · `cart_items_bucket` / `cart_query_count_bucket` ·
`commerce_cta_omitted{reason}` · `checkout_created{outcome}` — fără URL/ID-uri în labels (P12).
Alarme recomandate la activare: receipts `pending`/`unknown` peste prag de vârstă, mutație fără
receipt, breach de buget N+1 (`cart_query_count_bucket` > 1 per operație).

## Rollout / rollback

1. Aplică migrarea 041 (expand-only; nu atinge `conversations.state`). Flags rămân OFF —
   byte-identic.
2. Aprinde `CONVERSATION_CART_ENABLED` pe tenantul demo: tool-urile `cart_add`/`checkout_link`
   trec pe serviciu; starea primește `cart_ref`; `state.cart` legacy îngheață.
3. Acțiunile de comerț (NX-236 → kernel) devin executabile la consum; **emiterea** CTA-urilor de
   coș rămâne a NX-240 (nimic nu emite tokenuri de comerț până atunci).
4. Adaptor extern: DOAR după fault-tests de idempotency + reconciliation (testele există deja pe
   adaptor fake).
5. Rollback: stinge flagul — mutațiile noi se opresc, tool-urile revin pe calea legacy; tabelele
   și receipturile RĂMÂN (nu se șterg dovezi; nu se declară eșec pentru statusuri externe
   necunoscute).
