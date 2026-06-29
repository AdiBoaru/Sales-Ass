-- ============================================================================
-- 018 — orders.external_customer_ref (NX-130): legarea comenzilor de un client
-- ----------------------------------------------------------------------------
-- Azi `orders.contact_id` se populează DOAR prin atribuire de checkout-link (F2-2): o comandă
-- pusă direct pe eshop (fără linkul botului) are contact_id NULL → invizibilă oricărui contact.
-- Iar `orders` n-are nicio cheie de client pe care `check_order` să facă match. NX-130 adaugă o
-- coloană de IDENTITATE DE CLIENT, cărată de ingestie, ca login passthrough-ul web (NX-129) să
-- poată regăsi comenzile clientului verificat.
--
-- `external_customer_ref` = id-ul OPAC de client din eshop (ex. `cust_8842`) — NU PII (P12 /
-- CONTRIBUTING #3: `orders` rămâne fără email/telefon brut). Dacă magazinul are doar email ca
-- cheie stabilă, ingestia trimite un HASH (sha256), niciodată email brut. Trebuie să fie ACEEAȘI
-- cheie pe care o pune NX-129 în JWT (`sub`) — altfel identitatea e verificată dar nu mapează.
--
-- Index PARȚIAL `(business_id, external_customer_ref)` — scoped pe tenant (P7), doar rândurile cu
-- ref (majoritatea istorice rămân NULL → index mic). bot_runtime are deja SELECT/INSERT/UPDATE pe
-- `orders` (003) → coloana nouă e acoperită de grant-ul table-level. RLS neschimbat (pe business_id).
-- Aditiv + idempotent. Niciun DROP/UPDATE de date.
-- ============================================================================

alter table orders
  add column if not exists external_customer_ref text;

create index if not exists idx_orders_customer_ref
  on orders (business_id, external_customer_ref)
  where external_customer_ref is not null;
