# Refinements din testarea live (de implementat MAI TÂRZIU)

Observații apărute în testele live cu botul, pe care le **rafinăm după** ce
funcționalitatea de bază (pipeline-ul LLM complet) e gata. Adi le notează în
timpul testelor; NU le rezolvăm pe loc — le prioritizăm separat aici ca să nu
se piardă.

---

## 🔧 Deschise

### R1 — Debounce: mesajele succesive sunt tratate ca tururi separate · P1

**Observat:** 2026-06-13, test G3 (triaj) pe `@solechat_bot`. Trimise rapid, una
după alta: „salut", „ce faci", „acuma", „vreau să comand ceva" → **4 tururi
separate → 4 răspunsuri independente**, fiecare clasificat izolat (răspunsuri
redundante, ex. „cu ce te pot ajuta" de două ori).

**Cauză:** lipsește debounce-ul în worker (stagiul 2). Fiecare update Telegram →
un event pe stream `inbound` → un `handle_turn`. Nu există coalescing pe
conversație.

**Fix planificat:** debounce adaptiv ~2-3s per conversație — adună mesajele din
fereastră și procesează-le ca **UN singur tur** (lot de mesaje, NU string lipit),
+ lock per conversație pentru ordine. E deja în TODO-ul arhitecturii (CLAUDE.md
stagiul 2 + lista „Defer" din `worker/consumer.py`). De promovat în task când
ajungem la hardening-ul worker-ului.

**Simptome derivate (aceeași cauză):** răspunsuri redundante între mesaje
near-simultane; posibilă dezordine la răspunsuri concurente.

### R2 — Carduri de produs: format „pro" pe canale · P2 (Telegram FĂCUT)

**Context:** 2026-06-14, W1. Prima variantă (un `sendPhoto` per produs, poză +
buton) ocupa tot ecranul pe telefon (Telegram afișează `sendPhoto` mereu la
lățimea bulei — nu poate fi micșorat). Plus, o poză respinsă (placehold.co
returna SVG, Telegram vrea PNG) pica tot mesajul → retry storm (text de 3-4 ori).

**W1 v1:** **listă compactă** — UN mesaj cu text + buton-link per produs. Rămâne
ca **fallback** în dispatcher (canale fără suport de carusel).

**Telegram carusel — IMPLEMENTAT (R2):** UN card (poză+nume+preț) cu `◀ 🛒 ▶`;
`◀/▶` editează ACELAȘI mesaj (`editMessageMedia`), `🛒` = url-button spre pagină.
Navigarea = drum NON-LLM: `callback_query` → envelope `kind=callback` → handler
determinist (citește `displayed_products` din state, calculează indexul) → outbox
`edit_media`. Stateless (indexul în `callback_data`). Vezi `tasks/R2.md`.

**Rămas (WhatsApp prod):** Interactive **List Messages** + **Multi-Product
Messages** native (catalog Meta Commerce). Blocat pe WhatsApp e2e (T013) — task
separat.

### R3 — Follow-up de comparație/detalii pe produse afișate → DataError · P1

**Observat:** 2026-06-17, test de conversație live multi-tur (`scripts/convo_sim.py`,
demo). După ce botul a arătat 3 produse, mesajul „care dintre ele e cea mai bună?"
→ răspuns „Momentan n-am găsit produse potrivite"; în loguri `compare_products` și
`get_product_details` → **DataError**. Cascadează: turul următor („trimite-mi linkul
la prima") rămâne fără referent → nu generează checkout.

**Cauză:** `state_block` (`context.py:64-67`) expune produsele afișate ca **nume +
preț, FĂRĂ `product_id` (UUID)**. La un follow-up de comparație/detalii, agentul
(mini) n-are UUID-ul în context → pasează un id inventat (probabil numărul din numele
produsului, ex. „214") → `get_products_by_ids` face `p.id = any($2::uuid[])` →
DataError → zero produse → fallback. **Rupe pâlnia compară→detalii→checkout pe
produsele DEJA arătate.**

**Fix planificat:** expune `product_id`-ul în `state_block` (sau un bloc de referințe
dedicat) ca agentul să poată chema `compare_products`/`get_product_details`/
`checkout_link` pe produsele afișate fără re-căutare. Principiul 8: `product_id` E
ref-ul de state — trebuie expus, nu doar numele. Posibil overlap cu NX-116 (coș
persistent).

### R4 — Numărul din intro-ul rich e scrubuit (buget cerut de client) · P2

**Observat:** 2026-06-17, convo live. „ai ceva sub 80 de lei?" → intro-ul rich a ieșit
**„Ai ceva sub lei pentru ten gras?"** — lipsește „80".

**Cauză:** `compose.scrub_prose` taie TOATE cifrele din proză (anti-halucinație de
preț). Dar `intro` reia nevoia clientului în cuvintele LUI, inclusiv bugetul pe care
EL l-a zis → cifra clientului e înghițită → frază trunchiată/neîngrijită.

**Fix planificat:** o cifră pe care CLIENTUL a dat-o nu e halucinație. Fie exceptăm
`intro` de la scrub-ul de cifre, fie permitem cifrele care apar în mesajul clientului.
Grijă să NU reintroducem prețuri de produs inventate (intro nu citează prețuri de
produs, doar bugetul cerut).

### R5 — Cerere de operator uman → fallback generic (handoff neconsumat) · P1

**Observat:** 2026-06-17, convo live. „aș vrea să vorbesc cu un coleg uman" → **„Hmm,
n-am înțeles exact 🙂"** (fallback generic), în loc de escaladare la operator.

**Cauză:** triajul emite `Route.HANDOFF`, dar niciun stagiu nu-l consumă; `agent_stage`
iese no-op pe HANDOFF → cădere pe `fallback_stage`. (Confirmare live a găurii.)

**Fix planificat:** = **NX-123** (consumator rută handoff + `request_human` ca tool de
agent). Card existent, gata de implementat.

---

### R6 — FAQ policy: matching-ul pe prag relaxat alege uneori FAQ-ul vecin · P2

**Observat:** 2026-07-02, sim live (NX-137, după fix-ul de codec pgvector). „În câte zile
primesc comanda?" → hit corect (FAQ-ul de durată). Dar „Și în cât timp ajunge livrarea?" →
hit pe FAQ-ul de **livrare gratuită** („gratuită peste 200 lei"), nu pe cel de durată —
răspuns grounded, dar pe lângă întrebare.

**Cauză:** pragul relaxat pe întrebări de politică (#171, `faq_tau_policy`) acceptă cel mai
apropiat FAQ chiar când formularea clientului e la distanță de toate variantele seedate;
top-1 cosine nu e mereu cel semantic corect între FAQ-uri APROPIATE tematic (livrare-durată
vs livrare-cost).

**Fix planificat:** (a) mai multe variante de formulare în seed pe FAQ-ul de durată („în cât
timp ajunge", „când ajunge coletul"); (b) opțional, la prag relaxat: dacă top-2 sunt aproape
egale, servește-le combinat sau alege pe cuvinte-cheie (durată vs cost). De prioritizat după
ce strategia de seed FAQ per client e stabilă.

---

## ✅ Implementate

- **W1 v1 — carduri compacte** (listă text + butoane-link), 2026-06-14. Înlocuiește
  pozele mari individuale. Vezi R2 pentru pașii „pro" următori.
