# NX-202 — Audit golden set existent (52 cazuri + 11 conversații)

**Status:** DRAFT audit (pasul 1 din NX-202) — spre validare Adi + Codex · **Data:** 2026-07-23
**Card:** [tasks/NX-202.md](../tasks/NX-202.md) · **ADR:** [QUALITY-OVERHAUL-2026](QUALITY-OVERHAUL-2026.md) (D3, D15)

Metrul de măsură conversațional NU pornește de la zero: repo-ul are deja `tests/golden/cases.json`
(52) + `tests/golden/conversations.json` (11). Acest audit clasifică fiecare (KEEP / LEGACY /
REWRITE / HOLDOUT / UNIT) și trage concluziile pentru extindere. **Nicio modificare de test încă** —
întâi validăm clasificarea, apoi acționăm.

## Constatări structurale (importante înainte de clasificare)

1. **Cazurile sunt HERMETICE, nu rupte de catalogul nou.** `expected_product_ids` (p10, p11, …)
   sunt ID-uri din `fixtures.catalog` PROPRIU al fiecărui caz, nu UUID-uri din DB. Design G8-1:
   ScriptedLLM + stub-uri DB, zero OpenAI/DB real în CI. → Catalogul de 300 produse (#238) **nu
   invalidează** niciun caz. Bun.
2. **Fixture-urile codează contractul VECHI de tool.** `tool_calls: [["search_products", {query,
   price_max, limit}]]` + `catalog[].ai_summary` + `final`. Când arhitectura trece la
   `search_entities` (NX-209, output `constraint_results`/`match_class`) + `search_document`
   (NX-207), **forma** acestor fixture-uri devine stale — deși **intenția** (recomandare grounded)
   rămâne exact ce vrem. → clasa REWRITE, nu LEGACY.
3. **Niciun caz existent nu poate servi ca HOLDOUT curat.** Cele 52+11 sunt DEJA gate-ul CI curent
   → au fost văzute în dezvoltare. Holdout-ul (D15, split-ul NX-203-style pentru conversații)
   trebuie construit din cazuri NOI, niciodată-văzute. Nu se poate „promova" un caz vechi în holdout.
4. **Nimic pur UNIT, nimic de aruncat (LEGACY-discard).** Toate sunt cazuri de tur/conversație
   potrivite pentru golden; niciunul nu testează un pipeline mort care ar trebui șters.

## Clasificarea (52 cazuri)

Trei axe: **bucket** (KEEP/REWRITE) · **rol** (ro-quality-set = măsoară calitatea recomandării ·
safety-grounding-regression = gardă independentă de arhitectură · locale-regression = nucleu
locale-aware D3, dar NU în setul ro de calitate).

### KEEP — safety & grounding regression (independent de arhitectură) — 24 (22 ro-aplicabile + 2 locale)
Testează exact ce garantează validatorul + AnswerPlan pe arhitectura țintă (preț/link/produs/stoc
neinventate, ton neutru, rezistență la injection, niciodată-tăcere). `ai_summary` în fixture e
incidental — testul e despre grounding, nu despre forma documentului.
`moderation-neutral`, `risk-handoff`, `invented-price-blocked`, `prose-claim-scrubbed`,
`injection-fake-discount-blocked`, `injection-fake-link-blocked`, `injection-toxic-stays-neutral`,
`injection-legal-threat-handoff`, `injection-ignore-instructions-stays-grounded`,
`adversarial-price-one-leu`, `adversarial-fake-checkout-link`, `adversarial-fake-product`,
`adversarial-fake-stock-count`, `adversarial-fake-policy-price`, `adversarial-competitor-link`,
`adversarial-admin-override`, `adversarial-system-role-price`, `sales-no-result-specific`,
`sales-no-result-brand`, `order-never-silent`, `order-status-generic`, `order-awb`.
> `adversarial-en-fake-sale`, `adversarial-hu-fake-link` → aceeași gardă, dar **locale-regression**.

### KEEP — fast-path & clarify (valide pe țintă) — 15 (11 ro-quality-set + 4 locale)
Simple/greeting = teritoriu fast-path (D2); clarify = teritoriu NX-212 (intent valid, se
îmbogățește, nu se rescrie).
`greeting-simple`, `simple-thanks`, `simple-ok-perfect`, `simple-are-you-there`,
`simple-bot-identity`, `simple-short-ack`, `simple-politeness`, `clarify-vague-gift`,
`clarify-vague-something`, `clarify-budget-only`, `clarify-low-confidence-sales`.
> `simple-en-thanks`, `simple-hu-thanks`, `clarify-en-vague`, `clarify-hu-vague` → **locale-regression**.

### REWRITE — recomandare grounded (forma pe contract vechi) — 13 (11 ro-quality-set + 2 locale)
Inima setului de calitate. Intenție excelentă (recomandare pe atribut + `expected_product_ids` +
`expected_constraints`); forma se rescrie la NX-209/NX-207 (fixture pe `search_entities` +
`search_document`, `constraint_results` în loc de `tool_calls`/`ai_summary`). **Rulează neschimbate
până atunci.**
`sales-grounded`, `sales-face-cream-budget`, `sales-serum-vitamin-c`, `sales-shampoo-oily-hair`,
`sales-spf`, `sales-perfume`, `sales-lip-balm`, `sales-eye-cream`, `sales-cleanser`, `sales-toner`,
`sales-body-lotion`.
> `sales-en-face-cream`, `sales-hu-face-cream` → REWRITE + **locale-regression**.

## Clasificarea (11 conversații)

- **REWRITE, ro-quality-set** (multi-tur sales: carry state, topic-switch, refine, cheaper-link) —
  `conv-greeting-then-sales`, `conv-sales-refine-carries-category`,
  `conv-sales-topic-switch-resets-constraints`, `conv-greeting-sales-refine-3turn`,
  `conv-sales-cheaper-link-3turn`. Cele mai valoroase pt arhitectura țintă (needs_profile cumulativ,
  clarify, ancorare) — dar pe contract vechi de tool.
- **KEEP, safety-grounding** (grounding de-a lungul turelor) — `conv-sales-then-injection-price-blocked`,
  `conv-sales-no-result-no-invention`, `conv-sales-then-invented-product-blocked`.
- **KEEP, ro-quality-set** — `conv-order-then-thanks`.
- **REWRITE, locale-regression** — `conv-hu-sales-refine`, `conv-en-sales-refine`.

## Sinteză

| Bucket | Cazuri (din 52) | Conversații (din 11) |
|---|---|---|
| KEEP — safety/grounding regression | 24 (22 + 2 locale) | 3 |
| KEEP — fast-path/clarify/order | 15 (11 + 4 locale) | 1 |
| REWRITE (la NX-207/209) | 13 (11 + 2 locale) | 7 |
| LEGACY-discard / UNIT / HOLDOUT | 0 | 0 |
| **Total** | **52** | **11** |

Partiție verificată programatic: 52 cazuri, zero neclasificate, zero suprapuneri.

## Gap analysis — ce LIPSEȘTE (input pentru extindere)

Comparat cu compoziția-țintă din card (10 simple · 15 recomandări · **15 query-uri dificile** ·
5 comparații · 5 acțiuni/follow-up) + cele 12 scenarii NX-172 de absorbit:

1. **Query-uri dificile / compuse — golul MARE.** Cazurile sales actuale sunt toate mono-atribut
   („crema de fata sub 90"). Lipsește clasa colocvial-compusă („ceva să nu mă lucesc în avion,
   fără parfum, sub 100") — exact clasa pe care arhitectura nouă țintește cel mai mult câștig.
   ~0 din 15 acoperite.
2. **Comparații — absente.** Niciun caz `compare` (2-3 produse, ≥2 diferențe reale). 0 din 5.
3. **Cele 12 scenarii NX-172** (ten gras, ten sensibil, ingredient cerut, fără parfum, gramaj,
   utilizare, finish mat, nuanță, comparație, alternativă mai ieftină, rutină, contraindicație) —
   de absorbit (checker-ele: off-category, invented-facts, ≥2 diferențe compare, motiv concret).
4. **Contraindicații/safety-de-produs** (sarcină/retinoizi — NX-173) — subacoperit în golden.
5. **Holdout curat** — de construit din cazuri NOI (constatarea 3), nu din cele 52+11.

## Pași următori (după validarea acestei clasificări)
1. Adi validează axele (mai ales: en/hu = locale-regression nu ro-quality; REWRITE-timing la NX-209).
2. Reetichetare în `cases.json`/`conversations.json`: tag `bucket` + `role` per caz (metadata, fără
   a schimba aserțiunile — zero risc pe gate-ul CI).
3. Extindere pe gap-uri (1-4), cu etichetare Adi pe „răspunsul corect".
4. Holdout NOU (5), sigilat, deschis doar la gate-uri majore.
5. Baseline sistem actual pe setul rezultat (scoruri per dimensiune).
