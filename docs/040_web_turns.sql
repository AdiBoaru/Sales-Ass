-- ============================================================================
-- 040 — NX-232: `web_turns` — ledger durabil al turelor web (idempotency + replay)
-- ============================================================================
-- DE CE un tabel, nu lockul Redis (NX-221): lockul serializează, dar nu PERSISTĂ
-- rezultatul. Pierderea răspunsului HTTP, refreshul, retry-ul, restartul și două
-- taburi trebuie să întoarcă ACELAȘI rezultat terminal fără un al doilea apel LLM.
-- Redis e best-effort (bypass la timeout/FLUSHALL); ledgerul DB e garanția de
-- corectitudine. NX-221 rămâne optimizare temporară deasupra.
--
-- STATE MACHINE (scrisă ÎNAINTE de DDL — sursa: src/web/turn_service.py, aceeași
-- hartă e importată de teste; aici e forma normativă):
--
--   accepted ──claim──▶ running ──complete──▶ completed   (CAS + lease_epoch)
--       │                  │ │──fail──────▶ failed        (CAS + lease_epoch)
--       │                  │ │──renew─────▶ running        (același owner+epoch)
--       │                  │ └──reclaim───▶ running        (lease EXPIRAT → epoch+1)
--       └──cancel──▶ cancelled ◀──cancel──┘
--
--   INTERZIS: accepted→completed|failed (fără claim nu există single-flight);
--             orice tranziție DIN terminal (completed/failed/cancelled sunt finale);
--             running→accepted (nu se „dez-revendică");
--             complete/fail cu alt lease_epoch decât cel curent (fencing: un owner
--             vechi care se trezește după reclaim face UPDATE pe 0 rânduri).
--
-- IDEMPOTENCY: UNIQUE (business_id, conversation_id, client_turn_id). Retry cu
-- același ID + același request_fingerprint → același rând (replay la terminal);
-- același ID + ALT fingerprint → 409 idempotency_conflict (decis în service, pe
-- comparația de fingerprint — DB-ul garantează doar un singur rând per ID).
--
-- SINGLE-FLIGHT: un singur rând `accepted|running` per conversație (partial
-- unique). Policy explicit pentru „alt turn activ": accept NOU e respins cu
-- `active_turn` (frontendul NX-243 ține oricum composer-ul inactiv până la
-- terminal); NU se pune la coadă aici (executorul async e NX-233).
--
-- PII (P12): ZERO body brut, ZERO token, ZERO visitor_id în clar.
--   • `request_fingerprint` = HMAC peste inputul canonic (nu e inversabil și nu
--     se stochează sursa);
--   • `session_ref_hash` = sha256 al referinței de sesiune (corelare, nu identitate;
--     autorizarea reală se face pe contact_id, prin channel_identities);
--   • `response_json` = DOAR ViewModel validat (output de bot, deja trecut prin
--     frontiera de privacy NX-230 la persistarea mesajelor) sau error-view terminal.
--
-- RETENȚIE: `cleanup_web_turns` (job admin, bounded) șterge terminalele mai vechi
-- de fereastra de retenție și orfanii ne-terminali abandonați. GDPR: erase-ul de
-- contact (src/gdpr/erase.py) șterge rândurile contactului în aceeași tranzacție
-- cu gdpr_erase_contact — un ledger cu response_json e conținut de conversație.
-- ============================================================================

create table if not exists web_turns (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  conversation_id  uuid not null references conversations(id) on delete cascade,
  contact_id       uuid not null references contacts(id) on delete cascade,
  session_ref_hash text,                       -- sha256(ref sesiune), NU valoarea brută
  client_turn_id   uuid not null,              -- generat și persistat de frontend
  request_fingerprint text not null,           -- HMAC canonic; niciodată body/token
  schema_version   text not null default 'web-turn.v2',
  status           text not null default 'accepted'
                     check (status in ('accepted','running','completed','failed','cancelled')),
  attempt          integer not null default 0 check (attempt >= 0),
  lease_owner      text,                       -- id de proces/worker (uuid), nu PII
  lease_epoch      integer not null default 0 check (lease_epoch >= 0),
  lease_expires_at timestamptz,
  deadline_at      timestamptz,                -- deadline total al turului (NX-241 îl strânge)
  conversation_revision_at_accept integer,     -- state_version la accept (anti-stale, NX-233)
  pipeline_version text,                       -- ce contract are response_json (ex. web-chat.v1)
  response_json    jsonb,                      -- DOAR ViewModel validat / error-view terminal
  safe_error_code  text,                       -- cod STABIL, low-cardinality (fără mesaj brut)
  accepted_at      timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  completed_at     timestamptz,

  -- Idempotency: un singur turn per (tenant, conversație, ID de client).
  constraint uq_web_turns_client_turn unique (business_id, conversation_id, client_turn_id),
  -- P6 în DB: un terminal FĂRĂ payload randabil nu se poate scrie deloc.
  constraint web_turns_terminal_response_ck check (
    status not in ('completed','failed','cancelled') or response_json is not null
  ),
  -- `running` fără lease ar fi un turn pe care nimeni nu-l poate nici termina, nici reclama.
  constraint web_turns_running_lease_ck check (
    status <> 'running' or (lease_owner is not null and lease_expires_at is not null)
  ),
  -- terminalul poartă timestampul terminării (replay/retenție se bazează pe el)
  constraint web_turns_terminal_completed_at_ck check (
    status not in ('completed','failed','cancelled') or completed_at is not null
  )
);

-- SINGLE-FLIGHT: cel mult un turn activ per conversație (inclusiv două taburi).
create unique index if not exists uq_web_turns_one_active
  on web_turns (business_id, conversation_id)
  where status in ('accepted','running');

-- Lookup-ul de recovery/status (GET pe turn) și listarea per conversație.
create index if not exists idx_web_turns_conversation
  on web_turns (business_id, conversation_id, accepted_at desc);

-- Retenția scanează pe timp, nu pe tenant (job admin cross-tenant, bounded).
create index if not exists idx_web_turns_retention
  on web_turns (completed_at)
  where status in ('completed','failed','cancelled');
create index if not exists idx_web_turns_stale
  on web_turns (accepted_at)
  where status in ('accepted','running');

-- ── RLS + grants ────────────────────────────────────────────────────────────
-- Aceeași plasă ca 003: izolarea PRIMARĂ rămâne `business_id = $1` în cod;
-- RLS transformă un query greșit în „zero rezultate", nu în datele altui client.
alter table web_turns enable row level security;

drop policy if exists bot_runtime_tenant on web_turns;
create policy bot_runtime_tenant on web_turns to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

-- Dashboardul (membru al businessului) poate CITI ledgerul (debug/suport).
drop policy if exists "member read" on web_turns;
create policy "member read" on web_turns for select
  using (business_id in (select my_business_ids()));

-- Botul acceptă/revendică/finalizează. DELETE rămâne la admin (retenție + GDPR
-- rulează pe conexiunea de control plane, cross-tenant respectiv erase).
grant select, insert, update on web_turns to bot_runtime;

-- ROLLBACK operațional (păstrează REZULTATELE — nu șterge și nu reprocesează
-- turele acceptate; cutover-ul se face prin flag, tabelul rămâne):
--   • oprește flagul WEB_TURN_LEDGER_ENABLED (calea veche redevine activă);
--   • tabelul + rândurile rămân pe loc pentru observare/audit.
-- ROLLBACK distructiv (DOAR dacă migrarea însăși trebuie retrasă):
--   drop table if exists web_turns;
