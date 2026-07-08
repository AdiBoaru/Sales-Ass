-- NX-146: index pentru Turn Replay pe analytics_events (business_id, turn_id).
-- `fetch_turn_events` filtreaza WHERE business_id=$1 AND turn_id=$2 (reconstructia unui tur);
-- migrarea 016 a creat doar (business_id, conversation_id, created_at). Index PARTIAL
-- (turn_id not null → majoritatea randurilor de tur au turn_id, restul emise in afara unui
-- tur il au NULL) pe tabelul PARINTE => propagat automat pe partitiile lunare. Aditiv, idempotent.

create index if not exists analytics_events_biz_turn_idx
  on analytics_events (business_id, turn_id)
  where turn_id is not null;
