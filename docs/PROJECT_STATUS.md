# Nativx Assistant — Status proiect

_Actualizat: 2026-06-13 · Bază: `main` la zi (PR #1–#23) · Document VIU — se
actualizează la fiecare milestone; data stă aici, nu în numele fișierului._

Document de referință pentru: (1) ce e implementat și în ce stadiu, (2) riscuri
și datorie tehnică, (3) ce urmează — material pentru generarea taskurilor.

---

## 1. Executive summary

- **Milestone „echo e2e" ATINS** (G1 + G2): un mesaj inbound parcurge tot drumul
  — webhook (semnătură, dedupe, stream) → consumer → rezolvare tenant →
  contact/conversație → pipeline (echo determinist) → reply tranzacțional în
  `outbox`. Fără LLM încă, dar fluxul și contractele sunt dovedite pe DB real.
- **Dedupe în 2 straturi LIVE (NX-51, P0 din audit):** Redis SET NX (webhook) +
  `inbound_dedupe` ne-partiționat (worker, migrarea 004 aplicată). Gaura cu
  unique-ul pe tabela partiționată e închisă.
- **Capătul de ieșire NU există încă:** scriem în `outbox`, dar nimeni nu trimite
  la Meta. **Dispatcher-ul e următorul pas natural** — nu are nevoie de cheia
  OpenAI și închide bucla.
- **Blocker pe drumul critic: T017 (cheia OpenAI, manual Adi)** — fără ea nu
  pornesc G3 (triaj/agent live) și nici `embed_products`.

## 2. Ce e în main (delta față de 2026-06-12)

| Componentă | Stare | PR |
|---|---|---|
| G1: queries runtime (contacts, conversations, messages, outbox) | ✅ + 10 teste integration | #19 |
| G2a: POST /webhook (semnătură, parser Meta, dedupe L1, XADD) | ✅ + 13 teste CI | #20 |
| G2b: consumer + runner + processor (echo e2e) | ✅ + 8 teste | #21 |
| NX-51: dedupe layer 2 (`inbound_dedupe`, 004 aplicat live) | ✅ + 3 teste | #21→**#23** (recuperat) |
| Task cards + audit docs + TODO-MANUAL în repo | ✅ | #22 |

Fundațiile anterioare (infra Z1, schema v2 + RLS 003, config/pool/models,
search_products) — vezi istoricul PR #1–#18.

## 3. Stadiu pe pipeline (cele 9 stagii)

| # | Stagiu | Stare |
|---|---|---|
| 1 | Webhook: GET verify + POST inbound | ✅ |
| 2 | Redis backbone: stream + consumer group + dedupe 2L | ✅ (TODO: debounce, lock multi-consumer, rate limit, cost guard) |
| 3 | Gates | ❌ |
| 4 | Straturi gratuite | ❌ (faqs=0, cache=0) |
| 5 | Triaj (nano) | ❌ — blocat de T017 |
| 6 | Context builder | ❌ |
| 7 | Agent (mini) + tools | 🟡 doar `search_products` (SQL) |
| 8 | Validator | ❌ |
| 9 | Sender → outbox | ✅ contract implementat în processor / **dispatcher ❌** |
| — | Status webhook (delivered/read/failed) | ❌ |
| — | Proactiv / Jobs (embed, rollup, cleanup partiții) | ❌ (doar cleanup_dedupe ✅) |

## 4. Riscuri & datorie tehnică (curente)

1. **Worker-ul se loghează ca `postgres` + SET ROLE** (`tenant_conn`) — NX-50
   (P0-A audit) cere rol de LOGIN `bot_runtime`. De făcut înainte de load real;
   NX-04 (assert la checkout) și NX-53 (test concurent) vin peste.
2. **Echo stage e scaffold** — marcat explicit; nu intră în producție.
3. **Dedupe claim-first:** crash între claim și finalizarea turului = mesaj marcat
   văzut dar neprocesat. Dead-letter / reaper = follow-up (notat în #21).
4. **`get_or_create_conversation` are race teoretic** pe primul mesaj al unui
   contact nou (fără unique pe open-conv). Mitigat de debounce+lock când apar;
   advisory lock = follow-up.
5. **Rândurile `dispatching` orfane în outbox** (worker mort între claim și mark)
   au nevoie de reaper — intră natural în taskul de dispatcher.
6. **Evenimentele din runner nu se persistă** încă în `analytics_events`.
7. **`product_url` NULL + `ai_summary` templat + embeddings=0** — limitări de
   date demo, nu de cod (embed blocat de T017).
8. **Proces git:** 3 commit-uri orfane până acum (#15, #17, #23). Regulă nouă în
   CONTRIBUTING: branch cu PR deschis = ÎNGHEȚAT.

## 5. Ce urmează (ordine recomandată)

1. **Dispatcher (outbox → Meta)** — închide bucla de ieșire. Client HTTP Meta cu
   mock în teste; retry pe `mark_failed`; reaper `dispatching`; salvează
   `provider_msg_id` pe messages. Fără dependențe externe.
2. **Status webhook** — `webhook/status.py`: delivered/read/failed →
   `message_status_events` + update `messages.status`.
3. **NX-02 (Redis durabil)** — config compose: AOF, parolă, noeviction. Rapid.
4. **Adi: T017 (cheia OpenAI)** → deblochează G3 + embeddings.
5. **G3: LLM adapter + Triaj (nano) + prompt_builder** — cu replay/mock în CI.
6. **NX-50/04/53 (rol login + assert + test concurent)** — înainte de load real.
7. **G4: agent + tools + validator + sender real** · apoi G5 (gates, free layers,
   context builder, jobs) · G6 (proactiv).

**Milestone următor:** „WhatsApp e2e live" — mesaj real de pe telefon → răspuns
echo pe telefon. Cere: dispatcher (cod) + T013/T015 (manual: Meta app + tunel) +
rândul în `channels` pentru demo.

### Cale de test alternativă: Telegram pe VPS (epic NX-60→63)
Decizie 2026-06-13: pentru iterare rapidă, testăm botul vorbind direct pe
**Telegram** (long polling pe VPS, fără Meta/HTTPS/tunel). Aditiv — WhatsApp rămâne
primar. Cards:
- **NX-60** abstracție de canal (envelope neutru + ChannelSender registry) — fundație
- **NX-61** Telegram inbound (long polling) · **NX-62** Telegram outbound (TelegramClient)
- **NX-63** onboarding canal Telegram demo + rulare VPS (manual: BotFather token)

Ajunge la „Telegram e2e live" MULT mai repede decât WhatsApp (zero birocrație Meta).
> Notă: secțiunile 2-4 de mai sus se vor reîmprospăta după ce intră în main PR-urile
> deschise (#25 dispatcher, #26 status, #27 NX-02, #28 analytics).

## 6. Decizii de arhitectură luate pe parcurs (nereflectate în planul inițial)

- **Stream unic `inbound`** (nu per conversație): conversation_id nu e cunoscut
  la webhook fără DB; ordinea per conversație se rezolvă în worker (lock, TODO).
- **`admin_conn` (control plane)** — excepție unică documentată de la
  „business_id pe tot": lookup `phone_number_id → business_id` precede tenantul.
- **Dedupe NU pe `messages`** — unique-ul include cheia de partiționare; 2 straturi
  (Redis + `inbound_dedupe`).
- **`last_inbound_at` în worker** (processor), nu în webhook — webhook fără DB.
