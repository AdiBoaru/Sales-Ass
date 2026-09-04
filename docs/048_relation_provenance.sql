-- ============================================================================
-- 048 — NX-270: graful de relații capătă PROVENANCE, iar vocabularul de `kind`
--        se deschide pentru `variant_of`
-- ----------------------------------------------------------------------------
-- `product_relations` are azi 0 rânduri, iar codul de traversare e complet și generic
-- (`traverse_relations`, `walk_chain`, cele patru tipuri din pachet). Graful e o mașină fără
-- combustibil: 391 de produse epuizate n-au niciun substitut, deși 381 dintre ele au unul în stoc,
-- în aceeași categorie și în ±30% preț. „Nu mai avem" e răspunsul final pentru toate.
--
-- Migrarea asta face DOUĂ lucruri, amândouă necesare înainte ca jobul să poată scrie o muchie.
--
-- 1. PROVENANCE PE MUCHIE (`source`, `rule_id`, `reason`)
--
--    Motivul nu e igienă. La scară, muchia valoroasă nu e „aceste două produse se aseamănă ca
--    text", ci „oamenii care s-au uitat la ăsta au cumpărat pe ălălalt". Graful COMPORTAMENTAL bate
--    graful de conținut, iar în ziua în care există trafic trebuie să-l poată ÎNLOCUI fără
--    rescriere: aceeași tabelă, aceeași formă de muchie, alt `source`. Graful derivat din conținut e
--    SCHELĂ; dacă îl proiectăm ca permanent, va fi aruncat cu totul.
--
--    `reason` e afișabil clientului („aceeași nevoie, aceeași categorie, preț apropiat"). O muchie
--    face o AFIRMAȚIE implicită — „astea două sunt alternative" — pe care nicio poartă de adevăr
--    din aval n-o verifică: validatorul stagiului 8 verifică prețul, `grounding_guard` (NX-240)
--    verifică afirmațiile din text, iar muchia nu e nici preț, nici text. Motivul scris pe ea e
--    singurul loc unde greșeala mai poate fi văzută de un om.
--
-- 2. CHECK-UL DE `kind` PRIMEȘTE `variant_of`
--
--    NX-269 derivă grupuri de nuanță; muchia „alte nuanțe ale aceluiași produs" e felul ieftin și
--    REVERSIBIL de a le lega, în locul unei restructurări de catalog pe produs-părinte.
--
--    Fără extinderea asta, un insert cu `variant_of` crapă cu `CheckViolationError` — **exact clasa
--    de defect a lui `messages.content_type = 'action'`** (NX-236), care a stat ascunsă până a rulat
--    gate-ul E2E pe Postgres real, fiindcă flagul era stins și suitele foloseau monkeypatch. Ordinea
--    contează: schema întâi, jobul după.
--
-- Expand-only. Nu atinge rândurile existente (nu sunt), nu schimbă indexuri, nu schimbă grant-uri:
-- botul citește, joburile admin scriu (ca la 027).
--
-- ROLLBACK:
--   alter table product_relations drop column if exists reason;
--   alter table product_relations drop column if exists rule_id;
--   alter table product_relations drop column if exists source;
--   alter table product_relations drop constraint if exists product_relations_kind_check;
--   alter table product_relations add constraint product_relations_kind_check
--     check (kind in ('substitute', 'complement', 'accessory', 'routine_next'));
-- ============================================================================

alter table product_relations add column if not exists source  text;
alter table product_relations add column if not exists rule_id text;
alter table product_relations add column if not exists reason  jsonb;

-- De ce nu `not null`: tabelul e gol azi, dar o migrare care presupune asta ar crăpa pe orice mediu
-- unde nu e (seed vechi, restore parțial). Nevidul se impune la SCRIERE, în job, unde eșecul e
-- reparabil, nu la migrare, unde ar bloca deploy-ul. Contractul rămâne verificat de test.
comment on column product_relations.source is
  'De unde vine muchia: derived_content (azi) | behavioral (cand exista trafic) | merchant. '
  'Graful de continut e schela; cel comportamental il va inlocui pe aceeasi forma de rand.';
comment on column product_relations.rule_id is
  'Regula VERSIONATA care a produs muchia. „Regula R s-a dovedit gresita" trebuie sa se poata '
  'repara global (delete where rule_id = ...), nu muchie cu muchie.';
comment on column product_relations.reason is
  'Motivul AFISABIL, structurat: {shared_needs, same_category, price_delta_pct, ...}. O muchie '
  'face o afirmatie pe care nicio poarta de adevar din aval n-o verifica.';

-- Regenerarea globală a unei reguli citește pe `rule_id`; fără index, ștergerea unei reguli greșite
-- pe un catalog mare e un seq scan pe toată tabela.
create index if not exists product_relations_rule_idx
  on product_relations (business_id, rule_id);

-- Vocabularul de `kind`: se adaugă `variant_of`. Numele constrângerii e cel generat de Postgres la
-- 027 (`<tabela>_<coloana>_check`); `if exists` ține migrarea idempotentă și pe medii unde a fost
-- deja rulată.
alter table product_relations drop constraint if exists product_relations_kind_check;
alter table product_relations add constraint product_relations_kind_check
  check (kind in ('substitute', 'complement', 'accessory', 'routine_next', 'variant_of'));
