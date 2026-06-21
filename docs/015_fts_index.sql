-- ============================================================================
-- 015 — FTS lexical pe products (NX-113a): tsvector generat + index GIN
-- ----------------------------------------------------------------------------
-- Înlocuiește lexicalul spart `p.name ILIKE '%întreaga-frază%'` (care nu prinde
-- aproape nicio propoziție naturală) cu FTS real: coloană generată `search_tsv`
-- pe `name + ai_summary`, config `'simple'` (LIMBĂ-AGNOSTIC → RO/HU/EN, fără
-- stemming legat de o limbă, P11), interogată cu `websearch_to_tsquery`.
--
-- `pg_trgm` + `idx_products_name_trgm` (fuzzy pe nume, typo/SKU) EXISTĂ deja în
-- schema_v2 (liniile 16, 340) → NU le re-creăm. `brand` e FK (alt tabel) → NU intră
-- în coloana generată; rămâne FILTRU dur pe brand_id la query. Aditiv + idempotent.
-- `bot_runtime` are deja SELECT pe products (coloana nouă e acoperită).
-- ============================================================================

alter table products
  add column if not exists search_tsv tsvector
  generated always as (
    to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(ai_summary, ''))
  ) stored;

create index if not exists idx_products_search_tsv on products using gin (search_tsv);
