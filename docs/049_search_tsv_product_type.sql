-- ============================================================================
-- 049 — greutatea `A` a vectorului de căutare era umplutură de marketing
-- ----------------------------------------------------------------------------
-- Migrarea 046 a pus `name` în `setweight(..., 'A')` cu argumentul explicit: „A = numele
-- (identitatea produsului)". Pe catalogul demo argumentul era adevărat. Pe primul catalog REAL
-- (SOLE, 2.758 de produse) e fals, și e fals în mod măsurabil:
--
--     lungimea medie a numelui           191 caractere (min 38, max 383)
--     nume care conțin „care contribuie" 2.287 / 2.758   (83%)
--     nume cu o coadă >40 car. după ' - ' 2.647 / 2.758  (96%)
--
-- Numele nu e un nume, e nume PLUS descriere: „EVY TECHNOLOGY Sunscreen Mousse SPF 30, 150 ml -
-- Crema de fata si corp formulata cu glicerina si filtre solare UVA UVB, care contribuie la
-- protectia solara…". Deci fraza de marketing stă în greutatea maximă, iar `ts_rank_cd` o
-- răsplătește cu 1.0 în loc de 0.2. Consecința, măsurată pe traseul real de retrieval:
--
--     «protectie solara spf»    → ser cu retinol pe locul 1; 3 din 6 rezultate nu erau SPF
--     «ser hidratant ten uscat» → cremă de ochi în rezultate
--     «gel de curatare»         → gel de DUȘ
--     «rutina ten uscat»        → un ruj, de două ori (numele lui conține „hidratarea pielii")
--
-- Nu e o problemă de relevanță, e index otrăvit: cuvintele care decid ordinea sunt aceleași pe
-- 18% din catalog, deci nu decid nimic.
--
-- CE SCHIMBĂ
--
-- `A` primește DOUĂ lucruri, ambele identitate:
--   1. capul numelui — `split_part(name, ' - ', 1)`, adică exact partea dinaintea descrierii.
--      Pe catalogul SOLE are 40 de caractere în medie, față de 191 cât are numele întreg;
--   2. `attributes.product_type` — tipul canonic derivat (`src/catalog/product_type.py`).
--
-- Punctul 2 nu e un bonus, e condiția ca punctul 1 să nu strice recall-ul. Capul numelui e
-- aproape întotdeauna brand + denumire comercială în ENGLEZĂ („ANUA Ceramide 3 + Panthenol
-- Moisture Barrier Cream"), iar clientul scrie românește. Cuvântul „crema" există DOAR în coadă.
-- Dacă mutăm coada în `C` fără să punem tipul în `A`, o interogare românească rămâne fără niciun
-- termen de greutate mare și am muta problema, nu am rezolva-o.
--
-- Coada NU se aruncă: intră în `C`, împreună cu descrierea. Un produs care menționează „acnee"
-- doar în coadă rămâne găsibil — pe treapta de jos a ponderilor, ceea ce e exact locul ei.
-- Recall-ul rămâne identic (același set de rânduri se potrivește); se schimbă doar ORDINEA.
--
-- CE NU SCHIMBĂ
--   • nu atinge configurația de text search (vezi motivul din antetul 046: lista de cuvinte goale
--     a lui `'romanian'` e scrisă cu diacritice, iar noi indexăm text trecut prin `ro_unaccent`);
--   • nu atinge `WHERE`, deci nici recall-ul, nici treptele de relaxare din
--     `search_products_lexical`;
--   • nu atinge `product_search_documents` (brațul semantic, NX-207).
--
-- DEPENDENȚĂ: `attributes.product_type` se scrie cu `scripts/derive_product_type.py --apply`.
-- Migrarea e corectă și fără el (`coalesce(..., '')` ⇒ pur și simplu nu contribuie la vector),
-- dar atunci recall-ul pe termeni românești scade — deci jobul se rulează ÎNAINTE.
--
-- O coloană generată nu se poate ALTERA — se scoate și se pune la loc, ca în 033/046. Rescrie
-- tabelul: 2.758 de rânduri, sub o secundă.
--
-- ROLLBACK (revine exact la definiția din 046):
--   drop index if exists idx_products_search_tsv;
--   alter table products drop column if exists search_tsv;
--   alter table products add column search_tsv tsvector generated always as (
--     setweight(to_tsvector('simple', ro_unaccent(coalesce(name, ''))), 'A')
--     || setweight(to_tsvector('simple', ro_unaccent(coalesce(ai_summary, ''))), 'B')
--     || setweight(to_tsvector('simple', ro_unaccent(coalesce(description, ''))), 'C')
--   ) stored;
--   create index idx_products_search_tsv on products using gin (search_tsv);
-- ============================================================================

-- Rescrierea tabelului + reconstrucția indexului GIN depășesc `statement_timeout`-ul implicit al
-- poolerului Supabase (măsurat: prima încercare a fost anulată la mijloc). `set local` ține doar
-- până la capătul tranzacției migrării, deci nu schimbă nimic pentru runtime.
set local statement_timeout = '10min';

drop index if exists idx_products_search_tsv;
alter table products drop column if exists search_tsv;

alter table products
  add column search_tsv tsvector
  generated always as (
    -- A — identitatea: capul numelui + tipul canonic de produs.
    setweight(
      to_tsvector(
        'simple',
        ro_unaccent(
          split_part(coalesce(name, ''), ' - ', 1)
          || ' '
          || coalesce(attributes->>'product_type', '')
        )
      ),
      'A'
    )
    -- B — rezumatul derivat, când există.
    || setweight(to_tsvector('simple', ro_unaccent(coalesce(ai_summary, ''))), 'B')
    -- C — context: numele ÎNTREG (deci și coada descriptivă) plus descrierea. Numele intră aici
    -- din nou, deliberat: pe treapta de jos, ca să nu se piardă niciun termen din el.
    || setweight(
      to_tsvector(
        'simple',
        ro_unaccent(coalesce(name, '') || ' ' || coalesce(description, ''))
      ),
      'C'
    )
  ) stored;

create index idx_products_search_tsv on products using gin (search_tsv);

comment on column products.search_tsv is
  '049: A = capul numelui (înainte de '' - '') + attributes.product_type; B = ai_summary; '
  'C = numele întreg + descrierea. Motivul e în docs/049_search_tsv_product_type.sql: pe un '
  'catalog real numele conține descrierea, deci 046 punea fraza de marketing în greutatea '
  'maximă. Tipul canonic stă în A fiindcă un cap de nume e în engleză, iar clientul scrie '
  'românește. Interogarea se construiește în src/catalog/query_terms.py.';
