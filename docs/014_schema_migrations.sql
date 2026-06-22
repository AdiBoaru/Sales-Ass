-- ============================================================================
-- 014 — schema_migrations: tabel de tracking pentru migrări (NX-123)
-- ----------------------------------------------------------------------------
-- Înlocuiește scripturile one-off `apply_0NN.py` (fire-and-forget, fără stare)
-- cu stare INTEROGABILĂ. Runner-ul `scripts/migrate.py` aplică ordonat numeric
-- (003 < 010, NU lexicografic), înregistrează fiecare migrare aici și verifică
-- checksum-ul. Poarta de boot (`consumer.py`) refuză pornirea workerului dacă
-- există migrări pending (P6 — niciodată boot tăcut peste o schemă incompletă;
-- regresia 010/012 care crăpa PRIMUL mesaj al fiecărui client nou).
--
-- Control plane (NE-tenant): fără business_id, fără RLS. `bot_runtime` are DOAR
-- SELECT (poarta de boot poate citi starea); INSERT rămâne rezervat control
-- plane-ului (migrate.py rulează cu DSN privilegiat, ca apply_*). Aditiv +
-- idempotent (IF NOT EXISTS / ON CONFLICT) — rulabil de două ori fără eroare.
-- ============================================================================

create table if not exists schema_migrations (
  version    text primary key,           -- prefixul numeric din numele fișierului: "003".."014"
  filename   text not null,              -- numele complet al fișierului (sursă de adevăr)
  checksum   text not null,              -- sha256 al conținutului .sql la momentul aplicării
  applied_at timestamptz not null default now()
);

grant select on schema_migrations to bot_runtime;

-- Backfill idempotent: 003–013 sunt DEJA aplicate live (manual, prin apply_0NN.py
-- — vezi DB_MIGRATION_NOTES). Le marcăm cu checksum 'legacy' ca runner-ul să NU le
-- reaplice peste o DB de prod existentă. Drift-ul 'legacy' vs sha256 real e ignorat
-- intenționat (nu cunoaștem conținutul de la momentul aplicării istorice); 014+ se
-- aplică normal, cu checksum real. Pe o DB PROASPĂTĂ (CI/dev), runner-ul aplică
-- 003→013 ÎNAINTE de 014, le înregistrează cu checksum real, iar acest backfill
-- devine no-op (ON CONFLICT DO NOTHING).
insert into schema_migrations (version, filename, checksum) values
  ('003', '003_bot_runtime_role.sql',            'legacy'),
  ('004', '004_inbound_dedupe.sql',              'legacy'),
  ('005', '005_bot_runtime_login.sql',           'legacy'),
  ('006', '006_semantic_cache_v2.sql',           'legacy'),
  ('007', '007_semantic_cache_invalidation.sql', 'legacy'),
  ('008', '008_order_items_insert.sql',          'legacy'),
  ('009', '009_gdpr_svc_role.sql',               'legacy'),
  ('010', '010_conversations_one_open.sql',      'legacy'),
  ('011', '011_bot_runtime_read_aliases_faqs.sql', 'legacy'),
  ('012', '012_inbound_dedupe_completion.sql',   'legacy'),
  ('013', '013_usage_cached_tokens.sql',         'legacy')
on conflict (version) do nothing;
