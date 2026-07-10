-- NX-147: prioritate explicita pentru dispatcher.
-- `outbox.kind` ramane tip de transport; prioritatea decide ordinea operationala.

alter table outbox
  add column if not exists priority smallint not null default 50;

comment on column outbox.priority is
  'Dispatcher priority: lower is more urgent. user replies=10, transactional=20, default=50, marketing/proactive=80.';

create index if not exists idx_outbox_due_priority
  on outbox(business_id, priority, next_attempt_at, id)
  where status in ('pending','failed','dispatching');
