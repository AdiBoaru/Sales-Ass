# NX-202 — Audit golden set existent (62 cazuri + 13 conversații)

**Status:** DRAFT audit rev.2 (pasul 1 din NX-202) — spre validare Adi + Codex · **Data:** 2026-07-23
**Card:** [tasks/NX-202.md](../tasks/NX-202.md) · **ADR:** [QUALITY-OVERHAUL-2026](QUALITY-OVERHAUL-2026.md) (D3, D15)

> **Corecție rev.2:** prima versiune a numărat 52+11, auditând un tree stale (`feat/NX-164`).
> Pe `origin/main` sunt **62 cazuri + 13 conversații** — cele 12 scenarii NX-172 (10 cazuri +
> 2 conversații: ten gras/sensibil, ingredient, fără parfum, gramaj, utilizare, finish mat,
> nuanță, contraindicație, rutină, compare, cheaper) sunt **DEJA prezente**, nu „de absorbit".
> Clasificarea + gap analysis de mai jos sunt corectate contra realității de pe main.

Metrul de măsură conversațional NU pornește de la zero. Acest audit clasifică fiecare caz
(KEEP / REWRITE — nimic LEGACY-discard/UNIT/HOLDOUT) și trage concluziile pentru extindere.
Output machine-readable: [`tests/golden/classification.json`](../tests/golden/classification.json)
(cu guard `tests/test_golden_classification.py` care cere orice caz nou să fie clasificat).
**Aserțiunile golden rămân neatinse** — clasificarea e metadata paralelă, zero risc pe gate-ul CI.

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

### KEEP — safety & grounding regression (independent de arhitectură) — 25 (23 ro-aplicabile + 2 locale)
Testează exact ce garantează validatorul + AnswerPlan pe arhitectura țintă (preț/link/produs/stoc
neinventate, ton neutru, rezistență la injection, niciodată-tăcere). `ai_summary` în fixture e
incidental — testul e despre grounding, nu despre forma documentului.
`moderation-neutral`, `risk-handoff`, `invented-price-blocked`, `prose-claim-scrubbed`,
`injection-fake-discount-blocked`, `injection-fake-link-blocked`, `injection-toxic-stays-neutral`,
`injection-legal-threat-handoff`, `injection-ignore-instructions-stays-grounded`,
`adversarial-price-one-leu`, `adversarial-fake-checkout-link`, `adversarial-fake-product`,
`adversarial-fake-stock-count`, `adversarial-fake-policy-price`, `adversarial-competitor-link`,
`adversarial-admin-override`, `adversarial-system-role-price`, `sales-no-result-specific`,
`sales-no-result-brand`, `order-never-silent`, `order-status-generic`, `order-awb`,
`nx172-contraindicatie`.
> `adversarial-en-fake-sale`, `adversarial-hu-fake-link` → aceeași gardă, dar **locale-regression**.

### KEEP — fast-path & clarify (valide pe țintă) — 15 (11 ro-quality-set + 4 locale)
Simple/greeting = teritoriu fast-path (D2); clarify = teritoriu NX-212 (intent valid, se
îmbogățește, nu se rescrie).
`greeting-simple`, `simple-thanks`, `simple-ok-perfect`, `simple-are-you-there`,
`simple-bot-identity`, `simple-short-ack`, `simple-politeness`, `clarify-vague-gift`,
`clarify-vague-something`, `clarify-budget-only`, `clarify-low-confidence-sales`.
> `simple-en-thanks`, `simple-hu-thanks`, `clarify-en-vague`, `clarify-hu-vague` → **locale-regression**.

### REWRITE — recomandare grounded (forma pe contract vechi) — 22 (20 ro-quality-set + 2 locale)
Inima setului de calitate. Intenție excelentă (recomandare pe atribut + `expected_product_ids` +
`expected_constraints`; cele NX-172 au și `require_reason` = ancorare + `forbidden_categories` =
disciplină off-category — exact contractul arhitecturii țintă). Forma se rescrie la NX-209/NX-207
(fixture pe `search_entities` + `search_document`, `constraint_results` în loc de `tool_calls`/
`ai_summary`). **Rulează neschimbate până atunci.**
`sales-grounded`, `sales-face-cream-budget`, `sales-serum-vitamin-c`, `sales-shampoo-oily-hair`,
`sales-spf`, `sales-perfume`, `sales-lip-balm`, `sales-eye-cream`, `sales-cleanser`, `sales-toner`,
`sales-body-lotion`, `nx172-ten-gras`, `nx172-ten-sensibil`, `nx172-ingredient-niacinamida`,
`nx172-fara-parfum`, `nx172-gramaj`, `nx172-utilizare`, `nx172-fond-mat`, `nx172-nuanta`,
`nx172-rutina-completa`.
> `sales-en-face-cream`, `sales-hu-face-cream` → REWRITE + **locale-regression**.
> `nx172-contraindicatie` → **KEEP safety-grounding** (require_safety_referral + forbidden_product_ids).

## Clasificarea (11 conversații)

- **REWRITE, ro-quality-set** (multi-tur sales: carry state, topic-switch, refine, cheaper-link,
  **compare**) — `conv-greeting-then-sales`, `conv-sales-refine-carries-category`,
  `conv-sales-topic-switch-resets-constraints`, `conv-greeting-sales-refine-3turn`,
  `conv-sales-cheaper-link-3turn`, `nx172-conv-compare-diffs`, `nx172-conv-cheaper-alternative`.
  Cele mai valoroase pt arhitectura țintă (needs_profile cumulativ, clarify, ancorare, comparație)
  — dar pe contract vechi de tool.
- **KEEP, safety-grounding** (grounding de-a lungul turelor) — `conv-sales-then-injection-price-blocked`,
  `conv-sales-no-result-no-invention`, `conv-sales-then-invented-product-blocked`.
- **KEEP, ro-quality-set** — `conv-order-then-thanks`.
- **REWRITE, locale-regression** — `conv-hu-sales-refine`, `conv-en-sales-refine`.

## Sinteză

| Bucket | Cazuri (din 62) | Conversații (din 13) |
|---|---|---|
| KEEP — safety/grounding regression | 25 (23 + 2 locale) | 3 |
| KEEP — fast-path/clarify/order | 15 (11 + 4 locale) | 1 |
| REWRITE (la NX-207/209) | 22 (20 + 2 locale) | 9 |
| LEGACY-discard / UNIT / HOLDOUT | 0 | 0 |
| **Total** | **62** | **13** |

Partiție verificată programatic (`tests/test_golden_classification.py`): 62 cazuri + 13 conversații,
zero neclasificate, zero suprapuneri, zero fantome.

## Gap analysis — ce LIPSEȘTE (input pentru extindere)

Comparat cu compoziția-țintă din card (10 simple · 15 recomandări · **15 query-uri dificile** ·
5 comparații · 5 acțiuni/follow-up). Cele 12 scenarii NX-172 sunt DEJA prezente (corecție rev.2),
deci setul e în formă mai bună decât spunea rev.1. Golurile REALE rămase:

1. **Query-uri COMPUSE (multi-constrângere) — golul principal rămas.** NX-172 adaugă cazuri
   hard mono-concern (ten gras, fără parfum, finish mat), dar clasa colocvial-COMPUSĂ
   („ceva să nu mă lucesc în avion, fără parfum, sub 100" — 3+ constrângeri simultane) e încă
   ~0. Exact clasa pe care `search_entities` (NX-209, constraint_coverage per-facet) o țintește.
2. **Comparații — subacoperite, nu absente.** Există `nx172-conv-compare-diffs` (1). Ținta e ~5
   → încă ~4 de adăugat (axe diferite: preț/valoare, blândețe, finish).
3. ~~Cele 12 scenarii NX-172~~ — **DEJA PREZENTE** (corecție rev.2). Nu mai e gap.
4. **Contraindicații/safety-de-produs** — `nx172-contraindicatie` acoperă sarcina/antirid (1).
   Extensie utilă: retinoizi, acizi, alăptare (NX-173) — dar nu mai e „0".
5. **Holdout curat** — de construit din cazuri NOI (constatarea 3), nu din cele 62+13 existente.

## Decizii deja luate (Adi, 2026-07-23)
- **DOAR ro.** en/hu = locale-regression ÎNGHEȚAT — nu se extinde, nu intră în setul de calitate
  ro. Aplicat în `classification.json` + guard `test_golden_classification.py`.
- **Warmup + NX-201 baseline AMÂNATE** — nu blochează munca golden ro.

## Pași următori
1. ✅ Clasificare machine-readable (`classification.json`) + guard anti-drift — LIVRAT în acest PR.
2. **Extindere pe golurile reale (1, 2, 4):** query-uri compuse multi-constrângere (~10-15),
   încă ~4 comparații, extensie contraindicații — DRAFT de mine, etichetare Adi pe „răspunsul
   corect". Rămân pe contract vechi până la NX-209 (consistent cu REWRITE).
3. **Holdout NOU** sigilat (constatarea 3), deschis doar la gate-uri majore.
4. **Baseline sistem actual** pe setul ro-quality (scoruri per dimensiune) — după ce extinderea
   dă o masă critică; nu depinde de warmup (retrieval/agent, nu cache).
