-- ============================================================================
-- 041 — NX-237: coșul canonic al conversației + mutation receipts idempotente
-- ============================================================================
-- DE CE tabele, nu `conversations.state.cart` (NX-79): starea copia PREȚUL la
-- momentul add-ului (fact stale servit drept adevăr), un retry putea dubla o
-- linie, iar frontendul avea propriul cart în localStorage — trei „adevăruri".
-- Aici coșul devine UN sistem canonic server-side: linii = REFS + cantități
-- (prețul se rehidratează din catalog la fiecare citire/mutație), versiune
-- monotonă (optimistic concurrency pentru acțiunile NX-240), iar fiecare
-- mutație lasă un receipt idempotent (retry cu aceeași cheie = același
-- rezultat, zero a doua mutație).
--
-- SEMANTICA (decizia Definition of Ready, docs/CART-DATA-READINESS.md):
-- în mediul curent NU există storefront API → acesta e COȘUL ASISTENTULUI
-- (al conversației), numit onest așa; checkout-ul folosește exclusiv linkul
-- canonic `checkout_links`. `external_cart_ref` e seam-ul pentru un adaptor
-- viitor — nu o promisiune.
--
-- EXPAND-ONLY: nu atinge `conversations.state` (migrarea stării la `cart_ref`
-- e LAZY, în cod, sub flag — CONVERSATION_CART_ENABLED, default OFF).
--
-- PII (P12): ZERO — doar refs (uuid-uri de produs/conversație), cantități,
-- coduri low-cardinality. GDPR: erase-ul de contact nu are ce șterge aici;
-- legătura cu persoana e prin conversație, ca la messages-metadata.
-- RETENȚIE: coșuri `expired`/`checked_out` + receipts terminale > 90 zile pot
-- fi purjate de un job admin (bounded); nu se șterge nimic automat aici.
--
-- ROLLBACK OPERAȚIONAL: stinge CONVERSATION_CART_ENABLED (mutațiile noi se
-- opresc, tool-urile revin pe calea legacy); tabelele RĂMÂN (reads/receipts/
-- reconciliation încă funcționează). Down-ul SQL de mai jos e doar pentru
-- medii de test — în prod nu se dropează dovezi de mutație.
--
--   -- DOWN (test-only):
--   -- drop table if exists commerce_action_receipts;
--   -- drop table if exists conversation_cart_items;
--   -- drop table if exists conversation_carts;
-- ============================================================================

create table if not exists conversation_carts (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  conversation_id  uuid not null references conversations(id) on delete cascade,
  version          integer not null default 0 check (version >= 0),
  status           text not null default 'active'
                     check (status in ('active','checked_out','expired')),
  currency         text,                        -- setat la prima linie; NULL = încă necunoscut
  external_cart_ref text,                       -- seam adaptor storefront viitor; safe, fără PII
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  expires_at       timestamptz
);

-- UN singur coș ACTIV per conversație (checkout-ul îl închide; următorul add
-- creează altul). Partial unique = și ținta ON CONFLICT din `create_cart_if_absent`.
create unique index if not exists uq_conversation_carts_active
  on conversation_carts (business_id, conversation_id)
  where status = 'active';

create index if not exists idx_conversation_carts_conv
  on conversation_carts (business_id, conversation_id, updated_at desc);

create table if not exists conversation_cart_items (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null,
  cart_id          uuid not null references conversation_carts(id) on delete cascade,
  product_id       uuid not null,
  variant_id       uuid,
  -- Cap-ul per linie e POLITICA canonică (CART_MAX_LINE_QUANTITY, impusă și în cod);
  -- CHECK-ul e plasa: un writer nou nu poate strecura un bulk order pe lângă serviciu.
  quantity         integer not null check (quantity between 1 and 10),
  added_turn_id    text,                        -- turul care a creat linia (audit, nu PII)
  updated_turn_id  text,                        -- ultimul tur care a atins-o
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  -- FK COMPUS pe tenant (ca product_relations, 027): un produs al ALTUI tenant nu poate
  -- intra structural în coș, indiferent ce query greșit l-ar propune.
  constraint fk_cart_items_product foreign key (business_id, product_id)
    references products (business_id, id) on delete cascade,
  -- O linie per (coș, produs, variantă). NULLS NOT DISTINCT (PG15+): două linii
  -- „fără variantă" ale aceluiași produs sunt ACEEAȘI linie, nu două — altfel
  -- merge-ul de cantitate ar avea două ținte.
  constraint uq_cart_items_line unique nulls not distinct
    (business_id, cart_id, product_id, variant_id)
);

create index if not exists idx_cart_items_cart
  on conversation_cart_items (business_id, cart_id);

create table if not exists commerce_action_receipts (
  id               uuid primary key default gen_random_uuid(),
  business_id      uuid not null references businesses(id) on delete cascade,
  conversation_id  uuid not null references conversations(id) on delete cascade,
  cart_id          uuid references conversation_carts(id) on delete set null,
  operation        text not null
                     check (operation in ('add','set_quantity','remove','clear','checkout')),
  -- Cheia idempotenței: `t:<turn_id>:<op>:<fingerprint>` (calea LLM) sau
  -- `a:<action_id>` (calea de acțiuni NX-236). Safe: id-uri opace, zero PII.
  idempotency_key  text not null,
  status           text not null default 'succeeded'
                     check (status in ('pending','succeeded','failed','unknown_reconcile')),
  before_version   integer not null default 0 check (before_version >= 0),
  after_version    integer check (after_version >= 0),
  result_code      text,                        -- vocabular ÎNCHIS (CART_ERROR_CODES) sau NULL
  turn_id          text,                        -- turul-țintă (corelare cu ledgerul NX-232)
  action_id        text,                        -- acțiunea consumată (NX-236), dacă e cazul
  external_ref     text,                        -- ref_code checkout / referința providerului
  url              text,                        -- linkul de checkout emis (pt replay exact)
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  -- EXACT-ONCE per cheie: al doilea INSERT cu aceeași cheie nu creează al
  -- doilea receipt — serviciul face replay pe rândul existent.
  constraint uq_commerce_receipts_key unique (business_id, idempotency_key),
  -- Un receipt reușit fără versiune-după ar fi o mutație fără dovadă de efect.
  constraint commerce_receipts_succeeded_ck check (
    status <> 'succeeded' or after_version is not null
  )
);

create index if not exists idx_commerce_receipts_conv
  on commerce_action_receipts (business_id, conversation_id, created_at desc);

-- Sweep-ul de reconciliere + alarma „pending/unknown prea vechi" scanează pe status+timp.
create index if not exists idx_commerce_receipts_pending
  on commerce_action_receipts (business_id, created_at)
  where status in ('pending','unknown_reconcile');

-- ── RLS + grants ────────────────────────────────────────────────────────────
-- Aceeași plasă ca 003/040: izolarea PRIMARĂ rămâne `business_id = $1` în cod;
-- RLS transformă un query greșit în „zero rezultate", nu în coșul altui client.
alter table conversation_carts enable row level security;
alter table conversation_cart_items enable row level security;
alter table commerce_action_receipts enable row level security;

drop policy if exists bot_runtime_tenant on conversation_carts;
create policy bot_runtime_tenant on conversation_carts to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

drop policy if exists bot_runtime_tenant on conversation_cart_items;
create policy bot_runtime_tenant on conversation_cart_items to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

drop policy if exists bot_runtime_tenant on commerce_action_receipts;
create policy bot_runtime_tenant on commerce_action_receipts to bot_runtime
  using (business_id = current_business_id())
  with check (business_id = current_business_id());

-- Dashboardul (membru al businessului) poate CITI (suport/debug); nu scrie.
drop policy if exists "member read" on conversation_carts;
create policy "member read" on conversation_carts for select
  using (business_id in (select my_business_ids()));
drop policy if exists "member read" on conversation_cart_items;
create policy "member read" on conversation_cart_items for select
  using (business_id in (select my_business_ids()));
drop policy if exists "member read" on commerce_action_receipts;
create policy "member read" on commerce_action_receipts for select
  using (business_id in (select my_business_ids()));

-- Grants MINIME: coșul nu se șterge din worker (îl închide statusul), liniile da
-- (remove/clear); receipturile sunt DOVEZI — fără delete (retenția e job admin).
grant select, insert, update on conversation_carts to bot_runtime;
grant select, insert, update, delete on conversation_cart_items to bot_runtime;
grant select, insert, update on commerce_action_receipts to bot_runtime;

-- trigger updated_at (aceeași funcție ca restul schemei v2)
drop trigger if exists trg_conversation_carts_upd on conversation_carts;
create trigger trg_conversation_carts_upd before update on conversation_carts
  for each row execute function set_updated_at();
drop trigger if exists trg_cart_items_upd on conversation_cart_items;
create trigger trg_cart_items_upd before update on conversation_cart_items
  for each row execute function set_updated_at();
drop trigger if exists trg_commerce_receipts_upd on commerce_action_receipts;
create trigger trg_commerce_receipts_upd before update on commerce_action_receipts
  for each row execute function set_updated_at();
