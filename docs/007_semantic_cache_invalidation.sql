-- ============================================================================
-- 007 — G5b-2: invalidare cache + caching `dynamic` (recomandări de produs)
-- ----------------------------------------------------------------------------
-- STATUS: de aplicat pe Supabase via scripts/apply_007.py (coloane + grant).
-- Idempotent (if not exists) → re-rulabil safe. Aditiv peste 006.
--
-- Deblochează tierul `dynamic` în SIGURANȚĂ. Două mecanisme de invalidare:
--   • retrieval_signature jsonb = [{product_id, price}] care a fundamentat răspunsul.
--     La fiecare hit dynamic re-validăm prețul curent; orice diferență → regenerăm
--     (price-check self-healing — niciodată un preț învechit servit, ca validatorul).
--   • data_version (plasă bulk) = versiunea de date a businessului la scriere. Un
--     `bump_data_version` (la sync de catalog / intervenție manuală în masă) face
--     toate entry-urile dynamic vechi instant inaccesibile la următorul lookup.
-- Entry-urile `static` IGNORĂ ambele (politica de retur nu se schimbă la un sync de preț).
-- Vezi docs/semantic-cache-design.md §7 + tasks/G5b-2.md.
-- RLS pe semantic_cache vine din 003; grant CRUD din 006 (inclusiv DELETE pt evict/purjă).
-- ============================================================================

alter table semantic_cache
  add column if not exists retrieval_signature jsonb,    -- [{product_id, price}] (provenance)
  add column if not exists data_version        integer;  -- businesses.data_version la scriere

-- Versiunea de date a businessului. Bump → invalidează în bloc cache-ul dynamic.
alter table businesses
  add column if not exists data_version integer not null default 1;

-- ============================================================================
-- VERIFICARE POST-APLICARE:
--   \d semantic_cache   -- aștept: retrieval_signature (jsonb), data_version (integer)
--   \d businesses       -- aștept: data_version integer not null default 1
-- ============================================================================
