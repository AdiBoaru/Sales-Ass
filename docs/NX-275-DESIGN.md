# NX-275 — Design: profile de tur, dovezi pe ancoră, un singur apel

**Status:** DESIGN (nu e build) · **Card:** [`tasks/stage1/NX-275.md`](../tasks/stage1/NX-275.md)
**Scop:** același creier unic, aceeași schemă, aceleași porți de adevăr, dar (a) direcții de
răspuns declarate ca DATE, (b) o dovadă nouă la care creierul nu ajunge azi (graful de relații),
(c) mai puține APELURI de model per tur, (d) promptul așezat pentru cache.

---

## 0. Ce infirmă și ce confirmă măsurătoarea

| afirmație din card | verdict | dovadă |
|---|---|---|
| schema `response_format` NU se cache-uiește (§6) | **FALS** | ghidul OpenAI de prompt caching listează Structured Outputs în prefixul cache-uit; GPT-5.6: prag 1.024 tokeni, tarif 0,1×, TTL 30 min |
| v2 e +31…40% față de v1 pe cache cald | **FALS** | cu schema în prefixul cache-uibil, turul cu carduri e −1% față de v1 (−4% fără shadow nano) |
| schema umflă OUTPUTUL prin `strict` + 22 chei `required` | **ADEVĂRAT, dar mic** | un plan EXACT compact are 219 tokeni, din care 124 sunt schelet fără informație |
| L2 (schemă doar pe terminal) economisește | **FALS** | apelul care produce planul e de obicei al DOILEA apel din buclă (15/15 ture în `reports/nx239/drive.json` au o rundă); L2 l-ar transforma în proză și ar cere un al treilea apel |
| turul de recomandare cere 2 apeluri | **ADEVĂRAT, și e pârghia** | apelul 1 = prefix + un tool call de ~120 tokeni; e un sfert din cost și jumătate din latență |
| creierul poate compune o rutină | **FALS** | cele 30.881 de muchii din `product_relations` sunt citite doar de cross-sell-ul v1 după `cart_add` (`planner._cart_followup_products`); niciun tool nu le expune |

Cifre reproductibile: `python scripts/prompt_budget_probe.py` (tokenii), plus calculul de cost din
§6 al acestui doc, cu tarifele `mini` fiindcă `gpt-5.6-luna` n-are tarife (L4 din card).

---

## 1. Principii

1. **Direcția o decide CODUL, o dată, înainte de orice apel.** Obligațiile extrase din mesaj
   (`brain_models.extract_obligations`) dau clasa de tur (`turn_budget.turn_class_for`). Profilul
   se alege PUR din (clasă, tipuri de obligații). Niciun model nu decide ce fel de tur e.
2. **Un prefix, mai multe sufixe.** System-ul din DB + `_PLAN_V2_SYSTEM` + tool-urile + schema
   rămân byte-identice pe toate direcțiile. Profilul adaugă un sufix de 100-200 de tokeni la
   FINALUL system-ului și cel mult tool-uri ÎN PLUS. Trei prompturi separate ar însemna trei
   surse de adevăr care derivează (motivul pentru care NX-239 le-a unificat) și trei prefixe de
   cache care se încălzesc separat.
3. **Un tool per FEL de dovadă, nu per intenție.** Dovezile de azi: retrieval pe nevoie
   (`search_products`), un produs (`get_product_details`), diferențe între produse
   (`compare_products`), cunoștințe (`faq_lookup`), comandă (`check_order`), mutații. Lipsește
   UNA: vecinii unei ancore în graf. Se adaugă exact aceea.
4. **Nucleul de tool-uri nu se taie niciodată.** Un profil adaugă, nu scade. Un „adaugă în coș"
   pe care regexul nu-l prinde trebuie să aibă unealta. Tokenii nu contează: prefixul e cache-uit.
5. **Forma planului rămâne fixă.** Nu schemă per clasă. Se scoate din ce EMITE modelul doar ce
   știe deja serverul; câmpurile rămân în `AnswerPlanV2`, completate server-side.
6. **Nicio schimbare mare pe speranță (D15).** Fiecare felie are flag OFF = byte-identic și o
   poartă măsurată înainte de a se aprinde.

---

## 2. Turul DUPĂ

```text
mesaj → gates → straturi gratuite → control_plane.decide()
   │
   ▼  agent_stage
   ├─ obligații DETERMINISTE → turn_class (există)
   ├─ [nou] profile = turn_profile.select(turn_class, obligations)      ← PUR, din registru
   │        profile.extra_tools  → tools = nucleu ∪ extra (aditiv)
   │        profile.suffix       → system = db_system + PLAN_V2 + suffix
   │        profile.speculative  → vezi mai jos
   ├─ [L5, card separat] fast path exact → 0 apeluri când entitatea e identificată EXACT
   │
   ▼  run_main_brain
   ├─ [nou] retrieval speculativ (doar profile.speculative):
   │        codul rulează search_products din mesajul brut prin ACELAȘI _PortedExecute
   │        (port NX-238, safety, admission NX-241, run.retrieved) și SEEDUIEȘTE bucla
   │        cu perechea (assistant tool_call, tool result) — modelul „a căutat deja"
   ├─ run_tool_loop_structured(seed=..., prompt_cache_key=...)
   │        apel 1: prefix (cache) + istoric (cache în conversație) + per-tur + candidați → PLAN
   │        (re-search DOAR dacă candidații nu acoperă nevoia)
   ├─ [nou] plan = server_fields ⊕ model_fields   (business_id/locale/schema_version/obligations
   │        NU mai sunt emise de model)
   ├─ validator V2 (neatins) → un repair (neatins) → fallback (neatins)
   ▼
grounding guard → render → Sender → aftercare (triaj shadow EȘANTIONAT)
```

---

## 3. Componente

### 3.1 `src/agent/turn_profile.py` — registrul de profile (DATE, validat la import)

```python
@dataclass(frozen=True, slots=True)
class TurnProfile:
    name: str                      # "exact" | "recommend" | "compare" | "routine" | "mutation"
    extra_tools: tuple[str, ...]   # ADĂUGATE peste nucleu; fiecare ∈ TOOL_REGISTRY ∧ tool_budget.SPECS
    suffix_key: str                # cheia textului de sufix (versionat în BRAIN_PROMPT_VERSION)
    speculative_retrieval: bool    # doar "recommend"
    version: str                   # intră în brain_versions → trace attrs

def select(turn_class: TurnClass, obligations: Iterable[DetectedObligation]) -> TurnProfile:
    """PUR. Precedență: mutation > compare > routine > recommend > exact.
    Necunoscutul urcă (ca la turn_class_for): fără obligații → recommend."""
```

Sufixele trăiesc în același modul, în vocea din `voice.py` (fără liniuțe, fără punct și virgulă),
și sunt verificate la import prin `naturalize(suffix) == suffix`. Un test cere ca reuniunea
`extra_tools` a tuturor profilelor să fie exact `{compare_products, related_products}` și ca
niciun profil să nu scoată ceva din nucleu (`enabled_tools`).

| profil | extra_tools | sufixul spune | speculativ |
|---|---|---|---|
| exact | — | răspunde direct la întrebare; nu recomanda dacă nu ți se cere; `unknowns` onest | nu |
| recommend | — | cel mult 6 produse; o singură clarificare, doar dacă schimbă material rezultatul; candidații din seed sunt o căutare pe mesajul brut: re-caută cu filtre DOAR dacă nu acoperă nevoia | **da** |
| compare | `compare_products` | celulele `comparison` DOAR din axele întoarse de tool; o recomandare doar dacă nevoia e cunoscută (`need_ids`) | nu |
| routine | `related_products` | pași ordonați, un produs per pas, doar cumpărabile; ancora e produsul clientului sau primul rezultat; nu inventa pași fără muchie | nu |
| mutation | — | confirmă DOAR acțiunile din `successful_action_ids`; după coș, propune ce a întors tool-ul, nu ce crezi tu | nu |

**Unde se prinde:** `run_main_brain` calculează deja `turn_class`; adaugă `profile = select(...)`,
`brain_system = f"{system}\n{_PLAN_V2_SYSTEM}\n{profile.suffix}"`, `tools = tools + tool_schemas(
profile.extra_tools, examples)` (dedup pe nume). `brain_versions` primește `profile.version`.
Emite `turn_profile{name}` (low-cardinality).

**Clasa de tur NU se schimbă.** `TurnClass` rămâne cu 4 valori fiindcă manifestul NX-241 e
per clasă (bugete, tier de model). Profilul e ortogonal: `routine` rulează în clasa
RECOMMENDATION (buget 6s), `compare` în COMPLEX. Nu se adaugă o a cincea clasă de buget.

### 3.2 Obligația `routine` (`brain_models`)

Detecție deterministă, în `extract_obligations`, ÎNAINTEA lui `_RECOMMEND_RE`:
`\brutin\w*\b|\bpasi\b|\bpas cu pas\b|\bdiminea\w+\b.*\bsear\w+\b|\bce (?:folosesc|aplic) (?:intai|dupa|prima data)\b|\broutine\b|\bsteps\b`
(fără diacritice, ca restul). Kind `routine`, key `routine`. `turn_class_for` îl tratează ca
`recommend` (nu apare în setul EXACT, deci cade pe RECOMMENDATION). `_obligation_covered(plan,
"routine")` = cel puțin 2 `selected_products` ȘI `recommendations` nevide (o rutină cu un singur
produs nu e rutină). `control_plane`: FAQ/cache NU acoperă `routine` (nu intră în
`_SINGLE_OBLIGATION_ONLY` answerable).

### 3.3 Tool nou: `related_products` (`src/tools/catalog_tools.py`)

```python
class RelatedArgs(BaseModel):
    anchor_id: str                 # din rezultate / din contextul de pagină / din referință
    relation: str                  # enum per TENANT (vezi mai jos)
    limit: int = 4                 # ≤ 6
```

- **Enum-ul relației e al tenantului.** Schema OpenAI e `strict`, deci `relation` trebuie să fie
  `enum`. Se completează din `DomainPack.relation_kinds` la `tool_schemas(...)`, exact ca
  `{NEED_EXAMPLES}`: determinist per pachet ⇒ byte-identic ⇒ cache-uibil. Un tenant fără
  `relation_kinds` declarat NU primește tool-ul (profilul `routine` nu se poate selecta:
  `select` verifică `pack.relation_kinds.sequences()` nevid; altfel cade pe `recommend`).
- **Serverul face graful, nu modelul.** `spec = registry.get(relation)`;
  `NEIGHBORS` → vecinii direcți; `CHAIN` → `traverse_relation_chain` + `walk_chain` (ordine =
  pași); `BOUNDED` → `traverse_relations` cu plafon. Apoi `get_products_by_ids(...,
  respect_content_status=True)`, filtru de cumpărabilitate (`in_stock`/`low_stock`),
  `_safety_gate(purpose="related")` (NX-173: e o cale de AFIȘARE), `purpose=REQUIREMENT` nu se
  suprimă niciodată. Rezultatul intră în `products` ⇒ `run.retrieved` ⇒ evidence pentru validator.
- **Vederea pentru model:** o linie per produs, cu indexul pasului dacă `spec.ordered`, plus
  eticheta localizată `spec.label(locale)` ca titlu sau nimic (P11: fără titlu inventat).
  Ancoră inexistentă / fără muchii → `ok=False, error="no_relations"`, vedere onestă („nu am
  o rutină declarată pentru produsul ăsta"), ca modelul să pună `unknowns`, nu să inventeze.
- **Clasificare NX-241:** `_spec("related_products", ToolKind.READ, parallel=True, ...,
  ToolPriority.OPTIONAL)`. Registrul de tool-uri și cel de acțiuni rămân disjuncte (test există).
- **Nu intră în `_SALES_TOOLS`.** Se oferă DOAR prin profil (aditiv). Există în `TOOL_REGISTRY`.
- **Ancora vine din trei locuri, în precedența `reference_resolver`:** acțiune > produs numit >
  ordinal > contextul de pagină (PDP, NX-234) > produs selectat > singurul afișat. Sufixul
  `routine` îi spune modelului „ancora e X" când codul a rezolvat-o; altfel: caută întâi
  (`search_products`), apoi `related_products` pe primul rezultat.

Cross-sell-ul determinist v1 după `cart_add` rămâne neatins; tool-ul îi REFOLOSEȘTE funcțiile de
DB (`traverse_relation_chain`, `get_complementary_products`), nu le copiază.

### 3.4 Retrieval speculativ (`src/agent/speculative_retrieval.py`)

**Ideea:** pe profilul `recommend`, codul face căutarea ÎNAINTE de apelul 1, cu argumente
DETERMINISTE, și seeduiește bucla ca și cum modelul ar fi cerut-o. Apelul 1 devine, în cazul bun,
apelul FINAL.

```python
async def seed_search(ctx, execute: _PortedExecute, message: str) -> list[dict] | None:
    """Întoarce perechea de mesaje (assistant tool_call + tool) sau None (fără seed)."""
    args = {"query": message, "limit": 6, "sort_mode": "relevance", "in_stock_only": False,
            "price_max": corroborated_budget(message),  # NX-251: doar dacă a ROSTIT suma
            "category": None, "brand": None, "concerns": None, "features": None,
            "product_name": None, "variant_label": None}
    view = await execute("search_products", args)        # ACELAȘI drum: port, safety, admission
    if view is None or view.startswith(REFUSAL) or "dependency_unavailable" in view:
        return None
    call_id = "spec_" + hmac_short(ctx.turn_id)          # determinist, nu random
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": "search_products", "arguments": json.dumps(args, sort_keys=True)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": view},
    ]
```

- **Când NU se face:** profilul nu e `recommend`; există o acțiune opacă; există o ancoră
  rezolvată (produsul e deja identificat: nu cauți, întrebi `get_product_details`); mesajul nu
  are termeni de conținut după `query_terms` (P11, pe locale); `show_more`/paginare (drum
  determinist deja); bugetul NX-241 refuză (`admission` e același).
- **Ce vede modelul:** seed-ul + o linie în sufix: candidații vin dintr-o căutare pe mesajul
  brut; re-caută cu filtre doar dacă nu acoperă nevoia. Modelul PĂSTREAZĂ dreptul de a chema
  `search_products` (cu `concerns`, `category`, `price_max`), deci calitatea nu poate scădea sub
  cea de azi; poate doar costa un apel în plus când seed-ul e nepotrivit.
- **`run_tool_loop_structured(seed=...)`:** mesajele de seed se inserează după `user`. Nu
  consumă o rundă de model (`rounds` numără apelurile modelului), dar tool call-ul e REAL în
  ledgerul NX-241 (a trecut prin `admit`). Nimic altceva nu se schimbă în buclă.
- **Măsurătoare:** `speculative_retrieval{outcome}` cu `hit` (planul a folosit candidații și
  n-a mai căutat), `miss` (modelul a chemat `search_products` din nou), `skipped{reason}`.
  Pragul de rentabilitate calculat în §6: **43% hit**. Sub el, felia rămâne stinsă.
- **Flag:** `SPECULATIVE_RETRIEVAL_ENABLED=false`; cere `SINGLE_BRAIN_ENABLED` (validat la boot,
  în stilul `_turn_budget_relations`).

### 3.5 Ecoul scos din output (`answer_plan_runtime`)

`SERVER_OWNED_PLAN_FIELDS = ("schema_version", "business_id", "locale", "obligations")`.
`ANSWER_PLAN_V2_MODEL_SCHEMA` = schema de azi minus aceste proprietăți (derivată prin ștergere,
nu rescrisă). După parsare: `raw = {**raw, "schema_version": 2, "business_id": ctx.business.id,
"locale": ctx.language, "obligations": [o din required]}` → `AnswerPlanV2.model_validate(raw)`.
`AnswerPlanV2` nu se atinge (forma fixă rămâne; principiul 7: `business_id` server-owned devine
adevărat prin construcție, nu prin verificare). `obligation_uncovered` rămâne semnificativ:
verifică SECȚIUNILE planului (`_obligation_covered`), nu ecoul. Test: `set(model_schema.properties)
| SERVER_OWNED == set(full_schema.properties)`. Flag: `PLAN_SERVER_OWNED_FIELDS_ENABLED=false`.

### 3.6 Promptul așezat pentru cache

Azi, în `agent_stage`, `user` = limbă + hint categorie + filtre + semnal cumpărare + lead + context
de pagină + istoric + mesaj, iar `brain_user` prepune obligațiile, nevoile și semnalele. Tot ce e
per tur stă ÎNAINTEA istoricului, deci istoricul (partea care crește) nu poate lovi cache-ul.

Ordinea nouă, fără nicio schimbare de conținut:

```text
[system: db_system + PLAN_V2 + suffix(profil)]        stabil per (tenant, profil)
[tools]  [schema]                                       stabil per tenant
[user]   istoric (transcript) ──────────────────────    prefix stabil în conversație
         limbă, hint-uri, context de pagină, obligații, nevoi, semnale, mesaj   per tur
```

- `run_main_brain` primește `UserParts(history, per_turn)` în loc de un singur string; ordinea
  se compune într-un singur loc (`brain._compose_user`), testată pe poziție.
- `_chat(..., prompt_cache_key=f"{business_id}:{BRAIN_PROMPT_VERSION}")` pe toate apelurile
  buclei și pe repair, ca ruterul OpenAI să nimerească același cache.
- **NX-255:** blocul `[a aratat]` folosește indexul ABSOLUT al turului, nu „acum 2 ture", altfel
  fiecare tur rescrie istoricul și prefixul moare. Proza integrală a botului devine aproape
  gratuită după turul în care apare.
- Verificare: `cached_tokens` (deja citit în `usage.py`) pe turul 2+ al aceleiași conversații
  trebuie să includă istoricul. Fără trafic real nu se poate verifica; nu se pornesc rulări
  plătite pentru asta.

### 3.7 Fast path exact (D2) — card separat, doar contractul

`src/agent/fast_path.py`, chemat din `agent_stage` înaintea creierului, DOAR când: `turn_class ==
EXACT`, o singură obligație `answer`, referința rezolvată EXACT (`reference_resolver`, sau
`product_name` cu potrivire unică în catalog), faptul cerut ∈ {preț, stoc, link} detectat
determinist. Faptele vin din `facts_provider` (NX-237) cu prospețimea NX-240 (`stale` ⇒ creier),
textul din `src/web/localization` (niciun număr pe sârmă), `control_plane.decide` acoperă
obligația. Orice dubiu ⇒ creier. E singura felie care poate răspunde GREȘIT fără poartă în aval,
de aceea are cardul ei și golden propriu.

### 3.8 Instrumentare (înaintea oricărei felii)

- tarife `gpt-5.6-luna` în `_DEFAULT_PRICING` sau `LLM_PRICING_JSON` + test `has_rates(
  settings.model_agent)`;
- `usage` publică `cached_tokens` per APEL în `turn_latency`/trace, nu doar per tur;
- `scripts/prompt_budget_probe.py`: `_cost_model` mută schema în partea cache-uibilă și citează
  ghidul; flag `--schema-uncached` pentru scenariul pesimist;
- triajul shadow: `TRIAGE_SHADOW_SAMPLE_PCT` (default 100), eșantionare deterministă pe
  `turn_id`, aprinsă la 10% DUPĂ ce raportul de acord are eșantion (decizie de produs).

---

## 4. Direcțiile, tur cu tur

| clientul scrie | obligații → clasă → profil | drumul | apeluri model |
|---|---|---|---|
| „cât costă Hidra Boost?" | answer → EXACT → exact | fast path (când există) sau creier cu `get_product_details` | 0 sau 1-2 |
| „vreau un ser pentru ten uscat sub 150 lei" | recommend → RECOMMENDATION → recommend | seed search (buget corroborat 150) → plan | **1** (2 la miss) |
| „care e mai bun, X sau Y?" pe produse deja arătate | compare → COMPLEX → compare | `serve_comparison` determinist, înaintea creierului | 0 |
| „compară un ser cu retinol cu unul cu vitamina C" | compare → COMPLEX → compare | search ×2 (paralel, NX-241) → `compare_products` → plan | 2-3 |
| „ce rutină de seară îmi recomanzi cu crema asta?" (PDP) | routine → RECOMMENDATION → routine | ancoră din pagină → `related_products(routine_next)` → plan cu pași | 2 |
| „adaugă-l în coș" | action → MUTATION → mutation | `cart_add` prin `CartService` → plan cu confirmare | 2 |

---

## 5. Flag-uri și gărzi la boot

| flag | default | cere | OFF = |
|---|---|---|---|
| `TURN_PROFILES_ENABLED` | false | `SINGLE_BRAIN_ENABLED` | fără sufix, fără tool-uri extra: byte-identic |
| `SPECULATIVE_RETRIEVAL_ENABLED` | false | `TURN_PROFILES_ENABLED` | bucla neschimbată |
| `PLAN_SERVER_OWNED_FIELDS_ENABLED` | false | `SINGLE_BRAIN_ENABLED` | schema de azi |
| `PROMPT_CACHE_LAYOUT_ENABLED` | false | — | ordinea de azi a blocurilor |
| `TRIAGE_SHADOW_SAMPLE_PCT` | 100 | — | comportamentul de azi |

Validate în `Settings` prin `@model_validator`, ca `_turn_budget_relations`. Registrul de profile
și tool-ul nou se verifică la IMPORT (nume în `TOOL_REGISTRY` ∧ `tool_budget`, sufix în voce,
reuniune de tool-uri exactă), nu la primul tur.

---

## 6. Ce se măsoară și porțile

Cost per tur pe tarif `mini`, cache 0,9, ipotezele probei (user 900, tool result 1.200, plan 700):

| forma | cost | vs azi |
|---|---:|---:|
| recomandare azi (search → plan) | $0,00830 | |
| fără ecou în output | $0,00803 | −3% |
| speculativ, HIT | $0,00591 | −29% |
| speculativ, MISS (re-search) | $0,01010 | +22% |
| speculativ + fără ecou, HIT | $0,00564 | −32% |
| **prag de rentabilitate speculativ** | | **43% HIT** |
| exact prin creier (1 apel) | $0,00321 | |
| exact prin fast path | $0 | −100% |
| istoric 1.160 tokeni necached / cached | $0,00087 / $0,00017 | −80% pe partea de istoric |

| felie | poarta ca să se aprindă |
|---|---|
| instrumentare | `has_rates(model_agent)`; `cached_tokens` > 0 pe apelul 2 al unui tur real |
| ecou scos | golden verde; `tokens_out` mai mic pe trace, plan identic după injectare |
| layout cache | `cached_tokens` pe turul 2+ include istoricul (trafic real) |
| profile | golden per profil; pairwise fără regresie; `turn_profile` distribuit rezonabil |
| `related_products` + `routine` | golden pentru rutine: pași cumpărabili, zero produse fără muchie, `no_relations` onest |
| speculativ | `speculative_retrieval{hit}` ≥ 43% pe trafic real; p50 al turului de recomandare scăzut cu un round-trip |
| fast path exact | card propriu; golden cu UNKNOWN ≠ 0 și stale ⇒ creier |
| shadow 10% | raportul de acord brain-vs-nano are eșantion declarat suficient |

---

## 7. Ce NU face designul

- **Nu** raționament ascuns cu tool-uri (`/v1/responses`): schimbare mare, se decide pe măsurători
  (D15). Gândirea e în structura planului: obligații, claims cu evidence, unknowns.
- **Nu** schemă per clasă (L1) și **nu** schemă doar pe terminal (L2): infirmate în §0.
- **Nu** taie tool-uri per clasă; doar adaugă.
- **Nu** mai multe tool-uri „per intenție": un singur tool nou, pentru singura dovadă lipsă.
- **Nu** atinge validatorul, grounding guard-ul, safety, proiecția `web-view.v2`, contractul FE.
- **Nu** schimbă `TurnClass` sau manifestul NX-241.

---

## 8. Riscuri

| risc | unde | plasă |
|---|---|---|
| seed-ul cu argumente sărace ancorează modelul pe candidați slabi | 3.4 | modelul păstrează `search_products` cu filtre; sufixul o spune explicit; `miss` se numără; sub 43% rămâne stins |
| două prefixe de system per tenant (câte un sufix) răcesc cache-ul la trafic mic | 3.1 | sufixul e la FINAL; prefixul comun (system + tools + schemă) rămâne un singur prefix la nivel de bloc; la trafic mic costul absolut e derizoriu |
| `related_products` întoarce pași necumpărabili sau contraindicați | 3.3 | filtru de cumpărabilitate + `_safety_gate` + `REQUIREMENT` nesuprimat; `no_relations` onest |
| injectarea server-side a obligațiilor face `obligation_uncovered` tautologic | 3.5 | verificarea e pe secțiuni (`_obligation_covered`), nu pe ecou; test pe un plan gol |
| reordonarea blocurilor schimbă comportamentul modelului | 3.6 | golden + pairwise pe layout nou vs vechi; flag separat |
| `[a aratat]` cu index absolut derutează modelul | 3.6 | textul spune „turul 3 din 7"; testat în `test_structured_history` |

---

## 9. Ordinea PR-urilor

1. **Instrumentare** (3.8): tarife, `cached_tokens` per apel, proba corectată, eșantionare shadow
   ca parametru. Zero risc, deblochează măsurătorile.
2. **Ecou scos** (3.5): mic, izolat, testabil fără trafic.
3. **Layout pentru cache** (3.6) + index absolut în NX-255 (se coordonează cu PR-ul NX-255).
4. **Profile + sufixe** (3.1, 3.2 fără `routine`): registrul, `compare` și `recommend`, golden.
5. **`related_products` + obligația `routine`** (3.2, 3.3): tool, profil, golden de rutine.
6. **Retrieval speculativ** (3.4): flag OFF, măsurat pe primul trafic real.
7. **Fast path exact** (3.7): card propriu (D2), după ce 1-6 au cifre.
