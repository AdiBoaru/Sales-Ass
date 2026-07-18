-- ============================================================================
-- NX-189 (typed facets în SQL) — index GIN pe `products.attributes` (jsonb).
--
-- Fațetele tipizate (fragrance_free, finish, coverage, key_ingredients) trăiesc în
-- `products.attributes` (jsonb). Ca fațetele să PARTICIPE în retrieval (prerechizit
-- de candidate-recall pentru enforce-ul din NX-188 — altfel `MAX_SEARCH_POOL=24` dă
-- false-negative), filtrarea tri-state pe atribute trebuie să fie EFICIENTĂ. Un index
-- GIN pe attributes deblochează `attributes @> '{...}'` și `attributes ? 'key'` fără
-- scan secvențial. Additiv, idempotent — NU schimbă comportamentul (doar pregătește;
-- filtrarea propriu-zisă e gated de `typed_facet_sql_enabled`, activată per fațetă
-- matură DUPĂ paritate shadow + recall verzi).
--
-- `jsonb_path_ops` = variantă GIN mai mică/rapidă pt operatorul `@>` (containment),
-- suficientă pentru filtrele de fațetă (nu avem nevoie de existența cheii `?`).
-- ============================================================================

create index if not exists idx_products_attributes_gin
  on products using gin (attributes jsonb_path_ops);
