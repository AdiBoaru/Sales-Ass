-- ============================================================================
-- 017 — faqs.embedding_model (NX-124a): guard de model pe lookup-ul FAQ
-- ----------------------------------------------------------------------------
-- `semantic_cache` are deja `embedding_model` (006); `faqs` NU îl avea. Lookup-ul FAQ
-- (faqs.semantic_lookup) filtrează acum pe `embedding_model = <model curent>` ca un upgrade
-- de embeddings (ex. text-embedding-3-small → -3-large, dim diferită) să NU mai compare cosine
-- vectori din spații incompatibile (P11 — un hit pe model greșit e un bug, nu un hit).
--
-- Default = modelul curent (`text-embedding-3-small`) → rândurile EXISTENTE (seedate înainte)
-- rămân servite (zero downtime de deflecție). La un upgrade real de model: re-seed FAQ
-- (`python -m src.jobs.seed_faqs`, scrie modelul nou) → vechile rânduri nu mai matchează filtrul.
--
-- bot_runtime are deja SELECT pe faqs (011); coloana nouă e acoperită de grant-ul table-level.
-- Aditiv + idempotent. Niciun DROP/UPDATE de date.
-- ============================================================================

alter table faqs
  add column if not exists embedding_model text not null default 'text-embedding-3-small';
