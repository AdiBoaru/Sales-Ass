-- ============================================================================
-- NX-216 — `semantic_cache.prompt_version` FORMALIZAT (repară driftul live-vs-repo).
--
-- ISTORIC (de ce 034 și nu 030):
--   NX-181 (commit 592884b, PR #235) a introdus migrarea `030_semantic_cache_prompt_version.sql`
--   cu ACELAȘI conținut. PR-ul a fost merge-uit într-un branch STACKED, nu în `main`:
--   migrarea a ajuns aplicată în Supabase (`schema_migrations` conține '030'), dar NICI fișierul,
--   NICI codul care o consumă n-au ajuns în `main`. Rezultatul: indexul unic live are 4 coloane,
--   `ON CONFLICT`-ul din cod avea 3 → `InvalidColumnReferenceError` la FIECARE write-back, înghițit
--   de best-effort-ul din `aftercare.py`. Cache-ul a rămas ÎNGHEȚAT (citirile vechi mergeau,
--   scrierile nu). Vezi `tasks/NX-216.md`.
--
--   Nu putem re-folosi numărul 030: e deja înregistrat ca aplicat, deci `scripts/migrate.py`
--   l-ar sări → pe un DB PROASPĂT coloana n-ar apărea niciodată. De aceea 034, idempotent,
--   corect în AMBELE stări:
--     • DB live (coloana + indexul pe 4 coloane există deja, via 030) → no-op;
--     • DB proaspăt / de test (fără ele)                              → le creează.
--
-- DE CE EXISTĂ COLOANA (intenția originală NX-181, păstrată):
--   Prompt v1 și vNext compun răspunsuri DIFERITE din același catalog. Fără `prompt_version`
--   în cheie, cu vNext ON un hit pe o intrare scrisă de v1 ar servi un răspuns compus cu
--   promptul vechi (și invers). Cu ea, v1 și vNext coexistă ca rânduri DISTINCTE; kill-switch
--   OFF revine complet la namespace-ul 'v1'.
--
-- SIMETRIE OBLIGATORIE: citirile (`exact_lookup`, `semantic_lookup`) ȘI scrierea (`upsert_entry`)
-- filtrează/scriu pe aceeași sursă de versiune (`src/cache/version.py`). O parte fără cealaltă
-- = fie cache mort (scriere), fie servire încrucișată între versiuni de prompt (citire).
-- ============================================================================

alter table semantic_cache
  add column if not exists prompt_version text not null default 'v1';

-- Ținta de upsert (ON CONFLICT) + cheia L1 exact devin
-- (business_id, locale, canonical_hash, prompt_version) → v1/vNext nu se suprascriu.
-- `drop ... if exists` + `create ... if not exists` = idempotent pe ambele stări de mai sus.
drop index if exists idx_semcache_exact;
create unique index if not exists idx_semcache_exact
  on semantic_cache (business_id, locale, canonical_hash, prompt_version);
