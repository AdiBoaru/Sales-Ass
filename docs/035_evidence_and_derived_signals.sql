-- ============================================================================
-- 035 — NX-205: contractul adevărului, partea persistată
--        product_evidence_chunks + product_derived_signals
-- ----------------------------------------------------------------------------
-- Separă FIZIC cele trei stări ale adevărului (ADR D5), ca să nu mai depindă de disciplină:
--   • CONFIRMAT — `products.attributes` (facts + claim_provenance + not_recommended_for, NX-168d);
--   • CITABIL   — `product_evidence_chunks`: fragmentul cu ROL, cu sursă obligatorie. Negativele
--                 INTRĂ aici cu role='warning' (D9) — nu se filtrează din document;
--   • DERIVAT   — `product_derived_signals`: semnal + `derived_from[]` + `rule_id`. Regula fiind
--                 versionată, un semnal greșit se re-derivează GLOBAL (where rule_id = …), nu
--                 produs cu produs.
-- Un semnal derivat nu are voie să stea în același loc cu un fapt confirmat: prima dată când sunt
-- în aceeași coloană, nimeni nu mai știe care e care.
--
-- SCRIEREA e a joburilor de conținut (NX-206/207), admin. Botul DOAR citește (grant select).
--
-- INTEGRITATE DE TENANT — declarativă, ca la 027: FK COMPUS (business_id, product_id) →
-- products(business_id, id). Un rând nu poate referi produsul altui tenant: structural imposibil,
-- nu „respins de cod". P7 + RLS ca plasă.
--
-- D3: fiecare artefact poartă `locale` + `schema_version` — fără ele nu se poate nici servi pe
-- limba potrivită (P11), nici migra la o versiune nouă de contract fără rescriere oarbă.
--
-- Aditiv + idempotent. ZERO breaking change: nu atinge `products`, nu atinge auditul v2/v3.
--
-- ROLLBACK:
--   drop table if exists product_derived_signals;
--   drop table if exists product_evidence_chunks;
-- ============================================================================

-- target-ul FK-ului compus există deja din 027 (products_business_id_id_key); guard idempotent
-- ca migrarea să fie auto-suficientă dacă 027 lipsește într-un mediu nou.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'products_business_id_id_key'
      and conrelid = 'products'::regclass
  ) then
    alter table products add constraint products_business_id_id_key unique (business_id, id);
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Evidence: fragmentul CITABIL, cu rol explicit.
-- ---------------------------------------------------------------------------
create table if not exists product_evidence_chunks (
  id             uuid primary key default gen_random_uuid(),
  business_id    uuid not null references businesses(id) on delete cascade,
  product_id     uuid not null,
  -- D9: `warning` face negativele citabile — o contraindicație se ETICHETEAZĂ, nu se ascunde.
  role           text not null check (role in (
                   'benefit', 'usage', 'warning', 'ingredient', 'faq', 'review_summary', 'policy')),
  text           text not null check (length(btrim(text)) > 0),
  -- Fără sursă nu e evidence, e afirmație. De aceea e NOT NULL, nu „recomandat".
  source         text not null check (length(btrim(source)) > 0),
  locale         text not null,
  schema_version integer not null default 1 check (schema_version >= 1),
  -- Re-generarea e idempotentă: același conținut → același hash → un singur rând.
  content_hash   text not null,
  created_at     timestamptz not null default now(),
  unique (business_id, product_id, role, locale, content_hash),
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

-- read-path: fragmentele unui produs, pe limbă, grupate pe rol.
create index if not exists product_evidence_chunks_product_idx
  on product_evidence_chunks (business_id, product_id, locale, role);

-- ---------------------------------------------------------------------------
-- Derived signals: ce am DEDUS, cu din ce și după ce regulă.
--
-- ⚠ STARE (NX-206): tabelul NU ARE WRITER și NU ARE CITITOR. Schema există, codul nu.
--   Primul candidat evident — badge-urile — a fost DECIS altfel: proprietarul lor e RUNTIME-ul
--   (`src/worker/badges.py` le derivă la afișare din rating/reducere, cu praguri per-vertical din
--   `DomainPack.badge_rules`). A le persista și aici ar fi creat al doilea proprietar al aceleiași
--   noțiuni — exact ce interzice P3 („un singur proprietar per câmp").
--   Tabelul rămâne pentru semnalele derivate care VOR avea un consumator real; până atunci e
--   schemă pregătită, nu funcționalitate livrată. Nu-l raporta ca implementat.
-- ---------------------------------------------------------------------------
create table if not exists product_derived_signals (
  id             uuid primary key default gen_random_uuid(),
  business_id    uuid not null references businesses(id) on delete cascade,
  product_id     uuid not null,
  signal         text not null check (length(btrim(signal)) > 0),
  -- Nevid: un semnal fără intrări nu e derivat, e inventat.
  -- ATENȚIE (review #250): `array_length(x, 1)` întoarce NULL pe array GOL, iar un CHECK care se
  -- evaluează la NULL TRECE. `cardinality()` întoarce 0 → constrângerea chiar respinge `{}`.
  -- Elementele goale/NULL sunt și ele respinse: „derivat din nimic" scris cu spații e tot nimic.
  derived_from   text[] not null
                 check (cardinality(derived_from) >= 1)
                 check (array_position(derived_from, null) is null)
                 -- niciun element gol/din spații. Separatorul E'\x01' nu poate apărea într-un
                 -- path de fapt, deci un segment gol în join = un element gol în array.
                 check (array_to_string(derived_from, E'\x01') !~ E'(^|\x01)[[:space:]]*(\x01|$)'),
  -- Regula versionată — cheia prin care se re-derivează global (vezi `..._rule_idx`).
  rule_id        text not null check (length(btrim(rule_id)) > 0),
  locale         text not null,
  schema_version integer not null default 1 check (schema_version >= 1),
  created_at     timestamptz not null default now(),
  unique (business_id, product_id, signal, rule_id, locale),
  foreign key (business_id, product_id) references products (business_id, id) on delete cascade
);

create index if not exists product_derived_signals_product_idx
  on product_derived_signals (business_id, product_id, locale);

-- „regula R s-a dovedit greșită" → toate semnalele ei, într-un singur pas.
create index if not exists product_derived_signals_rule_idx
  on product_derived_signals (business_id, rule_id);

-- ---------------------------------------------------------------------------
-- RLS + grants: botul citește, joburile de conținut scriu (admin).
-- ---------------------------------------------------------------------------
alter table product_evidence_chunks enable row level security;
grant select on product_evidence_chunks to bot_runtime;
drop policy if exists bot_runtime_tenant on product_evidence_chunks;
create policy bot_runtime_tenant on product_evidence_chunks to bot_runtime
  using (business_id = current_business_id());

alter table product_derived_signals enable row level security;
grant select on product_derived_signals to bot_runtime;
drop policy if exists bot_runtime_tenant on product_derived_signals;
create policy bot_runtime_tenant on product_derived_signals to bot_runtime
  using (business_id = current_business_id());
