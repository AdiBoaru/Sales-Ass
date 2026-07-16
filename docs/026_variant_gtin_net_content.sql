-- NX-171a — coloane comerciale standard pe product_variants: GTIN, cantitate netă (gramaj) și
-- imagine proprie de variantă, + preț/unitate DERIVAT (preț/100ml, preț/100g). Faptele de vertical
-- rămân în jsonb; astea sunt cross-vertical (volum ml/l, masă g/kg, bucăți) → coloane. Sursa de
-- adevăr comercială e VARIANTA (o variantă de 30ml și una de 50ml au preț/unitate diferit).
-- Aditiv + idempotent (add column if not exists); rândurile vechi rămân valide (NULL = necunoscut).
--
-- price_per_unit = GENERATED STORED: preț efectiv (sale_price ori price) / cantitatea în unitatea de
-- BAZĂ a dimensiunii, ×100. Volum → bază ml (l×1000); masă → bază g (kg×1000); `buc` (ori unitate
-- necunoscută / cantitate ≤0) → NULL (nu există preț/unitate pt o unitate necantitativă, ex. kW).
--
-- ROLLBACK:
--   alter table product_variants
--     drop column if exists price_per_unit,
--     drop column if exists gtin,
--     drop column if exists net_content_value,
--     drop column if exists net_content_unit,
--     drop column if exists image_url;

alter table product_variants
  add column if not exists gtin              text,
  add column if not exists net_content_value numeric,
  add column if not exists net_content_unit  text,
  add column if not exists image_url         text;

alter table product_variants
  add column if not exists price_per_unit numeric generated always as (
    case
      when net_content_value is null or net_content_value <= 0 then null
      when net_content_unit in ('ml', 'l') then
        round(coalesce(sale_price, price)
              / (net_content_value * (case net_content_unit when 'l' then 1000 else 1 end)) * 100, 2)
      when net_content_unit in ('g', 'kg') then
        round(coalesce(sale_price, price)
              / (net_content_value * (case net_content_unit when 'kg' then 1000 else 1 end)) * 100, 2)
      else null
    end
  ) stored;

-- unitatea de cantitate netă e dintr-un set canonic (validare la nivel de check — permite NULL).
alter table product_variants
  drop constraint if exists product_variants_net_content_unit_chk;
alter table product_variants
  add constraint product_variants_net_content_unit_chk
  check (net_content_unit is null or net_content_unit in ('ml', 'l', 'g', 'kg', 'buc'));
