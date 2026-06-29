-- ============================================================================
-- 019 — proactive_jobs.dedupe_key (PL-1): idempotență pentru INIȚIATORII proactivi
-- ----------------------------------------------------------------------------
-- Motorul proactiv (NX-70) + poarta (NX-71) + calea template (PR #142) erau gata, dar NIMENI nu
-- INSERA joburi → zero mesaje proactive în prod (gap CRITICAL în CONV-COMMERCE-DEEP-ANALYSIS-2026).
-- Cablăm sweeper-ele de inițiere (coș abandonat, back-in-stock). Ca să nu creeze ACELAȘI job la
-- fiecare tură de scanare, `proactive_jobs` are nevoie de o cheie de idempotență — exact ca
-- `outbox.idempotency_key`.
--
-- `dedupe_key` = identitatea logică a inițierii (ex. `abandoned_cart:<checkout_link_id>`,
-- `awb_update:<order_id>`). Index unic PARȚIAL (doar rândurile cu cheie) → INSERT ... ON CONFLICT
-- DO NOTHING. Joburile create fără dedup (ex. back_in_stock — gardat de `notified_at` ca să suporte
-- re-armarea la re-subscribe; follow_up ad-hoc) au cheia NULL și NU sunt afectate de index.
--
-- bot_runtime are deja INSERT pe proactive_jobs (003) → coloana e acoperită de grantul table-level.
-- RLS neschimbat (pe business_id). Aditiv + idempotent. Niciun DROP/UPDATE de date.
-- ============================================================================

alter table proactive_jobs
  add column if not exists dedupe_key text;

create unique index if not exists uq_proactive_jobs_dedupe
  on proactive_jobs (business_id, dedupe_key)
  where dedupe_key is not null;
