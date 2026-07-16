-- ============================================================================
-- 029 — NX-171d: product_embeddings versionabile (PK compus)
-- ----------------------------------------------------------------------------
-- Azi PK = product_id → blochează versiuni paralele (produs × tip document × model). Re-cheie la
-- PK COMPUS (product_id, doc_type, model). Adaugă doc_type (default 'product'); backfill implicit
-- prin default pe rândurile existente.
--
-- HIGH — anti-duplicate: imediat ce există >1 rând/produs, orice join naiv pe product_id dublează
-- rezultatele de căutare. Read-path-ul TREBUIE să filtreze explicit doc_type + model activ (din
-- config) → un singur rând/produs garantat. Vezi catalog.py (join-ul pe product_embeddings).
--
-- Idempotent (add column if not exists + guard pe cardinalitatea PK).
--
-- ROLLBACK (revine la un rând/produs — presupune un singur doc_type/model păstrat):
--   delete from product_embeddings a using product_embeddings b
--     where a.product_id = b.product_id and a.ctid < b.ctid;
--   alter table product_embeddings drop constraint product_embeddings_pkey;
--   alter table product_embeddings add primary key (product_id);
--   alter table product_embeddings drop column if exists doc_type;
-- ============================================================================

alter table product_embeddings
  add column if not exists doc_type text not null default 'product';

-- re-cheie PK: product_id → (product_id, doc_type, model). Idempotent: dacă PK are deja >1 coloană,
-- e deja re-cheiat → skip.
do $$
declare
  pk_cols int;
begin
  select count(*) into pk_cols
  from pg_constraint c
  join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
  where c.conrelid = 'product_embeddings'::regclass and c.contype = 'p';
  if pk_cols = 1 then
    alter table product_embeddings drop constraint product_embeddings_pkey;
    alter table product_embeddings add primary key (product_id, doc_type, model);
  end if;
end $$;

-- lookup read-path: (business_id, product_id, doc_type, model).
create index if not exists product_embeddings_lookup_idx
  on product_embeddings (business_id, product_id, doc_type, model);
