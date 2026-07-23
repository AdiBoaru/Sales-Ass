-- ============================================================================
-- 032 — NX-191: stratul COMERCIAL (fereastră de reducere, reaprovizionare, clasă
--       de livrare) + casele lipsă de CONȚINUT (FAQ per produs, proveniență text,
--       tip de imagine)
-- ----------------------------------------------------------------------------
-- Măsurat pe catalogul servit (150 produse published, tenant demo) înainte de card:
--   • reducere reală: 4/150 (2%) — mecanica de anchor (list_price/on_sale/preț per
--     unitate) există în cod și NU are pe ce rula;
--   • availability: 150/150 `in_stock` — substitutul (222 relații), back-in-stock și
--     data de revenire nu se declanșează NICIODATĂ;
--   • livrare: NICIUN câmp, nicăieri (nici produs, nici businesses.settings) — la
--     „în cât timp ajunge?" botul n-are ce citi;
--   • FAQ per produs: inexistent (`faqs` e la nivel de business, fără product_id).
--
-- Ce e cross-vertical intră în COLOANE (orice magazin are livrare și promoții);
-- faptele de vertical rămân în `attributes` jsonb (regula din Catalog v3).
-- Catalogul rămâne READ-ONLY pentru worker (bot_runtime: doar SELECT).
-- Aditiv + idempotent.
--
-- ROLLBACK:
--   drop table if exists product_faqs;
--   alter table products drop column if exists sale_start, drop column if exists sale_end,
--     drop column if exists restock_date, drop column if exists delivery_class;
--   alter table product_images drop column if exists kind;
--   alter table product_sections drop column if exists voice, drop column if exists locale,
--     drop column if exists business_id;
-- ============================================================================

-- --------------------------------------------------------------------------
-- 1. products — fereastra promoției + reaprovizionare + clasa de livrare
-- --------------------------------------------------------------------------
alter table products
  add column if not exists sale_start     date,
  add column if not exists sale_end       date,
  add column if not exists restock_date   date,
  add column if not exists delivery_class text;

-- fereastră coerentă (ambele NULL = promoție fără termen, comportamentul de azi)
alter table products drop constraint if exists products_sale_window_chk;
alter table products
  add constraint products_sale_window_chk
  check (sale_start is null or sale_end is null or sale_end >= sale_start);

-- clasa de livrare: set canonic, cross-vertical. NULL = „ca magazinul" (default din
-- businesses.settings) → rândurile vechi rămân valide fără backfill.
alter table products drop constraint if exists products_delivery_class_chk;
alter table products
  add constraint products_delivery_class_chk
  check (delivery_class is null
         or delivery_class in ('next_day', 'standard', 'supplier', 'preorder'));

comment on column products.sale_end is
  'Ultima zi INCLUSIV în care sale_price se aplică. Read-path-ul TREBUIE să verifice '
  'fereastra: un sale_price expirat afișat ca preț curent e o minciună comercială.';
comment on column products.restock_date is
  'Când revine în stoc; se completează la availability=out_of_stock. Botul îl poate rosti.';
comment on column products.delivery_class is
  'next_day (comandă până la ora-limită → mâine) | standard | supplier | preorder. '
  'Promisiunea concretă se CALCULEAZĂ determinist din asta + config-ul magazinului.';

-- calea caldă „ce e la reducere acum": index parțial pe promoțiile cu fereastră activă.
create index if not exists products_sale_window_idx
  on products (business_id, sale_end)
  where sale_price is not null;

-- --------------------------------------------------------------------------
-- 2. product_images — tipul pozei
--    Azi: exact 1 poză/produs, fără tip → cardul ia orb prima poză și „arată-mi
--    textura" e imposibil. Coloana intră ACUM (structura), pozele vin ulterior.
-- --------------------------------------------------------------------------
alter table product_images
  add column if not exists kind text not null default 'main';

alter table product_images drop constraint if exists product_images_kind_chk;
alter table product_images
  add constraint product_images_kind_chk
  check (kind in ('main', 'texture', 'application', 'before_after', 'ingredient',
                  'packaging', 'other'));

-- --------------------------------------------------------------------------
-- 3. product_sections — tenant-scope + limbă + PROVENIENȚĂ
--    voice='brand'     → text al producătorului: afișabil/citabil DOAR atribuit,
--                        NU intră în embedding.
--    voice='assistant' → botul îl poate afirma direct; a trecut has_medical_claim
--                        la INGESTION (nu la runtime, unde produce răspunsuri ciuntite).
--    Default 'brand' = alegerea CONSERVATOARE: secțiunile existente nu devin brusc
--    afirmabile de bot; promovarea la 'assistant' e explicită, per bloc.
-- --------------------------------------------------------------------------
alter table product_sections
  add column if not exists business_id uuid,
  add column if not exists locale      text not null default 'ro',
  add column if not exists voice       text not null default 'brand';

update product_sections ps
   set business_id = p.business_id
  from products p
 where p.id = ps.product_id and ps.business_id is null;

-- NOT NULL doar dacă backfill-ul a acoperit tot (secțiuni orfane n-ar trebui să existe,
-- dar nu blocăm migrarea pe ele).
do $$
begin
  if not exists (select 1 from product_sections where business_id is null) then
    alter table product_sections alter column business_id set not null;
  else
    raise notice '032: product_sections cu business_id NULL (produs lipsă) — rămâne nullable';
  end if;
end $$;

alter table product_sections drop constraint if exists product_sections_voice_chk;
alter table product_sections
  add constraint product_sections_voice_chk check (voice in ('brand', 'assistant'));

create index if not exists idx_sections_business_product
  on product_sections (business_id, product_id, locale);

-- --------------------------------------------------------------------------
-- 4. product_faqs — întrebări frecvente PER PRODUS (6/produs, citite la DETALIU)
--    `faqs` e la nivel de business și n-are product_id: „se poate folosi cu retinol?"
--    n-avea casă. Integritate de tenant DECLARATIVĂ prin FK COMPUS
--    (business_id, product_id) → products(business_id, id), ca la product_relations
--    (027): un FAQ cross-tenant e STRUCTURAL imposibil, nu doar respins de cod.
--    Target-ul unique (products_business_id_id_key) există deja din 027.
--
--    `embedding` rămâne NULL deocamdată (decizie: FAQ se citește la detaliu, nu intră
--    în căutare — 1.800 de texte scurte și asemănătoare ar fi zgomot în același spațiu
--    vectorial cu produsele). Coloana există ca să pornim căutarea fără altă migrare.
--
--    `derived` separă cele ~4 FAQ compuse DETERMINIST din fapte (regenerabile la
--    fiecare rulare) de cele scrise de om (care NU se suprascriu).
-- --------------------------------------------------------------------------
create table if not exists product_faqs (
  id          uuid primary key default gen_random_uuid(),
  business_id uuid not null references businesses(id) on delete cascade,
  product_id  uuid not null,
  locale      text not null default 'ro',
  question    text not null,
  answer      text not null,
  position    integer not null default 0 check (position >= 0),
  -- derived: compus din fapte → se regenerează. curated/brand: scris → nu se atinge.
  source      text not null default 'derived'
              check (source in ('derived', 'curated', 'brand')),
  derived     boolean not null default true,
  embedding   vector(1536),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  -- aceeași întrebare o singură dată per produs + limbă (P11: limba e parte din cheie)
  unique (business_id, product_id, locale, question),
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

create index if not exists idx_product_faqs_lookup
  on product_faqs (business_id, product_id, locale, position);

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_product_faqs_upd') then
    create trigger trg_product_faqs_upd before update on product_faqs
      for each row execute function set_updated_at();
  end if;
end $$;

-- --------------------------------------------------------------------------
-- 5. Drepturi + RLS — catalogul e READ-ONLY pentru worker (P7).
--    Scrierea o fac seed/sync pe rol privilegiat, nu botul.
-- --------------------------------------------------------------------------
alter table product_faqs enable row level security;
grant select on product_faqs to bot_runtime;

drop policy if exists bot_runtime_tenant on product_faqs;
create policy bot_runtime_tenant on product_faqs to bot_runtime
  using (business_id = current_business_id());
