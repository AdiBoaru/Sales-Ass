-- ============================================================================
-- 044 — NX-249: policy de release (control plane, append-only) + captura
--       asignării pe fiecare turn din ledger
-- ============================================================================
-- DE CE o migrare, când cardul spune „nu e necesară implicit una":
--   Cardul cere STOP + aprobare dacă inventarul dovedește că asignarea nu poate fi
--   durabilă și neambiguă. Inventarul (docs/STAGE1-RELEASE-DECISIONS.md §1) o dovedește:
--
--     • singurul câmp capturat azi în `web_turns` e `pipeline_version`, iar el înseamnă
--       CONTRACTUL RĂSPUNSULUI (`web-chat.v1`, citit de proiecția NX-233), nu releaseul.
--       A-l încărca și cu „ce pipeline a rulat" ar fi exact suprapunerea semantică pe care
--       cardul o interzice — și ar rupe proiecția, care ramifică pe valoarea lui;
--     • tot ce decide CARE pipeline rulează (`SINGLE_BRAIN_ENABLED`, `WEB_VIEW_V2_
--       PROJECTOR_ENABLED`, providerul NX-238, `RELEASE_TRACK`) e un env citit de proces
--       ÎN TIMPUL turului. Deci un reclaim după deploy rulează alt cod pe același turn —
--       rândul „retry/reclaim după deploy → același pipeline" din failure matrix e încălcat
--       prin construcție, nu din neatenție;
--     • `src/observability/slo.py::_missing_capabilities` declară deja `per_row_release_sha`
--       drept capabilitate LIPSĂ. Fără ea, raportul candidate-vs-control cerut de card
--       (cohorts comparabile pe aceeași fereastră) nu se poate calcula din ledger.
--
--   Reconstruirea din `conversations.created_at` + istoricul de policy ar depinde de ceas
--   și ar cădea exact pe cazul care contează (un epoch aplicat între crearea conversației
--   și tur). Un turn trebuie să știe SINGUR ce a rulat.
--
-- EXPAND-ONLY (cerință de rollback, DoD „previous image compatibility"):
--   coloanele sunt NULLABLE, fără default și fără backfill; tabelul e nou. Imaginea
--   precedentă nu le citește și nu le scrie, deci rulează neschimbată peste schema asta.
--   NULL înseamnă „turn de dinainte de card" și se raportează ca `unknown` — NICIODATĂ
--   promovat tăcut la `champion` (lipsa datelor nu e verde, NX-246).
--
-- ============================================================================
-- PARTEA 1 — captura pe ledger
-- ============================================================================
-- Trei coloane, fiecare cu un motiv propriu (nu una compozită: un raport care trebuie să
-- despartă un text ca să afle cohortul e un raport care va despărți greșit):
--   • `release_track`          — care pipeline a rulat. Cohortul din TOATE rapoartele.
--   • `release_policy_id`      — care policy a decis. Evidence packetul o cere explicit.
--   • `release_policy_revision`— a câta revizie. Distinge două aplicări ale aceluiași policy.
--
-- Se scriu O SINGURĂ DATĂ, în acceptul atomic (`insert_turn`), ÎNAINTE de orice claim.
-- Nimic din execuție nu le rescrie: un reclaim CITEȘTE rândul, nu recalculează asignarea.

alter table web_turns
  add column if not exists release_track text,
  add column if not exists release_policy_id text,
  add column if not exists release_policy_revision integer;

-- Vocabular ÎNCHIS pe track (ca `status`): un track scris greșit ar produce un cohort
-- fantomă în raport, adică exact genul de diferență care se citește ca „candidate e mai bun".
-- NULL rămâne permis = rândurile de dinainte de migrare.
alter table web_turns drop constraint if exists web_turns_release_track_ck;
alter table web_turns add constraint web_turns_release_track_ck
  check (release_track is null or release_track in ('champion', 'candidate'));

-- Revizia e un contor, nu o etichetă.
alter table web_turns drop constraint if exists web_turns_release_revision_ck;
alter table web_turns add constraint web_turns_release_revision_ck
  check (release_policy_revision is null or release_policy_revision >= 0);

-- Cohortul se citește pe (tenant, track, fereastră) — exact forma query-ului de raport
-- (`db/queries/release.py::cohort_facts`). Fără index, raportul ar face seq scan pe ledger
-- la fiecare rulare zilnică.
create index if not exists idx_web_turns_release_cohort
  on web_turns (business_id, release_track, accepted_at desc);

-- ============================================================================
-- PARTEA 2 — `release_policies`: istoricul de policy, append-only
-- ============================================================================
-- DE CE un tabel și nu un artefact semnat pe disc (ca `decision.json` la NX-238):
--   NX-238 decide o dată, la build. Aici ținta operațională e „≤5 minute de la decizie la
--   zero accepturi candidate noi" — iar un artefact din imagine se schimbă doar prin deploy,
--   care la NX-248 e o promovare umană pe GitHub Environments. Kill-switchul ar fi mai lent
--   decât incidentul. Redis e interzis explicit de card (și ar fi oricum best-effort).
--
-- DE CE append-only și nu un rând mutabil:
--   „Policy-urile sunt append-only/istoric-reproductibile pe durata releaseului" (card).
--   Un UPDATE ar face imposibilă întrebarea „ce policy era în vigoare când s-a acceptat
--   turul X?" — care e chiar întrebarea unui incident. Aici răspunsul e un SELECT.
--
-- CAS FĂRĂ LOCK: `unique (environment, revision)`. Cine aplică trimite `revision =
--   expected + 1`; doi actori concurenți produc același număr, iar al doilea primește
--   UniqueViolation. Compare-and-set e în SCHEMĂ, nu într-o secvență de citire+scriere care
--   are o fereastră de race între ele (aceeași disciplină ca `upsert_feedback` la NX-246).
--
-- CONTROL PLANE, NU TENANT: policy-ul ține ALLOWLISTUL de tenanți eligibili. Un rând care
--   listează tenanți nu poate fi citit de pe o conexiune tenant-scoped fără să scurgă cine
--   altcineva e în canary. De aceea: fără `business_id`, fără RLS tenant, ȘI FĂRĂ grant
--   pentru `bot_runtime` — se citește exclusiv pe `admin_conn` (a doua excepție documentată
--   de control plane, după `provider_account_id → business_id`), cu cache bounded în proces.
--   Runtime-ul nu poate nici să-l scrie, nici să-l citească direct.

create table if not exists release_policies (
  id            bigint generated always as identity primary key,
  environment   text    not null,
  revision      integer not null check (revision >= 0),
  policy_id     text    not null,
  -- Documentul validat (`ReleasePolicy.to_payload()`), inclusiv amprenta canonică. NU conține
  -- saltul de bucketing — doar `stable_salt_id`: secretul stă în config, nu în DB și nu în log.
  policy        jsonb   not null,
  -- Cine și de ce. Obligatorii: un release fără actor nu e o decizie, e un accident.
  actor         text    not null check (length(trim(actor)) > 0),
  reason        text    not null check (length(trim(reason)) > 0),
  change_ticket text,
  applied_at    timestamptz not null default now(),

  -- CAS: a doua aplicare cu aceeași revizie pierde, determinist.
  constraint uq_release_policies_revision unique (environment, revision)
);

-- Citirea fierbinte e „ultima revizie a mediului".
create index if not exists idx_release_policies_current
  on release_policies (environment, revision desc);

alter table release_policies enable row level security;

-- Dashboardul NU vede release policy (nu e date de tenant, iar allowlistul e informație de
-- operare). Nicio politică permisivă = nimeni în afară de rolurile privilegiate nu citește.
-- `bot_runtime` NU primește grant: dacă mâine cineva încearcă să citească policy-ul de pe
-- calea tenantului, primește permission denied, nu date.
revoke all on release_policies from public;

-- ROLLBACK operațional (păstrează istoricul — nu se șterge o decizie de release):
--   • aplică un policy nou cu `mode='force_control'` (revizie NOUĂ, nu UPDATE);
--   • sau stinge `RELEASE_CONTROLLER_ENABLED` → asignarea devine champion pentru tot.
-- ROLLBACK distructiv (DOAR dacă migrarea însăși se retrage; imaginea veche nu are nevoie
-- de el, coloanele fiind nullable):
--   drop table if exists release_policies;
--   drop index if exists idx_web_turns_release_cohort;
--   alter table web_turns drop constraint if exists web_turns_release_track_ck;
--   alter table web_turns drop constraint if exists web_turns_release_revision_ck;
--   alter table web_turns drop column if exists release_track,
--                        drop column if exists release_policy_id,
--                        drop column if exists release_policy_revision;
