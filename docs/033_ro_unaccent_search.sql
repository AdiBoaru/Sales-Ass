-- ============================================================================
-- 033 — NX-178: căutare INSENSIBILĂ la diacritice (RO)
-- ----------------------------------------------------------------------------
-- Măsurat pe catalogul live, înainte:
--     «păr uscat» → 24 rezultate      «par uscat» → 0
--     «șampon»    → 20 rezultate      «sampon»    → 0
--
-- Adică un client român care scrie fără diacritice — majoritatea — primea ZERO rezultate.
-- Nu e o problemă de relevanță, e una de existență: produsul nu apare deloc.
--
-- DE CE `translate` ȘI NU `unaccent`:
--   • `search_tsv` e o coloană GENERATĂ → expresia trebuie să fie IMMUTABLE. `unaccent()` e
--     STABLE (depinde de dicționar), iar soluția uzuală — un wrapper marcat fals `IMMUTABLE` —
--     lasă indexul stale dacă dicționarul se schimbă vreodată. `translate` e imutabil prin natură.
--   • extensia `unaccent` e disponibilă, dar neinstalată; `translate` nu cere nicio dependență.
--   • proiectul e RO-only prin decizie explicită (Catalog v3), iar setul de caractere românești e
--     închis și cunoscut — inclusiv formele cu SEDILĂ (ş/ţ, U+015F/U+0163), care apar în text
--     copiat din surse vechi și pe care oamenii le tastează fără să știe.
--
-- Funcția e folosită de AMBELE capete: la indexare (coloana generată) și la interogare (read-path).
-- Dacă normalizezi doar o parte, tot nu se potrivesc.
--
-- ROLLBACK:
--   alter table products drop column if exists search_tsv;
--   alter table products add column search_tsv tsvector generated always as
--     (to_tsvector('simple', coalesce(name,'') || ' ' || coalesce(ai_summary,''))) stored;
--   create index idx_products_search_tsv on products using gin (search_tsv);
--   drop index if exists idx_products_name_ro_trgm;
--   drop function if exists ro_unaccent(text);
-- ============================================================================

-- Normalizare RO: lower + fără diacritice. IMMUTABLE (translate e pur) → poate sta într-o coloană
-- generată și într-un index de expresie. `search_path` fixat: o funcție folosită de indecși nu are
-- voie să depindă de search_path-ul apelantului.
create or replace function ro_unaccent(txt text)
returns text
language sql
immutable
parallel safe
strict
set search_path = pg_catalog, public
as $$
  select translate(
    lower(txt),
    'ăâîșțşţ',
    'aaistst'
  )
$$;

comment on function ro_unaccent(text) is
  'NX-178: lower + strip diacritice RO (inclusiv formele cu sedilă ş/ţ). IMMUTABLE — folosită '
  'în coloana generată products.search_tsv ȘI în read-path. Normalizarea trebuie să fie ACEEAȘI '
  'pe ambele capete, altfel potrivirea nu se produce.';

-- --------------------------------------------------------------------------
-- search_tsv: re-generat peste textul NORMALIZAT.
-- O coloană generată nu se poate ALTERA — se scoate și se pune la loc (rescrie tabelul; pe
-- ordinul de mărime al catalogului nostru e instantaneu).
-- --------------------------------------------------------------------------
drop index if exists idx_products_search_tsv;
alter table products drop column if exists search_tsv;

alter table products
  add column search_tsv tsvector
  generated always as (
    to_tsvector(
      'simple',
      ro_unaccent(coalesce(name, '') || ' ' || coalesce(ai_summary, ''))
    )
  ) stored;

create index idx_products_search_tsv on products using gin (search_tsv);

-- --------------------------------------------------------------------------
-- Fuzzy pe nume (typo/SKU): indexul trgm existent e pe `name` BRUT, deci tot diacritic-sensibil.
-- Adăugăm unul pe expresia normalizată; cel vechi rămâne (îl folosesc alte căi, iar un index în
-- plus pe 300 de rânduri nu costă nimic).
-- --------------------------------------------------------------------------
create index if not exists idx_products_name_ro_trgm
  on products using gin (ro_unaccent(name) gin_trgm_ops);

-- `bot_runtime` execută funcția pe calea de citire.
grant execute on function ro_unaccent(text) to bot_runtime;
