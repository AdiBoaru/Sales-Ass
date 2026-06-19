-- NX-87: o singură conversație DESCHISĂ per (business, contact, canal).
--
-- Închide race-ul de creare a conversației la mesaje strict simultane ale unui contact NOU
-- (înainte: SELECT-then-INSERT fără unique → două conversații deschise → state split + dublu
-- în Dashboard). `get_or_create_conversation` folosește ON CONFLICT pe ACEST index.
--
-- Index unic PARȚIAL (Postgres 15+; Supabase = 16), tenant-scoped (prima coloană business_id).
-- O conversație 'closed'/'snoozed' nu intră în index → un contact poate avea istoric de
-- conversații închise + exact una deschisă pe canal. `bot_runtime` inserează deja `conversations`
-- → niciun GRANT/RLS nou. Idempotent (`if not exists`).
--
-- NB: dacă există DEJA duplicate (>1 deschisă pe aceeași cheie), CREATE INDEX eșuează — vezi
-- scripts/apply_010.py, care le detectează și raportează ÎNAINTE (decizie de date, nu de migrare).

create unique index if not exists uq_conversations_one_open
  on public.conversations (business_id, contact_id, channel_id)
  where status = 'open';
