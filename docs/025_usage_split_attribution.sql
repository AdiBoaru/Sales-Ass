-- NX-162 (Funnel Truth): split atribuire bot-led (direct_bot) vs assisted în usage_daily.
-- Rollup-ul colapsează azi totul într-o singură pereche orders_attributed/revenue_attributed;
-- cumpărătorul citește bot-led (faptul dur) și assisted (cifra moale) SEPARAT — NICIODATĂ
-- însumate (dublă numărare). Coloanele se populează din orders.attribution în _ROLLUP_SQL
-- (FILTER pe attribution). Aditiv + idempotent; default 0 → rândurile vechi rămân consistente
-- (nu NULL). Perechea agregată orders_attributed/revenue_attributed rămâne pentru back-compat.

alter table usage_daily
  add column if not exists orders_direct_bot   integer       not null default 0,
  add column if not exists revenue_direct_bot  numeric(14,2) not null default 0,
  add column if not exists orders_assisted     integer       not null default 0,
  add column if not exists revenue_assisted    numeric(14,2) not null default 0;
