-- ============================================================================
-- 032 — Catalog v3.5: casele lipsă pentru ADÂNCIMEA de conținut
-- ----------------------------------------------------------------------------
-- Delta peste Catalog v3 (026–029), care a livrat faptele canonice, relațiile,
-- quality-gate-ul și embeddings versionate. Rămân patru găuri de CONȚINUT
-- (măsurate live pe cele 150 produse published — vezi docs/CATALOG-CONTENT-DEPTH.md):
--
--   1. FAQ per produs   — `faqs` e la nivel de business, fără product_id.
--   2. Proveniența textului (`voice`) — fără ea, textul de producător („repară
--      bariera în 3 zile") sau e afirmat de bot și tăiat de has_medical_claim la
--      RUNTIME, sau nu se scrie deloc → descrieri de 163 de caractere. Cu ea,
--      verificarea se mută la INGESTION și conținutul bogat devine sigur.
--   3. `product_sections` n-are business_id → fără tenant-scope/RLS/locale (P7, P11).
--   4. `product_images` n-are tip → cardul ia orb prima poză; „arată-mi textura" imposibil.
--   + `products.restock_date`: la stoc epuizat botul nu poate spune când revine.
--
-- Catalogul rămâne READ-ONLY pentru worker (bot_runtime: doar SELECT).
-- Aditiv + idempotent.
--
-- ROLLBACK:
--   drop table if exists product_faqs;
--   alter table product_sections drop column if exists voice, drop column if exists locale,
--     drop column if exists business_id;
--   alter table product_images drop column if exists kind;
--   alter table products drop column if exists restock_date;
-- ============================================================================

-- --------------------------------------------------------------------------
-- 1. products: data de reaprovizionare (rostibilă de bot la out_of_stock)
-- --------------------------------------------------------------------------
alter table products
  add column if not exists restock_date date;

comment on column products.restock_date is
  'Când revine în stoc; se completează la availability=out_of_stock. Fapt L1: botul îl poate rosti.';

-- --------------------------------------------------------------------------
-- 2. product_sections: tenant-scope + limbă + PROVENIENȚĂ
--    voice='brand'     → text al producătorului: afișabil/citabil DOAR atribuit,
--                        NU intră în embedding.
--    voice='assistant' → botul îl poate afirma direct; a trecut has_medical_claim
--                        la INGESTION (nu la runtime).
--    Default 'brand' = alegerea CONSERVATOARE: secțiunile existente nu devin
--    brusc afirmabile de bot; promovarea la 'assistant' e explicită, per bloc.
-- --------------------------------------------------------------------------
alter table product_sections
  add column if not exists business_id uuid,
  add column if not exists locale      text not null default 'ro',
  add column if not exists voice       text not null default 'brand';

-- backfill business_id din produsul părinte.
update product_sections ps
   set business_id = p.business_id
  from products p
 where p.id = ps.product_id and ps.business_id is null;

-- NOT NULL doar dacă backfill-ul a acoperit tot (secțiuni orfane ar bloca migrarea).
do $$
begin
  if not exists (select 1 from product_sections where business_id is null) then
    alter table product_sections alter column business_id set not null;
  else
    raise notice '032: product_sections cu business_id NULL (produs lipsă) — rămâne nullable';
  end if;
end $$;

alter table product_sections drop constraint if exists product_sections_voice_chk;
alter table product_sections
  add constraint product_sections_voice_chk check (voice in ('brand', 'assistant'));

create index if not exists idx_sections_business_product
  on product_sections (business_id, product_id, locale);

-- --------------------------------------------------------------------------
-- 3. product_images: tipul pozei (azi max 1 poză/produs, fără tip)
-- --------------------------------------------------------------------------
alter table product_images
  add column if not exists kind text not null default 'main';

alter table product_images drop constraint if exists product_images_kind_chk;
alter table product_images
  add constraint product_images_kind_chk
  check (kind in ('main', 'texture', 'application', 'before_after', 'ingredient',
                  'packaging', 'other'));

-- --------------------------------------------------------------------------
-- 4. product_faqs — întrebări frecvente PER PRODUS
--    Integritate de tenant DECLARATIVĂ prin FK COMPUS (business_id, product_id)
--    → products(business_id, id), la fel ca product_relations (027): un FAQ
--    cross-tenant e STRUCTURAL imposibil. Target-ul unique există deja din 027
--    (products_business_id_id_key).
--    `embedding` nullable: căutarea semantică peste FAQ de produs vine ulterior;
--    dimensiunea 1536 = text-embedding-3-small, ca restul.
-- --------------------------------------------------------------------------
create table if not exists product_faqs (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  product_id  uuid not null,
  locale      text not null default 'ro',
  question    text not null,
  answer      text not null,
  position    integer not null default 0 check (position >= 0),
  -- curated = scris de om; brand = preluat de la producător (citabil doar atribuit);
  -- generated = produs de pipeline, trecut prin verificarea de claim la ingestion.
  source      text not null default 'curated'
              check (source in ('curated', 'brand', 'generated')),
  embedding   vector(1536),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  -- aceeași întrebare o singură dată per produs + limbă
  unique (business_id, product_id, locale, question),
  -- P11: lookup-ul include locale; P7: izolare structurală de tenant
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

create index if not exists idx_product_faqs_lookup
  on product_faqs (business_id, product_id, locale, position);

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_product_faqs_upd') then
    create trigger trg_product_faqs_upd before update on product_faqs
      for each row execute function set_updated_at();
  end if;
end $$;

-- --------------------------------------------------------------------------
-- 5. Drepturi + RLS — catalogul e READ-ONLY pentru worker (P7).
--    Scrierea o fac seed/sync pe rol privilegiat, nu botul.
-- --------------------------------------------------------------------------
alter table product_faqs enable row level security;
grant select on product_faqs to bot_runtime;

drop policy if exists bot_runtime_tenant on product_faqs;
create policy bot_runtime_tenant on product_faqs to bot_runtime
  using (business_id = current_business_id());
