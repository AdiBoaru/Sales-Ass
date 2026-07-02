-- ============================================================================
-- 020 — messages.latency_s (SECUNDE): timpul de răspuns HUMAN-READABLE.
-- ----------------------------------------------------------------------------
-- `latency_ms` (milisecunde, integer) rămâne pt cod/analytics/rollup (precis, standard). `latency_s`
-- (numeric(6,2)) = ACELAȘI timp în SECUNDE, cu 2 zecimale (ex. 11497ms → 11.50s) — ca să se citească
-- direct în tabela `messages` fără împărțit la 1000. Coloană GENERATED (calculată automat din
-- latency_ms) → mereu corectă, ZERO cod de scris, nu poate diverge. NULL când latency_ms e NULL.
--
-- Pe tabel PARTIȚIONAT (messages, lunar): se adaugă pe PĂRINTE → propagat la partiții. Partițiile
-- existente (mici azi) se rescriu O DATĂ; partițiile NOI o moștenesc + o calculează inline la insert
-- (fără rescriere viitoare). bot_runtime are deja SELECT/INSERT pe messages (003) → coloana e
-- acoperită de grantul table-level. RLS neschimbat. Aditiv + idempotent. Verificat live (rollback).
-- ============================================================================

alter table messages
  add column if not exists latency_s numeric(6,2)
  generated always as (round(latency_ms / 1000.0, 2)) stored;
