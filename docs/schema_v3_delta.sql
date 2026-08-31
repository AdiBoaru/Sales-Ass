-- ============================================================================
-- schema_v3 — DELTA peste `schema_v3_generated.sql`
-- ----------------------------------------------------------------------------
-- Se aplică pe proiectul Supabase NOU, imediat după DDL-ul generat din baza live.
-- Două categorii, ținute separat deliberat:
--
--   A. DEFECTE moștenite (§5 din docs/DB-V3-SOLE-IMPORT.md). Există azi în producție,
--      ar cere migrări. Într-o schemă nouă se repară gratis, fără backfill.
--   B. IMPORT LOSSLESS. Principiul acestui import: nimic nu se exclude la scriere.
--      Proveniența intră în SCHEMĂ (`source`, `voice`, `kind`, `storage`), iar filtrarea
--      devine politică la CITIRE. Consecința practică: dacă mâine decidem că proza AURA
--      nu se rostește, e un `where`, nu un re-import de 2.767 de produse.
--
-- Idempotent (`if not exists` peste tot). Catalogul rămâne read-only pentru worker.
--
-- ROLLBACK: la finalul fișierului.
-- ============================================================================


-- ============================================================================
-- A. DEFECTE MOȘTENITE
-- ============================================================================

-- --------------------------------------------------------------------------
-- A1/A2 — `product_badges` și `product_images` n-au `business_id`.
--
-- Toate celelalte tabele de catalog sunt tenant-scoped. Astea două nu sunt, deci un
-- `join` scris greșit poate întoarce imaginea altui tenant, iar RLS n-are pe ce să se
-- prindă. `product_sections` a primit coloana în migrarea 032; astea au fost uitate.
--
-- FK-ul e COMPUS pe `(business_id, product_id)`, nu simplu pe `product_id`: așa e
-- imposibil la nivel de schemă ca un badge al tenantului A să atârne de produsul lui B.
-- Depinde de `products_business_id_id_key`, care există.
-- --------------------------------------------------------------------------
alter table product_badges add column if not exists business_id uuid;
alter table product_images add column if not exists business_id uuid;

-- Pe o bază NOUĂ tabelele sunt goale, deci NOT NULL se poate pune direct.
-- (Pe una populată ar fi cerut backfill întâi — motivul pentru care n-a fost făcut până acum.)
do $$
begin
  if not exists (select 1 from product_badges) then
    alter table product_badges alter column business_id set not null;
  end if;
  if not exists (select 1 from product_images) then
    alter table product_images alter column business_id set not null;
  end if;
end $$;

alter table product_badges drop constraint if exists product_badges_tenant_fk;
alter table product_badges add constraint product_badges_tenant_fk
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade;

alter table product_images drop constraint if exists product_images_tenant_fk;
alter table product_images add constraint product_images_tenant_fk
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade;

create index if not exists idx_product_badges_tenant on product_badges (business_id, product_id);
create index if not exists idx_product_images_tenant on product_images (business_id, product_id, position);


-- --------------------------------------------------------------------------
-- A3 — badge-urile n-au TIP, deci trei lucruri diferite arată identic.
--
-- Cele 111 valori distincte din sursă amestecă:
--   • fapte cu sens comercial  — „AM PM dimineata si seara" (1.992), „Aprobat pentru copii",
--                                 „Protectie UV Daily" (84)
--   • conformitate             — „CPNP" (2.711)
--   • marketing DE MAGAZIN     — „SOLE Exclusiv" (747), „SOLE.ro este magazin oficial al
--                                 brandului X" (~1.500)
--
-- Ultima categorie e o afirmație despre ALT magazin. Se IMPORTĂ (import lossless), dar
-- tipizată, ca promptul și randorul s-o poată exclude fără să piardă informația.
-- `locale`: badge-ul e text afișabil, iar limba e parte din cheie (principiul 11).
-- --------------------------------------------------------------------------
alter table product_badges
  add column if not exists kind     text not null default 'other',
  add column if not exists locale   text not null default 'ro',
  add column if not exists position integer not null default 0,
  add column if not exists source   text not null default 'import';

alter table product_badges drop constraint if exists product_badges_kind_chk;
alter table product_badges add constraint product_badges_kind_chk
  check (kind in ('fact', 'claim', 'compliance', 'merchant_marketing', 'award', 'other'));

-- același badge o singură dată per produs + limbă
create unique index if not exists uq_product_badges_label
  on product_badges (business_id, product_id, locale, label);

comment on column product_badges.kind is
  'fact = afirmație verificabilă despre PRODUS, utilizabilă în vânzare (moment de rutină AM/PM, '
  'protecție UV, aprobat pentru copii) | claim = afirmație de eficacitate FĂRĂ studiu citabil '
  '(„Eficienta demonstrata stintific") — se păstrează, nu se rostește ca dovadă | compliance = '
  'notificare/certificare (CPNP) | merchant_marketing = afirmație despre MAGAZINUL sursă '
  '(„SOLE Exclusiv", „magazin oficial al brandului X") — NU se rostește | award | other.';


-- --------------------------------------------------------------------------
-- A4 — prețul condiționat de cupon n-avea casă. Cel mai periculos defect din listă.
--
-- În sursă, 2.131 din 2.767 de produse au `price_promo < price_regular` DAR cu
-- `promo_code = WELCOME15`, adică o reducere care cere un cod de bun venit. Doar 102 au
-- reducere reală necondiționată.
--
-- Mapat naiv în `sale_price`, lanțul e: widgetul afișează reducerea → `grounding_guard`
-- (NX-240) o confirmă, fiindcă E în baza noastră, deci „e adevărată" → clientul aude un preț
-- pe care nu-l poate obține. Un fapt fals care trece toate porțile e mai rău decât o
-- halucinație, fiindcă halucinația e prinsă.
--
-- `sale_price` rămâne STRICT necondiționat. Cuponul e alt concept, cu alte coloane, iar
-- CHECK-ul face imposibil să declari un preț de cupon fără să declari codul.
-- --------------------------------------------------------------------------
alter table products
  add column if not exists coupon_code  text,
  add column if not exists coupon_price numeric(12,2);

alter table products drop constraint if exists products_coupon_chk;
alter table products add constraint products_coupon_chk check (
  (coupon_code is null and coupon_price is null)
  or (coupon_code is not null and coupon_price is not null and coupon_price < price)
);

comment on column products.coupon_price is
  'Preț obtenabil DOAR cu `coupon_code`. NU e `sale_price` și nu e prețul de ancoră. '
  'Se rostește numai ca frază care numește condiția.';


-- --------------------------------------------------------------------------
-- A5 — expirarea și temperatura de păstrare erau text liber.
--
-- „Cât ține?" și „a expirat?" sunt întrebări pe care clientul le pune direct. Ca text în
-- `product_sections` botul nu poate răspunde determinist; ca dată, poate.
--
-- Ce ARE sursa, măsurat pe cele 2.757 de produse cu secțiunea completată:
--   • `min_durability_date`  — 2.251 de valori REALE, per produs („: 06.01.2029")
--   • temperatura de păstrare — 2.713 reale („: între 5°C și 25°C"), 44 stricate („între °C și °C")
--
-- Ce NU are, deși pare că are: **PAO**. Linia apare pe 2.251 de produse, dar textul e IDENTIC
-- peste tot: „conform simbolului PAO înscris pe ambalaj — de exemplu, 12M, 24M sau 36M". E
-- boilerplate cu valori DE EXEMPLU, nu valoarea produsului. Un parser care extrage „12" de acolo
-- ar fabrica un fapt pentru 2.251 de produse, iar botul ar spune „se folosește 12 luni după
-- deschidere" despre oricare dintre ele, cu validatorul mulțumit fiindcă cifra e în baza noastră.
--
-- Coloana se creează (e locul corect când apare o sursă reală: fișa de produs a brandului),
-- dar importerul o lasă NULL și un test o ține așa. UNKNOWN nu e o valoare implicită.
--
-- Textul original nu se pierde: rămâne în `product_sections` și în `source_products_raw`.
-- --------------------------------------------------------------------------
alter table products
  add column if not exists min_durability_date date,
  add column if not exists pao_months          integer,
  add column if not exists storage_temp_min_c  numeric(4,1),
  add column if not exists storage_temp_max_c  numeric(4,1);

alter table products drop constraint if exists products_pao_chk;
alter table products add constraint products_pao_chk
  check (pao_months is null or pao_months between 1 and 120);

alter table products drop constraint if exists products_storage_temp_chk;
alter table products add constraint products_storage_temp_chk check (
  (storage_temp_min_c is null) = (storage_temp_max_c is null)
  and (storage_temp_max_c is null or storage_temp_max_c >= storage_temp_min_c)
);

comment on column products.pao_months is
  'Perioada de utilizare după deschidere. NU se poate extrage din sursa SOLE: linia „PAO" de '
  'acolo e boilerplate identic pe 2.251 de produse, cu valori DE EXEMPLU. Rămâne NULL până '
  'apare o sursă per produs. Vezi testul care păzește asta.';


-- --------------------------------------------------------------------------
-- A6 — `reviews.rating` respingea o recenzie validă.
--
-- CHECK-ul e `between 1 and 5`, iar sursa are o recenzie cu rating 0 și text bun. Într-un
-- import lossless textul nu se aruncă din cauza unei note lipsă: ratingul devine NULL, iar
-- recenzia intră. UNKNOWN nu e 0 (același principiu ca `stock_total`).
-- --------------------------------------------------------------------------
alter table reviews alter column rating drop not null;
alter table reviews drop constraint if exists reviews_rating_check;
alter table reviews add constraint reviews_rating_check
  check (rating is null or rating between 1 and 5);


-- --------------------------------------------------------------------------
-- A7 — `ingredients` avea patru coloane, deci „conține alcool?" n-avea răspuns.
--
-- Sursa dă liste INCI: 96.254 de legături, 7.051 de valori distincte BRUTE (majuscule,
-- sinonime, sufixe). `name` singur nu susține nici normalizarea, nici întrebările de
-- evitare, care sunt cele mai frecvente în beauty după preț.
-- --------------------------------------------------------------------------
alter table ingredients
  add column if not exists inci_name text,
  add column if not exists aliases   text[] not null default '{}',
  add column if not exists flags     text[] not null default '{}';

create index if not exists idx_ingredients_inci on ingredients (business_id, inci_name);
create index if not exists idx_ingredients_flags on ingredients using gin (flags);

comment on column ingredients.flags is
  'Etichete de EVITARE, derivate determinist din registru: alcohol, fragrance, essential_oil, '
  'silicone, sulfate, paraben, nut_derived. Sursa e registrul, nu inferența modelului.';


-- ============================================================================
-- B. IMPORT LOSSLESS
-- ============================================================================

-- --------------------------------------------------------------------------
-- B1 — `product_sections` trebuie să spună CINE a scris textul.
--
-- `sections_json` din sursă are 17 chei și DOUĂ familii, separabile mecanic pe diacritice:
--   F1 (18.424 secțiuni, 0-5% diacritice)  = fapte de PDP scrise de magazin/producător
--   F2 (25.337 secțiuni, 99-100%)          = proza generată de asistentul AURA al SOLE
--
-- Ambele se importă. Dar `voice` (care există deja: brand | assistant) nu spune de UNDE, iar
-- fără `source` cele două devin indistinctibile în două luni. Cu ele, „nu rosti proza AURA"
-- e `where source <> 'aura'`, nu un re-import.
--
-- `content_hash` face secțiunea comparabilă între rulări: un import repetat nu rescrie ce
-- n-a variat, iar o schimbare la sursă devine vizibilă în loc să fie tăcută.
-- --------------------------------------------------------------------------
alter table product_sections
  add column if not exists source       text not null default 'unknown',
  add column if not exists content_hash text,
  add column if not exists source_key   text;

alter table product_sections drop constraint if exists product_sections_source_chk;
alter table product_sections add constraint product_sections_source_chk
  check (source in ('unknown', 'merchant_pdp', 'aura', 'brand_supplied', 'nativx_derived'));

create unique index if not exists uq_product_sections_key
  on product_sections (business_id, product_id, locale, source, source_key)
  where source_key is not null;

comment on column product_sections.source_key is
  'Cheia EXACTĂ din sursă („Cui i se potrivește"), păstrată verbatim. Fără ea, maparea '
  'cheie → `kind` devine ireversibilă și nu mai poți re-extrage altfel din același import.';


-- --------------------------------------------------------------------------
-- B2 — `product_images` trebuie să distingă ce servim NOI de ce e la sursă.
--
-- Se importă toate cele 15.487 de rânduri (import lossless), dar se găzduiesc doar
-- imaginile principale, 2.758, la dimensiune originală (403 MB pe VPS). Restul galeriei
-- rămâne în arhiva locală de 4,78 GB.
--
-- Fără `storage`, un rând negăzduit ar fi indistinct de unul găzduit, iar `url` ar minți.
-- Cu el, un al doilea val de upload e un `update`, iar widgetul nu află niciodată.
-- --------------------------------------------------------------------------
alter table product_images
  add column if not exists source_url text,
  add column if not exists storage    text not null default 'source',
  add column if not exists width      integer,
  add column if not exists height     integer,
  add column if not exists bytes      integer;

alter table product_images drop constraint if exists product_images_storage_chk;
alter table product_images add constraint product_images_storage_chk
  check (storage in ('self', 'source', 'archived_only'));

-- Cheie naturală, ca importul să fie idempotent. `product_images` avea DOAR `pkey` pe un uuid
-- generat, deci a doua rulare a importului ar fi adăugat încă 15.487 de rânduri în loc să nu
-- facă nimic. Un importer care nu se poate re-rula nu e un importer, e o operație unică.
create unique index if not exists uq_product_images_url
  on product_images (business_id, product_id, url);

comment on column product_images.storage is
  'self = servită de pe infrastructura noastră (`url` e a noastră) | source = `url` e al '
  'magazinului sursă | archived_only = există doar în arhiva locală, nu e servită.';
comment on column product_images.source_url is
  'URL-ul ORIGINAL, păstrat întotdeauna, chiar și după ce găzduim noi fișierul. E singura '
  'cale de a re-descărca fără să rulezi scraperul din nou.';


-- --------------------------------------------------------------------------
-- B3 — proveniența importului, la nivel de produs.
--
-- `catalog_sync_runs` există și înregistrează RULAREA. Ce lipsea e legătura de la produs la
-- rulare, plus versiunea extractorului. Fără ele nu poți răspunde la „de ce scrie asta aici"
-- și nu poți regenera selectiv doar produsele atinse de un extractor schimbat (D4/D5).
-- --------------------------------------------------------------------------
alter table products
  add column if not exists source_site       text,
  add column if not exists last_sync_run_id  uuid references catalog_sync_runs(id) on delete set null,
  add column if not exists extractor_version text;

create index if not exists idx_products_sync_run on products (business_id, last_sync_run_id);

comment on column products.source_fingerprint is
  'SHA-256 peste rândul canonic din sursă. Un import repetat cu aceeași amprentă nu rescrie '
  'nimic; una schimbată e singurul semnal onest că sursa s-a mișcat.';


-- --------------------------------------------------------------------------
-- B4 — corpusul de întrebări din F2 nu intră în DB.
--
-- 12.667 de formulări, 10.237 distincte. Sunt date de EVALUARE (corpusul pe care NX-203 e
-- blocat), nu conținut de catalog. În DB ar fi 10 MB pe care nicio cale de citire nu le
-- atinge, plus tentația de a le embedda (27.931 de vectori = 483 MB, adică peste planul free).
--
-- Locul lor e `tests/golden/` ca artefact versionat în git, unde se pot eticheta pe familii.
-- Se notează aici pentru că absența lor din schemă e o DECIZIE, nu o omisiune.
-- --------------------------------------------------------------------------


-- ============================================================================
-- ROLLBACK
-- ============================================================================
-- alter table product_badges  drop column if exists business_id, drop column if exists kind,
--                             drop column if exists locale, drop column if exists position,
--                             drop column if exists source;
-- alter table product_images  drop column if exists business_id, drop column if exists source_url,
--                             drop column if exists storage, drop column if exists width,
--                             drop column if exists height, drop column if exists bytes;
-- alter table products        drop column if exists coupon_code, drop column if exists coupon_price,
--                             drop column if exists min_durability_date, drop column if exists pao_months,
--                             drop column if exists source_site, drop column if exists last_sync_run_id,
--                             drop column if exists extractor_version;
-- alter table ingredients     drop column if exists inci_name, drop column if exists aliases,
--                             drop column if exists flags;
-- alter table product_sections drop column if exists source, drop column if exists content_hash,
--                              drop column if exists source_key;
-- alter table reviews         alter column rating set not null;
