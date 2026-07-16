-- ============================================================================
-- 028 — NX-171c: content quality gate la nivel de DB (content_status)
-- ----------------------------------------------------------------------------
-- content_status ∈ {draft, reviewed, published, rejected} + schema_version + verified_at.
-- DOAR 'published' e servit clientului (filtru în read-path, în spatele unui flag PER-TENANT
-- default OFF — businesses.settings->>'content_status_filter'). Un produs incomplet/nerevizuit NU
-- ajunge la client, DAR un filtru fără backfill = catalog gol → outage. De aceea secvența e strictă.
--
-- SECVENȚĂ OBLIGATORIE (card NX-171c):
--   1. [ACEST FIȘIER, auto] add content_status NULLABLE + schema_version + verified_at + CHECK enum
--      + default 'draft' pentru rânduri NOI (nu atinge rândurile existente NULL). NICIUN filtru încă.
--   2. rulează  python -m src.jobs.backfill_content_status  per tenant: audit v3 pe catalogul
--      COMPLET → produsele din ≥1 violation = 'draft', restul = 'published'. Idempotent.
--   3. [MANUAL, DUPĂ backfill pe toți tenanții — blocul comentat de mai jos] NOT NULL.
--   4. activează flagul per-tenant DOAR după test-plasă (visible_count > 0 pentru acel tenant).
--
-- SQL NU poate ști dacă un produs trece auditul Python → backfill-ul e un JOB, nu în migrare.
--
-- ROLLBACK:
--   alter table products
--     drop column if exists content_status,
--     drop column if exists schema_version,
--     drop column if exists verified_at;
-- ============================================================================

alter table products
  add column if not exists content_status text,
  add column if not exists schema_version integer,
  add column if not exists verified_at    timestamptz;

alter table products drop constraint if exists products_content_status_chk;
alter table products
  add constraint products_content_status_chk
  check (content_status is null or content_status in ('draft', 'reviewed', 'published', 'rejected'));

-- rândurile NOI intră ca 'draft' (nu sunt servite până nu sunt promovate). Rândurile EXISTENTE
-- rămân NULL până le atinge backfill-ul — cu flagul per-tenant OFF, NULL e vizibil (fără outage).
alter table products alter column content_status set default 'draft';

-- calea caldă a filtrului: index parțial pe published (business_id).
create index if not exists products_published_idx
  on products (business_id) where content_status = 'published';

-- ============================================================================
-- PASUL 3 — de aplicat MANUAL după ce backfill-ul a rulat pe TOȚI tenanții (nu auto: ar face
-- NOT NULL peste rânduri încă NULL → eroare). Verifică întâi: select count(*) from products
-- where content_status is null;  → trebuie 0.
--   alter table products alter column content_status set not null;
-- ============================================================================
