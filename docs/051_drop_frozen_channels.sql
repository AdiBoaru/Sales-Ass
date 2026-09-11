-- ============================================================================
-- 051 — NX-289: schema pierde WhatsApp și Telegram (obiecte + vocabular)
-- ----------------------------------------------------------------------------
-- CONTEXT
--   NX-179 declarase cele două canale ÎNGHEȚATE: cod păstrat, zero investiție. Decizia de acum
--   e alta — se ȘTERG. Codul lor a plecat în același commit (`src/channels/telegram/`,
--   `src/meta_client.py`, `src/webhook/meta.py`, rutele `GET/POST /webhook`, poarta de template
--   din proactiv). Migrarea asta scoate ce rămăsese în schemă, ca ea să nu mai descrie un sistem
--   care nu există: un tabel gol pe care nimeni nu-l scrie e o promisiune, nu o rezervă.
--
-- CE S-A MĂSURAT ÎNAINTE (proiectul `NativexSales`, 2026-09-10, citire read-only):
--   channels                → 22 rânduri, TOATE `webchat`
--   conversations           → 1, pe canal `webchat`
--   channel_identities      → 0
--   wa_templates            → 0        message_status_events → 0
--   messages.template_id    → 0 non-NULL     proactive_jobs → 0 rânduri
--   messages.content_type   → doar 'text' (150) și 'action' (15)
--   Deci pe baza curentă migrarea e PUR DDL: nu șterge date de client. Cele 17 conversații
--   Telegram de test trăiau pe proiectul vechi (eu-west-1), abandonat din 2026-08-28.
--   `delete`-urile de mai jos rămân totuși în migrare — pe un mediu unde ar exista rânduri,
--   restrângerea CHECK-urilor ar eșua fără ele, iar un eșec la jumătate e mai rău decât un
--   no-op explicit. Sunt scrise ca să fie idempotente.
--   ⚠️ Pe un mediu CU astfel de rânduri, secțiunea 1 ȘTERGE conversațiile și mesajele lor.
--   E o pierdere de date DELIBERATĂ (canalul nu mai poate fi servit), dar verific-o înainte:
--     select ch.kind, count(distinct c.id) as conv, count(m.id) as msg
--       from channels ch
--       left join conversations c on c.channel_id = ch.id
--       left join messages m on m.conversation_id = c.id
--      where ch.kind in ('whatsapp','telegram','instagram') group by 1;
--   Dacă întoarce rânduri și vrei istoricul, exportă-l ÎNAINTE.
--
-- CE SE ȘTERGE ȘI DE CE
--   wa_templates            — ciclul de viață al template-urilor APROBATE de Meta. Fără WhatsApp
--                             nu există noțiunea „mesaj pre-aprobat de o platformă". Cade cu
--                             `cascade` (îi ia trigger-ul, politica RLS și FK-ul din
--                             `proactive_jobs.template_id`).
--   proactive_jobs.template_id — coloana rămânea uuid orfan după cascade; o scoatem explicit.
--   messages.template_id    — „dacă a fost template Meta". Niciodată non-NULL aici.
--   message_status_events   — delivered/read/failed raportate de provider prin webhook. Web-ul e
--                             sincron (răspunsul E transportul); nimeni nu mai produce astfel de
--                             evenimente, deci tabelul ar fi rămas un log care nu se scrie.
--   in_24h_window(conversations) — fereastra de 24h e o REGULĂ A PLATFORMEI Meta (în afara ei
--                             se putea trimite doar template). Pe webchat nu are corespondent.
--                             `conversations.last_inbound_at` RĂMÂNE: îl folosesc sweeper-ele
--                             proactive (coș abandonat) — funcția pleacă, faptul rămâne.
--
-- CE SE RESTRÂNGE (vocabular) — un CHECK care încă acceptă 'whatsapp' e o invitație
--   channels.kind                    → ('webchat')
--   channel_identities.channel_kind  → ('webchat')
--   outbox.kind                      → ('message')     [erau 'template','typing','reaction']
--   messages.content_type            → fără 'interactive' și 'template' (ambele = termeni Meta
--                                      pentru mesaje de PROVIDER; vezi nota migrării 043)
--
-- CE NU SE ATINGE, DELIBERAT
--   proactive_jobs.status păstrează 'skipped_no_window' — nimeni nu-l mai scrie (vezi
--     `_SKIP_STATUS` în src/proactive/scheduler.py), dar e un verdict ISTORIC; a-l scoate ar
--     face migrarea să eșueze pe orice mediu cu rânduri vechi, ca să câștige un cuvânt.
--   usage_daily.templates_sent rămâne, ca `handoffs`: are date istorice reale, iar dashboardul
--     citește seria ca sumă — 0 e adevărul, o coloană ștearsă ar fi o gaură.
--   channel_identities / conversations.channel_id — structura multi-canal (NX-60) NU dispare.
--     E motivul pentru care stagiile 3-9 nu știu de niciun canal; ce dispare sunt IMPLEMENTĂRILE.
--
-- IMPACT OPERAȚIONAL
--   `messages` e PARTIȚIONAT pe lună: DROP CONSTRAINT pe părinte îl scoate din partiții, ADD
--   recursează și VALIDEAZĂ fiecare partiție (lock ACCESS EXCLUSIVE cât ține). Noul set e un
--   SUBSET, deci validarea POATE eșua — dar numai dacă există rânduri 'interactive'/'template';
--   `delete`-ul de mai sus nu se aplică aici (nu ștergem mesaje istorice), așa că pe un mediu
--   cu astfel de rânduri migrarea se oprește ZGOMOTOS, cu constrângerea numită. Corect: un
--   mesaj real de provider în ledger e o decizie umană, nu ceva de aruncat de o migrare.
--
-- ROLLBACK: nu există „undrop table". Recrearea se face din `docs/schema_v2_production.sql`
--   (secțiunile wa_templates / message_status_events / in_24h_window) plus repunerea
--   CHECK-urilor largi. Datele nu se recuperează decât din backup.
--
-- IDEMPOTENT: `drop ... if exists` + `drop constraint if exists` urmat de `add`. Re-rularea dă
--   aceeași stare finală.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Rânduri ale canalelor dispărute (no-op pe baza curentă; vezi antetul)
-- ---------------------------------------------------------------------------
-- ORDINEA E O CONSTRÂNGERE, nu o preferință — verificată în `pg_constraint`:
--   • `conversations.channel_id → channels` e **RESTRICT**, deci canalul nu poate pleca
--     înaintea conversațiilor lui;
--   • `messages` e PARTIȚIONAT și **nu are FK** către `conversations` (un tabel partiționat nu
--     poate fi referit ca și copil aici), deci ștergerea conversațiilor ar lăsa mesaje ORFANE
--     dacă nu le ștergem EXPLICIT, întâi. Copiii cu `on delete cascade` (outbox, web_turns,
--     conversation_traces, carts, checkout_links, summaries, feedback, receipts) se ocupă singuri;
--     `proactive_jobs`/`appointments` primesc `set null`, corect — sunt ale contactului, nu ale
--     conversației.
-- `analytics_events` (append-only, partiționat, fără FK) NU se atinge: e ledgerul de măsurători,
-- iar un eveniment despre un tur care chiar a avut loc rămâne adevărat după ce canalul dispare.
delete from channel_identities where channel_kind in ('whatsapp', 'telegram', 'instagram');

delete from messages m
 where exists (
   select 1
     from conversations c
     join channels ch on ch.id = c.channel_id
    where c.id = m.conversation_id
      and c.business_id = m.business_id
      and ch.kind in ('whatsapp', 'telegram', 'instagram')
 );

delete from conversations c
 where exists (
   select 1 from channels ch
    where ch.id = c.channel_id
      and ch.kind in ('whatsapp', 'telegram', 'instagram')
 );

delete from channels where kind in ('whatsapp', 'telegram', 'instagram');

-- ---------------------------------------------------------------------------
-- 2. Obiecte care existau DOAR pentru canalele dispărute
-- ---------------------------------------------------------------------------
-- cascade: trigger `trg_wa_templates_upd`, politica RLS „member read",
-- FK `proactive_jobs_template_id_fkey`.
drop table if exists wa_templates cascade;

alter table proactive_jobs drop column if exists template_id;

drop table if exists message_status_events;

alter table messages drop column if exists template_id;

drop function if exists in_24h_window(conversations);

-- ---------------------------------------------------------------------------
-- 3. Vocabularul: CHECK-urile nu mai numesc canale care nu există
-- ---------------------------------------------------------------------------
alter table channels drop constraint if exists channels_kind_check;
alter table channels add constraint channels_kind_check
  check (kind in ('webchat'));

alter table channel_identities drop constraint if exists channel_identities_channel_kind_check;
alter table channel_identities add constraint channel_identities_channel_kind_check
  check (channel_kind in ('webchat'));

alter table outbox drop constraint if exists outbox_kind_check;
alter table outbox add constraint outbox_kind_check
  check (kind in ('message'));

alter table messages drop constraint if exists messages_content_type_check;
alter table messages add constraint messages_content_type_check
  check (content_type in ('text', 'image', 'audio', 'video', 'document',
                          'location', 'sticker',
                          -- NX-236: tur pornit dintr-un token de acțiune opac (web widget).
                          -- `body` e gol; înțelesul stă în `payload->'action'`.
                          'action'));
