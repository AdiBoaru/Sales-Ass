-- ============================================================================
-- 004 — DEDUPE INBOUND LAYER 2 (tabel durabil, NE-partiționat)  · NX-51
-- ----------------------------------------------------------------------------
-- De ce: unique-ul `(business_id, provider_msg_id, created_at)` de pe `messages`
-- include OBLIGATORIU cheia de partiționare (created_at). Retry-ul Meta sosește
-- cu alt created_at → ON CONFLICT pe messages NU se declanșează. Deci dedupe-ul
-- pe messages e nefuncțional pentru retry-uri.
--
-- Layer 1 (rapid): Redis SET NX EX la webhook (vezi src/redis_bus.py).
-- Layer 2 (durabil, AICI): tabel ne-partiționat cu PK (business_id, provider_msg_id).
-- Prinde retry-urile care scapă de Redis (FLUSHALL / restart / pierdere AOF).
--
-- Rulează ca admin (postgres): python scripts/apply_004.py
-- ============================================================================

create table if not exists public.inbound_dedupe (
  business_id     uuid        not null references businesses(id) on delete cascade,
  provider_msg_id text        not null,
  first_seen      timestamptz not null default now(),
  primary key (business_id, provider_msg_id)
);

-- index pentru jobul de cleanup (DELETE WHERE first_seen < cutoff)
create index if not exists idx_inbound_dedupe_first_seen
  on public.inbound_dedupe (first_seen);

-- RLS: izolare strictă pe business_id (ca restul tabelelor runtime).
alter table public.inbound_dedupe enable row level security;

grant select, insert, delete on public.inbound_dedupe to bot_runtime;

drop policy if exists bot_runtime_tenant on public.inbound_dedupe;
create policy bot_runtime_tenant on public.inbound_dedupe to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());
