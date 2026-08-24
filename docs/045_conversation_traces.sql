-- ============================================================================
-- 045 — NX-256: captura FULL a turului pentru diagnoză și testare
--       (conversation_traces)
-- ============================================================================
-- DE CE există tabelul (incidentul din 24 aug, al doilea simptom):
--   După cutoverul de model, apelul rich structurat a emis product_id-uri străine de
--   setul retrievat; membership-ul (compose.assemble) le-a aruncat TĂCUT, fallback-ul
--   pe proză a mers la client, iar la anchetă am constatat că NIMIC din ce ne trebuia
--   nu era persistat:
--     • messages.body     = doar textul final (fallback-ul care a reușit);
--     • messages.payload  = {turn_id, fragment_index} — nici cardurile randate;
--     • web_turns         = niciun rând (ledgerul NX-232 stins pe mediul care servea);
--     • JSON-ul structurat emis de model = ARUNCAT în memorie, zero urmă;
--     • analytics_events  = contorul `rich_downgraded` cu un `reason` și atât.
--   Știam CĂ a eșuat, dar nu CE a emis modelul. Tabelul ăsta e răspunsul: un rând per
--   tur, cu ce a zis clientul verbatim, TOT ce a zis botul (Reply-ul semantic complet,
--   nu doar textul), ce a recomandat și diagnosticele intermediare care azi mor în zbor.
--
-- CE NU e tabelul ăsta:
--   • NU e sursă de adevăr pentru nimic din produs — messages/outbox/web_turns rămân
--     autoritățile; traces e strict citit de oameni și de tooling de test/diagnoză.
--     Niciun cod de pe drumul turului nu CITEȘTE de aici.
--   • NU e analytics — analytics_events rămâne append-only, minimal (P12). Aici e
--     conținut integral, tocmai ce analytics-ului îi e interzis să care.
--   • NU dublează web_turns: ledgerul e contract de idempotență/replay pe UN canal
--     (web v2); traces e captură de diagnoză pe TOATE canalele, cu intermediarele
--     (ce a emis modelul ÎNAINTE de validare/membership), pe care ledgerul nu le are.
--
-- SCRIITOR: aftercare (post-tur, best-effort, sub CONVERSATION_TRACE_ENABLED, default
--   OFF) — un eșec de insert nu atinge reply-ul deja livrat, iar drumul fierbinte nu
--   capătă nicio scriere nouă. Flag OFF = zero rânduri, byte-identic.
--
-- PRIVACY / RETENȚIE:
--   • `client_text` e vocea clientului — conținut de conversație, aceeași clasă ca
--     messages.body. GDPR: erase-ul de contact (src/gdpr/erase.py) șterge rândurile
--     contactului în ACEEAȘI tranzacție cu gdpr_erase_contact (modelul NX-232).
--   • Fără PII de canal: niciun telefon/external_id — doar id-uri interne (P12).
--   • Retenția e a jobului `cleanup_conversation_traces` (admin, bounded, default 30
--     de zile) — captura e unealtă de diagnoză, nu arhivă.
--
-- EXPAND-ONLY: tabel nou, niciun obiect existent modificat. Imaginea precedentă nu
--   îl vede și rulează neschimbată (rollback NX-248 compatibil).
-- ============================================================================

create table if not exists conversation_traces (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  conversation_id  uuid not null references conversations(id) on delete cascade,
  contact_id       uuid not null references contacts(id) on delete cascade,
  turn_id          uuid not null,

  channel_kind     text,               -- webchat | whatsapp | telegram (denormalizat pt filtru ieftin)
  language         text,               -- limba turului (ctx.language)
  model_route      text,               -- modelele folosite, ca pe messages (ex. "gpt-5.4-nano,gpt-5.6-luna")

  client_text      text,               -- mesajul clientului, VERBATIM (inclusiv typo-uri)
  bot_text         text,               -- textul final integral al botului (NULL = tăcere/halt)
  reply            jsonb,              -- Reply-ul semantic COMPLET serializat: rich (carduri cu
                                       -- reason/badge/variants), comparison, offer, suggestions,
                                       -- pending_question, kind, cacheable — tot ce a ieșit
  recommended      jsonb,              -- ref-uri compacte [{product_id, name, price}] — SQL ieftin
                                       -- pt „ce a recomandat botul" fără să despici `reply`
  diagnostics      jsonb,              -- ctx.trace: intermediarele care azi se pierd — JSON-ul
                                       -- structurat brut emis de model, id-urile picate la
                                       -- membership, motivul degradării, verdictele validatorului
  created_at       timestamptz not null default now(),

  -- Un tur = un rând. Retry-ul de aftercare (dacă apare vreodată) nu dublează.
  unique (business_id, turn_id)
);

-- Citirea tipică e „conversația X, în ordine" — exact forma indexului.
create index if not exists idx_conversation_traces_conv
  on conversation_traces (business_id, conversation_id, created_at);
-- Retenția șterge pe vârstă, cross-tenant (job admin).
create index if not exists idx_conversation_traces_created
  on conversation_traces (created_at);
-- GDPR erase șterge pe contact.
create index if not exists idx_conversation_traces_contact
  on conversation_traces (business_id, contact_id);

-- ============================================================================
-- RLS + grants (modelul 041): bot_runtime scrie/citește DOAR tenantul curent;
-- dashboardul (membership) citește; DELETE rămâne al admin-ului (retenție + GDPR),
-- deci bot_runtime nu îl primește.
-- ============================================================================

alter table conversation_traces enable row level security;

drop policy if exists bot_runtime_tenant on conversation_traces;
create policy bot_runtime_tenant on conversation_traces to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

drop policy if exists "member read" on conversation_traces;
create policy "member read" on conversation_traces for select
  using (business_id in (select my_business_ids()));

grant select, insert on conversation_traces to bot_runtime;
