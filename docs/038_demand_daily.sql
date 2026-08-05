-- ============================================================================
-- 038 — NX-217 felia 2: `demand_daily` (rollup durabil al faptelor de cerere)
-- ============================================================================
-- DE CE un tabel, nu agregare la citire peste analytics_events:
--   1. `analytics_events` e partiționat pe lună cu retenție prin drop partition —
--      istoria cererii (activul: „cererea de brandul X crește de 3 luni") ar muri
--      odată cu evenimentele brute (efemere);
--   2. o fereastră de 30/90 zile ar scana de fiecare dată tabelul cel mai gras.
--
-- CE STOCĂM: FAPTE („brandul B a fost cerut de 73 ori pe 4 aug, dovada: aceste
-- conversații") — adevărate pentru totdeauna. JUDECĂȚILE („adaugă B în catalog")
-- NU se stochează niciodată: depind de starea catalogului ACUM, deci se re-derivă
-- la citire (NX-217 felia 3). Un rând materializat de acțiune ar minți imediat ce
-- comerciantul rezolvă problema.
--
-- GRANULARITATE: (business_id, day, signal_kind, dimension_kind, dimension_key).
--   • `dimension_key` e NOT NULL cu sentinelă '' — NULL într-un PK compus ar rupe
--     `ON CONFLICT` (și orice dedup), producând dubluri tăcute la re-rulare.
--   • Un singur eveniment poate produce MAI MULTE rânduri (brand ȘI categorie) →
--     NU se însumează niciodată peste `dimension_kind` (aceeași regulă ca la
--     split-ul bot-led/assisted din NX-162).
--   • `conversation_count` NU e aditiv peste zile (o conversație care traversează
--     miezul nopții se numără în ambele zile) — pe ferestre lungi se folosește
--     `request_count`, care e aditiv.
--
-- PII (P12): DOAR dimensiuni normalizate (brand/categorie/atribut) + id-uri. Zero
-- text de user, zero telefoane. `evidence_conversation_ids` = ref-uri pentru
-- drilldown, nu corpuri.
-- ============================================================================

create table if not exists demand_daily (
  business_id     uuid  not null references businesses(id) on delete cascade,
  day             date  not null,                    -- ziua UTC, ca usage_daily
  signal_kind     text  not null,
  dimension_kind  text  not null,
  dimension_key   text  not null,                    -- '' = dimensiune absentă (NU null)
  request_count   integer not null default 0 check (request_count >= 0),
  conversation_count integer not null default 0 check (conversation_count >= 0),
  evidence_conversation_ids uuid[] not null default '{}',
  primary key (business_id, day, signal_kind, dimension_kind, dimension_key),
  constraint demand_daily_signal_kind_ck check (signal_kind in (
    'unmet_no_result', 'unmet_named_not_found', 'unmet_out_of_stock',
    'unmet_missing_variant', 'unmet_price_gap',
    'requested_brand', 'requested_category',
    'recommended_product', 'cart_product', 'checkout_product',
    'faq_miss', 'clarify_asked'
  )),
  constraint demand_daily_dimension_kind_ck check (dimension_kind in (
    'brand', 'category', 'product', 'variant_attr', 'clarify_field', 'none'
  ))
);

-- Interogările de raport merg pe (business, semnal, fereastră de zile).
create index if not exists idx_demand_daily_signal
  on demand_daily (business_id, signal_kind, day desc);

-- RLS: dashboard-ul (membru al businessului) citește; `bot_runtime` NU primește
-- grant — botul n-are ce căuta în raportul de cerere (scrie doar evenimente).
-- Jobul de rollup rulează pe conexiunea admin, ca rollup_usage.
alter table demand_daily enable row level security;
drop policy if exists "member read" on demand_daily;
create policy "member read" on demand_daily for select
  using (business_id in (select my_business_ids()));

-- ROLLBACK:
-- drop table if exists demand_daily;
