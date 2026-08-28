-- DDL generat din baza LIVE de `scripts/dump_schema_ddl.py`.
-- NU edita direct: regenereaza, apoi aplica peste el patch-ul din schema_v3.
-- migrari aplicate in sursa: 003, 004, 005, 006, 007, 008, 009, 010, 011, 012, 013, 014, 015, 016, 017, 018, 019, 020, 021, 022, 023, 024, 025, 026, 027, 028, 029, 030, 032, 033, 034, 035, 036, 037, 038, 039, 040, 041, 042, 043, 044, 045

set check_function_bodies = off;

-- ==========================================================================
-- EXTENSII
-- ==========================================================================

create extension if not exists "pg_stat_statements" with schema extensions;
create extension if not exists "pg_trgm" with schema public;
create extension if not exists "pgcrypto" with schema extensions;
create extension if not exists "supabase_vault" with schema vault;
create extension if not exists "uuid-ossp" with schema extensions;
create extension if not exists "vector" with schema public;

-- ==========================================================================
-- FUNCȚII fără dependențe de tabele
-- ==========================================================================

CREATE OR REPLACE FUNCTION public.category_has_active_products(cat_id uuid)
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
  with recursive subtree as (
    select cat_id as id
    union all
    select c.id
    from categories c
    join subtree s on c.parent_id = s.id
  )
  select exists (
    select 1
    from products p
    join subtree s on s.id = p.primary_category_id
    where p.status = 'active'
  );
$function$;

CREATE OR REPLACE FUNCTION public.current_business_id()
 RETURNS uuid
 LANGUAGE sql
 STABLE
AS $function$
  select nullif(current_setting('app.business_id', true), '')::uuid;
$function$;

CREATE OR REPLACE FUNCTION public.gdpr_erase_contact(p_contact uuid)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
begin
  update contacts set
    display_name = null, profile = '{}'::jsonb, rfm = null,
    erased_at = now()
  where id = p_contact;
  delete from channel_identities where contact_id = p_contact;
  update messages set body = null, payload = '{}'::jsonb, media_ref = null
    where contact_id = p_contact;
  insert into audit_log(actor, action, entity, entity_id)
  values ('gdpr_svc', 'gdpr_erase', 'contact', p_contact::text);
end; $function$;

CREATE OR REPLACE FUNCTION public.my_business_ids()
 RETURNS SETOF uuid
 LANGUAGE sql
 STABLE SECURITY DEFINER
AS $function$
  select business_id from business_users where user_id = auth.uid();
$function$;

CREATE OR REPLACE FUNCTION public.rls_auto_enable()
 RETURNS event_trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$function$;

CREATE OR REPLACE FUNCTION public.ro_unaccent(txt text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE STRICT
 SET search_path TO 'pg_catalog', 'public'
AS $function$
  select translate(
    lower(txt),
    'ăâîșțşţ',
    'aaistst'
  )
$function$;

CREATE OR REPLACE FUNCTION public.set_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  new.updated_at = now();
  return new;
end; $function$;


-- ==========================================================================
-- TABELE
-- ==========================================================================

create table if not exists analytics_events (
  id bigint generated always as identity not null,
  business_id uuid not null,
  conversation_id uuid,
  contact_id uuid,
  event_type text not null,
  properties jsonb default '{}'::jsonb not null,
  tokens_in integer,
  tokens_out integer,
  cost_usd numeric(10,6),
  created_at timestamp with time zone default now() not null,
  turn_id uuid
)
partition by RANGE (created_at);

create table if not exists appointments (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  conversation_id uuid,
  service_name text not null,
  starts_at timestamp with time zone not null,
  ends_at timestamp with time zone not null,
  status text default 'booked'::text not null,
  external_ref text,
  notes text,
  created_at timestamp with time zone default now() not null
);

create table if not exists audit_log (
  id bigint generated always as identity not null,
  business_id uuid,
  actor text not null,
  action text not null,
  entity text,
  entity_id text,
  details jsonb default '{}'::jsonb not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists back_in_stock_subscriptions (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  product_id uuid not null,
  variant_id uuid,
  notified_at timestamp with time zone,
  created_at timestamp with time zone default now() not null
);

create table if not exists brands (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  name text not null,
  slug text not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists business_users (
  business_id uuid not null,
  user_id uuid not null,
  role text default 'member'::text not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists businesses (
  id uuid default gen_random_uuid() not null,
  name text not null,
  slug text not null,
  vertical text default 'ecommerce'::text not null,
  status text default 'active'::text not null,
  default_locale text default 'ro'::text not null,
  supported_locales text[] default '{ro}'::text[] not null,
  timezone text default 'Europe/Bucharest'::text not null,
  settings jsonb default '{}'::jsonb not null,
  daily_cost_cap_usd numeric(10,4),
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  data_version integer default 1 not null
);

create table if not exists catalog_quality_alerts (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  sync_run_id uuid,
  product_id uuid,
  kind text not null,
  details jsonb default '{}'::jsonb not null,
  resolved_at timestamp with time zone,
  created_at timestamp with time zone default now() not null
);

create table if not exists catalog_sync_runs (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  source text not null,
  status text default 'running'::text not null,
  stats jsonb default '{}'::jsonb not null,
  error text,
  started_at timestamp with time zone default now() not null,
  finished_at timestamp with time zone
);

create table if not exists categories (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  parent_id uuid,
  name text not null,
  slug text not null,
  path text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists channel_identities (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  channel_kind text not null,
  external_id text not null,
  external_id_hash text generated always as (encode(digest(external_id, 'sha256'::text), 'hex'::text)) stored,
  verified boolean default false not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists channels (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  kind text not null,
  provider_account_id text not null,
  display_name text,
  status text default 'active'::text not null,
  credentials_ref text,
  settings jsonb default '{}'::jsonb not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists checkout_links (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  contact_id uuid not null,
  ref_code text not null,
  cart jsonb default '[]'::jsonb not null,
  url text not null,
  clicked_at timestamp with time zone,
  converted_order_id uuid,
  expires_at timestamp with time zone,
  created_at timestamp with time zone default now() not null
);

create table if not exists commerce_action_receipts (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  cart_id uuid,
  operation text not null,
  idempotency_key text not null,
  status text default 'succeeded'::text not null,
  before_version integer default 0 not null,
  after_version integer,
  result_code text,
  turn_id text,
  action_id text,
  external_ref text,
  url text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists contacts (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  display_name text,
  locale text,
  profile jsonb default '{}'::jsonb not null,
  lead_score numeric(5,2) default 0 not null,
  lifecycle text default 'new'::text not null,
  rfm jsonb,
  consent jsonb default '{}'::jsonb not null,
  is_blocked boolean default false not null,
  erased_at timestamp with time zone,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists conversation_cart_items (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  cart_id uuid not null,
  product_id uuid not null,
  variant_id uuid,
  quantity integer not null,
  added_turn_id text,
  updated_turn_id text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists conversation_carts (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  version integer default 0 not null,
  status text default 'active'::text not null,
  currency text,
  external_cart_ref text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  expires_at timestamp with time zone
);

create table if not exists conversation_evals (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  judge_model text not null,
  scores jsonb not null,
  overall numeric(4,2) not null,
  notes text,
  evaluated_at timestamp with time zone default now() not null
);

create table if not exists conversation_facts (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  conversation_id uuid,
  fact_type text not null,
  fact_value jsonb not null,
  confidence real default 0.5 not null,
  source_message_id uuid,
  first_seen_at timestamp with time zone default now() not null,
  last_seen_at timestamp with time zone default now() not null,
  expires_at timestamp with time zone,
  raw_key text,
  canonical_key text,
  memory_key text not null,
  safety_class text default 'unknown'::text not null,
  visibility text default 'candidate'::text not null
);

create table if not exists conversation_summaries (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  upto_message_at timestamp with time zone not null,
  summary text not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists conversation_traces (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  contact_id uuid not null,
  turn_id uuid not null,
  channel_kind text,
  language text,
  model_route text,
  client_text text,
  bot_text text,
  reply jsonb,
  recommended jsonb,
  diagnostics jsonb,
  created_at timestamp with time zone default now() not null
);

create table if not exists conversations (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  channel_id uuid not null,
  status text default 'open'::text not null,
  bot_active boolean default true not null,
  handoff_until timestamp with time zone,
  assigned_user_id uuid,
  last_inbound_at timestamp with time zone,
  last_outbound_at timestamp with time zone,
  last_message_at timestamp with time zone,
  locale text,
  state jsonb default '{}'::jsonb not null,
  state_version integer default 0 not null,
  risk_flags text[] default '{}'::text[] not null,
  shadow_mode boolean default false not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists demand_daily (
  business_id uuid not null,
  day date not null,
  signal_kind text not null,
  dimension_kind text not null,
  dimension_key text not null,
  request_count integer default 0 not null,
  conversation_count integer default 0 not null,
  evidence_conversation_ids uuid[] default '{}'::uuid[] not null
);

create table if not exists faqs (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  question text not null,
  answer text not null,
  locale text default 'ro'::text not null,
  embedding vector(1536),
  is_active boolean default true not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  embedding_model text default 'text-embedding-3-small'::text not null
);

create table if not exists gdpr_requests (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid,
  kind text not null,
  status text default 'pending'::text not null,
  requested_by text,
  result_ref text,
  created_at timestamp with time zone default now() not null,
  completed_at timestamp with time zone
);

create table if not exists golden_tests (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  input text not null,
  expected jsonb not null,
  source text default 'manual'::text not null,
  is_active boolean default true not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists inbound_dedupe (
  business_id uuid not null,
  provider_msg_id text not null,
  first_seen timestamp with time zone default now() not null,
  claimed_at timestamp with time zone default now() not null,
  completed_at timestamp with time zone
);

create table if not exists ingredients (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  name text not null,
  slug text not null
);

create table if not exists intent_aliases (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  phrase_norm text not null,
  target_kind text not null,
  target_id uuid,
  target_value text,
  approved_by uuid,
  source text default 'manual'::text not null,
  status text default 'candidate'::text not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists message_status_events (
  id bigint generated always as identity not null,
  business_id uuid not null,
  provider_msg_id text not null,
  status text not null,
  payload jsonb default '{}'::jsonb not null,
  occurred_at timestamp with time zone default now() not null
);

create table if not exists messages (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  contact_id uuid not null,
  direction text not null,
  author text default 'contact'::text not null,
  provider_msg_id text,
  reply_to_provider_msg_id text,
  content_type text default 'text'::text not null,
  body text,
  payload jsonb default '{}'::jsonb not null,
  media_ref text,
  template_id uuid,
  status text default 'received'::text not null,
  error text,
  model_route text,
  tokens_in integer,
  tokens_out integer,
  cost_usd numeric(10,6),
  latency_ms integer,
  created_at timestamp with time zone default now() not null,
  latency_s numeric(6,2) generated always as (round(((latency_ms)::numeric / 1000.0), 2)) stored
)
partition by RANGE (created_at);

create table if not exists order_items (
  id uuid default gen_random_uuid() not null,
  order_id uuid not null,
  product_id uuid,
  variant_id uuid,
  name text not null,
  sku text,
  quantity integer default 1 not null,
  unit_price numeric(12,2) not null
);

create table if not exists orders (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid,
  external_id text not null,
  status text not null,
  total numeric(12,2) not null,
  currency text default 'RON'::text not null,
  attributed_checkout_link_id uuid,
  attribution text default 'none'::text not null,
  payload jsonb default '{}'::jsonb not null,
  placed_at timestamp with time zone not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  external_customer_ref text
);

create table if not exists outbox (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  idempotency_key text not null,
  kind text default 'message'::text not null,
  payload jsonb not null,
  status text default 'pending'::text not null,
  attempts integer default 0 not null,
  next_attempt_at timestamp with time zone default now() not null,
  last_error text,
  sent_message_id uuid,
  created_at timestamp with time zone default now() not null,
  priority smallint default 50 not null
);

create table if not exists proactive_jobs (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  contact_id uuid not null,
  conversation_id uuid,
  kind text not null,
  scheduled_at timestamp with time zone not null,
  status text default 'scheduled'::text not null,
  payload jsonb default '{}'::jsonb not null,
  template_id uuid,
  executed_at timestamp with time zone,
  created_at timestamp with time zone default now() not null,
  dedupe_key text
);

create table if not exists product_badges (
  id uuid default gen_random_uuid() not null,
  product_id uuid not null,
  label text not null
);

create table if not exists product_card_blurbs (
  business_id uuid not null,
  product_id uuid not null,
  locale text not null,
  document_version integer not null,
  schema_version integer not null,
  text text not null,
  content_hash text not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists product_category_map (
  product_id uuid not null,
  category_id uuid not null,
  position integer default 0 not null
);

create table if not exists product_derived_signals (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  signal text not null,
  derived_from text[] not null,
  rule_id text not null,
  locale text not null,
  schema_version integer default 1 not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists product_embeddings (
  product_id uuid not null,
  business_id uuid not null,
  model text not null,
  embedding vector(1536) not null,
  content_hash text not null,
  updated_at timestamp with time zone default now() not null,
  doc_type text default 'product'::text not null
);

create table if not exists product_evidence_chunks (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  role text not null,
  text text not null,
  source text not null,
  locale text not null,
  schema_version integer default 1 not null,
  content_hash text not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists product_faqs (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  locale text default 'ro'::text not null,
  question text not null,
  answer text not null,
  position integer default 0 not null,
  source text default 'derived'::text not null,
  derived boolean default true not null,
  embedding vector(1536),
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists product_images (
  id uuid default gen_random_uuid() not null,
  product_id uuid not null,
  url text not null,
  alt text,
  position integer default 0 not null,
  created_at timestamp with time zone default now() not null,
  kind text default 'main'::text not null
);

create table if not exists product_ingredients (
  product_id uuid not null,
  ingredient_id uuid not null,
  position integer default 0 not null,
  is_key boolean default false not null
);

create table if not exists product_relations (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  related_id uuid not null,
  kind text not null,
  position integer default 0 not null,
  created_at timestamp with time zone default now() not null
);

create table if not exists product_review_summaries (
  product_id uuid not null,
  business_id uuid not null,
  summary text not null,
  sentiment numeric(3,2),
  top_pros text[] default '{}'::text[] not null,
  top_cons text[] default '{}'::text[] not null,
  review_count_at_build integer not null,
  built_at timestamp with time zone default now() not null
);

create table if not exists product_search_documents (
  business_id uuid not null,
  product_id uuid not null,
  locale text not null,
  document_version integer not null,
  schema_version integer not null,
  positive_search_document text not null,
  fts_document tsvector not null,
  content_hash text not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists product_sections (
  id uuid default gen_random_uuid() not null,
  product_id uuid not null,
  kind text not null,
  title text not null,
  body text not null,
  position integer default 0 not null,
  business_id uuid not null,
  locale text default 'ro'::text not null,
  voice text default 'brand'::text not null
);

create table if not exists product_variants (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  label text not null,
  sku text not null,
  external_id text,
  price numeric(12,2) not null,
  sale_price numeric(12,2),
  stock integer default 0 not null,
  color_hex text,
  attributes jsonb default '{}'::jsonb not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  gtin text,
  net_content_value numeric,
  net_content_unit text,
  image_url text,
  price_per_unit numeric generated always as (
CASE
    WHEN ((net_content_value IS NULL) OR (net_content_value <= (0)::numeric)) THEN NULL::numeric
    WHEN (net_content_unit = ANY (ARRAY['ml'::text, 'l'::text])) THEN round(((COALESCE(sale_price, price) / (net_content_value * (
    CASE net_content_unit
        WHEN 'l'::text THEN 1000
        ELSE 1
    END)::numeric)) * (100)::numeric), 2)
    WHEN (net_content_unit = ANY (ARRAY['g'::text, 'kg'::text])) THEN round(((COALESCE(sale_price, price) / (net_content_value * (
    CASE net_content_unit
        WHEN 'kg'::text THEN 1000
        ELSE 1
    END)::numeric)) * (100)::numeric), 2)
    ELSE NULL::numeric
END) stored
);

create table if not exists products (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  brand_id uuid,
  primary_category_id uuid,
  external_id text,
  source_fingerprint text,
  name text not null,
  slug text not null,
  short_description text,
  description text,
  ai_summary text,
  currency text default 'RON'::text not null,
  price numeric(12,2) not null,
  sale_price numeric(12,2),
  availability text default 'in_stock'::text not null,
  stock_total integer,
  rating numeric(3,2) default 0 not null,
  review_count integer default 0 not null,
  status text default 'active'::text not null,
  attributes jsonb default '{}'::jsonb not null,
  seo jsonb default '{}'::jsonb not null,
  product_url text,
  synced_at timestamp with time zone,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  content_status text default 'draft'::text,
  schema_version integer,
  verified_at timestamp with time zone,
  sale_start date,
  sale_end date,
  restock_date date,
  delivery_class text,
  search_tsv tsvector generated always as (to_tsvector('simple'::regconfig, ro_unaccent(((COALESCE(name, ''::text) || ' '::text) || COALESCE(ai_summary, ''::text))))) stored
);

create table if not exists release_policies (
  id bigint generated always as identity not null,
  environment text not null,
  revision integer not null,
  policy_id text not null,
  policy jsonb not null,
  actor text not null,
  reason text not null,
  change_ticket text,
  applied_at timestamp with time zone default now() not null
);

create table if not exists reviews (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  product_id uuid not null,
  source text default 'platform'::text not null,
  external_id text,
  author text,
  rating integer not null,
  body text,
  created_at timestamp with time zone default now() not null
);

create table if not exists schema_migrations (
  version text not null,
  filename text not null,
  checksum text not null,
  applied_at timestamp with time zone default now() not null
);

create table if not exists semantic_cache (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  locale text default 'ro'::text not null,
  query_norm text not null,
  embedding vector(1536) not null,
  answer text not null,
  hit_count integer default 0 not null,
  last_hit_at timestamp with time zone,
  expires_at timestamp with time zone not null,
  created_at timestamp with time zone default now() not null,
  canonical_hash text,
  volatility_class text default 'static'::text not null,
  embedding_model text default 'text-embedding-3-small'::text not null,
  quality_score real,
  is_curated boolean default false not null,
  retrieval_signature jsonb,
  data_version integer,
  prompt_version text default 'v1'::text not null
);

create table if not exists shipments (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  order_id uuid not null,
  carrier text,
  awb text,
  status text default 'created'::text not null,
  eta timestamp with time zone,
  events jsonb default '[]'::jsonb not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists source_products_raw (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  source_site text not null,
  source_url text not null,
  scraped_at timestamp with time zone default now() not null,
  payload jsonb not null
);

create table if not exists usage_daily (
  business_id uuid not null,
  day date not null,
  conversations integer default 0 not null,
  messages_in integer default 0 not null,
  messages_out integer default 0 not null,
  templates_sent integer default 0 not null,
  tokens_in bigint default 0 not null,
  tokens_out bigint default 0 not null,
  cost_usd numeric(12,4) default 0 not null,
  cache_hits integer default 0 not null,
  handoffs integer default 0 not null,
  orders_attributed integer default 0 not null,
  revenue_attributed numeric(14,2) default 0 not null,
  intents jsonb default '{}'::jsonb not null,
  cached_tokens bigint default 0 not null,
  orders_direct_bot integer default 0 not null,
  revenue_direct_bot numeric(14,2) default 0 not null,
  orders_assisted integer default 0 not null,
  revenue_assisted numeric(14,2) default 0 not null
);

create table if not exists wa_templates (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  channel_id uuid not null,
  name text not null,
  language text default 'ro'::text not null,
  category text default 'utility'::text not null,
  version integer default 1 not null,
  body text not null,
  variables jsonb default '[]'::jsonb not null,
  status text default 'draft'::text not null,
  provider_template_id text,
  rejected_reason text,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists web_feedback (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  turn_id uuid not null,
  feedback_prompt_id text not null,
  rating text not null,
  reason_code text,
  taxonomy_version text default 'feedback.v1'::text not null,
  source text default 'web_widget'::text not null,
  schema_version text not null,
  release_sha text,
  release_track text default 'unknown'::text not null,
  pipeline_version text,
  last_action_id text not null,
  revision integer default 1 not null,
  created_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null
);

create table if not exists web_turns (
  id uuid default gen_random_uuid() not null,
  business_id uuid not null,
  conversation_id uuid not null,
  contact_id uuid not null,
  session_ref_hash text,
  client_turn_id uuid not null,
  request_fingerprint text not null,
  schema_version text default 'web-turn.v2'::text not null,
  status text default 'accepted'::text not null,
  attempt integer default 0 not null,
  lease_owner text,
  lease_epoch integer default 0 not null,
  lease_expires_at timestamp with time zone,
  deadline_at timestamp with time zone,
  conversation_revision_at_accept integer,
  pipeline_version text,
  response_json jsonb,
  safe_error_code text,
  accepted_at timestamp with time zone default now() not null,
  updated_at timestamp with time zone default now() not null,
  completed_at timestamp with time zone,
  release_track text,
  release_policy_id text,
  release_policy_revision integer
);


-- ==========================================================================
-- FUNCȚII care depind de tabele
-- ==========================================================================

CREATE OR REPLACE FUNCTION public.in_24h_window(conv conversations)
 RETURNS boolean
 LANGUAGE sql
 STABLE
AS $function$
  select conv.last_inbound_at is not null
     and conv.last_inbound_at > now() - interval '24 hours';
$function$;


-- ==========================================================================
-- CONSTRÂNGERI
-- ==========================================================================

-- PK / UNIQUE / CHECK
alter table analytics_events add constraint analytics_events_pkey PRIMARY KEY (id, created_at);
alter table appointments add constraint appointments_status_check CHECK ((status = ANY (ARRAY['booked'::text, 'confirmed'::text, 'cancelled'::text, 'no_show'::text, 'done'::text])));
alter table appointments add constraint appointments_pkey PRIMARY KEY (id);
alter table audit_log add constraint audit_log_pkey PRIMARY KEY (id);
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_pkey PRIMARY KEY (id);
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_business_id_contact_id_product__key UNIQUE (business_id, contact_id, product_id, variant_id);
alter table brands add constraint brands_pkey PRIMARY KEY (id);
alter table brands add constraint brands_business_id_slug_key UNIQUE (business_id, slug);
alter table business_users add constraint business_users_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'agent'::text, 'member'::text])));
alter table business_users add constraint business_users_pkey PRIMARY KEY (business_id, user_id);
alter table businesses add constraint businesses_status_check CHECK ((status = ANY (ARRAY['onboarding'::text, 'shadow'::text, 'active'::text, 'paused'::text, 'churned'::text])));
alter table businesses add constraint businesses_vertical_check CHECK ((vertical = ANY (ARRAY['ecommerce'::text, 'beauty_salon'::text, 'auto_service'::text, 'other'::text])));
alter table businesses add constraint businesses_pkey PRIMARY KEY (id);
alter table businesses add constraint businesses_slug_key UNIQUE (slug);
alter table catalog_quality_alerts add constraint catalog_quality_alerts_pkey PRIMARY KEY (id);
alter table catalog_sync_runs add constraint catalog_sync_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text, 'partial'::text])));
alter table catalog_sync_runs add constraint catalog_sync_runs_pkey PRIMARY KEY (id);
alter table categories add constraint categories_pkey PRIMARY KEY (id);
alter table categories add constraint categories_business_id_slug_key UNIQUE (business_id, slug);
alter table channel_identities add constraint channel_identities_channel_kind_check CHECK ((channel_kind = ANY (ARRAY['whatsapp'::text, 'telegram'::text, 'instagram'::text, 'webchat'::text])));
alter table channel_identities add constraint channel_identities_pkey PRIMARY KEY (id);
alter table channel_identities add constraint channel_identities_business_id_channel_kind_external_id_key UNIQUE (business_id, channel_kind, external_id);
alter table channels add constraint channels_kind_check CHECK ((kind = ANY (ARRAY['whatsapp'::text, 'telegram'::text, 'instagram'::text, 'webchat'::text])));
alter table channels add constraint channels_status_check CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text])));
alter table channels add constraint channels_pkey PRIMARY KEY (id);
alter table channels add constraint channels_kind_provider_account_id_key UNIQUE (kind, provider_account_id);
alter table checkout_links add constraint checkout_links_pkey PRIMARY KEY (id);
alter table checkout_links add constraint checkout_links_business_id_ref_code_key UNIQUE (business_id, ref_code);
alter table commerce_action_receipts add constraint commerce_action_receipts_after_version_check CHECK ((after_version >= 0));
alter table commerce_action_receipts add constraint commerce_action_receipts_before_version_check CHECK ((before_version >= 0));
alter table commerce_action_receipts add constraint commerce_action_receipts_operation_check CHECK ((operation = ANY (ARRAY['add'::text, 'set_quantity'::text, 'remove'::text, 'clear'::text, 'checkout'::text])));
alter table commerce_action_receipts add constraint commerce_action_receipts_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'succeeded'::text, 'failed'::text, 'unknown_reconcile'::text])));
alter table commerce_action_receipts add constraint commerce_receipts_succeeded_ck CHECK (((status <> 'succeeded'::text) OR (after_version IS NOT NULL)));
alter table commerce_action_receipts add constraint commerce_action_receipts_pkey PRIMARY KEY (id);
alter table commerce_action_receipts add constraint uq_commerce_receipts_key UNIQUE (business_id, idempotency_key);
alter table contacts add constraint contacts_lifecycle_check CHECK ((lifecycle = ANY (ARRAY['new'::text, 'engaged'::text, 'customer'::text, 'repeat'::text, 'churn_risk'::text])));
alter table contacts add constraint contacts_pkey PRIMARY KEY (id);
alter table conversation_cart_items add constraint conversation_cart_items_quantity_check CHECK (((quantity >= 1) AND (quantity <= 10)));
alter table conversation_cart_items add constraint conversation_cart_items_pkey PRIMARY KEY (id);
alter table conversation_cart_items add constraint uq_cart_items_line UNIQUE NULLS NOT DISTINCT (business_id, cart_id, product_id, variant_id);
alter table conversation_carts add constraint conversation_carts_status_check CHECK ((status = ANY (ARRAY['active'::text, 'checked_out'::text, 'expired'::text])));
alter table conversation_carts add constraint conversation_carts_version_check CHECK ((version >= 0));
alter table conversation_carts add constraint conversation_carts_pkey PRIMARY KEY (id);
alter table conversation_evals add constraint conversation_evals_pkey PRIMARY KEY (id);
alter table conversation_facts add constraint conversation_facts_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)));
alter table conversation_facts add constraint conversation_facts_safety_class_ck CHECK ((safety_class = ANY (ARRAY['safe'::text, 'pii'::text, 'health'::text, 'financial'::text, 'sensitive'::text, 'unknown'::text])));
alter table conversation_facts add constraint conversation_facts_visibility_ck CHECK ((visibility = ANY (ARRAY['inject'::text, 'candidate'::text, 'drop'::text])));
alter table conversation_facts add constraint conversation_facts_pkey PRIMARY KEY (id);
alter table conversation_facts add constraint conversation_facts_biz_contact_memkey_key UNIQUE (business_id, contact_id, memory_key);
alter table conversation_summaries add constraint conversation_summaries_pkey PRIMARY KEY (id);
alter table conversation_traces add constraint conversation_traces_pkey PRIMARY KEY (id);
alter table conversation_traces add constraint conversation_traces_business_id_turn_id_key UNIQUE (business_id, turn_id);
alter table conversations add constraint chk_state_size CHECK ((pg_column_size(state) < 8192));
alter table conversations add constraint conversations_status_check CHECK ((status = ANY (ARRAY['open'::text, 'snoozed'::text, 'closed'::text])));
alter table conversations add constraint conversations_pkey PRIMARY KEY (id);
alter table demand_daily add constraint demand_daily_conversation_count_check CHECK ((conversation_count >= 0));
alter table demand_daily add constraint demand_daily_dimension_kind_ck CHECK ((dimension_kind = ANY (ARRAY['brand'::text, 'category'::text, 'product'::text, 'variant_attr'::text, 'clarify_field'::text, 'none'::text])));
alter table demand_daily add constraint demand_daily_request_count_check CHECK ((request_count >= 0));
alter table demand_daily add constraint demand_daily_signal_kind_ck CHECK ((signal_kind = ANY (ARRAY['unmet_no_result'::text, 'unmet_named_not_found'::text, 'unmet_out_of_stock'::text, 'unmet_missing_variant'::text, 'unmet_price_gap'::text, 'requested_brand'::text, 'requested_category'::text, 'recommended_product'::text, 'cart_product'::text, 'checkout_product'::text, 'faq_miss'::text, 'clarify_asked'::text])));
alter table demand_daily add constraint demand_daily_pkey PRIMARY KEY (business_id, day, signal_kind, dimension_kind, dimension_key);
alter table faqs add constraint faqs_pkey PRIMARY KEY (id);
alter table gdpr_requests add constraint gdpr_requests_kind_check CHECK ((kind = ANY (ARRAY['erase'::text, 'export'::text, 'access'::text])));
alter table gdpr_requests add constraint gdpr_requests_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'failed'::text])));
alter table gdpr_requests add constraint gdpr_requests_pkey PRIMARY KEY (id);
alter table golden_tests add constraint golden_tests_source_check CHECK ((source = ANY (ARRAY['manual'::text, 'shadow_correction'::text])));
alter table golden_tests add constraint golden_tests_pkey PRIMARY KEY (id);
alter table inbound_dedupe add constraint inbound_dedupe_pkey PRIMARY KEY (business_id, provider_msg_id);
alter table ingredients add constraint ingredients_pkey PRIMARY KEY (id);
alter table ingredients add constraint ingredients_business_id_slug_key UNIQUE (business_id, slug);
alter table intent_aliases add constraint intent_aliases_source_check CHECK ((source = ANY (ARRAY['manual'::text, 'shadow'::text, 'post_turn'::text])));
alter table intent_aliases add constraint intent_aliases_status_check CHECK ((status = ANY (ARRAY['candidate'::text, 'approved'::text, 'rejected'::text])));
alter table intent_aliases add constraint intent_aliases_target_kind_check CHECK ((target_kind = ANY (ARRAY['faq'::text, 'product'::text, 'category'::text, 'route'::text])));
alter table intent_aliases add constraint intent_aliases_pkey PRIMARY KEY (id);
alter table intent_aliases add constraint intent_aliases_business_id_phrase_norm_target_kind_key UNIQUE (business_id, phrase_norm, target_kind);
alter table message_status_events add constraint message_status_events_pkey PRIMARY KEY (id);
alter table messages add constraint messages_author_check CHECK ((author = ANY (ARRAY['contact'::text, 'bot'::text, 'human_agent'::text, 'system'::text])));
alter table messages add constraint messages_content_type_check CHECK ((content_type = ANY (ARRAY['text'::text, 'image'::text, 'audio'::text, 'video'::text, 'document'::text, 'interactive'::text, 'template'::text, 'location'::text, 'sticker'::text, 'action'::text])));
alter table messages add constraint messages_direction_check CHECK ((direction = ANY (ARRAY['inbound'::text, 'outbound'::text, 'internal'::text])));
alter table messages add constraint messages_status_check CHECK ((status = ANY (ARRAY['received'::text, 'queued'::text, 'sent'::text, 'delivered'::text, 'read'::text, 'failed'::text])));
alter table messages add constraint messages_pkey PRIMARY KEY (id, created_at);
alter table order_items add constraint order_items_pkey PRIMARY KEY (id);
alter table orders add constraint orders_attribution_check CHECK ((attribution = ANY (ARRAY['none'::text, 'assisted'::text, 'direct_bot'::text])));
alter table orders add constraint orders_pkey PRIMARY KEY (id);
alter table orders add constraint orders_business_id_external_id_key UNIQUE (business_id, external_id);
alter table outbox add constraint outbox_kind_check CHECK ((kind = ANY (ARRAY['message'::text, 'template'::text, 'typing'::text, 'reaction'::text])));
alter table outbox add constraint outbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'dispatching'::text, 'sent'::text, 'failed'::text, 'dead'::text])));
alter table outbox add constraint outbox_pkey PRIMARY KEY (id);
alter table outbox add constraint outbox_business_id_idempotency_key_key UNIQUE (business_id, idempotency_key);
alter table proactive_jobs add constraint proactive_jobs_kind_check CHECK ((kind = ANY (ARRAY['awb_update'::text, 'back_in_stock'::text, 'abandoned_cart'::text, 'follow_up'::text, 'custom'::text])));
alter table proactive_jobs add constraint proactive_jobs_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'sent'::text, 'skipped_no_window'::text, 'skipped_no_optin'::text, 'cancelled'::text, 'failed'::text])));
alter table proactive_jobs add constraint proactive_jobs_pkey PRIMARY KEY (id);
alter table product_badges add constraint product_badges_pkey PRIMARY KEY (id);
alter table product_card_blurbs add constraint product_card_blurbs_content_hash_check CHECK ((length(btrim(content_hash)) > 0));
alter table product_card_blurbs add constraint product_card_blurbs_document_version_check CHECK ((document_version >= 1));
alter table product_card_blurbs add constraint product_card_blurbs_locale_check CHECK ((length(btrim(locale)) >= 2));
alter table product_card_blurbs add constraint product_card_blurbs_schema_version_check CHECK ((schema_version >= 1));
alter table product_card_blurbs add constraint product_card_blurbs_text_check CHECK ((length(btrim(text)) > 0));
alter table product_card_blurbs add constraint product_card_blurbs_pkey PRIMARY KEY (business_id, product_id, locale, document_version);
alter table product_category_map add constraint product_category_map_pkey PRIMARY KEY (product_id, category_id);
alter table product_derived_signals add constraint product_derived_signals_derived_from_check CHECK ((cardinality(derived_from) >= 1));
alter table product_derived_signals add constraint product_derived_signals_derived_from_check1 CHECK ((array_position(derived_from, NULL::text) IS NULL));
alter table product_derived_signals add constraint product_derived_signals_derived_from_check2 CHECK ((array_to_string(derived_from, ''::text) !~ '(^|)[[:space:]]*(|$)'::text));
alter table product_derived_signals add constraint product_derived_signals_rule_id_check CHECK ((length(btrim(rule_id)) > 0));
alter table product_derived_signals add constraint product_derived_signals_schema_version_check CHECK ((schema_version >= 1));
alter table product_derived_signals add constraint product_derived_signals_signal_check CHECK ((length(btrim(signal)) > 0));
alter table product_derived_signals add constraint product_derived_signals_pkey PRIMARY KEY (id);
alter table product_derived_signals add constraint product_derived_signals_business_id_product_id_signal_rule__key UNIQUE (business_id, product_id, signal, rule_id, locale);
alter table product_embeddings add constraint product_embeddings_pkey PRIMARY KEY (product_id, doc_type, model);
alter table product_evidence_chunks add constraint product_evidence_chunks_role_check CHECK ((role = ANY (ARRAY['benefit'::text, 'usage'::text, 'warning'::text, 'ingredient'::text, 'faq'::text, 'review_summary'::text, 'policy'::text])));
alter table product_evidence_chunks add constraint product_evidence_chunks_schema_version_check CHECK ((schema_version >= 1));
alter table product_evidence_chunks add constraint product_evidence_chunks_source_check CHECK ((length(btrim(source)) > 0));
alter table product_evidence_chunks add constraint product_evidence_chunks_text_check CHECK ((length(btrim(text)) > 0));
alter table product_evidence_chunks add constraint product_evidence_chunks_pkey PRIMARY KEY (id);
alter table product_evidence_chunks add constraint product_evidence_chunks_business_id_product_id_role_locale__key UNIQUE (business_id, product_id, role, locale, content_hash);
alter table product_faqs add constraint product_faqs_position_check CHECK (("position" >= 0));
alter table product_faqs add constraint product_faqs_source_check CHECK ((source = ANY (ARRAY['derived'::text, 'curated'::text, 'brand'::text])));
alter table product_faqs add constraint product_faqs_pkey PRIMARY KEY (id);
alter table product_faqs add constraint product_faqs_business_id_product_id_locale_question_key UNIQUE (business_id, product_id, locale, question);
alter table product_images add constraint product_images_kind_chk CHECK ((kind = ANY (ARRAY['main'::text, 'texture'::text, 'application'::text, 'before_after'::text, 'ingredient'::text, 'packaging'::text, 'other'::text])));
alter table product_images add constraint product_images_pkey PRIMARY KEY (id);
alter table product_ingredients add constraint product_ingredients_pkey PRIMARY KEY (product_id, ingredient_id);
alter table product_relations add constraint product_relations_check CHECK ((product_id <> related_id));
alter table product_relations add constraint product_relations_kind_check CHECK ((kind = ANY (ARRAY['substitute'::text, 'complement'::text, 'accessory'::text, 'routine_next'::text])));
alter table product_relations add constraint product_relations_position_check CHECK (("position" >= 0));
alter table product_relations add constraint product_relations_pkey PRIMARY KEY (id);
alter table product_relations add constraint product_relations_business_id_product_id_related_id_kind_key UNIQUE (business_id, product_id, related_id, kind);
alter table product_review_summaries add constraint product_review_summaries_pkey PRIMARY KEY (product_id);
alter table product_search_documents add constraint product_search_documents_content_hash_check CHECK ((length(btrim(content_hash)) > 0));
alter table product_search_documents add constraint product_search_documents_document_version_check CHECK ((document_version >= 1));
alter table product_search_documents add constraint product_search_documents_locale_check CHECK ((length(btrim(locale)) >= 2));
alter table product_search_documents add constraint product_search_documents_positive_search_document_check CHECK ((length(btrim(positive_search_document)) > 0));
alter table product_search_documents add constraint product_search_documents_schema_version_check CHECK ((schema_version >= 1));
alter table product_search_documents add constraint product_search_documents_pkey PRIMARY KEY (business_id, product_id, locale, document_version);
alter table product_sections add constraint product_sections_voice_chk CHECK ((voice = ANY (ARRAY['brand'::text, 'assistant'::text])));
alter table product_sections add constraint product_sections_pkey PRIMARY KEY (id);
alter table product_variants add constraint product_variants_net_content_unit_chk CHECK (((net_content_unit IS NULL) OR (net_content_unit = ANY (ARRAY['ml'::text, 'l'::text, 'g'::text, 'kg'::text, 'buc'::text]))));
alter table product_variants add constraint product_variants_pkey PRIMARY KEY (id);
alter table product_variants add constraint product_variants_business_id_sku_key UNIQUE (business_id, sku);
alter table products add constraint products_availability_check CHECK ((availability = ANY (ARRAY['in_stock'::text, 'low_stock'::text, 'out_of_stock'::text, 'preorder'::text, 'discontinued'::text])));
alter table products add constraint products_content_status_chk CHECK (((content_status IS NULL) OR (content_status = ANY (ARRAY['draft'::text, 'reviewed'::text, 'published'::text, 'rejected'::text]))));
alter table products add constraint products_delivery_class_chk CHECK (((delivery_class IS NULL) OR (delivery_class = ANY (ARRAY['next_day'::text, 'standard'::text, 'supplier'::text, 'preorder'::text]))));
alter table products add constraint products_sale_window_chk CHECK (((sale_start IS NULL) OR (sale_end IS NULL) OR (sale_end >= sale_start)));
alter table products add constraint products_status_check CHECK ((status = ANY (ARRAY['active'::text, 'draft'::text, 'archived'::text])));
alter table products add constraint products_pkey PRIMARY KEY (id);
alter table products add constraint products_business_id_external_id_key UNIQUE (business_id, external_id);
alter table products add constraint products_business_id_id_key UNIQUE (business_id, id);
alter table products add constraint products_business_id_slug_key UNIQUE (business_id, slug);
alter table release_policies add constraint release_policies_actor_check CHECK ((length(TRIM(BOTH FROM actor)) > 0));
alter table release_policies add constraint release_policies_reason_check CHECK ((length(TRIM(BOTH FROM reason)) > 0));
alter table release_policies add constraint release_policies_revision_check CHECK ((revision >= 0));
alter table release_policies add constraint release_policies_pkey PRIMARY KEY (id);
alter table release_policies add constraint uq_release_policies_revision UNIQUE (environment, revision);
alter table reviews add constraint reviews_rating_check CHECK (((rating >= 1) AND (rating <= 5)));
alter table reviews add constraint reviews_pkey PRIMARY KEY (id);
alter table reviews add constraint reviews_business_id_source_external_id_key UNIQUE (business_id, source, external_id);
alter table schema_migrations add constraint schema_migrations_pkey PRIMARY KEY (version);
alter table semantic_cache add constraint chk_semcache_volatility CHECK ((volatility_class = ANY (ARRAY['static'::text, 'semi_dynamic'::text, 'dynamic'::text, 'realtime'::text])));
alter table semantic_cache add constraint semantic_cache_pkey PRIMARY KEY (id);
alter table shipments add constraint shipments_pkey PRIMARY KEY (id);
alter table shipments add constraint shipments_business_id_awb_key UNIQUE (business_id, awb);
alter table source_products_raw add constraint source_products_raw_pkey PRIMARY KEY (id);
alter table source_products_raw add constraint source_products_raw_business_id_source_url_key UNIQUE (business_id, source_url);
alter table usage_daily add constraint usage_daily_pkey PRIMARY KEY (business_id, day);
alter table wa_templates add constraint wa_templates_category_check CHECK ((category = ANY (ARRAY['utility'::text, 'marketing'::text, 'authentication'::text])));
alter table wa_templates add constraint wa_templates_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'submitted'::text, 'approved'::text, 'rejected'::text, 'paused'::text, 'deprecated'::text])));
alter table wa_templates add constraint wa_templates_pkey PRIMARY KEY (id);
alter table wa_templates add constraint wa_templates_business_id_channel_id_name_language_version_key UNIQUE (business_id, channel_id, name, language, version);
alter table web_feedback add constraint web_feedback_rating_check CHECK ((rating = ANY (ARRAY['positive'::text, 'negative'::text])));
alter table web_feedback add constraint web_feedback_revision_check CHECK ((revision >= 1));
alter table web_feedback add constraint web_feedback_source_check CHECK ((source = 'web_widget'::text));
alter table web_feedback add constraint web_feedback_pkey PRIMARY KEY (id);
alter table web_turns add constraint web_turns_attempt_check CHECK ((attempt >= 0));
alter table web_turns add constraint web_turns_lease_epoch_check CHECK ((lease_epoch >= 0));
alter table web_turns add constraint web_turns_release_revision_ck CHECK (((release_policy_revision IS NULL) OR (release_policy_revision >= 0)));
alter table web_turns add constraint web_turns_release_track_ck CHECK (((release_track IS NULL) OR (release_track = ANY (ARRAY['champion'::text, 'candidate'::text]))));
alter table web_turns add constraint web_turns_running_lease_ck CHECK (((status <> 'running'::text) OR ((lease_owner IS NOT NULL) AND (lease_expires_at IS NOT NULL))));
alter table web_turns add constraint web_turns_status_check CHECK ((status = ANY (ARRAY['accepted'::text, 'running'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])));
alter table web_turns add constraint web_turns_terminal_completed_at_ck CHECK (((status <> ALL (ARRAY['completed'::text, 'failed'::text, 'cancelled'::text])) OR (completed_at IS NOT NULL)));
alter table web_turns add constraint web_turns_terminal_response_ck CHECK (((status <> ALL (ARRAY['completed'::text, 'failed'::text, 'cancelled'::text])) OR (response_json IS NOT NULL)));
alter table web_turns add constraint web_turns_pkey PRIMARY KEY (id);
alter table web_turns add constraint uq_web_turns_client_turn UNIQUE (business_id, conversation_id, client_turn_id);

-- CHEI STRĂINE
alter table appointments add constraint appointments_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table appointments add constraint appointments_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table appointments add constraint appointments_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table back_in_stock_subscriptions add constraint back_in_stock_subscriptions_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(id) ON DELETE CASCADE;
alter table brands add constraint brands_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table business_users add constraint business_users_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table catalog_quality_alerts add constraint catalog_quality_alerts_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table catalog_quality_alerts add constraint catalog_quality_alerts_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table catalog_quality_alerts add constraint catalog_quality_alerts_sync_run_id_fkey FOREIGN KEY (sync_run_id) REFERENCES catalog_sync_runs(id) ON DELETE SET NULL;
alter table catalog_sync_runs add constraint catalog_sync_runs_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table categories add constraint categories_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table categories add constraint categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL;
alter table channel_identities add constraint channel_identities_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table channel_identities add constraint channel_identities_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table channels add constraint channels_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table checkout_links add constraint checkout_links_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table checkout_links add constraint checkout_links_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table checkout_links add constraint checkout_links_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table commerce_action_receipts add constraint commerce_action_receipts_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table commerce_action_receipts add constraint commerce_action_receipts_cart_id_fkey FOREIGN KEY (cart_id) REFERENCES conversation_carts(id) ON DELETE SET NULL;
alter table commerce_action_receipts add constraint commerce_action_receipts_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table contacts add constraint contacts_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversation_cart_items add constraint conversation_cart_items_cart_id_fkey FOREIGN KEY (cart_id) REFERENCES conversation_carts(id) ON DELETE CASCADE;
alter table conversation_cart_items add constraint fk_cart_items_product FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table conversation_carts add constraint conversation_carts_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversation_carts add constraint conversation_carts_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table conversation_evals add constraint conversation_evals_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversation_summaries add constraint conversation_summaries_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversation_summaries add constraint conversation_summaries_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table conversation_traces add constraint conversation_traces_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversation_traces add constraint conversation_traces_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table conversation_traces add constraint conversation_traces_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table conversations add constraint conversations_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table conversations add constraint conversations_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE RESTRICT;
alter table conversations add constraint conversations_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table demand_daily add constraint demand_daily_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table faqs add constraint faqs_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table gdpr_requests add constraint gdpr_requests_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table gdpr_requests add constraint gdpr_requests_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL;
alter table golden_tests add constraint golden_tests_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table inbound_dedupe add constraint inbound_dedupe_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table ingredients add constraint ingredients_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table intent_aliases add constraint intent_aliases_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table order_items add constraint order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE;
alter table order_items add constraint order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL;
alter table order_items add constraint order_items_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(id) ON DELETE SET NULL;
alter table orders add constraint orders_attributed_checkout_link_id_fkey FOREIGN KEY (attributed_checkout_link_id) REFERENCES checkout_links(id) ON DELETE SET NULL;
alter table orders add constraint orders_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table orders add constraint orders_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL;
alter table outbox add constraint outbox_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table outbox add constraint outbox_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table proactive_jobs add constraint proactive_jobs_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table proactive_jobs add constraint proactive_jobs_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table proactive_jobs add constraint proactive_jobs_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
alter table proactive_jobs add constraint proactive_jobs_template_id_fkey FOREIGN KEY (template_id) REFERENCES wa_templates(id) ON DELETE SET NULL;
alter table product_badges add constraint product_badges_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_card_blurbs add constraint product_card_blurbs_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_card_blurbs add constraint product_card_blurbs_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_category_map add constraint product_category_map_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE;
alter table product_category_map add constraint product_category_map_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_derived_signals add constraint product_derived_signals_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_derived_signals add constraint product_derived_signals_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_embeddings add constraint product_embeddings_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_evidence_chunks add constraint product_evidence_chunks_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_evidence_chunks add constraint product_evidence_chunks_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_faqs add constraint product_faqs_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_faqs add constraint product_faqs_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_images add constraint product_images_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_ingredients add constraint product_ingredients_ingredient_id_fkey FOREIGN KEY (ingredient_id) REFERENCES ingredients(id) ON DELETE CASCADE;
alter table product_ingredients add constraint product_ingredients_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_relations add constraint product_relations_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_relations add constraint product_relations_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_relations add constraint product_relations_business_id_related_id_fkey FOREIGN KEY (business_id, related_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_review_summaries add constraint product_review_summaries_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_search_documents add constraint product_search_documents_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_search_documents add constraint product_search_documents_business_id_product_id_fkey FOREIGN KEY (business_id, product_id) REFERENCES products(business_id, id) ON DELETE CASCADE;
alter table product_sections add constraint product_sections_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table product_variants add constraint product_variants_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table product_variants add constraint product_variants_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table products add constraint products_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE SET NULL;
alter table products add constraint products_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table products add constraint products_primary_category_id_fkey FOREIGN KEY (primary_category_id) REFERENCES categories(id) ON DELETE SET NULL;
alter table reviews add constraint reviews_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table reviews add constraint reviews_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE;
alter table semantic_cache add constraint semantic_cache_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table shipments add constraint shipments_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table shipments add constraint shipments_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE;
alter table source_products_raw add constraint source_products_raw_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table usage_daily add constraint usage_daily_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table wa_templates add constraint wa_templates_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table wa_templates add constraint wa_templates_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE;
alter table web_feedback add constraint web_feedback_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table web_feedback add constraint web_feedback_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;
alter table web_turns add constraint web_turns_business_id_fkey FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
alter table web_turns add constraint web_turns_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE;
alter table web_turns add constraint web_turns_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE;


-- ==========================================================================
-- INDEXURI
-- ==========================================================================

create index if not exists analytics_events_biz_conv_created_idx ON ONLY public.analytics_events USING btree (business_id, conversation_id, created_at);
create index if not exists analytics_events_biz_turn_idx ON ONLY public.analytics_events USING btree (business_id, turn_id) WHERE (turn_id IS NOT NULL);
create index if not exists conversation_facts_biz_contact_idx ON public.conversation_facts USING btree (business_id, contact_id);
create index if not exists conversation_facts_inject_idx ON public.conversation_facts USING btree (business_id, contact_id) WHERE (visibility = 'inject'::text);
create index if not exists idx_aliases_lookup ON public.intent_aliases USING btree (business_id, phrase_norm) WHERE (status = 'approved'::text);
create index if not exists idx_appointments_business_time ON public.appointments USING btree (business_id, starts_at);
create index if not exists idx_audit_business ON public.audit_log USING btree (business_id, created_at DESC);
create index if not exists idx_badges_product ON public.product_badges USING btree (product_id);
create index if not exists idx_business_users_user ON public.business_users USING btree (user_id);
create index if not exists idx_cart_items_cart ON public.conversation_cart_items USING btree (business_id, cart_id);
create index if not exists idx_categories_business_parent ON public.categories USING btree (business_id, parent_id);
create index if not exists idx_channel_identities_contact ON public.channel_identities USING btree (contact_id);
create index if not exists idx_channel_identities_hash ON public.channel_identities USING btree (business_id, external_id_hash);
create index if not exists idx_channels_business ON public.channels USING btree (business_id);
create index if not exists idx_checkout_links_conv ON public.checkout_links USING btree (conversation_id);
create index if not exists idx_commerce_receipts_conv ON public.commerce_action_receipts USING btree (business_id, conversation_id, created_at DESC);
create index if not exists idx_commerce_receipts_pending ON public.commerce_action_receipts USING btree (business_id, created_at) WHERE (status = ANY (ARRAY['pending'::text, 'unknown_reconcile'::text]));
create index if not exists idx_contacts_business ON public.contacts USING btree (business_id);
create index if not exists idx_contacts_lead ON public.contacts USING btree (business_id, lead_score DESC);
create index if not exists idx_conv_summaries ON public.conversation_summaries USING btree (conversation_id, upto_message_at DESC);
create index if not exists idx_conversation_carts_conv ON public.conversation_carts USING btree (business_id, conversation_id, updated_at DESC);
create index if not exists idx_conversation_traces_contact ON public.conversation_traces USING btree (business_id, contact_id);
create index if not exists idx_conversation_traces_conv ON public.conversation_traces USING btree (business_id, conversation_id, created_at);
create index if not exists idx_conversation_traces_created ON public.conversation_traces USING btree (created_at);
create index if not exists idx_conversations_business_open ON public.conversations USING btree (business_id, last_message_at DESC) WHERE (status = 'open'::text);
create index if not exists idx_conversations_contact ON public.conversations USING btree (contact_id);
create index if not exists idx_demand_daily_signal ON public.demand_daily USING btree (business_id, signal_kind, day DESC);
create index if not exists idx_evals_business ON public.conversation_evals USING btree (business_id, evaluated_at DESC);
create index if not exists idx_events_business_type ON ONLY public.analytics_events USING btree (business_id, event_type, created_at DESC);
create index if not exists idx_faqs_emb ON public.faqs USING hnsw (embedding vector_cosine_ops);
create index if not exists idx_inbound_dedupe_first_seen ON public.inbound_dedupe USING btree (first_seen);
create index if not exists idx_inbound_dedupe_orphan ON public.inbound_dedupe USING btree (claimed_at) WHERE (completed_at IS NULL);
create index if not exists idx_messages_business_time ON ONLY public.messages USING btree (business_id, created_at DESC);
create index if not exists idx_messages_conv ON ONLY public.messages USING btree (conversation_id, created_at DESC);
create index if not exists idx_msg_status_provider ON public.message_status_events USING btree (business_id, provider_msg_id);
create index if not exists idx_order_items_order ON public.order_items USING btree (order_id);
create index if not exists idx_orders_attributed ON public.orders USING btree (business_id, placed_at DESC) WHERE (attribution <> 'none'::text);
create index if not exists idx_orders_contact ON public.orders USING btree (contact_id);
create index if not exists idx_orders_customer_ref ON public.orders USING btree (business_id, external_customer_ref) WHERE (external_customer_ref IS NOT NULL);
create index if not exists idx_outbox_due ON public.outbox USING btree (next_attempt_at) WHERE (status = ANY (ARRAY['pending'::text, 'failed'::text]));
create index if not exists idx_outbox_due_priority ON public.outbox USING btree (business_id, priority, next_attempt_at, id) WHERE (status = ANY (ARRAY['pending'::text, 'failed'::text, 'dispatching'::text]));
create index if not exists idx_pcm_category ON public.product_category_map USING btree (category_id);
create index if not exists idx_proactive_due ON public.proactive_jobs USING btree (scheduled_at) WHERE (status = 'scheduled'::text);
create index if not exists idx_prod_ingr_ingredient ON public.product_ingredients USING btree (ingredient_id);
create index if not exists idx_product_emb_business ON public.product_embeddings USING btree (business_id);
create index if not exists idx_product_emb_hnsw ON public.product_embeddings USING hnsw (embedding vector_cosine_ops);
create index if not exists idx_product_faqs_lookup ON public.product_faqs USING btree (business_id, product_id, locale, "position");
create index if not exists idx_product_images_product ON public.product_images USING btree (product_id);
create index if not exists idx_products_attrs_gin ON public.products USING gin (attributes);
create index if not exists idx_products_business_cat ON public.products USING btree (business_id, primary_category_id);
create index if not exists idx_products_business_status ON public.products USING btree (business_id, status);
create index if not exists idx_products_name_ro_trgm ON public.products USING gin (ro_unaccent(name) gin_trgm_ops);
create index if not exists idx_products_name_trgm ON public.products USING gin (name gin_trgm_ops);
create index if not exists idx_products_search_tsv ON public.products USING gin (search_tsv);
create index if not exists idx_quality_alerts_open ON public.catalog_quality_alerts USING btree (business_id, created_at DESC) WHERE (resolved_at IS NULL);
create index if not exists idx_release_policies_current ON public.release_policies USING btree (environment, revision DESC);
create index if not exists idx_reviews_product ON public.reviews USING btree (product_id);
create index if not exists idx_sections_business_product ON public.product_sections USING btree (business_id, product_id, locale);
create index if not exists idx_sections_product ON public.product_sections USING btree (product_id);
create index if not exists idx_semcache_emb ON public.semantic_cache USING hnsw (embedding vector_cosine_ops);
create unique index if not exists idx_semcache_exact ON public.semantic_cache USING btree (business_id, locale, canonical_hash, prompt_version);
create index if not exists idx_semcache_expiry ON public.semantic_cache USING btree (expires_at);
create index if not exists idx_source_raw_business ON public.source_products_raw USING btree (business_id);
create index if not exists idx_sync_runs_business ON public.catalog_sync_runs USING btree (business_id, started_at DESC);
create index if not exists idx_variants_product ON public.product_variants USING btree (product_id);
create index if not exists idx_web_feedback_turn ON public.web_feedback USING btree (business_id, turn_id);
create index if not exists idx_web_feedback_window ON public.web_feedback USING btree (business_id, created_at DESC);
create index if not exists idx_web_turns_conversation ON public.web_turns USING btree (business_id, conversation_id, accepted_at DESC);
create index if not exists idx_web_turns_release_cohort ON public.web_turns USING btree (business_id, release_track, accepted_at DESC);
create index if not exists idx_web_turns_retention ON public.web_turns USING btree (completed_at) WHERE (status = ANY (ARRAY['completed'::text, 'failed'::text, 'cancelled'::text]));
create index if not exists idx_web_turns_stale ON public.web_turns USING btree (accepted_at) WHERE (status = ANY (ARRAY['accepted'::text, 'running'::text]));
create index if not exists product_derived_signals_product_idx ON public.product_derived_signals USING btree (business_id, product_id, locale);
create index if not exists product_derived_signals_rule_idx ON public.product_derived_signals USING btree (business_id, rule_id);
create index if not exists product_embeddings_lookup_idx ON public.product_embeddings USING btree (business_id, product_id, doc_type, model);
create index if not exists product_evidence_chunks_product_idx ON public.product_evidence_chunks USING btree (business_id, product_id, locale, role);
create index if not exists product_relations_anchor_idx ON public.product_relations USING btree (business_id, product_id, kind, "position");
create index if not exists product_search_documents_fts_idx ON public.product_search_documents USING gin (fts_document);
create index if not exists product_search_documents_shadow_lookup_idx ON public.product_search_documents USING btree (business_id, locale, document_version, content_hash);
create index if not exists products_published_idx ON public.products USING btree (business_id) WHERE (content_status = 'published'::text);
create index if not exists products_sale_window_idx ON public.products USING btree (business_id, sale_end) WHERE (sale_price IS NOT NULL);
create unique index if not exists uq_conversation_carts_active ON public.conversation_carts USING btree (business_id, conversation_id) WHERE (status = 'active'::text);
create unique index if not exists uq_conversations_one_open ON public.conversations USING btree (business_id, contact_id, channel_id) WHERE (status = 'open'::text);
create unique index if not exists uq_messages_provider ON ONLY public.messages USING btree (business_id, provider_msg_id, created_at) WHERE (provider_msg_id IS NOT NULL);
create unique index if not exists uq_proactive_jobs_dedupe ON public.proactive_jobs USING btree (business_id, dedupe_key) WHERE (dedupe_key IS NOT NULL);
create unique index if not exists uq_web_feedback_prompt ON public.web_feedback USING btree (business_id, feedback_prompt_id);
create unique index if not exists uq_web_turns_one_active ON public.web_turns USING btree (business_id, conversation_id) WHERE (status = ANY (ARRAY['accepted'::text, 'running'::text]));

-- ==========================================================================
-- TRIGGERE
-- ==========================================================================

CREATE TRIGGER trg_brands_upd BEFORE UPDATE ON public.brands FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_businesses_upd BEFORE UPDATE ON public.businesses FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_categories_upd BEFORE UPDATE ON public.categories FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_commerce_receipts_upd BEFORE UPDATE ON public.commerce_action_receipts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_contacts_upd BEFORE UPDATE ON public.contacts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_cart_items_upd BEFORE UPDATE ON public.conversation_cart_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_conversation_carts_upd BEFORE UPDATE ON public.conversation_carts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_conversations_upd BEFORE UPDATE ON public.conversations FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_faqs_upd BEFORE UPDATE ON public.faqs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_orders_upd BEFORE UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_product_faqs_upd BEFORE UPDATE ON public.product_faqs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_variants_upd BEFORE UPDATE ON public.product_variants FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_products_upd BEFORE UPDATE ON public.products FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_wa_templates_upd BEFORE UPDATE ON public.wa_templates FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ==========================================================================
-- RLS + POLITICI
-- ==========================================================================

alter table analytics_events enable row level security;
alter table appointments enable row level security;
alter table audit_log enable row level security;
alter table back_in_stock_subscriptions enable row level security;
alter table brands enable row level security;
alter table business_users enable row level security;
alter table businesses enable row level security;
alter table catalog_quality_alerts enable row level security;
alter table catalog_sync_runs enable row level security;
alter table categories enable row level security;
alter table channel_identities enable row level security;
alter table channels enable row level security;
alter table checkout_links enable row level security;
alter table commerce_action_receipts enable row level security;
alter table contacts enable row level security;
alter table conversation_cart_items enable row level security;
alter table conversation_carts enable row level security;
alter table conversation_evals enable row level security;
alter table conversation_facts enable row level security;
alter table conversation_summaries enable row level security;
alter table conversation_traces enable row level security;
alter table conversations enable row level security;
alter table demand_daily enable row level security;
alter table faqs enable row level security;
alter table gdpr_requests enable row level security;
alter table golden_tests enable row level security;
alter table inbound_dedupe enable row level security;
alter table ingredients enable row level security;
alter table intent_aliases enable row level security;
alter table message_status_events enable row level security;
alter table messages enable row level security;
alter table order_items enable row level security;
alter table orders enable row level security;
alter table outbox enable row level security;
alter table proactive_jobs enable row level security;
alter table product_badges enable row level security;
alter table product_card_blurbs enable row level security;
alter table product_category_map enable row level security;
alter table product_derived_signals enable row level security;
alter table product_embeddings enable row level security;
alter table product_evidence_chunks enable row level security;
alter table product_faqs enable row level security;
alter table product_images enable row level security;
alter table product_ingredients enable row level security;
alter table product_relations enable row level security;
alter table product_review_summaries enable row level security;
alter table product_search_documents enable row level security;
alter table product_sections enable row level security;
alter table product_variants enable row level security;
alter table products enable row level security;
alter table release_policies enable row level security;
alter table reviews enable row level security;
alter table schema_migrations enable row level security;
alter table semantic_cache enable row level security;
alter table shipments enable row level security;
alter table source_products_raw enable row level security;
alter table usage_daily enable row level security;
alter table wa_templates enable row level security;
alter table web_feedback enable row level security;
alter table web_turns enable row level security;

create policy bot_runtime_analytics on analytics_events
  as permissive for insert to bot_runtime
  with check ((business_id = current_business_id()));
create policy "member read" on analytics_events
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on appointments
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on back_in_stock_subscriptions
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on brands
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on businesses
  as permissive for all to bot_runtime
  using ((id = current_business_id()))
  with check ((id = current_business_id()));
create policy "member read" on businesses
  as permissive for select to public
  using ((id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on catalog_quality_alerts
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on catalog_sync_runs
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on categories
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "public read in-use categories" on categories
  as permissive for select to anon, authenticated
  using (category_has_active_products(id));
create policy bot_runtime_tenant on channel_identities
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on channels
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on checkout_links
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on commerce_action_receipts
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on commerce_action_receipts
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on contacts
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on contacts
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on conversation_cart_items
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on conversation_cart_items
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on conversation_carts
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on conversation_carts
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on conversation_facts
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on conversation_summaries
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on conversation_traces
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on conversation_traces
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on conversations
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on conversations
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy "member read" on demand_daily
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy "admin write faqs" on faqs
  as permissive for all to authenticated
  using ((business_id IN ( SELECT bu.business_id
   FROM business_users bu
  WHERE ((bu.user_id = auth.uid()) AND (bu.role = ANY (ARRAY['owner'::text, 'admin'::text]))))));
create policy bot_runtime_tenant on faqs
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on faqs
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on inbound_dedupe
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_child_read on ingredients
  as permissive for select to bot_runtime
  using (true);
create policy "admin write aliases" on intent_aliases
  as permissive for all to authenticated
  using ((business_id IN ( SELECT bu.business_id
   FROM business_users bu
  WHERE ((bu.user_id = auth.uid()) AND (bu.role = ANY (ARRAY['owner'::text, 'admin'::text]))))));
create policy bot_runtime_tenant on intent_aliases
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on message_status_events
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on messages
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on messages
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_child_read on order_items
  as permissive for select to bot_runtime
  using (true);
create policy bot_runtime_order_items_insert on order_items
  as permissive for insert to bot_runtime
  with check ((EXISTS ( SELECT 1
   FROM orders o
  WHERE ((o.id = order_items.order_id) AND (o.business_id = current_business_id())))));
create policy bot_runtime_tenant on orders
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on orders
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on outbox
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on proactive_jobs
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_child_read on product_badges
  as permissive for select to bot_runtime
  using (true);
create policy bot_runtime_tenant on product_card_blurbs
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_child_read on product_category_map
  as permissive for select to bot_runtime
  using (true);
create policy bot_runtime_tenant on product_derived_signals
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_tenant on product_embeddings
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on product_evidence_chunks
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_tenant on product_faqs
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_child_read on product_images
  as permissive for select to bot_runtime
  using (true);
create policy "public read product images" on product_images
  as permissive for select to anon, authenticated
  using (true);
create policy bot_runtime_child_read on product_ingredients
  as permissive for select to bot_runtime
  using (true);
create policy bot_runtime_tenant on product_relations
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_tenant on product_review_summaries
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on product_search_documents
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()));
create policy bot_runtime_child_read on product_sections
  as permissive for select to bot_runtime
  using (true);
create policy bot_runtime_tenant on product_variants
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on products
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on products
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy "public read active products" on products
  as permissive for select to anon, authenticated
  using ((status = 'active'::text));
create policy bot_runtime_tenant on reviews
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on semantic_cache
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on shipments
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy bot_runtime_tenant on usage_daily
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on usage_daily
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on wa_templates
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on wa_templates
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on web_feedback
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on web_feedback
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));
create policy bot_runtime_tenant on web_turns
  as permissive for all to bot_runtime
  using ((business_id = current_business_id()))
  with check ((business_id = current_business_id()));
create policy "member read" on web_turns
  as permissive for select to public
  using ((business_id IN ( SELECT my_business_ids() AS my_business_ids)));

-- ==========================================================================
-- GRANTURI (rolurile noastre)
-- ==========================================================================

grant insert on analytics_events to bot_runtime;
grant delete, insert, select, update on appointments to bot_runtime;
grant delete, insert, select, update on back_in_stock_subscriptions to bot_runtime;
grant select on brands to bot_runtime;
grant select on businesses to bot_runtime;
grant select on catalog_quality_alerts to bot_runtime;
grant select on catalog_sync_runs to bot_runtime;
grant select on categories to bot_runtime;
grant delete, insert, select, update on channel_identities to bot_runtime;
grant select on channels to bot_runtime;
grant delete, insert, select, update on checkout_links to bot_runtime;
grant insert, select, update on commerce_action_receipts to bot_runtime;
grant delete, insert, select, update on contacts to bot_runtime;
grant delete, insert, select, update on conversation_cart_items to bot_runtime;
grant insert, select, update on conversation_carts to bot_runtime;
grant insert, select, update on conversation_facts to bot_runtime;
grant delete, insert, select, update on conversation_summaries to bot_runtime;
grant insert, select on conversation_traces to bot_runtime;
grant delete, insert, select, update on conversations to bot_runtime;
grant select on faqs to bot_runtime;
grant delete, insert, select, update on inbound_dedupe to bot_runtime;
grant select on ingredients to bot_runtime;
grant insert, select, update on intent_aliases to bot_runtime;
grant delete, insert, select, update on message_status_events to bot_runtime;
grant delete, insert, select, update on messages to bot_runtime;
grant insert, select on order_items to bot_runtime;
grant delete, insert, select, update on orders to bot_runtime;
grant delete, insert, select, update on outbox to bot_runtime;
grant delete, insert, select, update on proactive_jobs to bot_runtime;
grant select on product_badges to bot_runtime;
grant select on product_card_blurbs to bot_runtime;
grant select on product_category_map to bot_runtime;
grant select on product_derived_signals to bot_runtime;
grant select on product_embeddings to bot_runtime;
grant select on product_evidence_chunks to bot_runtime;
grant select on product_faqs to bot_runtime;
grant select on product_images to bot_runtime;
grant select on product_ingredients to bot_runtime;
grant select on product_relations to bot_runtime;
grant select on product_review_summaries to bot_runtime;
grant select on product_search_documents to bot_runtime;
grant select on product_sections to bot_runtime;
grant select on product_variants to bot_runtime;
grant select on products to bot_runtime;
grant select on reviews to bot_runtime;
grant delete, insert, select, update on semantic_cache to bot_runtime;
grant delete, insert, select, update on shipments to bot_runtime;
grant insert, select, update on usage_daily to bot_runtime;
grant select on wa_templates to bot_runtime;
grant insert, select, update on web_feedback to bot_runtime;
grant insert, select, update on web_turns to bot_runtime;

-- ==========================================================================
-- COMENTARII
-- ==========================================================================

comment on column outbox.priority is 'Dispatcher priority: lower is more urgent. user replies=10, transactional=20, default=50, marketing/proactive=80.';
comment on column products.sale_end is 'Ultima zi INCLUSIV în care sale_price se aplică. Read-path-ul TREBUIE să verifice fereastra: un sale_price expirat afișat ca preț curent e o minciună comercială.';
comment on column products.restock_date is 'Când revine în stoc; se completează la availability=out_of_stock. Botul îl poate rosti.';
comment on column products.delivery_class is 'next_day (comandă până la ora-limită → mâine) | standard | supplier | preorder. Promisiunea concretă se CALCULEAZĂ determinist din asta + config-ul magazinului.';

-- ==========================================================================
-- PARTITII
-- ==========================================================================

-- Partitiile lunare NU se emit: sunt date de calendar.
-- Ruleaza `python -m src.jobs.partition_maintenance` dupa aplicarea schemei.
