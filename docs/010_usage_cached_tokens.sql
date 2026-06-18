-- 010 — usage_daily.cached_tokens (observabilitatea economiei din prompt caching, NX-78)
-- ============================================================================
-- Adaugă coloana `cached_tokens` pe rollup-ul zilnic: tokenii de prompt serviți din
-- cache-ul OpenAI (prefix static byte-identic, NX-78). Raportul de cost arată astfel
-- direct cât din `tokens_in` a fost cache-uit (economie). Sursa: event-ul `llm_usage`
-- (properties->>'cached_tokens'), agregat de jobul de rollup.
--
-- `analytics_events` are deja tokens_in/out/cost_usd (coloane dedicate) — cached_tokens
-- trăiește în properties jsonb (nu adăugăm coloană pe tabelul partiționat hot).
--
-- Aditiv + idempotent (IF NOT EXISTS) — rulabil de două ori fără eroare.

alter table usage_daily
  add column if not exists cached_tokens bigint not null default 0;
