-- ============================================================================
-- 006 — G5b-1: semantic_cache pregătit pentru hybrid exact+semantic + provenance
-- ----------------------------------------------------------------------------
-- STATUS: APLICAT pe Supabase via scripts/apply_006.py (coloane + index unic +
--   grant CRUD bot_runtime). Idempotent (if not exists) → re-rulabil safe.
-- Aditiv peste schema_v2 (tabelul semantic_cache există deja: business_id, locale,
-- query_norm, embedding vector(1536), answer, hit_count, last_hit_at, expires_at).
-- Adaugă stratul L1 exact (canonical_hash) + clasa de volatilitate + provenance.
-- Idempotent (if not exists). Vezi docs/semantic-cache-design.md §5.
-- RLS pe semantic_cache vine din 003 (bot_runtime_tenant) — nu se atinge aici.
-- ============================================================================

alter table semantic_cache
  add column if not exists canonical_hash   text,
  add column if not exists volatility_class text not null default 'static',
  add column if not exists embedding_model  text not null default 'text-embedding-3-small',
  add column if not exists quality_score    real,
  add column if not exists is_curated       boolean not null default false;

-- volatility_class ∈ {static, semi_dynamic, dynamic, realtime} (review §8).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'chk_semcache_volatility'
  ) then
    alter table semantic_cache
      add constraint chk_semcache_volatility
      check (volatility_class in ('static','semi_dynamic','dynamic','realtime'));
  end if;
end $$;

-- L1 exact: cheie unică (business_id, locale, canonical_hash) → lookup O(1) +
-- ținta pentru upsert-ul de write-back. NULL-urile (rânduri vechi) sunt distincte.
create unique index if not exists idx_semcache_exact
  on semantic_cache (business_id, locale, canonical_hash);

-- GRANT: 003 a dat doar INSERT/UPDATE pe semantic_cache. Cache-ul are nevoie de
-- SELECT (lookup L1/L2), iar `insert ... on conflict do update` cere și SELECT;
-- DELETE pentru evict/purjă (G5b-2). Completăm CRUD-ul pentru bot_runtime.
grant select, insert, update, delete on semantic_cache to bot_runtime;

-- ============================================================================
-- VERIFICARE POST-APLICARE:
--   \d semantic_cache   -- aștept: canonical_hash, volatility_class, embedding_model,
--                          quality_score, is_curated + idx_semcache_exact (unique)
-- ============================================================================
