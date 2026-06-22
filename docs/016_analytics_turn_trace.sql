-- ============================================================================
-- 016 — Trace per-tur pe analytics_events (NX-122): coloană turn_id + index conv
-- ----------------------------------------------------------------------------
-- Corelare per-tur: fiecare event (stage_completed, llm_usage, tool_call,
-- validator_rejected, agent_recommended, rich_downgraded...) primește `turn_id`-ul
-- turului din care provine (injectat în `TurnContext.emit`, P10). `insert_events`
-- îl extrage din `properties` într-o COLOANĂ dedicată (rămâne și în jsonb, cost
-- zero) → filtrare ieftină + replay al traiectoriei unui singur mesaj inbound.
--
-- `analytics_events` e PARTIȚIONAT lunar: ADD COLUMN pe părinte propagă pe toate
-- partițiile; CREATE INDEX pe părinte creează un index partiționat propagat. Ambele
-- rulează în tranzacția runner-ului (NU `CONCURRENTLY` — ar crăpa în tranzacție).
-- Nullable: append-only, event-uri vechi / fără tur (emise în afara pipeline-ului)
-- → NULL fără excepție. Aditiv + idempotent.
--
-- Grant: `bot_runtime` are deja INSERT table-level pe analytics_events (003) →
-- coloana nouă e acoperită (nu e nevoie de GRANT pe coloană). RLS append-only
-- neschimbat. Niciun DROP/UPDATE.
-- ============================================================================

alter table analytics_events
  add column if not exists turn_id uuid;

create index if not exists analytics_events_biz_conv_created_idx
  on analytics_events (business_id, conversation_id, created_at);
