-- ============================================================================
-- 023 — NX-148 felia 1: conversation_facts (memorie structurată per contact)
-- ----------------------------------------------------------------------------
-- Facts stabile despre client (buget, tip de piele, mărime, brand preferat,
-- restricții) extrase post-tur cu nano și injectate țintit în prompt — memoria
-- care face botul să pară că „te știe" dincolo de ultimele mesaje din istoric.
-- P8: valori mici structurate (NU obiecte). P12: `fact_value` FĂRĂ telefon/PII
-- (whitelist de fact_type per vertical; telefonul trăiește în channel_identities).
-- P7: tenant-scoped — grant + RLS pe bot_runtime (business_id = current_business_id()).
-- Aditiv + idempotent.
-- ============================================================================

create table if not exists conversation_facts (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null,
  contact_id       uuid not null,
  conversation_id  uuid,
  fact_type        text not null,           -- ex. budget_band, skin_type, brand_pref, size, restriction
  fact_value       jsonb not null,          -- valoare mică structurată (P8); fără PII (P12)
  confidence       real not null default 0.5 check (confidence >= 0 and confidence <= 1),
  source_message_id uuid,                   -- trasabilitate: mesajul din care a ieșit
  first_seen_at    timestamptz not null default now(),
  last_seen_at     timestamptz not null default now(),
  expires_at       timestamptz,
  -- upsert per (contact, tip): un fact re-menționat bump-uie last_seen + max(confidence),
  -- nu duplică. Un client are cel mult un fact activ per tip.
  unique (business_id, contact_id, fact_type)
);

-- injectarea citește facts ale unui contact (business_id + contact_id, ne-expirate).
create index if not exists conversation_facts_biz_contact_idx
  on conversation_facts (business_id, contact_id);

-- runtime: extractorul (post-tur) scrie, facts_block citește → SELECT/INSERT/UPDATE.
alter table conversation_facts enable row level security;
grant select, insert, update on conversation_facts to bot_runtime;

-- izolare tenant (plasa RLS peste `WHERE business_id = $1` din cod).
drop policy if exists bot_runtime_tenant on conversation_facts;
create policy bot_runtime_tenant on conversation_facts to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());
