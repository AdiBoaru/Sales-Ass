# Analiză exhaustivă (verificată): Nativx „Aria" vs. iZi/industrie conversational-commerce 2026

**Data:** 2026-06-29
**Autor:** workflow multi-agent (Claude Code, ultracode) + sinteză de arhitect
**Metodă:** 51 agenți — 10 hărți din cod + 6 cercetări 2026 (cu surse) → 10 sinteze de gap pe
dimensiune → **24 verificări adversariale** ale gap-urilor high/critical împotriva codului →
critic de completitudine. **Rezultat verificare: 24/24 confirmate, 0 respinse** (niciun gap
halucinat). ~2.4M tokens, 919 tool-calls.

> Versiune COMPLETĂ (rulare 2). Înlocuiește schița parțială anterioară. Complementar:
> [`PRODUCT-RANKING-ANALYSIS-2026.md`](PRODUCT-RANKING-ANALYSIS-2026.md) (ranking P0, PR #140),
> [`IZI-VS-NATIVX-CONVERSATION-GAP.md`](IZI-VS-NATIVX-CONVERSATION-GAP.md) (turul „crema X").

---

## 0. TL;DR în trei propoziții

1. **Creierul nostru e la paritate sau peste** standardul 2026 pe coloana vertebrală (retrieval
   hibrid, ranking blended + pick determinist [PR #140], anti-halucinație structural, intent,
   motor de carduri) — verdictul agenților pe Display e literal **`at_par`**.
2. Diferența reală față de iZi e **îngustă pe comportament** (4 lipsuri: disclosure pe produs
   numit, onorare „cumpăr acum", nevoie-inventată, relaxare tăcută) și **mare pe DATE** (catalogul
   demo: rating-uri ne-variate [283/500 la 5.0], nume cu ID rezidual, `product_url` 0/500) — care
   ascund chiar munca de ranking deja livrată.
3. **Dar criticul a demolat scopul analizei:** cele 10 sinteze au convers pe ACELEAȘI 3-4 gap-uri
   (ancorate pe UN singur tur), ratând piloni întregi — **proactivul e neoperant în producție
   (CRITICAL), nu există guardrail pe sfat medical/beauty (răspundere juridică), zero buget de
   latență/cost, și Vision/Voice/multi-tur/post-vânzare nici n-au fost evaluate.**

---

## 1. Scorecard de paritate (verdicte ale agenților)

| # | Dimensiune | Paritate | Esența |
|---|---|:---:|---|
| 1 | Retrieval & Ranking | **slightly_behind** | motor la par/peste; lipsesc disclosure named-entity + metrici |
| 2 | Intent & Routing | slightly_behind | triaj+sloturi solide; lipsă named-product + purchase-intent |
| 3 | Honesty & Grounding | slightly_behind | apărări structurale puternice; lipsă disclosure named + anti-nevoie-inventată |
| 4 | **Display, Cards & Comparison** | **`at_par`** | motor determinist anti-halucinație la paritate; restul = DATE/platformă |
| 5 | Commerce & Conversion | slightly_behind | buclă atribuire OK; lipsă fulfillment „cumpăr acum" + delivery/voucher |
| 6 | Personalization & Context | **behind** | profil persistă, dar lifecycle nescris, RFM mort, lead_score necitit, fără memorie navigare |
| 7 | **Proactivity & Lifecycle** | **behind → de fapt CRITICAL** | motor+gating OK dar **0 inițiatori creează `proactive_jobs`** → zero mesaje proactive în prod |
| 8 | Multilingual | at_par (RO) / behind (HU/EN) | RO solid; HU/EN fără risk_terms |
| 9 | Eval & Observability | behind | fundație matură; fără NDCG/MRR, A/B, drift, safety-eval |
| 10 | Data & Catalog Quality | **far_behind** | 283/500 rating 5.0, `product_url` 0/500, concerns 0, nume cu ID |

---

## 2. Lista MASTER de gap-uri (deduplicate — agenții au repetat aceleași ~25 ori)

> „✅ V" = verificat adversarial în cod (confirmat real, 24/24, 0 false). Categorie:
> **B**=behavior(cod) · **D**=date(seed/sync) · **P**=platformă(retailer) · **E**=eval · **S**=safety.

### Comportament — „creierul" (îngust, recuperabil ieftin)
| Gap | Cat | Sev | ✅ | Ce avem azi / fix |
|---|:--:|:--:|:--:|---|
| Disclosure pe **produs NUMIT inexistent** (nu doar brand) | B | high | ✅ | doar brand-not-found (`catalog_tools.py:426`); adaugă semnal `named_product_not_found` + prompt |
| Onorare **„cumpăr acum"** → stoc/ETA/push coș | B | high | ✅ | tool-uri există dar nelegate; flag purchase-intent în triaj + CTA coș |
| **Nevoie inventată** în justificare („ten sensibil" nespus) + lipsă hedge rafinare | B | high | ✅ | scrub taie cifre, NU atribute nemenționate; extinde scrub + hedge |
| **Relaxare tăcută** (`relaxed`/`relax_depth` calculate, neconsumate) | B | med | — | propagă în `llm_view` + disclosure per-locale |
| Motive de card **tautologice** („uniform—uniformizează") | B | low | — | prompt: ancoră pe atribut real; blocat parțial de date |
| Ingredient/atribut activ tratat **soft**, relaxat tăcut | B | med | — | filtru dur la nivel variantă, relaxat ultimul + disclosure |
| **Cite-before-you-speak** la întrebări de atribut fără evidență | B | low | — | declară lipsa în loc de gloss |

### Personalizare & Proactiv (structural — piloni de venit)
| Gap | Cat | Sev | ✅ | Ce avem azi / fix |
|---|:--:|:--:|:--:|---|
| **PROACTIV: 0 inițiatori creează `proactive_jobs`** → zero mesaje proactive în prod | B | **CRITICAL** | ✅ | motor NX-70 + gating NX-71 OK, dar nimeni nu INSEREAZĂ joburi; cablează cei 4 inițiatori |
| Proactiv: **calea `template` blocată în dispatcher** → nimic în afara ferestrei 24h | B | high | ✅ | `dispatcher.py:125` respinge template; extinde ChannelSender Meta |
| **Detecția coșului abandonat** nespecificată (fără sursă persistentă) | B | high | ✅ | sweeper pe `checkout_links` neconvertite |
| **Lifecycle nescris niciodată** — toți clienții rămân „new" | B | high | ✅ | job determinist new→engaged→customer→repeat→churn_risk |
| **Memorie de navigare** inexistentă (0 tabel viewed/search) | B | high | ✅ | tabel `browsing_events` + agregator în profil |
| **lead_score calculat dar NEcitit** de agent (câmp mort) | B | med | — | injectează bucket-ul în prompt → bias spre checkout la high-intent |
| **RFM coloană moartă** (0 referințe în `src/`) | D | med | — | rollup nocturn din `orders` → segmentare |
| **Coșul nu supraviețuiește** conversației | B | med | — | persistă coș la nivel contact + reminder |
| Proactiv fără **segmentare** lifecycle/RFM | B | med | — | branch pe segment în builders |

### DATE (cel mai mare ROI vizual — seed/sync, nu cod)
| Gap | Cat | Sev | ✅ | Ce avem azi / fix |
|---|:--:|:--:|:--:|---|
| **Rating-uri uniforme** (283/500 la 5.0, review_count=0) → badge blindness + PR #140 invizibil | D | high | ✅ | re-seed rating/review variate (4.3–4.9) |
| **Nume cu ID rezidual** („…328/…003") | D | high | ✅ | curăță seed / strip determinist |
| **`product_url` 0/500** + `ai_summary` templat + **concerns 0** | D | high | ✅ | populează URL/atribute/top_pros reale |
| **delivery cutoff/ETA** câmp de card lipsă | D/P | high | ✅ | regulă „comandă până la X→mâine" + model + FE contract |
| **voucher-la-coș** câmp de card lipsă | D/P | med | — | feed promo + FE contract |

### Eval
| Gap | Cat | Sev | ✅ | Fix |
|---|:--:|:--:|:--:|---|
| **Fără NDCG@10/MRR** ca gate CI de ranking | E | high | ✅ | golden stratificat + NDCG/MRR; drift pe top-1 cosine |
| Fără metrici de **outcome lifecycle** (recovery/atribuire proactivă) | E | med | — | leagă outbox proactiv de atribuire |
| Cross-encoder/LTR absent (scor handcrafted) | B | low | — | amânat până la metrici+trafic |

---

## 3. Verificarea adversarială — semnal puternic

**24 din 24 gap-uri high/critical confirmate, 0 respinse.** Un agent separat (Explore) a căutat
fiecare gap în codul real cu mandatul de a-l RESPINGE dacă găsim deja capabilitatea. Niciunul n-a
fost halucinat — gap-urile sunt reale. (7 gap-uri high/critical au rămas neverificate, peste capul
de 24, din economie de buget — listate ca atare.) Câteva corecții pe care verificarea le-a adus
peste hărți:
- **Proactiv:** wrapper-ele `get_approved_template`/`is_in_24h_window` raportate „missing" în hartă
  EXISTĂ acum cu teste — gap-ul real e că **nimeni nu inserează joburi** (PL-1).
- **Personalizare:** profilul de contact PERSISTĂ cross-conversație (nu „blank slate") — gap-ul e
  lifecycle/RFM/navigare, nu fundație.
- **Date:** nu „toate 4.8" ci **283/500 la exact 5.0 cu review_count=0** + `product_url` 0/500 +
  atribut `concerns` complet gol.

---

## 4. Ce a ratat CHIAR analiza (criticul de completitudine) — partea cea mai valoroasă

Criticul a identificat că cele 10 sinteze au **convers pe aceleași 3-4 gap-uri** (named-entity,
purchase-intent, rating-variance, eval) repetate de ~8 ori — **iluzie de acoperire** din ancorarea
pe UN singur tur observat. Dimensiuni întregi lipsesc:

1. **🚨 Trust & Safety / sfat dăunător (beauty).** Zero guardrail pe claim-uri medicale/dermatologice
   („tratează acneea", „sigur în sarcină", „fără alergeni", interacțiuni retinol+vit C). `RISK_PATTERNS`
   acoperă doar `human_request`+`legal_complaint`. **Gap de RĂSPUNDERE JURIDICĂ**, nu de calitate.
2. **🚨 Buget de latență/cost.** Niciun gap propus n-are buget de latență/cost. Pipeline-ul
   (nano+mini+≤3 tool calls+validator+retry) poate depăși 5-8s; iZi <2-3s. Toate recomandările
   ADAUGĂ (cross-encoder, NER, RFM jobs, eval) fără buget — **exact eroarea Klarna** pe care analiza
   o citează dar n-o aplică.
3. **Vision/multimodal** (poză→produs, shade/skin-match — killer feature beauty 2026) — în
   arhitectură (Gates media routing) dar **niciodată evaluat** vs Sephora/Amazon Lens.
4. **Voice/STT** (voice notes RO domină WhatsApp) — menționat, zero paritate evaluată.
5. **Coerență multi-tur** (context drift, răzgândire mid-cart, recovery) — **tot ce s-a analizat e
   single-turn**; partea cea mai grea a unui asistent e necotată.
6. **Post-vânzare / retur / AWB / order-status** — Commerce s-a oprit la checkout. Și **web login
   passthrough (NX-128/129/130 — exact branch-ul curent!)** nu apare în nicio dimensiune.
7. **Reliability sub load** (lock per conversație, debounce, rate-limit — toate TODO în CLAUDE.md):
   2 mesaje rapide consecutive = race invizibil în single-turn.
8. **Cross-channel parity** (WhatsApp carousel max 10 vs Web JSON vs Telegram text) — presupusă, nu
   verificată: supraviețuiesc cele 5 semnale de card pe WhatsApp real?

Plus meta-critici ascuțite:
- **PL-1 (proactiv neoperant) e sub-cotat** ca „behind" — un pilon întreg de venit (recuperare coș
  abandonat, cea mai profitabilă buclă) e mort în prod; ar trebui să **domine** prioritizarea, dar e
  îngropat sub 8 repetări ale gap-ului de named-entity.
- **Insight-ul „semnalele sunt date, nu AI" e folosit ca scuză** să declasăm gap-uri — dar DECIZIA
  *care* 3-din-5 semnale surfacează, în ce ordine, cu ce CTA, **ESTE orchestrare/creier**.
- **Dependența de date = risc de business neexaminat:** dacă ~85% din diferență e date și datele vin
  din feed-ul fiecărui CLIENT, paritatea cu iZi e **gated pe igiena datelor clientului**, nu pe codul
  nostru — o problemă contractuală/SLA, nu de cod.

---

## 5. Roadmap prioritizat (revizuit după critic)

**Val 0 — corecții de prioritate impuse de critic:**
- **P0-safety:** guardrail pe sfat medical/beauty (claim-uri dermatologice/sarcină/alergeni) —
  răspundere juridică, **înaintea** finisajelor cosmetice.
- **P0-proactiv:** cablează cei 4 inițiatori de `proactive_jobs` + calea template în dispatcher —
  deblochează un pilon de venit mort (abandoned-cart recovery ~10% → +20-25% vânzări).
- Stabilește un **buget de latență/cost per tur** înainte de a adăuga orice (cross-encoder, NER, eval).

**Val 1 — quick wins de comportament (S/M):** A1 disclosure named-entity, A2 purchase-intent + CTA
coș, anti-nevoie-inventată + hedge, relaxare cu disclosure.

**Val 2 — DATE (cel mai mare ROI vizual, M):** re-seed catalog (rating variat, nume curate, URL,
atribute/top_pros, concerns), recalibrare badge (15-25%), + 2 câmpuri card (delivery/voucher → FE).

**Val 3 — structural (M-L):** lifecycle scris + RFM + lead_score citit + memorie navigare; eval
NDCG/MRR + safety/robustness red-team; multi-tur coherence; post-vânzare/retur; cross-channel parity;
reliability sub load; HU/EN parity; Vision/Voice.

---

## 6. Insight-ul unic

Pe **creier** suntem competitivi (uneori înaintea iZi prin grounding-ul structural). Diferența
percepută e **DATE + un set îngust de comportamente de onestitate/funnel**. DAR adevăratul rezultat
al acestei analize nu e lista de gap-uri vizibile — e că **am fost ancorați de un singur tur și am
ratat unde pierdem cel mai mult: proactivul mort în prod, sfatul medical fără gardă, costul/latența
ne-bugetate, și tot ce e dincolo de single-turn (Vision/Voice/multi-tur/post-vânzare).** Paritatea
reală cu iZi nu se câștigă pe „crema X", ci pe aceste piloni neevaluați.

---

### Anexă — proces & limitări
- Workflow `wf_08232165-a65`: 51 agenți, ~2.4M tokens, 919 tool-calls; verificare 24/24 confirmate.
- ⚠️ Sintezele converg pe un singur tur observat (bias de ancorare, semnalat de critic) → dimensiunile
  din §4 (safety/latency/vision/voice/multi-tur/post-vânzare/reliability/cross-channel) **nu au fost
  cartografiate din cod**, doar identificate ca lipsuri — necesită o rundă dedicată per pilon.
