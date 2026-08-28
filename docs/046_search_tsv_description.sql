-- ============================================================================
-- 046 — vectorul de căutare conținea DOAR numele produsului
-- ----------------------------------------------------------------------------
-- Măsurat pe catalogul SOLE live (2.758 produse, 2026-08-28), pe conexiunea `bot_runtime`:
--
--     «produse pentru acnee»              → 0 rezultate
--     «ce imi recomanzi pentru riduri»    → 0
--     «ceva pentru cearcane»              → 0
--     «crema hidratanta pentru ten uscat» → 1
--
-- Cauza nu e relevanța, e conținutul indexului. `search_tsv` a fost definită în 015 și
-- re-generată în 033 peste `name || ai_summary`, iar pe catalogul SOLE **`ai_summary` e NULL pe
-- toate cele 2.758 de rânduri** (nu s-a rulat niciodată jobul care o scrie). Deci vectorul de
-- căutare al fiecărui produs e LITERALMENTE numele lui, iar numele unui produs de cosmetice nu
-- conține cuvântul „acnee" sau „riduri" — le conține descrierea, care există pe 2.758/2.758 și
-- nu era indexată nicăieri.
--
-- Nu e o regresie apărută acum: definiția a fost mereu asta. Pe catalogul demo (150 de produse
-- cu `ai_summary` scris de un job) vectorul avea text real, deci gaura era acoperită de o
-- coloană derivată. Pe primul catalog REAL, coloana derivată lipsește și rămâne numele gol.
--
-- DE CE `setweight` acum, când 015/033 nu-l foloseau:
--   Cu numele și descrierea în ACELAȘI vector și greutate egală (D pentru tot, cum era), un
--   produs care doar MENȚIONEAZĂ „șampon" în descriere ar rangui la fel cu unul care se NUMEȘTE
--   „Șampon". Adăugarea descrierii fără ponderi ar fi cumpărat recall plătind cu precizie.
--   Ponderile fac ordinea să spună ce spune structura: A = numele (identitatea produsului),
--   B = `ai_summary` (rezumat derivat, când există), C = descrierea (context). `ts_rank_cd`
--   folosește implicit {D,C,B,A} = {0.1, 0.2, 0.4, 1.0}, deci raportul e 10:1 între nume și
--   descriere — recall-ul crește, dar potrivirea pe nume rămâne câștigătoare.
--   Efect secundar util: NX-226 normalizează rangul lexical relativ la pool; cu tot vectorul pe
--   greutatea D, `ts_rank_cd` trăia în 0,01–0,3 și semnalul era practic plat.
--
-- CE NU FACE migrarea:
--   • NU schimbă configurația de text search. `'romanian'` ar aduce stemming, dar lista ei de
--     cuvinte goale e scrisă CU diacritice, iar noi indexăm text trecut prin `ro_unaccent` (033):
--     „pentru", „si", „ceva" ar trece neatinse prin ea. Verificat cu `ts_debug`. Cuvintele goale
--     se rezolvă în read-path (`src/catalog/query_terms.py`), unde normalizarea e cunoscută.
--   • NU atinge `product_search_documents` (NX-207) și nu îl aduce în calea lexicală. Acolo e
--     documentul bogat pentru brațul SEMANTIC; unificarea celor două e o decizie de retrieval
--     (NX-238), nu una de schemă.
--
-- O coloană generată nu se poate ALTERA — se scoate și se pune la loc, ca în 033. Rescrie
-- tabelul: 2.758 de rânduri, sub o secundă.
--
-- ROLLBACK (revine exact la definiția din 033):
--   drop index if exists idx_products_search_tsv;
--   alter table products drop column if exists search_tsv;
--   alter table products add column search_tsv tsvector generated always as (
--     to_tsvector('simple', ro_unaccent(coalesce(name, '') || ' ' || coalesce(ai_summary, '')))
--   ) stored;
--   create index idx_products_search_tsv on products using gin (search_tsv);
-- ============================================================================

drop index if exists idx_products_search_tsv;
alter table products drop column if exists search_tsv;

alter table products
  add column search_tsv tsvector
  generated always as (
    setweight(to_tsvector('simple', ro_unaccent(coalesce(name, ''))), 'A')
    || setweight(to_tsvector('simple', ro_unaccent(coalesce(ai_summary, ''))), 'B')
    || setweight(to_tsvector('simple', ro_unaccent(coalesce(description, ''))), 'C')
  ) stored;

create index idx_products_search_tsv on products using gin (search_tsv);

comment on column products.search_tsv is
  '046: nume (A) + ai_summary (B) + description (C), normalizate cu ro_unaccent (033). '
  'Ponderile nu sunt cosmetice: fără ele, o mențiune în descriere ar rangui la fel cu numele '
  'produsului. Interogarea se construiește în src/catalog/query_terms.py, care presupune '
  'ACEEAȘI normalizare — dacă schimbi un capăt, potrivirea nu se mai produce.';
