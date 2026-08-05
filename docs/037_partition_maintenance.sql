-- ============================================================================
-- 037 — NX-218: partiții lunare reale pentru analytics_events + messages
-- ============================================================================
-- PROBLEMA (constatată live 2026-08-05): schema_v2 a creat DOAR partițiile
-- 2026_06 / 2026_07 + partiția DEFAULT, iar întreținerea (pg_cron / pg_partman,
-- lăsată ca „exemplu manual" în schema_v2 liniile 210 și 871) n-a rulat NICIODATĂ.
-- Consecință: tot ce s-a scris de la 1 august încoace a aterizat în DEFAULT
-- (analytics_events_default = 33 rânduri, messages_default = 4, toate august).
-- Nimic nu e pierdut — dar partiționarea e efectiv oprită: scanările cresc
-- monoton, iar retenția prin `drop partition` devine imposibilă pe date
-- amestecate în default.
--
-- CE FACE: creează partițiile 2026-08 .. 2026-12 pentru ambele tabele și MUTĂ
-- rândurile din DEFAULT în partiția corectă.
--
-- ORDINEA CONTEAZĂ: Postgres nu te lasă să creezi o partiție al cărei interval e
-- deja acoperit de rânduri aflate în partiția DEFAULT (validează constrângerea
-- default-ului). Deci pentru fiecare (tabel, lună): scoatem rândurile din default
-- într-o tabelă temporară, creăm partiția, le reinserăm prin PĂRINTE (routing
-- normal le duce în partiția nouă). Totul într-o singură tranzacție — runner-ul
-- de migrări (scripts/migrate.py) rulează o tranzacție per fișier.
--
-- IDEMPOTENTĂ: partițiile deja existente sunt sărite (to_regclass). Rulabilă și
-- pe o DB curată (default gol → zero mutări).
--
-- DE AICI ÎNCOLO: întreținerea o face src/jobs/partition_maintenance.py (zilnic,
-- creează luna curentă + următoarea ÎNAINTE să existe rânduri pentru ele, deci
-- fără mutări). Migrarea asta e reparația unică a istoriei.
-- ============================================================================

do $$
declare
  v_tables text[] := array['analytics_events', 'messages'];
  v_table  text;
  v_month  date;
  v_part   text;
  v_lo     timestamptz;
  v_hi     timestamptz;
  v_moved  bigint;
  v_left   bigint;
  v_ovr    text;
begin
  foreach v_table in array v_tables loop
    -- `overriding system value` e obligatoriu la reinserarea unui id de tip
    -- `generated always as identity` (analytics_events.id) și ILEGAL pe un tabel
    -- fără coloană identity (messages.id = uuid cu default) → îl calculăm per tabel.
    select case when exists (
             select 1 from pg_attribute
             where attrelid = v_table::regclass
               and attidentity <> ''
               and not attisdropped
           ) then 'overriding system value' else '' end
      into v_ovr;

    for v_month in
      select generate_series('2026-08-01'::date, '2026-12-01'::date, interval '1 month')::date
    loop
      v_part := v_table || '_' || to_char(v_month, 'YYYY_MM');
      continue when to_regclass(v_part) is not null;

      v_lo := v_month::timestamptz;
      v_hi := (v_month + interval '1 month')::timestamptz;

      -- 1. scoate din DEFAULT rândurile care aparțin intervalului
      execute format(
        'create temp table _nx218_move as
           with moved as (
             delete from %I
             where created_at >= %L and created_at < %L
             returning *
           )
           select * from moved',
        v_table || '_default', v_lo, v_hi
      );
      execute 'select count(*) from _nx218_move' into v_moved;

      -- 2. creează partiția (acum default-ul nu mai are rânduri în interval)
      execute format(
        'create table %I partition of %I for values from (%L) to (%L)',
        v_part, v_table, v_lo, v_hi
      );

      -- 3. reinserează prin PĂRINTE → routing-ul le duce în partiția nouă
      if v_moved > 0 then
        execute format('insert into %I %s select * from _nx218_move', v_table, v_ovr);
      end if;
      execute 'drop table _nx218_move';

      raise notice 'NX-218: % creată, % rânduri mutate din DEFAULT', v_part, v_moved;
    end loop;

    execute format('select count(*) from %I', v_table || '_default') into v_left;
    raise notice 'NX-218: %_default conține % rânduri după migrare', v_table, v_left;
  end loop;
end $$;
