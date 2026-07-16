-- ============================================================================
-- SALES ASSISTANT PLATFORM — SCHEMA DE PRODUCȚIE v2 (Postgres 16+ / Supabase)
-- Multi-tenant, conversational commerce, vector search, attribution, GDPR.
-- Aliniat la arhitectura: Canale&Edge → Redis backbone → Conversation Engine
-- → AI Platform → Data Platform → Integrări → Proactiv → Admin/Billing.
--
-- Convenții:
--  * TOATE tabelele tenant-scoped au business_id NOT NULL + index compus.
--  * Idempotență: unique pe (business_id, external/provider id).
--  * Hot tables (messages, events) sunt PARTIȚIONATE pe lună.
--  * RLS: service_role (workerii) bypass; dashboard-ul intră prin membership.
-- ============================================================================

create extension if not exists pgcrypto;
create extension if not exists vector;        -- pgvector
create extension if not exists pg_trgm;       -- fuzzy search pe nume produse

-- ----------------------------------------------------------------------------
-- 0. UTIL: updated_at trigger
-- ----------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end; $$ language plpgsql;

-- ============================================================================
-- 1. CORE — tenants, utilizatori dashboard, config
-- ============================================================================

create table businesses (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  slug          text not null unique,
  vertical      text not null default 'ecommerce'
                check (vertical in ('ecommerce','beauty_salon','auto_service','other')),
  status        text not null default 'active'
                check (status in ('onboarding','shadow','active','paused','churned')),
  default_locale text not null default 'ro',
  supported_locales text[] not null default '{ro}',
  timezone      text not null default 'Europe/Bucharest',
  -- config operațional (cost guard, plafoane, feature flags)
  settings      jsonb not null default '{}'::jsonb,
  daily_cost_cap_usd numeric(10,4),           -- cost guard: la plafon → handoff
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create trigger trg_businesses_upd before update on businesses
  for each row execute function set_updated_at();

-- membri dashboard (auth.users din Supabase)
create table business_users (
  business_id uuid not null references businesses(id) on delete cascade,
  user_id     uuid not null,                  -- auth.users.id
  role        text not null default 'member'
              check (role in ('owner','admin','agent','member')),
  created_at  timestamptz not null default now(),
  primary key (business_id, user_id)
);
create index idx_business_users_user on business_users(user_id);

-- canalele conectate per business (WhatsApp number, Telegram bot etc.)
create table channels (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  kind          text not null check (kind in ('whatsapp','telegram','instagram','webchat')),
  -- ex: phone_number_id la Meta, bot id la Telegram
  provider_account_id text not null,
  display_name  text,
  status        text not null default 'active' check (status in ('active','disabled')),
  credentials_ref text,                       -- referință în secret manager, NU secrete în DB
  settings      jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  unique (kind, provider_account_id)
);
create index idx_channels_business on channels(business_id);

-- ============================================================================
-- 2. CONTACTS & IDENTITY RESOLUTION
--    „același client pe WhatsApp+Telegram → un singur contact + profil"
-- ============================================================================

create table contacts (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  display_name  text,
  locale        text,                          -- detectat: ro/hu/en
  -- profil auto-învățat de extractorul nano (tip ten, mărimi, preferințe...)
  profile       jsonb not null default '{}'::jsonb,
  lead_score    numeric(5,2) not null default 0,
  lifecycle     text not null default 'new'
                check (lifecycle in ('new','engaged','customer','repeat','churn_risk')),
  rfm           jsonb,                         -- {recency, frequency, monetary} din warehouse
  consent       jsonb not null default '{}'::jsonb,  -- opt-in proactiv, marketing
  is_blocked    boolean not null default false,      -- abuse blocklist
  erased_at     timestamptz,                   -- GDPR: anonimizat, nu șters fizic
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index idx_contacts_business on contacts(business_id);
create index idx_contacts_lead on contacts(business_id, lead_score desc);
create trigger trg_contacts_upd before update on contacts
  for each row execute function set_updated_at();

-- identitățile pe canale: aici stă PII-ul (telefon E.164, telegram user id)
create table channel_identities (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  contact_id    uuid not null references contacts(id) on delete cascade,
  channel_kind  text not null check (channel_kind in ('whatsapp','telegram','instagram','webchat')),
  external_id   text not null,                 -- wa phone E.164 / tg user id
  external_id_hash text generated always as (encode(digest(external_id,'sha256'),'hex')) stored,
  verified      boolean not null default false,
  created_at    timestamptz not null default now(),
  unique (business_id, channel_kind, external_id)
);
create index idx_channel_identities_contact on channel_identities(contact_id);
create index idx_channel_identities_hash on channel_identities(business_id, external_id_hash);

-- ============================================================================
-- 3. CONVERSAȚII & MESAJE (hot path)
-- ============================================================================

create table conversations (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  contact_id    uuid not null references contacts(id) on delete cascade,
  channel_id    uuid not null references channels(id) on delete restrict,
  status        text not null default 'open'
                check (status in ('open','snoozed','closed')),
  bot_active    boolean not null default true,
  handoff_until timestamptz,                   -- gate: omul a preluat până la...
  assigned_user_id uuid,                       -- agentul uman din inbox
  -- 24h window tracker: fereastra Meta = last_inbound_at + 24h
  last_inbound_at  timestamptz,
  last_outbound_at timestamptz,
  last_message_at  timestamptz,
  locale        text,
  -- state compact al agentului (≤8KB, impus de context builder)
  state         jsonb not null default '{}'::jsonb,
  state_version integer not null default 0,    -- optimistic locking pt patch state
  risk_flags    text[] not null default '{}',
  shadow_mode   boolean not null default false, -- propune → om aprobă
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index idx_conversations_business_open
  on conversations(business_id, last_message_at desc) where status = 'open';
create index idx_conversations_contact on conversations(contact_id);
create trigger trg_conversations_upd before update on conversations
  for each row execute function set_updated_at();

-- helper pentru workers/proactiv: suntem în fereastra liberă de 24h?
create or replace function in_24h_window(conv conversations) returns boolean
language sql stable as $$
  select conv.last_inbound_at is not null
     and conv.last_inbound_at > now() - interval '24 hours';
$$;

-- sumarele conversațiilor lungi (summarizer din context builder)
create table conversation_summaries (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  upto_message_at timestamptz not null,        -- sumarizează tot până la acest punct
  summary         text not null,
  created_at      timestamptz not null default now()
);
create index idx_conv_summaries on conversation_summaries(conversation_id, upto_message_at desc);

-- MESSAGES — partiționat pe lună (retenție + performanță).
-- NB: pe tabele partiționate, unique-urile includ cheia de partiționare.
create table messages (
  id              uuid not null default gen_random_uuid(),
  business_id     uuid not null,
  conversation_id uuid not null,
  contact_id      uuid not null,
  direction       text not null check (direction in ('inbound','outbound','internal')),
  author          text not null default 'contact'
                  check (author in ('contact','bot','human_agent','system')),
  -- dedupe la webhook: insert ... on conflict do nothing
  provider_msg_id text,
  reply_to_provider_msg_id text,
  content_type    text not null default 'text'
                  check (content_type in ('text','image','audio','video','document',
                                          'interactive','template','location','sticker')),
  body            text,                        -- text sau transcriptul STT
  payload         jsonb not null default '{}'::jsonb, -- butoane, liste, carduri, raw provider
  media_ref       text,                        -- cheie în object storage (TTL acolo)
  template_id     uuid,                        -- dacă a fost template Meta
  status          text not null default 'received'
                  check (status in ('received','queued','sent','delivered','read','failed')),
  error           text,
  -- observabilitate per tur
  model_route     text,                        -- nano/mini/cache/faq/template
  tokens_in       integer,
  tokens_out      integer,
  cost_usd        numeric(10,6),
  latency_ms      integer,
  created_at      timestamptz not null default now(),
  primary key (id, created_at)
) partition by range (created_at);

create unique index uq_messages_provider
  on messages(business_id, provider_msg_id, created_at)
  where provider_msg_id is not null;
create index idx_messages_conv on messages(conversation_id, created_at desc);
create index idx_messages_business_time on messages(business_id, created_at desc);

-- partițiile: creează-le cu pg_partman sau pg_cron; exemplu manual:
create table messages_2026_06 partition of messages
  for values from ('2026-06-01') to ('2026-07-01');
create table messages_2026_07 partition of messages
  for values from ('2026-07-01') to ('2026-08-01');
create table messages_default partition of messages default;

-- evenimentele de status de la provider (delivered/read/failed → events)
create table message_status_events (
  id              bigint generated always as identity primary key,
  business_id     uuid not null,
  provider_msg_id text not null,
  status          text not null,
  payload         jsonb not null default '{}'::jsonb,
  occurred_at     timestamptz not null default now()
);
create index idx_msg_status_provider on message_status_events(business_id, provider_msg_id);

-- OUTBOX — singurul punct de ieșire; dispatcher idempotent cu retry.
-- Răspunsul se scrie tranzacțional cu state-ul; dispatcherul citește de aici.
create table outbox (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  idempotency_key text not null,
  kind            text not null default 'message'
                  check (kind in ('message','template','typing','reaction')),
  payload         jsonb not null,
  priority        smallint not null default 50, -- lower = more urgent (user=10, transactional=20, marketing=80)
  status          text not null default 'pending'
                  check (status in ('pending','dispatching','sent','failed','dead')),
  attempts        integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error      text,
  sent_message_id uuid,
  created_at      timestamptz not null default now(),
  unique (business_id, idempotency_key)
);
create index idx_outbox_due on outbox(next_attempt_at)
  where status in ('pending','failed');
create index idx_outbox_due_priority on outbox(business_id, priority, next_attempt_at, id)
  where status in ('pending','failed','dispatching');

-- TEMPLATE MANAGER — ciclul de viață al template-urilor Meta
create table wa_templates (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  channel_id    uuid not null references channels(id) on delete cascade,
  name          text not null,
  language      text not null default 'ro',
  category      text not null default 'utility'
                check (category in ('utility','marketing','authentication')),
  version       integer not null default 1,
  body          text not null,
  variables     jsonb not null default '[]'::jsonb,
  status        text not null default 'draft'
                check (status in ('draft','submitted','approved','rejected','paused','deprecated')),
  provider_template_id text,
  rejected_reason text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (business_id, channel_id, name, language, version)
);
create trigger trg_wa_templates_upd before update on wa_templates
  for each row execute function set_updated_at();

-- ============================================================================
-- 4. CATALOG (evoluția schemei tale actuale → tenant-scoped + AI-ready)
-- ============================================================================

create table brands (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  name        text not null,
  slug        text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (business_id, slug)
);
create trigger trg_brands_upd before update on brands
  for each row execute function set_updated_at();

create table categories (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  parent_id   uuid references categories(id) on delete set null,
  name        text not null,
  slug        text not null,
  path        text,                            -- 'machiaj/buze/rujuri' pt filtre rapide
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (business_id, slug)
);
create index idx_categories_business_parent on categories(business_id, parent_id);
create trigger trg_categories_upd before update on categories
  for each row execute function set_updated_at();

create table products (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  brand_id      uuid references brands(id) on delete set null,
  primary_category_id uuid references categories(id) on delete set null,
  -- id-ul din platforma magazinului (feed/API) → upsert idempotent la sync
  external_id   text,
  source_fingerprint text,                     -- păstrat din pipeline-ul tău
  name          text not null,
  slug          text not null,
  short_description text,
  description   text,
  -- sumar generat de LLM la ingestion, pe care se face embedding + ranking
  ai_summary    text,
  currency      text not null default 'RON',
  price         numeric(12,2) not null,
  sale_price    numeric(12,2),
  availability  text not null default 'in_stock'
                check (availability in ('in_stock','low_stock','out_of_stock','preorder','discontinued')),
  stock_total   integer,
  rating        numeric(3,2) not null default 0,
  review_count  integer not null default 0,
  status        text not null default 'active'
                check (status in ('active','draft','archived')),
  attributes    jsonb not null default '{}'::jsonb,
  seo           jsonb not null default '{}'::jsonb,
  product_url   text,                          -- linkul real din magazin (validatorul îl verifică)
  -- NX-171c: quality-gate la nivel de DB. DOAR 'published' e servit clientului (filtru read-path,
  -- flag per-tenant default off). Nullable → NOT NULL după backfill (job, nu SQL). Vezi migrarea 028.
  content_status text default 'draft'
                check (content_status is null or content_status in
                       ('draft','reviewed','published','rejected')),
  schema_version integer,                       -- versiunea contractului de conținut (v3 = 3)
  verified_at    timestamptz,                   -- când a fost promovat la 'published'
  synced_at     timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (business_id, slug),
  unique (business_id, external_id),
  unique (business_id, id)                       -- NX-171b: target de FK compus (product_relations)
);
create index idx_products_business_status on products(business_id, status);
create index idx_products_business_cat on products(business_id, primary_category_id);
create index idx_products_attrs_gin on products using gin(attributes);
create index idx_products_name_trgm on products using gin (name gin_trgm_ops);
create trigger trg_products_upd before update on products
  for each row execute function set_updated_at();

-- embeddings separate de products: re-embed fără lock pe tabelul fierbinte,
-- și poți ține mai multe modele/versiuni în paralel
-- NX-171d: PK COMPUS (product_id, doc_type, model) → versiuni paralele (produs × tip doc × model).
-- Read-path-ul filtrează doc_type + model activ (altfel join naiv pe product_id dublează rezultatele).
create table product_embeddings (
  product_id  uuid not null references products(id) on delete cascade,
  business_id uuid not null,
  model       text not null,
  doc_type    text not null default 'product', -- 'product' | 'review' | 'usage' | ...
  embedding   vector(1536) not null,
  content_hash text not null,                  -- re-embed doar dacă s-a schimbat ai_summary
  updated_at  timestamptz not null default now(),
  primary key (product_id, doc_type, model)
);
create index idx_product_emb_hnsw on product_embeddings
  using hnsw (embedding vector_cosine_ops);
create index idx_product_emb_business on product_embeddings(business_id);

create table product_category_map (
  product_id  uuid not null references products(id) on delete cascade,
  category_id uuid not null references categories(id) on delete cascade,
  position    integer not null default 0,
  primary key (product_id, category_id)
);
create index idx_pcm_category on product_category_map(category_id);

create table product_images (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid not null references products(id) on delete cascade,
  url         text not null,
  alt         text,
  position    integer not null default 0,
  created_at  timestamptz not null default now()
);
create index idx_product_images_product on product_images(product_id);

create table product_variants (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  product_id  uuid not null references products(id) on delete cascade,
  label       text not null,
  sku         text not null,
  external_id text,
  price       numeric(12,2) not null,
  sale_price  numeric(12,2),
  stock       integer not null default 0,
  color_hex   text,
  attributes  jsonb not null default '{}'::jsonb,
  -- NX-171a (migrare 026): coloane comerciale standard — sursa de adevăr comercială e VARIANTA.
  gtin              text,                 -- GS1, validat mod-10 la seed (invalid → NULL)
  net_content_value numeric,              -- cantitate netă (gramaj): 30, 250, ...
  net_content_unit  text,                 -- ml | l | g | kg | buc (CHECK; NULL permis)
  image_url         text,                 -- imagine proprie de variantă (nuanță)
  price_per_unit    numeric generated always as (  -- preț/100ml (volum) sau /100g (masă); buc→NULL
    case when net_content_value is null or net_content_value <= 0 then null
         when net_content_unit in ('ml','l') then
           round(coalesce(sale_price, price) / (net_content_value * (case net_content_unit when 'l' then 1000 else 1 end)) * 100, 2)
         when net_content_unit in ('g','kg') then
           round(coalesce(sale_price, price) / (net_content_value * (case net_content_unit when 'kg' then 1000 else 1 end)) * 100, 2)
         else null end) stored,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (business_id, sku),
  constraint product_variants_net_content_unit_chk
    check (net_content_unit is null or net_content_unit in ('ml','l','g','kg','buc'))
);
create index idx_variants_product on product_variants(product_id);
create trigger trg_variants_upd before update on product_variants
  for each row execute function set_updated_at();

-- NX-171b: relații explicite curate între produse (rutină/complement/substitut/accesoriu),
-- înlocuind heuristica same-brand/concern. Integritate de tenant DECLARATIVĂ prin FK COMPUS:
-- ambele capete pe același business_id → relația cross-tenant e structural imposibilă. Vezi migr. 027.
create table product_relations (
  id           uuid primary key default gen_random_uuid(),
  business_id  uuid not null references businesses(id) on delete cascade,
  product_id   uuid not null,
  related_id   uuid not null,
  kind         text not null
               check (kind in ('substitute','complement','accessory','routine_next')),
  position     integer not null default 0 check (position >= 0),
  created_at   timestamptz not null default now(),
  unique (business_id, product_id, related_id, kind),   -- fără relații duplicate
  check (product_id <> related_id),                     -- fără self-relation
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade,
  foreign key (business_id, related_id) references products (business_id, id) on delete cascade
);
create index product_relations_anchor_idx
  on product_relations (business_id, product_id, kind, position);

create table product_sections (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid not null references products(id) on delete cascade,
  kind        text not null,                   -- usage / benefits / warnings...
  title       text not null,
  body        text not null,
  position    integer not null default 0
);
create index idx_sections_product on product_sections(product_id);

-- ingrediente: FK corect către ingredients (fix față de v1)
create table ingredients (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  name        text not null,
  slug        text not null,
  unique (business_id, slug)
);

create table product_ingredients (
  product_id    uuid not null references products(id) on delete cascade,
  ingredient_id uuid not null references ingredients(id) on delete cascade,
  position      integer not null default 0,
  is_key        boolean not null default false,
  primary key (product_id, ingredient_id)
);
create index idx_prod_ingr_ingredient on product_ingredients(ingredient_id);

create table product_badges (
  id          uuid primary key default gen_random_uuid(),
  product_id  uuid not null references products(id) on delete cascade,
  label       text not null
);
create index idx_badges_product on product_badges(product_id);

create table reviews (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  product_id  uuid not null references products(id) on delete cascade,
  source      text not null default 'platform',
  external_id text,
  author      text,
  rating      integer not null check (rating between 1 and 5),
  body        text,
  created_at  timestamptz not null default now(),
  unique (business_id, source, external_id)
);
create index idx_reviews_product on reviews(product_id);

-- Review intelligence (job offline): sumar per produs, citit de get_product_details
create table product_review_summaries (
  product_id   uuid primary key references products(id) on delete cascade,
  business_id  uuid not null,
  summary      text not null,
  sentiment    numeric(3,2),                   -- -1..1
  top_pros     text[] not null default '{}',
  top_cons     text[] not null default '{}',
  review_count_at_build integer not null,
  built_at     timestamptz not null default now()
);

-- CATALOG INGESTION: sync runs + quality monitor („alertă, nu publicare")
create table catalog_sync_runs (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  source        text not null,                 -- feed url / api / manual
  status        text not null default 'running'
                check (status in ('running','succeeded','failed','partial')),
  stats         jsonb not null default '{}'::jsonb, -- inserted/updated/skipped/anomalies
  error         text,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz
);
create index idx_sync_runs_business on catalog_sync_runs(business_id, started_at desc);

create table catalog_quality_alerts (
  id           uuid primary key default gen_random_uuid(),
  business_id  uuid not null references businesses(id) on delete cascade,
  sync_run_id  uuid references catalog_sync_runs(id) on delete set null,
  product_id   uuid references products(id) on delete cascade,
  kind         text not null,                  -- price_anomaly / stock_frozen / sync_failed
  details      jsonb not null default '{}'::jsonb,
  resolved_at  timestamptz,
  created_at   timestamptz not null default now()
);
create index idx_quality_alerts_open
  on catalog_quality_alerts(business_id, created_at desc) where resolved_at is null;

-- ============================================================================
-- 5. KNOWLEDGE: FAQ, aliasuri, cache semantic (straturile gratuite 40-60%)
-- ============================================================================

create table faqs (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  question    text not null,
  answer      text not null,
  locale      text not null default 'ro',
  embedding   vector(1536),
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index idx_faqs_emb on faqs using hnsw (embedding vector_cosine_ops);
create trigger trg_faqs_upd before update on faqs
  for each row execute function set_updated_at();

-- aliasuri exacte (corecturile din shadow mode devin aliase)
create table intent_aliases (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  phrase_norm text not null,                   -- lowercased, fără diacritice
  target_kind text not null check (target_kind in ('faq','product','category','route')),
  target_id   uuid,
  target_value text,
  approved_by uuid,                            -- din coada de aprobare
  source      text not null default 'manual' check (source in ('manual','shadow','post_turn')),
  status      text not null default 'candidate' check (status in ('candidate','approved','rejected')),
  created_at  timestamptz not null default now(),
  unique (business_id, phrase_norm, target_kind)
);
create index idx_aliases_lookup on intent_aliases(business_id, phrase_norm)
  where status = 'approved';

-- cache semantic: întrebare → răspuns validat, cu TTL
create table semantic_cache (
  id           uuid primary key default gen_random_uuid(),
  business_id  uuid not null references businesses(id) on delete cascade,
  locale       text not null default 'ro',
  query_norm   text not null,
  embedding    vector(1536) not null,
  answer       text not null,
  hit_count    integer not null default 0,
  last_hit_at  timestamptz,
  expires_at   timestamptz not null,           -- invalidare la sync de catalog/prețuri
  created_at   timestamptz not null default now()
);
create index idx_semcache_emb on semantic_cache using hnsw (embedding vector_cosine_ops);
create index idx_semcache_expiry on semantic_cache(expires_at);

-- ============================================================================
-- 6. COMERȚ & ATRIBUIRE — bucla de bani
-- ============================================================================

-- checkout link cu ref de conversație → webhook comenzi → revenue atribuit
create table checkout_links (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  contact_id      uuid not null references contacts(id) on delete cascade,
  ref_code        text not null,               -- ce pui în URL (?ref=...)
  cart            jsonb not null default '[]'::jsonb,
  url             text not null,
  clicked_at      timestamptz,
  converted_order_id uuid,
  expires_at      timestamptz,
  created_at      timestamptz not null default now(),
  unique (business_id, ref_code)
);
create index idx_checkout_links_conv on checkout_links(conversation_id);

create table orders (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  contact_id    uuid references contacts(id) on delete set null,
  external_id   text not null,                 -- id-ul din platforma magazinului
  status        text not null,                 -- statusurile platformei, normalizate
  total         numeric(12,2) not null,
  currency      text not null default 'RON',
  -- atribuire: dacă a venit prin checkout link al botului
  attributed_checkout_link_id uuid references checkout_links(id) on delete set null,
  attribution   text not null default 'none'
                check (attribution in ('none','assisted','direct_bot')),
  payload       jsonb not null default '{}'::jsonb,
  placed_at     timestamptz not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (business_id, external_id)
);
create index idx_orders_contact on orders(contact_id);
create index idx_orders_attributed on orders(business_id, placed_at desc)
  where attribution <> 'none';
create trigger trg_orders_upd before update on orders
  for each row execute function set_updated_at();

create table order_items (
  id          uuid primary key default gen_random_uuid(),
  order_id    uuid not null references orders(id) on delete cascade,
  product_id  uuid references products(id) on delete set null,
  variant_id  uuid references product_variants(id) on delete set null,
  name        text not null,
  sku         text,
  quantity    integer not null default 1,
  unit_price  numeric(12,2) not null
);
create index idx_order_items_order on order_items(order_id);

-- evenimente AWB de la curier → motorul proactiv
create table shipments (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  order_id    uuid not null references orders(id) on delete cascade,
  carrier     text,
  awb         text,
  status      text not null default 'created',
  eta         timestamptz,
  events      jsonb not null default '[]'::jsonb,
  updated_at  timestamptz not null default now(),
  unique (business_id, awb)
);

create table back_in_stock_subscriptions (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  contact_id  uuid not null references contacts(id) on delete cascade,
  product_id  uuid not null references products(id) on delete cascade,
  variant_id  uuid references product_variants(id) on delete cascade,
  notified_at timestamptz,
  created_at  timestamptz not null default now(),
  unique (business_id, contact_id, product_id, variant_id)
);

-- coș abandonat: detectat din state-ul conversației → follow-up proactiv
create table proactive_jobs (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  contact_id    uuid not null references contacts(id) on delete cascade,
  conversation_id uuid references conversations(id) on delete set null,
  kind          text not null
                check (kind in ('awb_update','back_in_stock','abandoned_cart','follow_up','custom')),
  scheduled_at  timestamptz not null,
  status        text not null default 'scheduled'
                check (status in ('scheduled','sent','skipped_no_window','skipped_no_optin','cancelled','failed')),
  payload       jsonb not null default '{}'::jsonb,
  template_id   uuid references wa_templates(id) on delete set null,
  executed_at   timestamptz,
  created_at    timestamptz not null default now()
);
create index idx_proactive_due on proactive_jobs(scheduled_at) where status = 'scheduled';

-- programări (verticale servicii: salon, auto)
create table appointments (
  id            uuid primary key default gen_random_uuid(),
  business_id   uuid not null references businesses(id) on delete cascade,
  contact_id    uuid not null references contacts(id) on delete cascade,
  conversation_id uuid references conversations(id) on delete set null,
  service_name  text not null,
  starts_at     timestamptz not null,
  ends_at       timestamptz not null,
  status        text not null default 'booked'
                check (status in ('booked','confirmed','cancelled','no_show','done')),
  external_ref  text,                          -- event id în Google Calendar
  notes         text,
  created_at    timestamptz not null default now()
);
create index idx_appointments_business_time on appointments(business_id, starts_at);

-- ============================================================================
-- 7. ANALYTICS & USAGE (dashboard per client + billing per tenant)
-- ============================================================================

-- evenimente de produs (intents, rute, tool calls, handoffs, validări eșuate)
create table analytics_events (
  id              bigint generated always as identity,
  business_id     uuid not null,
  conversation_id uuid,
  contact_id      uuid,
  event_type      text not null,               -- intent_detected / route / tool_call /
                                               -- validator_retry / handoff / cache_hit ...
  properties      jsonb not null default '{}'::jsonb,
  tokens_in       integer,
  tokens_out      integer,
  cost_usd        numeric(10,6),
  created_at      timestamptz not null default now(),
  primary key (id, created_at)
) partition by range (created_at);

create index idx_events_business_type on analytics_events(business_id, event_type, created_at desc);

create table analytics_events_2026_06 partition of analytics_events
  for values from ('2026-06-01') to ('2026-07-01');
create table analytics_events_2026_07 partition of analytics_events
  for values from ('2026-07-01') to ('2026-08-01');
create table analytics_events_default partition of analytics_events default;

-- rollup zilnic per tenant: dashboardul citește DOAR de aici, nu din events
create table usage_daily (
  business_id     uuid not null references businesses(id) on delete cascade,
  day             date not null,
  conversations   integer not null default 0,
  messages_in     integer not null default 0,
  messages_out    integer not null default 0,
  templates_sent  integer not null default 0,
  tokens_in       bigint not null default 0,
  tokens_out      bigint not null default 0,
  cost_usd        numeric(12,4) not null default 0,
  cache_hits      integer not null default 0,
  handoffs        integer not null default 0,
  orders_attributed integer not null default 0,
  revenue_attributed numeric(14,2) not null default 0,
  intents         jsonb not null default '{}'::jsonb,  -- {intent: count}
  primary key (business_id, day)
);

-- scoruri LLM-as-judge (eval nocturn pe 2-3% din conversații)
create table conversation_evals (
  id              uuid primary key default gen_random_uuid(),
  business_id     uuid not null references businesses(id) on delete cascade,
  conversation_id uuid not null,
  judge_model     text not null,
  scores          jsonb not null,              -- {helpfulness, accuracy, tone, ...}
  overall         numeric(4,2) not null,
  notes           text,
  evaluated_at    timestamptz not null default now()
);
create index idx_evals_business on conversation_evals(business_id, evaluated_at desc);

-- golden tests per client (gate în CI/CD)
create table golden_tests (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  input       text not null,
  expected    jsonb not null,                  -- route așteptat / fapte obligatorii / interdicții
  source      text not null default 'manual' check (source in ('manual','shadow_correction')),
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

-- ============================================================================
-- 8. GDPR & AUDIT
-- ============================================================================

create table gdpr_requests (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  contact_id  uuid references contacts(id) on delete set null,
  kind        text not null check (kind in ('erase','export','access')),
  status      text not null default 'pending'
              check (status in ('pending','processing','done','failed')),
  requested_by text,
  result_ref  text,                            -- link export în storage (TTL)
  created_at  timestamptz not null default now(),
  completed_at timestamptz
);

create table audit_log (
  id          bigint generated always as identity primary key,
  business_id uuid,
  actor       text not null,                   -- user id / service name
  action      text not null,                   -- gdpr_erase / template_approve / alias_approve...
  entity      text,
  entity_id   text,
  details     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index idx_audit_business on audit_log(business_id, created_at desc);

-- ștergere GDPR = anonimizare (păstrezi agregatele, distrugi PII)
create or replace function gdpr_erase_contact(p_contact uuid) returns void
language plpgsql security definer as $$
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
end; $$;

-- ============================================================================
-- 9. RLS — izolare per tenant
--    Workers/servicii: service_role (bypass RLS).
--    Dashboard: user autenticat, acces doar la business-urile unde e membru.
-- ============================================================================

create or replace function my_business_ids() returns setof uuid
language sql stable security definer as $$
  select business_id from business_users where user_id = auth.uid();
$$;

-- activează RLS pe tot (nimic public by default)
do $$
declare t text;
begin
  for t in
    select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

-- politici dashboard (read) pe tabelele tenant-scoped principale
create policy "member read" on businesses for select
  using (id in (select my_business_ids()));
create policy "member read" on contacts for select
  using (business_id in (select my_business_ids()));
create policy "member read" on conversations for select
  using (business_id in (select my_business_ids()));
create policy "member read" on messages for select
  using (business_id in (select my_business_ids()));
create policy "member read" on products for select
  using (business_id in (select my_business_ids()));
create policy "member read" on orders for select
  using (business_id in (select my_business_ids()));
create policy "member read" on usage_daily for select
  using (business_id in (select my_business_ids()));
create policy "member read" on analytics_events for select
  using (business_id in (select my_business_ids()));
create policy "member read" on wa_templates for select
  using (business_id in (select my_business_ids()));
create policy "member read" on faqs for select
  using (business_id in (select my_business_ids()));
-- write-urile din dashboard (aprobare aliase, template-uri, FAQ) — per rol:
create policy "admin write faqs" on faqs for all
  using (business_id in (select bu.business_id from business_users bu
                         where bu.user_id = auth.uid() and bu.role in ('owner','admin')));
create policy "admin write aliases" on intent_aliases for all
  using (business_id in (select bu.business_id from business_users bu
                         where bu.user_id = auth.uid() and bu.role in ('owner','admin')));
-- restul tabelelor: fără politici = acces doar prin service_role. Adaugă
-- politici punctuale pe măsură ce dashboard-ul are nevoie.

-- ============================================================================
-- 10. RETENȚIE (pg_cron) — exemple
-- ============================================================================
-- select cron.schedule('drop-old-message-partitions', '0 3 1 * *',
--   $$ /* detach + drop partiții > N luni, conform contractului per business */ $$);
-- select cron.schedule('expire-semantic-cache', '*/30 * * * *',
--   $$ delete from semantic_cache where expires_at < now() $$);
-- select cron.schedule('rollup-usage-daily', '5 0 * * *',
--   $$ insert into usage_daily ... on conflict (business_id, day) do update ... $$);
