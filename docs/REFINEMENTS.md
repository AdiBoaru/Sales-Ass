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

---

## ✅ Implementate

- **W1 v1 — carduri compacte** (listă text + butoane-link), 2026-06-14. Înlocuiește
  pozele mari individuale. Vezi R2 pentru pașii „pro" următori.
