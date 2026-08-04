# Analytics — Audit complet + Blueprint premium

> Document de analiză (2026-06-19). Două livrabile într-unul:
> **(A)** audit complet al stratului de analytics din cod, și
> **(B)** research (piață + concurenți premium) despre ce vor *cu adevărat* firmele care
> cumpără un sales assistant — și de ce „limba detectată" NU e printre ele.
>
> Metodă: audit pe cod (4 agenți de citire pe `analytics_events` / `usage_daily` / rollup /
> tabele de calitate), sweep web pe 6 unghiuri (priorități cumpărător, dashboard-uri
> conversational-commerce, metrici de outcome în support-AI, atribuire/ROI, anatomie dashboard
> premium, piața RO/CEE), apoi verificare adversarială a fiecărui claim de concurent. Cifrele
> de concurenți cu asterisc sunt marcate la §7 (capcane de onestitate).

---

## 0. Verdict executiv

**Substratul de analytics e solid; suprafața către client e ~0% construită.**

- Pipeline-ul emite **~40 de tipuri de evenimente** PII-safe, append-only, pe toate cele 9 stagii.
  Rollup-ul nocturn `usage_daily` e **corect acolo unde e cablat** (filtrele CTE au fost
  verificate față de emițătorii reali). **Dar din ~40 de evenimente, doar 3 sunt consumate**
  de un rollup (`intent_detected`, `cache_lookup`, `handoff_requested`); restul e telemetrie
  emisă-dar-nefolosită.
- **Trei defecte structurale** subminează povestea de business (detaliu la §1.2):
  1. **CRITIC pt. facturare** — `usage_daily.tokens_in/out/cost_usd` sunt `SUM`-ate din coloane
     din `analytics_events` pe care **niciun emițător nu le populează** → costul de pe acest
     traseu e structural **0**, în timp ce costul real stă necitit pe `messages`.
  2. **Funnelul de vânzare nu se poate reconstrui din evenimente** — `checkout_link_created` și
     `cart_updated` nu au consumator, și **nu există eveniment de click/conversie**.
  3. **Nimic din repo nu citește `usage_daily`**, deși e documentată ca „singura sursă pentru
     dashboard + facturare".
- Tabelele de calitate (`conversation_evals`, `golden_tests`) sunt **schemă moartă** (nu scrie
  nimeni în ele; gate-ul golden din CI rulează dintr-un fixture JSON, nu din tabel).
- **Corecțiile de onestitate deja făcute în marketing sunt corecte**: atribuirea ad/ROAS și
  sentimentul per-conversație au fost scoase din copy ([LANDING-PREMIUM-PACK.md](../LANDING-PREMIUM-PACK.md) §6.6) —
  și **chiar lipsesc din cod**. Bine. Nu promitem ce n-avem.

**Ipoteza ta e confirmată de piață:** cumpărătorii (proprietari de magazine) reînnoiesc pe
**bani și cerere**, nu pe metrici operaționale. „Limba detectată", numărul brut de mesaje,
taxonomia internă de intent-uri = **vanity metrics** (vezi §2, rank 8). Sunt utile intern
pentru tuning și cost — dar n-au ce căuta pe dashboard-ul de cumpărător.

---

# PARTEA A — AUDIT

## 1.1 Ce există și e corect (puncte forte)

| Componentă | Stare | Detaliu |
|---|---|---|
| Taxonomie de evenimente | ✅ wired | ~40 tipuri emise prin `ctx.emit` pe toate stagiile, append-only, PII-safe (P12), tenant-scoped. Persistare: [analytics.py](../src/db/queries/analytics.py) `insert_events`. |
| Rollup `usage_daily` nocturn | ✅ wired | [rollup_usage.py](../src/jobs/rollup_usage.py) + [usage.py](../src/db/queries/usage.py). Idempotent (upsert pe `business_id+day`), izolat per-tenant, programat în prod (NX-83). Filtrele CTE verificate față de emițători. |
| Atribuire venit asistat | ✅ wired (onest by construction) | [orders.py](../src/webhook/orders.py): match determinist `checkout_links.ref_code → orders.attributed_checkout_link_id` setează `orders.attribution` (`direct_bot`/`assisted`/`none`). E un match de cod, nu o estimare de model → **sub-raportează** (modul de eșec sigur). |
| `lead_score` per contact | ✅ wired | Formulă deterministă (`compute_lead_score`, [profile.py](../src/worker/profile.py)), clamp 0–100, persistat post-tur, sortabil prin `idx_contacts_lead`. „Hottest leads" e deja queryable. |
| Infrastructură pt. premium | ✅ prezentă | pgvector (clustering topic), tabel `conversation_evals` (QA scoring), `back_in_stock_subscriptions` + evenimente tool-call (unmet demand), `proactive_jobs` (rapoarte programate), `business_id` (benchmark pe vertical). |

**Net pe rollup:** `usage_daily` derivă fidel fiecare coloană — `conversations` (distinct
`conversation_id`), `messages_in/out` + `templates_sent` (din `messages`), `cache_hits`
(`cache_lookup` layer exact|semantic), `handoffs` (`handoff_requested`), `orders_attributed` +
`revenue_attributed` (din `orders`), `intents` (`{route:count}` din `intent_detected`).

## 1.2 Cele 3 defecte structurale (de reparat ÎNAINTE de orice dashboard)

### Defect 1 — CRITIC: cost/tokens deconectați de la realitate
`usage_daily.tokens_in/out/cost_usd` = `SUM` peste coloanele dedicate din `analytics_events`
([usage.py:21-24](../src/db/queries/usage.py)). Acele coloane sunt populate de `insert_events`
**doar dacă** `event.properties` conține `tokens_in/out/cost_usd` ([analytics.py:42-44](../src/db/queries/analytics.py)).
**Niciun `emit()`** (triaj la [triage.py:105](../src/worker/stages/triage.py), agent la
[agent.py](../src/worker/stages/agent.py)) **nu atașează** token/cost. Adevărul de cost trăiește
**doar pe `messages.cost_usd` / `tokens_in/out`** ([messages.py](../src/db/queries/messages.py)),
pe care rollup-ul **nu le citește pentru cost**. → **Costul din dashboard/facturare e structural 0.**

### Defect 2 — funnelul de vânzare nu se reconstruiește din evenimente
`checkout_link_created` ([commerce_tools.py:111](../src/tools/commerce_tools.py)) și `cart_updated`
([commerce_tools.py:170](../src/tools/commerce_tools.py)) sunt emise dar **n-au consumator**, și
**nu există** un eveniment `checkout_link_clicked` / `_converted`. `checkout_links.clicked_at` și
`converted_order_id` se scriu, dar nicio analitică nu le marchează. → `created → clicked →
converted` și `recomandare → coș → comandă` **nu se pot calcula din evenimente**; venitul vine
doar dintr-un join pe `orders`.

### Defect 3 — `usage_daily` n-are niciun cititor în repo
`grep` pe `from usage_daily` = **0** rezultate. E documentată drept „singura sursă pentru
dashboard + facturare", dar nimic din cod n-o citește: nici API de dashboard, nici export de
facturare, nici query time-series. RLS de citire există (pt. un dashboard extern), dar
consumatorul nu e construit. Rollup-ul în sine e un script `__main__` — programat de NX-83, dar
**SQL-ul real de agregare n-are test** (testele existente fac `monkeypatch` pe el).

## 1.3 Tabele moarte / claim-uri înaintea codului

| Tabel/claim | Stare reală |
|---|---|
| `conversation_evals` (LLM-as-judge) | **Schemă moartă.** Nimic nu scrie (0 hits `insert into conversation_evals`); nu există job nocturn de judge. NX-92 = backlog. |
| `golden_tests` (gate per-tenant din DB) | **Necitit.** Harness-ul golden ([golden.py](../src/evals/golden.py)) rulează dintr-un **fixture JSON** (`tests/golden/cases.json`), nu din tabel. |
| Audit §6.8 „conversation_evals.overall, nocturn 2-3% sample, CI gate" | **NU e live** — există doar checker-ul determinist substring/route (G8-1). |
| `contacts.lifecycle` / `rfm` | Coloane există, dar **nu sunt populate la runtime** (doar curățate de `gdpr_erase`). Retenția/cohortele sunt necalculabile până nu le scriem. |

## 1.4 Tile marketing → plumbing real (gap tile-cu-tile)

Dashboard-ul din [LANDING-PREMIUM-PACK.md](../LANDING-PREMIUM-PACK.md) §6.6 vs. ce poate randa codul azi:

| Tile promis | Plumbing real | Verdict |
|---|---|---|
| **Revenue the assistant helped close** | `orders.attribution` + `usage_daily.revenue_attributed` (F2-2/NX-94 merged) | ✅ **Complet susținut** (singurul cu plumbing onest 100%) |
| **Handled without a human** | Derivabil din `handoffs` vs `conversations` în `usage_daily`, dar **niciun query nu calculează raportul** | 🟡 Parțial |
| **Carts recovered** | Doar latura de *trimitere* (NX-70 proactiv); **nu există eveniment de recovery→order** | 🟡 Send-side only |
| **High-intent contacts (asked for price/stock)** | `contacts.lead_score` (NX-88) există, dar fără agregare/suprafață și fără semnal explicit „a cerut preț/stoc" | 🟡 Primitivă există |
| **Which conversations led to orders** | Evenimentele de funnel există în `analytics_events`, dar join-ul/vizualul = NX-33 (nebuild) | 🟡 Date brute da, vizual nu |
| **Unmet demand** (poziționat ca tile-ul *cel mai* proeminent) | **Zero capture** — niciun eveniment `no_result`/`unmet_query` | 🔴 Cel mai mare gap promisiune-vs-realitate |
| **Demand by language** | `locale` capturat per mesaj, dar **niciodată agregat** | 🔴 Brut da, raport nu |

**De reținut:** cele două tile-uri „diferențiatoare" (Unmet demand, Demand by language) au
**cel mai slab** plumbing real. Iar „Demand by language" e oricum un tile secundar — vezi §2.

---

# PARTEA B — RESEARCH

## 2. Ce vor CU ADEVĂRAT firmele (ranking) — și de ce limba NU contează

Confirmat pe surse vendor + ghiduri citate de analiști. Ordinea = ce *închide și reînnoiește
contracte*. (Evidența numerică e marcată la §7 unde e vendor-self-cited.)

| Rank | Metrica | De ce contează (povestea de cumpărare) |
|---|---|---|
| **1** | **Venit atribuit asistentului** (bot-led vs assisted, în **RON**) | Cel mai apropiat proxy de „îmi face bani?". Decizia literală de reînnoire. iZi (eMAG) și-a condus dovada beta cu *„5% dintre useri plasează o comandă direct din chat"*. Gorgias/Rep AI/Manychat conduc toate cu tile de revenue-from-chat. **Bot-led și assisted pe linii SEPARATE, niciodată însumate.** |
| **2** | **Rată de conversie chat→comandă** (cu numitor explicit) | North-star-ul universal de ecommerce. Demonstrează că asistentul mută browseri în cumpărători. Gorgias: *+154%* conversie la cei care conversează. Numitor corect = comenzi atribuite / conversații cu intenție de cumpărare (NU / toate mesajele = diluează; NU / click-uri = umflă). |
| **3** | **Rată de rezolvare automată / containment** (*confirmată*, nu deflection) | *„Cea mai importantă variabilă din ROI-ul de customer service AI"*; tot mai des e unitatea de facturare (Fin ~$0.99, Zendesk ~$1.50–2.00, HubSpot ~$0.50 / rezoluție). Trebuie **rezoluție confirmată**, nu deflection — un client care renunță NU e rezolvat. WISMO = ~25–40% din ticketele de ecommerce, ~100% deflectabil cu date AWB live. |
| **4** | **Venit din cart-recovery / proactiv** | „Bani găsiți" pe care altfel îi pierzi. RO: ~70% abandon coș (~75% mobil; 80%+ trafic RO e mobil) → recuperarea e o linie concretă de venit. Necesită un eveniment de atribuire recovery→order pe care **nu-l emitem încă**. |
| **5** | **Demand intelligence / cerere neîmplinită** (goluri de catalog & conținut) | Date **unice și defensabile** pe care comerciantul NU le ia din GA/ads: ce cer clienții și magazinul n-are (căutări fără rezultat, out-of-stock, întrebări pre-cumpărare recurente). Reframe-ază botul din *cost* în *senzor de cerere*. Cel mai puternic diferențiator de retenție pe vertical ecommerce. |
| **6** | **Cost/conversație + cost/rezoluție + ROI/payback** | Linia CFO care finanțează retainerul: AI ~$0.99–2.00 vs uman ~$6–12 / rezoluție. Payback = luna în care **profitul brut** atribuit cumulat depășește setup + retainer cumulat. **Necesită întâi repararea cablajului token/cost (Defect 1).** |
| **7** | **CSAT / guardrail de experiență + repeat-purchase / retenție** | Garda că un bot „eficient" nu alungă tăcut clienții (un bot „de succes" pe volum poate masca churn). **ONESTITATE: nu capturăm NICIUN CSAT/sentiment azi** → raportabil DOAR după ce adăugăm un eveniment de feedback; nu se revendică până atunci. |
| **8** | 🔻 **VANITY: metrici operaționale/interne** — *limbă detectată*, count brut de mesaje/conversații/useri, taxonomie internă de intent, model-route, % cache-hit, latență per-stage | Rank-uite **JOS** fiindcă descriu *ce a făcut botul*, nu *ce a câștigat firma* — și pot **masca** performanța slabă. **„Limba detectată" e exemplul canonic**: un fapt intern de routing pe care un cumpărător nu reînnoiește niciodată. Țin de un view de inginerie/ops, **nu** de dashboard-ul de cumpărător. (Sunt reale și utile — intern, pt. tuning și cost.) |

> **Direct la întrebarea ta:** nu, firmele nu cumpără pe „limbă". Limba e un detaliu de
> capabilitate (o pui în copy-ul de produs: „răspunde în RO/HU/EN"), nu o metrică de dashboard.
> Pe dashboard, locale devine cel mult o **felie secundară** sub „cerere pe limbă" (ce segment
> de piață îți scrie), niciodată un tile principal.

## 3. Benchmark premium — ce arată concurenții (verificat pe surse primare)

| Vendor | Metrici cheie din dashboard | Sursă |
|---|---|---|
| **Gorgias** (+ Convert / Shopping Assistant) | Tickets converted (vânzare în 5 zile), Conversion ratio, **Total sales from support**, Sales per day/agent, AI automation rate, **Revenue per interaction**, **Orders influenced**, Time/Cost saved | docs.gorgias.com |
| **Intercom Fin** | **Resolution rate (assumed vs confirmed)**, Automation rate, CX Score, Deflection, Escalation rate, Topics Explorer (clustering) | intercom.com/help |
| **Tidio (Lyro)** | Interactions, **Lyro resolution rate** (~67% avg, 50% garantat pe Premium), transfer rate, CSAT, **Leads acquired**, answer-by-intent | help.tidio.com |
| **Rep AI** | **AI-Generated Sales**, AOV din checkout AI-asistat, Conversion rate, Add-to-cart, **Top products recomandate**, Re-engagement rate, drop-off reasons, buyer-intent | hellorep.ai/data |
| **Octane AI** | Revenue (fereastră atribuire **7 zile**), Opt-ins, quiz drop-off per întrebare, conversie, AOV pt. completers | help.octaneai.com |
| **Manychat** | **Earned** (revenue), ARPPU, conversions, grafic revenue zilnic, contacts/channel | manychat.com/blog |
| **Charles** (WhatsApp) | **Revenue per Recipient (RPR)**, **ROAS** (~16x), CTR, Open rate (~81%), AOV, Opt-in/Opt-out rate | hello-charles.com |
| **Wati** (WhatsApp) | Solved by Bot vs Operator, funnel broadcast (Read/Replied/Clicked/Ignored), CSAT, ROI dashboard | support.wati.io |
| **Yellow.ai** | **Containment rate**, Resolution rate, sentiment, escalation, **topic clusters** cu containment per-topic | yellow.ai |
| **Ada / Zendesk / Decagon / Forethought** | **Automated Resolution Rate** (strict: safe+accurate+relevant, AI-judged), Cost per resolution, FCR (până la 93%), CSAT ca *guardrail* anti-gaming al „resolved" | ada.cx / zendesk / decagon |

**Convergența premium:** două familii domină — **(1) eficiență/automatizare** (resolution
rate, containment, handoff, CSAT) și **(2) atribuire de venit** (revenue from chat, conversion,
orders influenced, AOV, RPR). Mecanismul standard de atribuire = **fereastră explicită** (Octane
7 zile, Gorgias 5 zile pt. ticket convertit). Toolurile **commerce-first** adaugă tile-uri de
produs (top products recomandate, add-to-cart din chat) pe care cele support-first nu le au.

## 4. Context RO/CEE (iZi, Aura, specific local)

- **iZi (eMAG)** — verificat (oficial/presă, mar. 2026): „primul agent de shopping AI dezvoltat
  în RO", combină catalog eMAG + intenție + surse web externe în timp real. **Metrici beta
  (~50k useri, *raportate de eMAG*, neauditate):** 2.6 întrebări/conversație, **1 din 3 revin
  săptămâna următoare**, 5–6 pagini de produs/sesiune, >90% review-uri pozitive, **5% plasează
  comandă direct din chat**. Cel mai puternic în Electronice & Auto (spec-heavy), Beauty în creștere.
- **Aura (SOLE.ro)** — verificat: asistent 24/7 pe WhatsApp; Q&A ingrediente/produs,
  recomandări pe tip de ten, routine building, **alerte back-in-stock**, cuplat cu Dermochoice
  (analiză AI a tenului, 50+ parametri). **Niciun KPI public** — doar capabilități. („Primul
  retailer RO de cosmetice cu asistent 24/7" = claim de marketing, neconfirmat independent.)
- **Specific local de adăugat pe dashboard:**
  - **RON** peste tot (nu €/$).
  - **WISMO + AWB**: lookup AWB (Sameday ~32% piață, 7.700+ easybox) e o sursă deterministă,
    automatabilă → tile de deflection WISMO (`check_order` + `shipments`).
  - **COD vs prepaid**: COD ~51–65% din comenzile RO; rata de succes scade cu prețul (~90–95%
    suplimente, ~80–88% electronice >€200). Pt. HVAC/auto (AOV mare), nudge-ul spre prepaid /
    checkout_link reduce pierderile din colete refuzate — **levier de cost unic CEE**.
  - **Benchmark de bătut:** „1 din 3 revin săptămâna următoare" și „2.6 întrebări/conversație"
    (iZi) ca țintă, nu garanție.

---

# PARTEA C — BLUEPRINT PREMIUM (mapat pe schema reală)

7 tier-uri. `dataModelFit` = `available-now` (din evenimente/tabele existente) /
`needs-new-event` / `needs-new-table` / `needs-external-integration`. Fiecare metrică are o
**constrângere de onestitate** — eticheta care o ține credibilă.

### Tier 1 — Executive / North-Star
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Venit atribuit asistentului (RON)** — tile dominant stânga-sus, cu Δ WoW/MoM | available-now | `orders` + `usage_daily.revenue_attributed` | mic | „atribuit (last-touch via checkout link), nu incremental-dovedit". Bot-led & assisted pe linii separate, **neînsumabile**. Afișează fereastra de atribuire. **Niciodată „ROAS".** |
| **ROI / payback (pe profit brut)** | available-now | `revenue_attributed` × marjă client + retainer (config) | mediu | Profit **brut**, nu top-line, altfel ROI umflat. „atribuit, nu holdout-incremental" până nu rulezi un holdout randomizat. |
| **Conversații gestionate (volum)** cu Δ perioadă | available-now | `usage_daily.conversations` | mic | E **numitor/baseline**, nu metrică de valoare — ține-l mic și secundar. |

### Tier 2 — Revenue & Attribution
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Split bot-led (direct) vs assisted** | needs-new-table* | `orders.attribution` per-event există, dar `usage_daily` colapsează în 1 pereche → adaugă coloane de split la rollup | mic | **Niciodată însuma direct+assisted** (dublu-numărare). Assisted = cifra mai moale; bot-led = faptul dur. |
| **Revenue per conversație (RPC)** | available-now | `revenue_attributed / conversations` | mic | Tipărește ce numitor folosești. Nu e ROAS. |
| **AOV pt. comenzile atribuite** | available-now | `orders.total` filtrat `attribution<>'none'` | mic | Comparația atribuiți-vs-toți are bias de selecție → etichetează „AOV comenzi atribuite", nu „uplift AOV din asistent" (uplift real cere holdout). |
| **Split COD vs prepaid** (atribuite) | needs-external-integration | `orders.total` + câmp payment-method din webhook/ERP | mediu | Cere îmbogățirea webhook-ului de comenzi; până atunci nu raporta. Framing = levier de pierderi din colete refuzate. |

### Tier 3 — Conversion Funnel
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Funnel: reached → sales-intent → recommended → checkout link → comandă atribuită** | available-now | `intent_detected` (route sales) → `agent_recommended` → `checkout_link_created`; `orders` pt. pasul final | mediu | Pasul final = join pe `orders`, nu eveniment de conversie → etichetează link→order ca last-touch determinist. |
| **Rată de conversie asistent** | available-now | `intent_detected` (sales) + `orders.attribution` | mic | Numitorul = conversații cu intenție de cumpărare, NU toate mesajele, NU click-uri. Expune numitorul pe tile. |
| **Micro-funnel checkout link: created → clicked → converted** | needs-new-event | `checkout_link_created` există; lipsesc `checkout_link_clicked` + `_converted` | mediu | Azi doar endpoint-ul e reconstruibil (join orders). Atenție la fraudă de coupon-scraping (click→conversie implauzibil de scurt). |
| **Recommendation CTR / add-to-cart din recomandare** (atribuit) | needs-new-event | `agent_recommended` + `cart_updated` există, dar fără `recommendation_clicked` și fără legătură de tur | mediu | Fără eveniment de click + legare de tur, CTR e necalculabil — nu-l revendica. |

### Tier 4 — Demand Intelligence (diferențiatorul)
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Cerere neîmplinită** — căutări fără rezultat & out-of-stock (listă de merchandising) | needs-new-event | `product_search` ([catalog_tools.py:174](../src/tools/catalog_tools.py)) + `back_in_stock_subscriptions`; lipsește un eveniment `no_result` | mediu | `product_search` emite bogăție dar nu semnal dedicat de zero-rezultat — adaugă-l. Pe demo (embeddings=0/faqs=0) e structural gol până e catalog real. |
| **Clustering topic/sub-intent** al întrebărilor | needs-new-table | `messages.body` embed prin pgvector + job de clustering → tabel `topics` | mare | `intents` jsonb e pe **route-ul** grosier de triaj (simple|sales|order|...), NU taxonomie semantică — nu prezenta count-uri de route ca „topics". |
| **Întrebări pre-cumpărare recurente** (goluri PDP/conținut) | needs-new-event | `faq_lookup` misses + `clarify_asked` emise dar neagregate; lipsește `faq_resolved` | mediu | `faq_hit` marchează hit, nu confirmă că răspunsul „a ținut" → raportează „top întrebate", nu „top rezolvate". |

### Tier 5 — Automation & Efficiency
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Rată de rezolvare automată / containment (confirmată)** | needs-new-event | `handoff_requested` + `conversations.handoff_until` dau latura de escaladare; confirmarea cere clasificator quiet-period/no-reopen | mediu | Distinge **confirmat** de simplă deflection (renunțarea NU e rezolvare). Separă „assumed" (pe tăcere) de „confirmed". Pereche cu guardrail CSAT. |
| **Rată handoff/escaladare (tag good vs failure)** | available-now | `handoff_requested {reason, source}`; `usage_daily.handoffs` | mic | Un handoff corect de politică e **succes**, nu ratare — taguiește pe motiv. Reconciliază cele 2 emit-sites (gates + tool). |
| **% deflection strat gratuit (rezolvare fără LLM)** | available-now* | `alias_lookup` + `faq_hit` + `cache_lookup`; doar `cache_lookup` intră azi în `cache_hits` | mic | `alias_lookup`/`faq_hit` emise dar neconsumate → cere cablare în rollup. Pe demo faqs=0/aliases=0 → ~zero până seedezi conținut. |
| **Cost/conversație + cost/rezoluție + cost total token** | available-now* | `messages.cost_usd` + `tokens_in/out` (sursa REALĂ); `usage_daily.cost_usd` e azi 0 | mediu | **FIX CRITIC ÎNTÂI (Defect 1):** repointează rollup-ul pe `messages.cost_usd`. Cost uman ($6–12) e furnizat de client, nu presupus. |
| **Timp de răspuns (p50/p95) + acoperire în afara orelor** | available-now* | delte `messages.created_at` + `messages.latency_ms`; `stage_completed.latency_ms` per-stage există dar niciun rollup nu-l citește | mediu | Per-stage e capturat dar neagregat → cere rollup. Raportează p50/p95, nu media. Secundar față de tile-urile de bani. |

### Tier 6 — Quality & Experience
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **CSAT / feedback post-conversație** (thumbs / 1-tap) | needs-new-event | **NIMIC azi** — cere un eveniment `reply_feedback` (+/−1) din reacție/quick-reply de canal | mediu | CSAT/sentiment **nu e capturat nicăieri** și a fost scos din marketing. **Nu afișa/revendica CSAT** până nu există evenimentul. E guardrail-ul anti „over-marking resolved". |
| **Scor LLM-as-judge** (helpfulness/accuracy/tone) | needs-new-event | `conversation_evals` **există** dar nimic nu scrie — cere jobul nocturn (NX-92) | mare | Schemă **moartă** azi; „nocturn 2-3% sample" NU e live. Nu prezenta scoruri ca operaționale până nu există writerul. |
| **Rată de blocare validator (hallucination-guard)** | available-now* | `validator_rejected` ([agent.py:480](../src/worker/stages/agent.py)); neagregat azi | mic | Emis dar neconsumat → cablează în rollup. Framing = „prinderi de guardrail" (dovada de zero-prețuri-inventate), nu scor de calitate pt. client. |
| **Drill-to-conversation (strat de evidență)** | available-now | `analytics_events`/topics → `conversation_id` → `messages.body` | mediu | **Redactează PII** (telefoanele doar în `channel_identities`). Arată „last updated" din rularea rollup-ului ca un număr stale să nu pară live. |

### Tier 7 — Proactive & Retention
| Metrică | Fit | Sursă | Efort | Onestitate |
|---|---|---|---|---|
| **Venit din cart-recovery** (abandoned_cart → comandă atribuită) | needs-new-event | `proactive_jobs(abandoned_cart)` (NX-70) + `checkout_links`; lipsește `cart_recovered` | mediu | Send-side există, contor recovery→order NU → „Carts recovered" nesusținut până emiți evenimentul. Atribuie prin `ref_code`. |
| **Conversie notificare back-in-stock** | needs-new-event | `back_in_stock_subscriptions` + `proactive_jobs(back_in_stock)` + orders | mediu | Consent-gated + fereastră 24h/template. Raportează trimiteri vs conversii separat. |
| **Funnel campanie proactivă** (enqueued→sent→delivered→read→replied) | needs-new-event | `proactive_*` există; delivered/read în `message_status_events` (neconsumate) | mediu | `templates_sent` vine din `messages`, nu din evenimentele proactive → nu e joinabil azi. Construiește puntea întâi. |
| **Listă high-intent (lead_score)** | available-now | `contacts.lead_score` (`idx_contacts_lead`) | mic | `lead_score` există dar neagregat/nesuprafațat și fără semnal explicit „a cerut preț/stoc" → framing = ranking determinist, nu garanție comportamentală. |
| **Cohorte repeat-purchase / retenție** | needs-new-table | `contacts.lifecycle/rfm` există dar **niciodată populate**; cere job + tabel cohorte | mare | Necalculabil până le scriem la runtime. Returning-vs-first-time pe profit brut, nu top-line. |
| **Rată de revenire 7 zile / re-engagement** | available-now | `conversations` grupate pe `contact_id` în bucket-uri săptămânale | mediu | Computabil din timestamps dar neagregat azi. Benchmark = „1 din 3 revin" (iZi) ca țintă. |

---

## 4bis. Modulul «Cerere & Produse» — raportul de BUSINESS (direcția cerută)

> Asta e analiza cerută explicit: **nu tehnică, ci de business** — *ce se vinde, ce se caută, ce
> se cere, pe ce să consumi bani, ce ar vrea clienții să găsească la tine*. E expansiunea Tier 4
> (Demand Intelligence) într-un **raport pe care proprietarul îl deschide și ia decizii din el**.
> Botul devine un **senzor de cerere**: vorbește deja cu fiecare client, deci știe deja ce vor —
> date pe care GA / ads NU le dau. E diferențiatorul cu cea mai mare retenție.

### Ce vede comerciantul — 5 secțiuni (rânduri = exemple, sample)

**1. Top produse cerute & vândute prin asistent** — *„ce se vinde"*

| Produs | # recomandate | # add-to-cart | # comenzi atribuite | Venit atribuit (RON) | Conversie |
|---|---|---|---|---|---|
| CeraVe PM Lotion *(exemplu)* | 142 | 58 | 31 | 4.805 | 22% |
| La Roche Toleriane *(exemplu)* | 96 | 33 | 18 | 3.222 | 19% |

→ Răspunde: *ce produse împinge botul și care chiar convertesc* (nu doar ce afișezi).

**2. Top căutări & atribute cerute** — *„ce se caută"*

| Căutare / categorie / atribut | # cereri | Trend 30z | % cu rezultat | % → comandă |
|---|---|---|---|---|
| „cremă ten uscat fără parfum" *(exemplu)* | 210 | ↑ | 88% | 14% |
| brand „Bioderma" *(exemplu)* | 73 | ↑ | 0% | — |

→ Răspunde: *ce limbaj/atribute folosesc clienții, ce branduri cer* (input de merchandising + SEO).

**3. 🎯 CERERE NEÎMPLINITĂ** — *„pe ce să consumi bani"* (tile-ul cel mai valoros)

| Tip | Ce-au cerut | # cereri | Recomandare derivată |
|---|---|---|---|
| **Fără rezultat** | brand „Bioderma" *(exemplu)* | 73 | **Adu în catalog** — cerere reală, zero ofertă |
| **Out-of-stock cerut** | „Vitamin C serum 30ml" *(exemplu)* | 41 (+ 19 abonări back-in-stock) | **Reaprovizionează prioritar** |
| **Variantă lipsă** | nuanța „medium" la fond ten X *(exemplu)* | 28 | **Extinde gama de variante** |
| **Gol de preț** | „ceva mai ieftin" în categoria Y *(exemplu)* | 64 | **Adaugă opțiune entry-level** |

→ Răspunde direct la *„pe ce să investești"* și *„ce ar vrea clienții să găsească"*. Date pe care
**numai** asistentul le are (căutările eșuate nu apar nicăieri altundeva).

**4. Întrebări pre-cumpărare recurente** — *„ce informație le lipsește"*

Ce întreabă repetat (ingrediente, compatibilitate, livrare, mod de folosire) → ce să adaugi pe
PDP / în FAQ ca să nu mai blocheze vânzarea. (ex.: *38% din chat-urile pre-cumpărare la un brand
de cosmetice întrebau despre siguranța ingredientelor* → fix de conținut.)

**5. Lead-uri fierbinți nematerializate (buy-signals pierdute)** — *„cine era gata să cumpere"*

Contacte cu `lead_score` mare care au cerut preț/stoc dar n-au comandat → listă de follow-up
pentru operator, cu ce produs/întrebare i-a blocat.

### Ce date avem deja vs. ce trebuie instrumentat

| Semnal de business | Sursă reală azi | Ce lipsește |
|---|---|---|
| Ce produse recomandă botul | `agent_recommended` ([agent.py:474](../src/worker/stages/agent.py)) emite doar `{n}` | **enrich cu `product_ids[]`** |
| Ce se caută / ce atribute | `product_search` ([catalog_tools.py:174](../src/tools/catalog_tools.py)) emite `{count, had_*}` | **enrich cu `category_key`, `brand`, `concerns[]`, `price_band`, `top_product_ids[]`** (normalizate, PII-safe) |
| **Căutări fără rezultat** | — *nimic* | **event nou `unmet_query`** când `result_count=0`/`matched=false` |
| Out-of-stock cerut | `back_in_stock_subscriptions` (există ✅) + `product_search` pe produse fără stoc | agregare top-N |
| Add-to-cart / checkout pe produs | `cart_updated` / `checkout_link_created` emit `{items, value}` | **enrich cu `product_ids[]`** |
| Produse care convertesc | `orders` + `attributed_checkout_link_id` (există ✅) | join produs → comandă |
| Întrebări recurente | `faq_lookup` misses + `clarify_asked` (emise, neagregate) | agregare + (later) clustering topic |
| Lead-uri fierbinți | `contacts.lead_score` (există ✅) | filtru „a cerut preț/stoc + n-a comandat" |

**Cheia:** capturăm **atribute de produs / interogări normalizate**, NU text brut cu PII
(principiul 12). `product_search` și straturile gratuite normalizează deja (`query_norm`,
`phrase_norm`) — extindem acea normalizare, nu logăm mesaje brute.

### Decizii pe care le deblochează (de ce plătește comerciantul)
- **Restock prioritizat** — reaprovizionează ce se cere cel mai mult acum (nu pe ghicite).
- **Extindere catalog** — branduri/produse cerute pe care nu le ai deloc = venit lăsat pe masă.
- **Conținut PDP/FAQ** — completează info care blochează cumpărarea.
- **Gamă de preț** — umple golul unde clienții cer „mai ieftin/mai scump".
- **Follow-up vânzări** — listă de lead-uri fierbinți gata de închis manual.

### Onestitate (de respectat în raport)
- Count-urile sunt **cereri/recomandări, nu vânzări garantate** — etichetează clar.
- **PII**: doar atribute normalizate, niciodată text personal brut în tabelele de cerere.
- Pe tenant-ul demo (`embeddings=0`, `faqs=0`, `orders=0`, `product_url` NULL) raportul e
  **structural gol** până e catalog + date reale — nu prezenta sample-ul ca rezultat live.
- „Cerere neîmplinită" **cere** event-ul `unmet_query` nou — nu se revendică până nu există.

### Decupaj în taskuri (track «Cerere & Produse»)
1. **Demand Capture** — instrumentarea evenimentelor: enrich `product_search` / `agent_recommended`
   / `cart_updated` / `checkout_link_created` cu `product_ids[]` + atribute normalizate; emite
   `unmet_query` la zero/low-result. (Fundația — fără ea nu există raport.)
2. **Demand & Product Intelligence** — rollup + tabelele de raport care produc cele 5 secțiuni
   de mai sus + recomandările derivate. (Citește evenimentele din #1 + `back_in_stock_subscriptions` + `orders`.)
3. *(Later)* **Topic clustering** — grupare semantică a întrebărilor pentru secțiunea 4.

---

## 5. Roadmap (ordine de execuție)

**Phase 0 — Repară fundația (ÎNAINTE de orice dashboard de cumpărător):**
1. **CRITIC:** repointează rollup-ul token/cost pe `messages.cost_usd` + `tokens_in/out` (sau
   atașează token/cost la emit-sites LLM) — azi sunt 0 și stau sub facturare.
2. Adaugă un **cititor in-repo pentru `usage_daily`** (sursa documentată n-are niciun cititor).
3. Adaugă **test pe SQL-ul real `rollup_usage_day`** (azi e monkeypatch-uit).
4. Auto-creare partiții lunare + drop de retenție pt. `analytics_events`/`messages` înainte de
   2026-08 (totul cade acum în `_default`).

**Now — cablează ce pipeline-ul deja emite (available-now):**
- Tile north-star: venit atribuit (RON) cu split bot-led/assisted (adaugă coloanele la rollup) + etichete last-touch/fereastră.
- Funnel de conversație + rată de conversie + RPC cu numitor sales-intent explicit.
- % deflection strat gratuit (cablează `alias_lookup` + `faq_hit` lângă `cache_lookup`).
- Rată handoff tag-uită pe motiv; rată blocare validator; timp răspuns p50/p95.
- Listă lead_score + rată revenire 7 zile.
- ROI/payback (profit brut) + AOV atribuit, după ce capturezi config marjă/retainer.
- **IA stratificat:** north-star stânga-sus, fiecare cifră cu Δ WoW/MoM + status RAG, filtre self-service, drill-to-(redacted)-conversation, etichetă „last updated".

**Next — evenimente mici noi (instrumentare low-medium):**
- `checkout_link_clicked` + `_converted` → micro-funnel (atenție coupon-scraping).
- `recommendation_clicked` + legarea `cart_updated` de turul de recomandare → CTR + add-to-cart atribuit.
- `no_result`/`unmet_query` pt. căutări zero-rezultat → lista de cerere neîmplinită.
- `reply_feedback` (+/−1) din thumbs/quick-reply → CSAT (NU afișa CSAT până nu există).
- `cart_abandoned`/`cart_recovered` + punte `proactive_sent/engaged` → venit recovery + funnel proactiv.
- Clasificator de rezoluție confirmată (quiet-period + semnal de confirmare) → containment, assumed vs confirmed.

**Later — tabele/joburi/integrări noi (efort mare, diferențiatori):**
- Job de clustering topic peste embeddings pgvector → tabel `topics` cu tree-map (feature-ul premium marquee; înlocuiește count-urile de route).
- Job nocturn LLM-as-judge (NX-92) scriind `conversation_evals.overall` pe 2-3% → scoruri + scorecards.
- Populează `contacts.lifecycle/rfm` la runtime + tabele de cohorte → repeat-purchase pe profit brut.
- Grain `channel_kind`/`locale` la rollup → breakdown cross-channel & multilingv.
- Alerting statistic de anomalii (spike cost / surge handoff / drop conversie) + sumar narativ săptămânal + rapoarte email/PDF programate.
- Îmbogățire payment-method la webhook → split COD vs prepaid (levier RO/CEE).
- Benchmark-uri pe vertical (beauty/HVAC/auto/salon) respectând izolarea `business_id`.
- Opțional rigoare: harness de holdout/incrementalitate (RPV lift, 15–20k/cohortă, CI) pt. a
  ridica „atribuit" la „incremental dovedit" pt. QBR-uri. **NU** construi ROAS/atribuire ad
  cross-channel (NX-06 de-scoped) — păstrează poziția „fără claim ROAS".

---

## 6. Top 5 mutări cu cel mai mare ROI (dacă faci doar atât)

1. **Repară Defect 1** (cost/token din `messages`) — fără el, orice cifră de cost/ROI/payback e 0 sau falsă. *(Phase 0, mic)*
2. **Tile north-star de venit atribuit RON** cu split bot-led/assisted + funnel de conversație — singura poveste care reînnoiește contracte. *(Now, mic-mediu)*
3. **`no_result`/`unmet_query` event → tile de Cerere neîmplinită** — diferențiatorul tău unic de demand-sensing; azi e promis dar gol. *(Next, mediu)*
4. **Rată de rezolvare confirmată + handoff tag-uit pe motiv** — povestea de economisire de cost/CFO, făcută onest (confirmat, nu deflection). *(Next, mediu)*
5. **`reply_feedback` (+/−1) → CSAT guardrail** — micul semnal care apără brandul și deblochează „rezolvare confirmată"; nu pretinde CSAT până nu-l ai. *(Next, mediu)*

---

## 7. Capcane de onestitate (corecții din verificarea adversarială)

Folosește aceste cifre cu eticheta corectă; nu le pune brute în pitch:

- **Tatcha (3x conversie, +38% AOV, 11.4% din venitul site-ului)** = studiu **Alhena AI**, NU
  raportul Gorgias „State of Conversational Commerce 2026". (Atribuire greșită în surse.)
- **Forrester „ROI 3 ani 331–391% pt. voice AI"** = **TEI specific de vendor** (PolyAI; 391%
  trasează la un TEI Verint din 2021), **nu** un benchmark generic Forrester. Formulează „studii
  TEI Forrester ale unor vendori individuali raportează ROI 3 ani până la ~391%".
- **Cost/rezoluție** — folosește **$0.99–2.00 AI / $6–12 uman**; „$0.46" e mai agresiv decât
  susțin sursele. „$13.50" e costul Gartner de contact **agent-asistat** vs **$1.84** self-service
  — citează-le separat, nu ca „cost uman per rezoluție".
- **„79% din branduri raportează vânzări crescute"** — **negăsit** pe pagina Gorgias citată →
  neverificat până e localizat.
- **Chat-Data „~60% ROI mai mic la orgs pe activity-metrics" și „retailer Fortune 500: 2M
  interacțiuni / +25% churn"** = **auto-analiză de vendor** / scenariu ilustrativ, nu cercetare
  independentă sau caz auditat. Bun ca *argument retoric*, nu ca dovadă.
- **ROAS / atribuire ad cross-channel** — **NU e revendicabil** fără integrare cu platforma de
  ads (iROAS rulează cu 30–60% sub ce raportează platforma). Corect că e scos din copy.
- **iZi (5% comandă din chat, 1/3 revin, 2.6 întrebări, >90% pozitiv)** = cifre **beta raportate
  de eMAG**, neauditate → benchmark de bătut, nu adevăr absolut.
- **„SOLE primul retailer RO de cosmetice cu asistent 24/7"** = poziționare de marketing,
  neconfirmată independent.
- **Abandon coș ~70% / mobil ~75%** = benchmark **global** (Baymard etc.); **nu** s-a găsit cifră
  RO-specifică → aplică global cu acest caveat. Adopția WhatsApp-commerce e tot proxy global/DACH
  (lipsă date CEE-specifice).

---

## 8. Surse principale

**Priorități cumpărător & ROI:** gorgias.com/state-of-conversational-commerce-2026 ·
fin.ai/learn/roi-ai-customer-service-agents-benchmarks · growwstacks.com (ecommerce ROI metrics) ·
chat-data.com (vanity vs value) · ringly.io/blog/ai-customer-service-roi ·
**Dashboard-uri concurenți:** docs.gorgias.com · intercom.com/help (Fin reporting) ·
help.tidio.com (Lyro Analytics) · hellorep.ai/data · help.octaneai.com · manychat.com/blog ·
hello-charles.com (WhatsApp KPIs) · support.wati.io · yellow.ai/platform/ai-analytics ·
ada.cx · zendesk (cost-per-resolution) · **RO/CEE:** startupcafe.ro / wall-street.ro (iZi) ·
sole.ro (Aura/Dermochoice) · gpec.ro · zf.ro (MerchantPro) · paysera.com / ecomlog.eu (COD) ·
sameday.ro / ordertracker.com (AWB) · alhena.ai (WISMO) · baymard.com (cart abandonment).

> Listă completă de URL-uri în transcriptul workflow-ului de research (17 agenți, ~956k tokens,
> verificare adversarială per-claim).

---

# PARTEA D — DECIZIE & PLAN DE IMPLEMENTARE (2026-07-10)

> Condensarea gândirii de produs + arhitectură convenite după audit. Fixează **ce construim, în
> ce ordine, și cu ce reguli de onestitate**. Backend întâi; **frontend la final** (repo FE separat
> — vezi `docs/FRONTEND-CONTRACT-IZI.md`, backend-ul emite doar JSON).

## D.0 Reality-check — ce s-a schimbat în cod față de audit (19 iun → 10 iul)

Codul a avansat; două „defecte" din PARTEA A sunt (parțial) rezolvate. **Nu re-planifica ce e făcut.**

| Audit (PARTEA A) | Realitate în cod la 2026-07-10 |
|---|---|
| **Defect 1: cost/tokeni structural 0** | ✅ **REPARAT.** Event `llm_usage` emis din `runner.py` (per tur) + `aftercare.py` (post-tur) cu cost/tokeni reali (`ctx.usage`); rollup-ul îl agregă cu FILTER explicit (NX-103/NX-125). |
| **Defect 3: `usage_daily` fără cititor** | 🟡 **Parțial.** Cititor intern există (`limits.py` — plafon cost zilnic). Cititor de **raport/API** încă lipsește. |
| **Defect 2: funnel fără click/conversie** | 🔴 **Valabil.** `checkout_link_created` se emite; `clicked`/`converted` nu. |
| **Demand capture (`product_ids[]` + atribute + `unmet_query`)** | 🔴 **Neînceput.** `product_search` emite `mode/count/had_price_filter` (PII-safe) fără `product_ids`/`brand`/`category`. `named_product_not_found` = primitivă parțială de unmet. |

## D.1 Principiul de secvențiere (mai important decât ordinea de features)

Blocajul real nu e codul, sunt **datele acumulate**. Deci:

> **Instrumentarea write-side (captura) merge prima și se deployează devreme — ca să pornească
> ceasul de date. Straturile de read/raport se construiesc după, cât datele se adună** (oricum
> sunt inutile până nu e volum). Fiecare zi fără captură = cerere pierdută nerecuperabilă.

## D.2 Arhitectura de bază (corecția față de „Conversation Action Extractor")

**NU** extragem acțiuni post-hoc din conversații cu un LLM (al 3-lea punct LLM, `confidence`,
fabricare). **Capturăm faptul determinist când se întâmplă** — un tool call cu parametri
normalizați ESTE faptul. Semantica (topic/obiecții) vine ca strat offline separat, mai târziu.

## D.3 Fazele (backend; frontend = Faza 5, la final)

**Faza 0 — Închide golurile de adevăr rămase · ~3-4 zile**
- `checkout_link_clicked` (endpoint redirect care ștampilează `checkout_links.clicked_at` + emite) — piesă mică de infra, nu doar `emit()`.
- `checkout_link_converted` (match-ul de comandă scrie deja `converted_order_id`; lipsește doar emiterea).
- Teste: tenant-scoped + „zero PII în properties" pe fiecare eveniment nou.

**Faza 1 — Demand Capture (EROUL, write-side, deploy devreme) · ~1.5-2 săpt**
Determinist, fără LLM, fără `confidence`, fără `estimated_value`. Îmbogățește evenimente existente:
- `product_search` → `product_ids[]` (top-N), `category_key`, `brand`, `price_band`, `stock_status`, `query_norm` (normalizat, PII-safe).
- `unmet_query` (event consolidat, `reason` tipizat) din: `count=0` · `named_product_not_found` · out-of-stock cerut · variantă lipsă.
- `agent_recommended` → `product_ids[]` (azi doar `n`).
- `cart_updated` / `checkout_link_created` → `product_ids[]` (azi doar counts/value).

**Faza 2 — Revenue + Leads (read-side, available-now) · ~1.5 săpt** (în paralel cu acumularea din Faza 1)
- Split bot-led vs assisted în rollup (coloane noi; `orders.attribution` există per-event). **Niciodată însumate.**
- Semnal „a cerut preț/stoc" pentru hot leads (mic semnal nou; `lead_score` singur nu-l dă).
- Modul de query de raport: venit atribuit (RON), RPC, AOV atribuit, listă hot leads, **drill-down** `conversation_id → messages` cu **PII redactat**.

**Faza 3 — Demand rollup + „Actions This Week" · ~1.5-2 săpt**
Agregă faptele din Faza 1 în rânduri de acțiune, fiecare cu `evidence = conversation_id[]`:
- „Adaugă brand X: N cereri, 0 în catalog" · „Reaprovizionează Y: N cereri, stoc epuizat"
- „Variantă lipsă la Z" · „Sună ăștia: au cerut preț, n-au comandat" · „Conversații riscante" (`validator_rejected`, handoff pe motiv)

**Faza 4 (later) — Semantic, DOAR unde structura nu poate ști**
Clustering topic, obiecții de nuanță, CSAT (cere event de feedback), AI coach. Job nocturn pe
sample, niciodată inline, mereu cu `confidence` + etichetă „inferat". Nu se atinge până nu există
date reale + încredere.

**Faza 5 — Frontend** (repo FE separat, la final, pe date deja acumulate).

**Total Faza 0-3 ≈ 5-6 săptămâni backend**, cu Faza 1 în prod în ~săpt 1-2. Blocaj rămas =
**pilot live cu trafic + comenzi** (track paralel, nu task de cod).

## D.4 Invariante de onestitate (regula pe DOUĂ straturi — bătute în cuie)

1. **Fapt determinist = fără `confidence`, fără `estimated_value` în DB.** Are `conversation_id` ca dovadă. Punct.
2. **Oportunitatea se derivă la citire, în UI** („73 cereri · 0 în catalog · AOV categorie 180 RON · indicator: high") — niciodată „ai pierdut 13.140 RON" stocat ca adevăr.
3. **Zero PII** în `properties` — doar atribute normalizate (principiul 12).
4. Stratul semantic (Faza 4) e **singurul** cu `confidence`, etichetat „inferat", fizic separat de fapte.

> Formula: **nu construim un AI care ghicește ce s-a întâmplat; construim un sistem care nu pierde
> faptul când se întâmplă.** Fapte numărate (lasă comerciantul să înmulțească) > bani estimați.
