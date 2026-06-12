-- ============================================================================
-- 003 — ROL bot_runtime + PLASĂ RLS (app.business_id) + GUARD state 8KB
-- ----------------------------------------------------------------------------
-- STATUS: DRAFT — NEAPLICAT, NETESTAT.
--   Se rulează și se verifică la T018 (acces DB direct, conexiunea 5432).
--   Verificare obligatorie după aplicare: conectat ca bot_runtime, fără
--   `set app.business_id`, un `select * from contacts` trebuie să dea 0 rânduri;
--   cu business_id setat → doar rândurile tenantului.
-- ----------------------------------------------------------------------------
-- Aditiv peste schema_v2_production.sql (NU modifică tabele existente).
-- Implementează principiul 7 (RLS ca plasă) peste schema reală:
--   • workerul aplicației se conectează ca `bot_runtime` (FĂRĂ bypassrls)
--   • `service_role` rămâne DOAR pentru migrări și joburi admin
--   • pool-ul asyncpg face `SET app.business_id = $1` per conexiune
-- ============================================================================

-- 1. Rol aplicație (worker). nologin: login/parola se setează la provisioning.
do $$
begin
  if not exists (select from pg_roles where rolname = 'bot_runtime') then
    create role bot_runtime nologin;
  end if;
end $$;

grant usage on schema public to bot_runtime;

-- 2. Helper: business_id-ul sesiunii curente (setat de pool per conexiune).
--    `true` = missing_ok → întoarce NULL dacă nu e setat (=> 0 rânduri, safe).
create or replace function current_business_id() returns uuid
language sql stable as $$
  select nullif(current_setting('app.business_id', true), '')::uuid;
$$;

-- 3. GRANTS pe categorii de tabele
-- 3a. Catalog tenant-scoped (read) — botul citește, nu scrie (excepție 3c)
grant select on
  products, product_variants, product_embeddings, reviews,
  product_review_summaries, brands, categories, faqs, channels,
  businesses, wa_templates, catalog_sync_runs, catalog_quality_alerts
to bot_runtime;

-- 3b. Catalog child fără business_id (citite prin join la parent tenant-filtrat)
grant select on
  product_images, product_sections, product_ingredients, ingredients,
  product_badges, product_category_map, order_items
to bot_runtime;

-- 3c. Catalog scriere permisă botului (straturi gratuite + shadow)
grant insert, update on semantic_cache to bot_runtime;
grant insert, update on intent_aliases to bot_runtime;  -- candidates din shadow

-- 3d. Runtime conversațional — CRUD
grant select, insert, update, delete on
  contacts, channel_identities, conversations, conversation_summaries,
  messages, message_status_events, outbox, checkout_links, orders,
  shipments, back_in_stock_subscriptions, proactive_jobs, appointments
to bot_runtime;

-- 3e. Analytics append-only (fără UPDATE/DELETE — forțat); usage_daily = rollup
grant insert on analytics_events to bot_runtime;
grant select, insert, update on usage_daily to bot_runtime;

-- 4. POLITICI RLS pentru bot_runtime
--    RLS e deja ENABLED pe toate tabelele public (din schema_v2). Politicile
--    sunt permisive (OR), deci cele „member read" pt dashboard coexistă cu astea.

-- 4a. Tabele tenant-scoped (au business_id): izolare strictă pe app.business_id
do $$
declare
  t text;
  tenant_tables text[] := array[
    'contacts','channel_identities','conversations','conversation_summaries',
    'messages','message_status_events','outbox','checkout_links','orders',
    'shipments','back_in_stock_subscriptions','proactive_jobs','appointments',
    'semantic_cache','intent_aliases','products','product_variants',
    'product_embeddings','reviews','product_review_summaries','brands',
    'categories','faqs','channels','businesses','wa_templates','usage_daily',
    'catalog_sync_runs','catalog_quality_alerts'
  ];
begin
  foreach t in array tenant_tables loop
    execute format('drop policy if exists bot_runtime_tenant on %I', t);
    -- businesses: coloana e `id`, nu `business_id`
    if t = 'businesses' then
      execute format($f$
        create policy bot_runtime_tenant on %I to bot_runtime
          using (id = current_business_id())
          with check (id = current_business_id())
      $f$, t);
    else
      execute format($f$
        create policy bot_runtime_tenant on %I to bot_runtime
          using (business_id = current_business_id())
          with check (business_id = current_business_id())
      $f$, t);
    end if;
  end loop;
end $$;

-- 4b. analytics_events: append-only, dar tot izolat pe business_id la insert
drop policy if exists bot_runtime_analytics on analytics_events;
create policy bot_runtime_analytics on analytics_events to bot_runtime
  for insert with check (business_id = current_business_id());

-- 4c. Catalog child fără business_id (no PII): citibile prin join la parent.
--     Parent-ul (products) e deja filtrat pe business_id → join-ul e tenant-safe.
do $$
declare
  t text;
  child_tables text[] := array[
    'product_images','product_sections','product_ingredients','ingredients',
    'product_badges','product_category_map','order_items'
  ];
begin
  foreach t in array child_tables loop
    execute format('drop policy if exists bot_runtime_child_read on %I', t);
    execute format($f$
      create policy bot_runtime_child_read on %I to bot_runtime
        for select using (true)
    $f$, t);
  end loop;
end $$;

-- 5. GUARD: state-ul conversației ≤ 8KB (ultima linie de apărare; bugetul
--    real se impune în context builder — principiul 4).
alter table conversations
  drop constraint if exists chk_state_size;
alter table conversations
  add constraint chk_state_size check (pg_column_size(state) < 8192);

-- ============================================================================
-- VERIFICARE POST-APLICARE (manual, la T018):
--   set role bot_runtime;
--   select * from contacts;                       -- aștept: 0 rânduri (fără app.business_id)
--   set app.business_id = '<un business_id real>';
--   select count(*) from contacts;                -- aștept: doar tenantul respectiv
--   insert into analytics_events(business_id,event_type) values ('<alt_id>','x'); -- aștept: refuzat de with check
--   reset role;
-- ============================================================================
