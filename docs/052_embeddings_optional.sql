-- ============================================================================
-- 052 — embeddings scoase din proiect: coloanele vectoriale devin OPȚIONALE (expand)
-- ----------------------------------------------------------------------------
-- CONTEXT
--   Decizie 2026-09-24: proiectul nu mai folosește `text-embedding-3-small`. Măsurat pe 30 de
--   zile (`sole-ro`) înainte de decizie: stratul FAQ pe vectori servise 0 răspunsuri din 102,
--   cache-ul semantic 0 din 92 (singurul hit al perioadei fusese EXACT, pe hash), unealta
--   `faq_lookup` 0 apeluri, iar căutarea vectorială de produse era deja oprită din 2026-09-08.
--   Fiecare tur plătea totuși 1-2 apeluri de rețea pentru embedding.
--
--   Codul nou scrie în `semantic_cache` rânduri FĂRĂ vector (potrivire doar exactă, pe
--   `canonical_hash`), iar `faq_lookup` citește FAQ-ul fără vector. Coloanele erau însă
--   `NOT NULL`, deci scrierea ar fi eșuat: migrarea le face opționale.
--
-- DE CE EXPAND, NU CONTRACT
--   Imaginea de dinainte scrie ÎN CONTINUARE vectori; cu migrarea asta aplicată ea funcționează
--   neschimbat, deci rollback-ul rămâne posibil. Ștergerea tabelelor și a coloanelor
--   (`product_embeddings`, `*.embedding`, indexurile HNSW) e o migrare de CONTRACT separată,
--   după ce imaginea fără embeddings a rulat în producție: altfel un rollback ar ateriza pe o
--   schemă în care imaginea veche nu mai poate porni.
--
-- Idempotentă: `drop not null` pe o coloană deja opțională e no-op.
-- ============================================================================

alter table semantic_cache
  alter column embedding drop not null,
  alter column embedding_model drop not null;

alter table faqs
  alter column embedding_model drop not null;

comment on column semantic_cache.embedding is
  '052: NULL pe rândurile scrise după 2026-09-24 (potrivire doar exactă); de șters la contract';
comment on column faqs.embedding_model is
  '052: opțional; FAQ-ul nu se mai caută pe vectori (unealta faq_lookup aduce setul activ)';
