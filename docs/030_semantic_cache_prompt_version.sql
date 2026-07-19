-- ============================================================================
-- NX-181 (Prompt vNext) — namespace `semantic_cache` pe `prompt_version`.
--
-- Prompt v1 și vNext compun răspunsuri DIFERITE din același catalog. Nu trebuie să
-- împartă aceleași intrări de cache: cu vNext ON, un hit pe o intrare scrisă de v1
-- ar servi un răspuns compus cu promptul vechi (și invers). Adăugăm `prompt_version`
-- ca dimensiune de cheie → v1 și vNext coexistă ca rânduri DISTINCTE; kill-switch OFF
-- revine complet la namespace-ul 'v1'. Lookup (exact + semantic) și write-back sunt
-- filtrate/scrise simetric pe această coloană (src/db/queries/semantic_cache.py).
-- Idempotent (if [not] exists) — rulat ordonat de scripts/migrate.py.
-- ============================================================================

alter table semantic_cache
  add column if not exists prompt_version text not null default 'v1';

-- Ținta de upsert (ON CONFLICT) + cheia L1 exact devin
-- (business_id, locale, canonical_hash, prompt_version) → v1/vNext nu se suprascriu.
drop index if exists idx_semcache_exact;
create unique index if not exists idx_semcache_exact
  on semantic_cache (business_id, locale, canonical_hash, prompt_version);
