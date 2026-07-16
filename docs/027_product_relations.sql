-- ============================================================================
-- 027 — NX-171b: product_relations (relații explicite curate între produse)
-- ----------------------------------------------------------------------------
-- Înlocuiește heuristica same-brand/concern din get_complementary_products cu relații
-- EXPLICITE: rutină (cleanser→ser→cremă→SPF), complement („se poartă cu"), substitut
-- (alternativă mai ieftină), accesoriu. kind ∈ {substitute, complement, accessory, routine_next}.
--
-- INTEGRITATE DE TENANT (HIGH) — DECLARATIVĂ, nu trigger: FK COMPUS pe ambele capete
-- (business_id, product_id) și (business_id, related_id) → products(business_id, id). Cum ambele
-- FK folosesc ACELAȘI business_id al rândului de relație, ambele produse trebuie să aibă acel
-- business_id → același tenant. O relație cross-tenant e STRUCTURAL imposibilă (nu doar respinsă de
-- cod). Necesită un unique(business_id, id) pe products ca target de FK (id e deja PK, dar FK compus
-- cere unique explicit pe pereche). P7 + RLS ca plasă.
--
-- Aditiv + idempotent (if not exists / guard pe constraint).
--
-- ROLLBACK:
--   drop table if exists product_relations;
--   alter table products drop constraint if exists products_business_id_id_key;
-- ============================================================================

-- target pentru FK-ul compus (idempotent prin guard pe pg_constraint).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'products_business_id_id_key'
      and conrelid = 'products'::regclass
  ) then
    alter table products add constraint products_business_id_id_key unique (business_id, id);
  end if;
end $$;

create table if not exists product_relations (
  id           uuid primary key default gen_random_uuid(),
  business_id  uuid not null references businesses(id) on delete cascade,
  product_id   uuid not null,                 -- ancora
  related_id   uuid not null,                 -- produsul înrudit
  kind         text not null
               check (kind in ('substitute', 'complement', 'accessory', 'routine_next')),
  position     integer not null default 0 check (position >= 0),
  created_at   timestamptz not null default now(),
  -- fără relații duplicate (aceeași pereche + kind)
  unique (business_id, product_id, related_id, kind),
  -- fără self-relation
  check (product_id <> related_id),
  -- integritate de tenant: ambele capete pe ACELAȘI business_id (FK compus → cross-tenant imposibil)
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade,
  foreign key (business_id, related_id) references products (business_id, id) on delete cascade
);

-- read-path: relațiile unei ancore (business_id + product_id + kind, ordonate pe position).
create index if not exists product_relations_anchor_idx
  on product_relations (business_id, product_id, kind, position);

-- runtime: botul DOAR citește (relațiile sunt scrise de seed/sync ca admin, nu de worker).
alter table product_relations enable row level security;
grant select on product_relations to bot_runtime;
drop policy if exists bot_runtime_tenant on product_relations;
create policy bot_runtime_tenant on product_relations to bot_runtime
  using (business_id = current_business_id());
