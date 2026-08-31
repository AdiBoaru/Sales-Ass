-- ============================================================================
-- 047 — `product_variants.stock` nu putea fi UNKNOWN, deci mințea cu 0
-- ----------------------------------------------------------------------------
-- Asimetria, măsurată pe schema live:
--
--     products.stock_total       integer      NULL permis, fără default
--     product_variants.stock     integer      NOT NULL, DEFAULT 0     ← aici
--
-- Regula proiectului e „UNKNOWN nu e 0", iar la nivel de PRODUS schema o permite. La nivel de
-- VARIANTĂ n-o permitea, deci un importator care nu cunoaște cantitatea nu avea cum să spună
-- asta: singura valoare pe care o putea scrie era o cifră, iar cifra implicită era zero.
--
-- Consecința pe catalogul SOLE (sursa dă doar binar `in stoc` / `stoc epuizat`, fără cantitate):
-- toate cele 2.755 de variante au primit `stock = 0`. `src/commerce/facts_provider.py` tratează
-- un stoc CUNOSCUT 0 pe variantă drept `out_of_stock` pentru acea variantă — „faptul mai specific
-- bate faptul produsului", care e regula CORECTĂ. Rezultatul: **2.364 din cele 2.367 de produse
-- `in_stock` se prezentau ca epuizate** revalidării de coș (NX-237) și faptelor turului (NX-240).
-- Cu `CONVERSATION_CART_ENABLED` aprins, fiecare adăugare în coș ar fi fost respinsă, pe aproape
-- tot catalogul.
--
-- CODUL era deja scris pentru UNKNOWN: `evidence_bundle` verifică `stock is None` înainte de a
-- citi varianta, `facts_provider` intră pe ramura de stoc doar la `is not None`, iar `fallbacks`
-- omite câmpul când lipsește. Nimic nu presupune non-null. Constrângerea era singurul loc care
-- forța o minciună.
--
-- CE NU FACE migrarea: **nu atinge datele.** Un `update ... set stock = null where stock = 0`
-- global ar distruge informație adevărată la orice tenant unde zero înseamnă chiar „epuizat, și
-- știm asta". Corecția rândurilor fabricate e scoped pe importul care le-a fabricat (varianta
-- sintetică `label='Standard'` a tenantului `sole-ro`) și se face separat, nu dintr-o migrare de
-- schemă care rulează la toți.
--
-- Sursa nu se mai poate întoarce: `scripts/import_sole.py` scrie acum `null` și include `stock`
-- în `do update`, deci o re-rulare repară rândurile vechi în loc să le păstreze.
--
-- ROLLBACK (numai dacă nicio variantă n-are `stock` NULL — altfel `set not null` eșuează, exact
-- cum trebuie):
--   alter table product_variants alter column stock set default 0;
--   alter table product_variants alter column stock set not null;
-- ============================================================================

alter table product_variants alter column stock drop not null;

-- Default-ul pleacă odată cu constrângerea: un `insert` care omite coloana înseamnă „nu știu",
-- nu „zero". Cu default-ul rămas, prima cale de scriere care uită coloana ar reintroduce exact
-- defectul pe care migrarea îl repară.
alter table product_variants alter column stock drop default;

comment on column product_variants.stock is
  '047: cantitate pe variantă. NULL = UNKNOWN (sursa nu dă cantitate), 0 = epuizat CUNOSCUT. '
  'Distincția e citită de facts_provider (NX-237) și evidence_bundle (NX-240): un 0 fabricat '
  'transformă tot catalogul în „nu mai avem". Nu pune default aici.';
