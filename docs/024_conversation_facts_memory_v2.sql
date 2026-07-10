-- ============================================================================
-- 024 — NX-160 felia 2: conversation_facts Memory v2
-- ----------------------------------------------------------------------------
-- Extinde memoria de la „whitelist per-vertical fail-closed" (NX-148) la memorie
-- GENERICĂ pe orice business: capture broad → classify safety → canonicalize →
-- inject only safe. Modelul extrage `raw_key` liber; codul clasează siguranța
-- (`safety_class` + `visibility`) și canonizează (`canonical_key`) determinist.
--   • raw_key       — cheia liberă emisă de model (Raw Candidate Memory)
--   • canonical_key — slotul canonic rezolvat de canonicalizer (NULL = doar raw)
--   • memory_key    — cheia de deduplicare: 'canonical:<k>' dacă avem canonical,
--                     altfel 'raw:<raw_key>'. Un fact activ per memory_key/contact.
--   • safety_class  — safe | pii | health | financial | sensitive | unknown
--   • visibility    — inject (poate ajunge în prompt) | candidate | drop
-- `fact_type` rămâne ca ALIAS backcompat (nu-l ștergem — cod vechi + rollback).
-- `source_message_id` există deja din 023 (populat real de felia 3).
-- Aditiv + idempotent. P7: tenant-scoped (grant + RLS moștenite din 023).
-- ============================================================================

-- --- coloane noi (idempotent) ----------------------------------------------
alter table conversation_facts
  add column if not exists raw_key       text,
  add column if not exists canonical_key text,
  add column if not exists memory_key    text,
  add column if not exists safety_class  text not null default 'unknown',
  add column if not exists visibility    text not null default 'candidate';

-- --- backfill rândurile legacy (NX-148) ------------------------------------
-- Cheia liberă a rândurilor vechi = fact_type. Erau deja filtrate prin whitelist-ul
-- vechi → sigure de injectat, deci visibility='inject', safety_class='safe'.
update conversation_facts
   set raw_key       = coalesce(raw_key, fact_type),
       canonical_key = coalesce(canonical_key, fact_type),
       memory_key    = coalesce(memory_key, 'canonical:' || fact_type),
       safety_class  = case when safety_class = 'unknown' then 'safe' else safety_class end,
       visibility    = case when visibility = 'candidate' then 'inject' else visibility end
 where memory_key is null;

-- memory_key devine obligatoriu abia DUPĂ backfill (rândurile vechi au acum valoare).
alter table conversation_facts
  alter column memory_key set not null;

-- --- CHECK-uri de igienă pe enum-uri (idempotent prin DROP+ADD) -------------
alter table conversation_facts drop constraint if exists conversation_facts_safety_class_ck;
alter table conversation_facts add constraint conversation_facts_safety_class_ck
  check (safety_class in ('safe', 'pii', 'health', 'financial', 'sensitive', 'unknown'));

alter table conversation_facts drop constraint if exists conversation_facts_visibility_ck;
alter table conversation_facts add constraint conversation_facts_visibility_ck
  check (visibility in ('inject', 'candidate', 'drop'));

-- --- rotația unique-ului: fact_type → memory_key ---------------------------
-- Vechiul unique (business_id, contact_id, fact_type) ar bloca canonicalizarea
-- (două raw_key diferite care mapează pe același canonical trebuie să conveargă
-- pe memory_key, nu pe fact_type). Îl înlocuim cu unique pe memory_key.
alter table conversation_facts
  drop constraint if exists conversation_facts_business_id_contact_id_fact_type_key;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'conversation_facts_biz_contact_memkey_key'
  ) then
    alter table conversation_facts
      add constraint conversation_facts_biz_contact_memkey_key
      unique (business_id, contact_id, memory_key);
  end if;
end $$;

-- read path filtrează pe visibility='inject' → index parțial util.
create index if not exists conversation_facts_inject_idx
  on conversation_facts (business_id, contact_id)
  where visibility = 'inject';
