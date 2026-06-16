# fix — INSERT pe order_items pentru bot_runtime (gaură F2-2)
**Owner:** S · **Tip:** fix migrare DB · **Branch:** `fix/order-items-insert-grant` · **Complexitate:** S · **Estimare:** ~1h

## Goal
Închide o gaură de grant/RLS descoperită la testul `@integration` din G7-3: `order_items` era
read-only pentru `bot_runtime` (003: grant doar `select` + politică RLS `for select`), DAR
`process_order` (worker = `bot_runtime`, F2-2) îl SCRIE la ingestia comenzilor. `insert_order_items`
pica → comanda se atribuia, dar liniile NU se salvau.

## Fix
`docs/008_order_items_insert.sql` (aditiv peste 003, idempotent):
- `grant insert on order_items to bot_runtime`.
- Politică RLS de INSERT, izolată TRANZITIV prin `orders` (order_items n-are `business_id`):
  `with check (exists (select 1 from orders o where o.id = order_items.order_id and
  o.business_id = current_business_id()))` — poți insera o linie DOAR într-o comandă a businessului
  tău; alt tenant e blocat de WITH CHECK.

## Principii
P7 (izolare multi-tenant: o linie nu poate fi atașată comenzii altui business; RLS = plasă).

## Files
**Create:** `docs/008_order_items_insert.sql` · `scripts/apply_008.py` (apply + verificare izolare) ·
`tests/test_order_items_grant.py` (`@integration`)

## Aplicare
`python scripts/apply_008.py` — aplică pe Supabase + verifică: grant prezent, politică prezentă,
insert în comanda proprie OK, insert din alt tenant blocat. **Aplicat live (2026-06-16): toate testele trec.**

## Definition of Done
- [x] `grant insert` + politică RLS de insert pe `order_items` (008).
- [x] Izolare: insert în comanda proprie OK; din alt tenant → blocat de RLS (apply_008 + test `@integration`).
- [x] 008 aplicat live + verificat.
- [ ] `ruff check .` verde (cod nou: apply_008.py + test).

## Out of Scope
- Restul grant-urilor (003 e corect pe rest); NX-52 (HMAC /orders) e separat.
