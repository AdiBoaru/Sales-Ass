-- ============================================================================
-- 036 — NX-207: search documents + card blurbs, numai SHADOW
-- ============================================================================
-- Nu schimbă products.ai_summary și nu modifică read-path-ul actual. Jobul NX-207 scrie aici;
-- retrieval-ul rămâne pe indexul vechi până după benchmark-ul NX-203 + un switch explicit.

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'products_business_id_id_key'
      and conrelid = 'products'::regclass
  ) then
    alter table products add constraint products_business_id_id_key unique (business_id, id);
  end if;
end $$;

create table if not exists product_search_documents (
  business_id              uuid not null references businesses(id) on delete cascade,
  product_id               uuid not null,
  locale                   text not null check (length(btrim(locale)) >= 2),
  document_version         integer not null check (document_version >= 1),
  schema_version           integer not null check (schema_version >= 1),
  positive_search_document text not null check (length(btrim(positive_search_document)) > 0),
  fts_document             tsvector not null,
  content_hash             text not null check (length(btrim(content_hash)) > 0),
  updated_at               timestamptz not null default now(),
  primary key (business_id, product_id, locale, document_version),
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

create index if not exists product_search_documents_fts_idx
  on product_search_documents using gin (fts_document);
create index if not exists product_search_documents_shadow_lookup_idx
  on product_search_documents (business_id, locale, document_version, content_hash);

create table if not exists product_card_blurbs (
  business_id      uuid not null references businesses(id) on delete cascade,
  product_id       uuid not null,
  locale           text not null check (length(btrim(locale)) >= 2),
  document_version integer not null check (document_version >= 1),
  schema_version   integer not null check (schema_version >= 1),
  text             text not null check (length(btrim(text)) > 0),
  content_hash     text not null check (length(btrim(content_hash)) > 0),
  updated_at       timestamptz not null default now(),
  primary key (business_id, product_id, locale, document_version),
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

alter table product_search_documents enable row level security;
grant select on product_search_documents to bot_runtime;
drop policy if exists bot_runtime_tenant on product_search_documents;
create policy bot_runtime_tenant on product_search_documents to bot_runtime
  using (business_id = current_business_id());

alter table product_card_blurbs enable row level security;
grant select on product_card_blurbs to bot_runtime;
drop policy if exists bot_runtime_tenant on product_card_blurbs;
create policy bot_runtime_tenant on product_card_blurbs to bot_runtime
  using (business_id = current_business_id());

-- ROLLBACK (după oprirea oricărui writer shadow):
-- drop table if exists product_card_blurbs;
-- drop table if exists product_search_documents;
