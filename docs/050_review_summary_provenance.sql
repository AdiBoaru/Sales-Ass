-- ============================================================================
-- 050 — NX-279: rezumatul de recenzii capătă PROVENANCE (`rule_id`, `evidence`, `locale`)
-- ----------------------------------------------------------------------------
-- `product_review_summaries` are 0 rânduri pe catalogul SOLE, deși `reviews` are 183.003 de
-- recenzii reale pe 2.743 din 2.758 de produse. Singurul producător existent
-- (`scripts/summarize_reviews.py`, acum `scripts/archive/summarize_reviews_demo.py`) nu citea
-- tabela `reviews` deloc: inventa cu un model ce AR spune clienții, pornind de la numele produsului,
-- și rescria `products.rating` cu o variație inventată. Pe date reale ar fi fost falsificare.
--
-- Jobul nou (`scripts/derive_review_summaries.py`) derivă rândul DETERMINIST: teme din vocabularul
-- tenantului (`domain_pack.review_themes`), numărate ca recenzii distincte, praguri explicite,
-- text din șablon localizat. Consumatorii (`evidence_bundle`, `finalize`, `_review_answer`,
-- `context_resolver`) tratează rândul ca SURSĂ DE ADEVĂR — validatorul și `grounding_guard`
-- verifică afirmațiile FAȚĂ DE el —, deci rândul trebuie să poată spune CINE l-a produs și DIN CE.
--
-- CE ADAUGĂ (expand-only, fără backfill, zero schimbare de comportament pentru cititori):
--
--   rule_id   — regula + versiunea vocabularului care a produs rândul (`review_themes.v1:<ver>`).
--               Regenerarea se decide pe el ȘI pe `review_count_at_build` (care exista deja, dar
--               singur nu spune ce regulă a rulat). NULL = rând scris de un producător care nu
--               declară provenance (rândurile demo istorice).
--   evidence  — temele numărate, cu recenzii distincte, cotă și id-uri de dovadă, plus ce s-a
--               numărat dar NU s-a afișat și de ce (`claim_unratified`, `below_min_share`…).
--               Un rând fără evidence e o afirmație; cu evidence e o afirmație verificabilă.
--   locale    — limba etichetelor și a rezumatului. Textul e localizat la build (P11), iar un
--               cititor care servește altă limbă trebuie să poată vedea că rândul nu e al lui.
--
-- Nicio schimbare pe grants: `bot_runtime` avea SELECT (003) și îl păstrează; scrierea rămâne
-- exclusiv a jobului offline, pe `admin_conn`, ca la NX-268/270.
-- ============================================================================

alter table product_review_summaries
  add column if not exists rule_id  text,
  add column if not exists evidence jsonb not null default '{}'::jsonb,
  add column if not exists locale   text;

comment on column product_review_summaries.rule_id is
  'NX-279: regula + versiunea vocabularului care a produs rândul; NULL = producător fără provenance';
comment on column product_review_summaries.evidence is
  'NX-279: teme numărate (recenzii distincte, cotă, id-uri de dovadă) + ce s-a reținut și de ce';
comment on column product_review_summaries.locale is
  'NX-279: limba rezumatului și a etichetelor (textul e localizat la build)';
