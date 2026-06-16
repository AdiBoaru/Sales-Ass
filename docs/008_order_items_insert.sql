-- ============================================================================
-- 008 — INSERT pe order_items pentru bot_runtime (gaură descoperită la G7-3)
-- ----------------------------------------------------------------------------
-- order_items e SCRIS de process_order (worker = bot_runtime) la ingestia
-- comenzilor (F2-2, webhook /orders), DAR 003 îl trata read-only (catalog child:
-- grant doar `select` + politică RLS doar `for select`). Insert-ul pica pe AMBELE
-- (permission denied + RLS deny). Efect: la ingestia unei comenzi cu linii,
-- `insert_order_items` eșua → comanda se atribuia, dar liniile nu se salvau.
--
-- Fix: grant INSERT + politică RLS de INSERT, izolată TRANZITIV prin orders
-- (order_items n-are coloană business_id): poți insera o linie DOAR într-o
-- comandă a businessului tău (orders.business_id = current_business_id()).
-- Aditiv peste 003, idempotent (drop policy if exists; grant re-rulabil).
-- ============================================================================

grant insert on order_items to bot_runtime;

drop policy if exists bot_runtime_order_items_insert on order_items;
create policy bot_runtime_order_items_insert on order_items
  for insert to bot_runtime
  with check (
    exists (
      select 1 from orders o
      where o.id = order_items.order_id
        and o.business_id = current_business_id()
    )
  );

-- ============================================================================
-- VERIFICARE (rulată de scripts/apply_008.py):
--   set role bot_runtime; set app.business_id = '<biz>';
--   insert order_item într-o comandă a <biz>           -> OK
--   set app.business_id = '<alt_biz>';
--   insert order_item în aceeași comandă (a <biz>)      -> respins de WITH CHECK
-- ============================================================================
