-- ============================================================================
-- 042 — NX-246 (felia 2): feedback one-tap, autorizat, idempotent, tenant-scoped
-- ============================================================================
-- DE CE tabel nou și nu `analytics_events`: acolo scrierile sunt append-only și
-- fără unicitate, deci nu pot impune „un singur feedback ACTIV per prompt" și nu
-- pot exprima o corecție (revizie). Un retry de rețea ar produce două rânduri,
-- iar `positive_feedback_rate` ar număra același vot de două ori — adică exact
-- semnalul pe care rollout-ul îl folosește ca să decidă. Cardul cere explicit:
-- „Folosește tabelele existente NUMAI dacă pot impune aceste invariants".
--
-- CE NU CONȚINE (P12, și e o listă, nu o intenție): niciun text liber, niciun
-- comentariu, niciun IP, niciun user-agent, niciun token, nicio identitate brută.
-- Doar refs opace (uuid-uri), enum-uri din vocabular închis și timestamps.
-- Un vot e „acest turn, pozitiv, motiv X" — nimic despre CINE.
--
-- IDEMPOTENȚĂ:
--   • `uq_web_feedback_prompt` = UN singur rând per (tenant, prompt) ⇒ retry-ul
--     de rețea nu poate dubla, iar agregatele nu numără de două ori;
--   • `last_action_id` = ce token a produs starea curentă. Retry IDENTIC (același
--     action_id) ⇒ no-op + ACELAȘI receipt, fără să crească `revision`;
--   • `revision` crește DOAR la o schimbare reală (👍→👎, sau adăugarea unui
--     motiv), plafonat în cod (`MAX_FEEDBACK_REVISIONS`) ca un flip-flop automat
--     să nu poată scrie la infinit.
--
-- `feedback_prompt_id` e DERIVAT (HMAC peste turn_id + versiune), nu random —
-- vezi src/web/feedback.py pentru motiv: proiecția v2 trebuie să rămână PURĂ
-- (NX-240), iar un id random ar face ca două citiri ale aceluiași rând să
-- producă bytes diferiți. Proprietatea care contează („clientul nu-l poate
-- ghici") se păstrează prin cheia server-owned.
--
-- GDPR: legătura cu persoana e prin conversație (ca la `conversation_carts`).
-- `on delete cascade` pe conversație ⇒ erase-ul de contact duce rândurile cu el.
-- RETENȚIE: rândurile sunt agregate operaționale, nu conținut; se pot purja
-- odată cu conversația. Nu se șterge nimic automat aici.
--
-- ROLLBACK OPERAȚIONAL: stinge WEB_FEEDBACK_ENABLED (nu se mai emit prompturi,
-- endpointul refuză onest cu `feedback_disabled`); tabelul RĂMÂNE, iar voturile
-- deja strânse rămân citibile de raport. Down-ul SQL e doar pentru medii de test —
-- în prod nu se dropează semnal deja colectat.
--
--   -- DOWN (test-only):
--   -- drop table if exists web_feedback;
-- ============================================================================

create table if not exists web_feedback (
  id                  uuid primary key default gen_random_uuid(),
  business_id         uuid not null references businesses(id) on delete cascade,
  conversation_id     uuid not null references conversations(id) on delete cascade,
  -- Turul EVALUAT (sursa promptului). Fără FK compus către `web_turns`: ledgerul
  -- are retenție proprie (168h), iar un vot nu are de ce să dispară odată cu
  -- rândul de ledger — semnalul supraviețuiește ferestrei de replay.
  turn_id             uuid not null,
  -- Derivat din turn_id + versiune de schemă; unic per tenant (vezi indexul).
  feedback_prompt_id  text not null,
  rating              text not null check (rating in ('positive','negative')),
  -- Din taxonomia VERSIONATĂ (`FEEDBACK_TAXONOMY_VERSION`). NULL = vot fără motiv.
  reason_code         text,
  taxonomy_version    text not null default 'feedback.v1',
  source              text not null default 'web_widget'
                        check (source in ('web_widget')),
  schema_version      text not null,
  -- Markerii de release ai turului EVALUAT: fără ei, un raport nu poate spune
  -- „candidate a fost votat mai prost decât champion", ci doar „au fost voturi".
  release_sha         text,
  release_track       text not null default 'unknown',
  pipeline_version    text,
  -- Ce token a produs starea curentă. Opac, derivat (HMAC): nu e un secret și nu
  -- poate fi folosit ca să reconstruiască tokenul.
  last_action_id      text not null,
  revision            integer not null default 1 check (revision >= 1),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- Invariantul central: UN singur feedback activ per prompt, per tenant.
-- E și ținta lui ON CONFLICT din `upsert_feedback` — deci idempotența nu e o
-- convenție de cod care se poate uita, ci o constrângere pe care DB-ul o impune.
create unique index if not exists uq_web_feedback_prompt
  on web_feedback (business_id, feedback_prompt_id);

-- Raportul citește pe (tenant, fereastră) și grupează pe rating/reason/track.
create index if not exists idx_web_feedback_window
  on web_feedback (business_id, created_at desc);

create index if not exists idx_web_feedback_turn
  on web_feedback (business_id, turn_id);

-- ── RLS + grants ────────────────────────────────────────────────────────────
-- Aceeași plasă ca 003/040/041: izolarea PRIMARĂ rămâne `business_id = $1` în
-- cod; RLS transformă un query greșit în „zero rezultate", nu în voturile altui
-- client.
alter table web_feedback enable row level security;

drop policy if exists bot_runtime_tenant on web_feedback;
create policy bot_runtime_tenant on web_feedback to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

-- Dashboardul (membru al businessului) poate CITI; nu scrie. Un vot se naște
-- doar dintr-un token semnat, niciodată dintr-un client de dashboard.
drop policy if exists "member read" on web_feedback;
create policy "member read" on web_feedback for select
  using (business_id in (select my_business_ids()));

-- Grants MINIME: fără DELETE. Un vot e o DOVADĂ — se corectează prin `revision`,
-- nu prin ștergere; retenția pleacă odată cu conversația (cascade).
grant select, insert, update on web_feedback to bot_runtime;
