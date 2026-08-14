# NX-240 — Grounding strict și projector pur `web-view.v2`

**Flag:** `WEB_VIEW_V2_PROJECTOR_ENABLED` (default `false`) · cere `WEB_TURN_V2_ENABLED` +
`SINGLE_BRAIN_ENABLED` (validat la boot) · **producția rămâne OFF** până la gate-ul NX-246.

---

## Ce se schimbă, în două propoziții

Până acum, ViewModel-ul v2 se **deriva** din payload-ul v1 persistat (NX-233): un text deja
compus, niște carduri, iar proiecția recalcula prețuri și reduceri din ele. Acum turul îngheață
**faptele** (`EvidenceBundle`), le trece printr-o poartă care confruntă fiecare afirmație cu ele
(`GroundingGuard`), și un projector **pur** produce envelope-ul complet display-ready din
verdictul înghețat.

Diferența nu e de arhitectură, e de adevăr: în varianta veche, „ce scrie în text" și „ce arată
cardurile" erau două lucruri care se puteau contrazice fără ca nimic să observe.

---

## Traseul

```
MainBrain (NX-239)  →  AnswerPlanV2 validat (evidence IDs, tenant, hard constraints)
        │
        ├── build_evidence_bundle(rândurile retrievate, now, sla)   ← ZERO I/O propriu
        │      fapte typed: known | unknown(reason) | stale(age, sla), cu sursă
        │
        ├── ground_answer(plan, bundle)                              ← poarta de adevăr
        │      failures  → răspunsul NU se livrează (fallback determinist, P6)
        │      omissions → răspunsul se livrează mai sărac (+ telemetrie)
        │
        └── ctx.grounded → CommitFacts → `response_json["grounded_v2"]`
                                          (aceeași tranzacție cu rezultatul)
                                              │
GET / SSE  →  terminal_view  →  render_v2.project(verdict înghețat, acțiuni, as_of)
                                    funcție PURĂ → `WebViewV2` → `parse_view`
```

**Se persistă verdictul, nu intrarea lui.** Dacă am persista planul + bundle-ul și am re-rula
guardul la fiecare citire, un kill-switch rotit între timp ar putea transforma un răspuns deja
livrat într-un eșec. Verdictul e o decizie luată o dată, în tranzacția terminală.

---

## Cele patru module

| modul | ce face | ce NU face |
|---|---|---|
| `src/web/localization.py` | `Decimal` → text localizat, plural CLDR, copy server-owned | nu decide dacă un câmp are voie să apară |
| `src/agent/evidence_bundle.py` | fapte cu proveniență + prospețime, serializabile | nu citește DB (primește rânduri) |
| `src/agent/grounding_guard.py` | confruntă afirmațiile cu faptele | nu corectează un fapt greșit — respinge |
| `src/channels/web/render_v2.py` | verdict → `WebViewV2` display-ready | nu are I/O, ceas, config sau `await` |

---

## Reguli care schimbă comportamentul

**1. Nu există `float` pe sârmă.** Prețul e `"89,00 lei"`, reducerea `"-25%"`, stocul
`"Ultimele 3 bucăți"`, cantitatea `"2 buc."`. Frontendul nu poate calcula ce nu are — regula e
verificată structural pe payload (`evals/web_response.passive_boundary_failures`), nu prin review.

**2. Reducerea se rotunjește în JOS.** 25,83% se afișează `-25%`, nu `-26%`. O ofertă afișată mai
mare decât cea reală e o afirmație pe care faptele n-o susțin; sub adevăr e o alegere sigură.
Evaluatorul aplică regula asimetric: `claimed > actual` e eșec.

**3. Livrarea, promoțiile, voucherele și garanția resping răspunsul.** Nu există adaptor canonic
pentru niciunul, deci orice formulare din familiile astea e falsă prin construcție — nu „aproape
adevărată". Codurile: `unsourced_delivery_claim`, `unsourced_promo_claim`,
`unsourced_warranty_claim`.

**4. `check_claims` din NX-117 nu se moștenește — se înlocuiește cu ceva mai strict.** NX-117
interzice orice pomenire de „reducere"/„recenzii"/„livrare" fiindcă pe calea de proză nu are cum
să le verifice. Aici avem cum: procentul se recalculează din două prețuri în aceeași monedă,
stocul se cere dintr-un fapt de disponibilitate, livrarea/promoția n-au sursă deloc. Superlativul
rămâne respins (nu există fapt „cel mai bun"). Un bot care nu poate spune „are 120 de recenzii"
despre un produs cu 120 de recenzii e mai prost, nu mai sigur.

**5. Motivele cad singure.** Un motiv de recomandare cu superlativ sau cifră nefondată ȘTERGE
MOTIVUL, nu răspunsul: sancțiunea e la fel de locală ca greșeala. Restul prozei (răspuns,
disclosures, claims, clarificare) e fatal.

**6. Hard MISMATCH scoate produsul de peste tot** — card, comparație, acțiuni. UNKNOWN nu scoate
nimic: se declară, nu se ascunde (D7).

**7. Un plan care selecta produse și n-a rămas cu niciunul e respins** (`no_renderable_product`).
Textul ar fi rămas vorbind despre ce nu se arată.

---

## CTA de comerț (NX-237 → NX-240)

NX-237 a dat comerțului handler (`CartService` + receipt idempotent). NX-240 îi dă **condiție de
emitere**, în trei porți independente:

1. `GroundingGuard` marchează `commerce_allowed` doar pentru produse cu identitate, preț și
   disponibilitate `known` (nu expirate) și fără contrazicere hard;
2. `plan_actions` planifică `cart_add_line` doar pentru intersecția „afișat ∧ vandabil", iar
   `checkout` doar peste un coș declarat eligibil de `CartService`. **Fără plan persistat nu
   există token** — deci o rotire de flag nu poate reînvia butoane emise ieri;
3. `authorize_action` refuză oricum mutantele cu `CONVERSATION_CART_ENABLED` stins.

`cart_set_quantity` / `cart_remove` / `cart_clear` au handler la consum dar rămân **neemise**: nu
există încă un loc în ViewModel din care să pornească (controale de linie — NX-244).

---

## Ce rămâne neatins

- **v1 e byte-identic.** `/web/chat`, `render_web`, `docs/FRONTEND-CONTRACT-IZI.md` — nimic
  modificat. Cutoverul e NX-249.
- **Proiecția NX-233 rămâne fallback-ul.** Un rând fără `grounded_v2` (scris înainte de card, sau
  de un tur care n-a trecut prin MainBrain) se randează exact ca înainte. Migrarea e LAZY.
- **Cu flagul stins nu se scrie și nu se citește nimic nou** — verificat în
  `tests/test_web_view_v2_ledger.py`.
- **Zero migrare.** Verdictul intră aditiv în `response_json`, ca `actions` la NX-236.

---

## Observabilitate

`evidence_bundle{outcome,product_bucket,source_coverage_bucket}` ·
`evidence_query_count_bucket` · `commercial_fact{field,status,freshness_bucket}` ·
`grounding_claim{type,outcome,reason}` · `view_field_omitted{field,reason}` ·
`commerce_cta_omitted{reason}`

Toate low-cardinality: vocabulare închise și benzi, zero id-uri de produs, zero sume, zero text.

---

## Cum se verifică

```bash
python -m pytest tests/test_evidence_bundle.py tests/test_grounding_guard_v2.py \
                 tests/test_web_render_v2.py tests/test_web_view_v2_golden.py \
                 tests/test_web_view_v2_ledger.py tests/test_web_data_readiness.py \
                 tests/test_web_localization.py -q

python scripts/nx240_data_readiness.py            # coverage real pe tenantul demo
NX240_UPDATE_GOLDEN=1 python -m pytest tests/test_web_view_v2_golden.py   # regenerare goldens
```

Data readiness completă: [`docs/WEB-VIEW-V2-DATA-READINESS.md`](WEB-VIEW-V2-DATA-READINESS.md).
