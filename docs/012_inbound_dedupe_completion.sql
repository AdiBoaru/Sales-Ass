-- ============================================================================
-- 012 — INBOUND DEDUPE: watermark de finalizare (claim-or-resume + dead-letter) · NX-86
-- ----------------------------------------------------------------------------
-- Închide gaura „mesaj revendicat dar neprocesat" (crash în mijlocul turului): pe lângă claim,
-- ținem `claimed_at` (când a fost revendicat ultima dată) și `completed_at` (când turul a fost
-- FINALIZAT; NULL = în lucru / orfan). claim_inbound devine claim-or-resume: un orfan (completed
-- NULL + claim expirat) e reclamat și reprocesat; un finalizat e skip. Reaper-ul PEL (XAUTOCLAIM,
-- consumer.py) recuperează separat intrările de la consumeri morți.
--
-- Numerotare: 010 = conversations one-open (NX-87); 011 = WIP local → NX-86 ia 012.
-- Rulează ca admin: python scripts/apply_012.py
-- ============================================================================

alter table public.inbound_dedupe
  add column if not exists claimed_at   timestamptz not null default now(),
  add column if not exists completed_at timestamptz;

-- Backfill ONE-TIME: rândurile clar istorice (first_seen vechi) sunt deja procesate →
-- marchează-le finalizate. Guard pe `first_seen < now()-1h` → re-rularea NU atinge in-flight-ul.
update public.inbound_dedupe
   set completed_at = first_seen
 where completed_at is null
   and first_seen < now() - make_interval(hours => 1);

-- Index pentru reaper-ul de orfani (scanează DOAR nefinalizatele).
create index if not exists idx_inbound_dedupe_orphan
  on public.inbound_dedupe (claimed_at) where completed_at is null;

-- bot_runtime trebuie să poată UPDATE (claim-or-resume + mark_completed). Avea select/insert/delete.
grant update on public.inbound_dedupe to bot_runtime;
