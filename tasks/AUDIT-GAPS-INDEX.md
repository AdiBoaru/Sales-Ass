# Index carduri din auditul de arhitectură (NX-70 … NX-98)

_Generat: 2026-06-16 · Sursă: audit de cod per subsistem (verificat în `main`, nu în docs)._

**52 carduri noi · ~244h**, în două loturi:
- **Partea 1 — găuri de cod uncarded (NX-70 … NX-98), 29 carduri ~105h** — vezi tabelul de mai jos.
- **Partea 2 — backlog expandat din [`NX_backlog_compact.md`](NX_backlog_compact.md) în carduri complete
  (NX-03/06/07/09/11/13/14/17/30/31/32/33/41/42/43/54 + epic web NX-20..26), 23 carduri ~139h** —
  vezi secțiunea „Backlog complet" mai jos. (Spec-ul compact rămâne ca rezumat; cardul complet e sursa de implementare.)

Restul (F2 bucla de bani, G8-1 evals, G6-2 context builder, G7-3 check_order, NX-16 security golden)
sunt **deja în main** — vezi `git log`.

---

## Toate cardurile

| ID | Titlu | Cmplx | Est | Prioritate | Dep cheie |
|----|-------|:-----:|:---:|:----------:|-----------|
| [NX-70](NX-70.md) | Motorul proactiv — scheduler peste `proactive_jobs` | M | 4h | 🔴 | NX-71 |
| [NX-71](NX-71.md) | Gating proactiv: consent + 24h + template approved | S | 3.5h | 🔴 | — |
| [NX-72](NX-72.md) | Strat GDPR Python — erase + export/access | M | 4h | 🔴 | 003 |
| [NX-73](NX-73.md) | Strat gratuit: alias lookup (`intent_aliases`) | S | 3.5h | 🔴 | G5a/b/c |
| [NX-74](NX-74.md) | Strat gratuit FAQ + tool `faq_lookup` (`faqs`) | M | 4h | 🔴 | G7 |
| [NX-75](NX-75.md) | Media routing: STT voce (Whisper) în Gates | M | 4h | 🔴 | NX-60 |
| [NX-76](NX-76.md) | Media routing: Vision imagine→catalog | L | 7h ⚠️ | 🔴 | NX-60, embed |
| [NX-77](NX-77.md) | Clarificare deterministă din `pending_question` | S | 3h | 🔴 | G7 |
| [NX-78](NX-78.md) | `prompt_builder` din `categories` + prompt caching | M | 4h | 🔴 | G7 |
| [NX-79](NX-79.md) | Tool-uri agent: `cart_add` + `reorder` | M | 4h | 🟠 | G7, F2 |
| [NX-80](NX-80.md) | Tool-uri: `subscribe_back_in_stock` + `delivery_eta` | S | 3.5h | 🟠 | NX-79* |
| [NX-81](NX-81.md) | Tool `book_appointment` + Google Calendar | M | 4h | 🟠 | G7 |
| [NX-82](NX-82.md) | Tool `request_human` + activare tool-uri per business | S | 3.5h | 🟠 | G5a |
| [NX-83](NX-83.md) | Wiring scheduler/cron pentru joburi (compose) | M | 4h | 🔴 | F2-3 |
| [NX-84](NX-84.md) | Job cleanup — drop partiții + expire `semantic_cache` | S | 3h | 🟠 | NX-83 |
| [NX-85](NX-85.md) | Lock per conversație (ordonare multi-consumer) | S | 3.5h | 🔴 | R1, NX-51 |
| [NX-86](NX-86.md) | XAUTOCLAIM reaper + dead-letter inbound | M | 4h | 🔴 | NX-51 |
| [NX-87](NX-87.md) | Debounce durabil + creare conversație fără race | M | 4h | 🟠 | R1, NX-51 |
| [NX-88](NX-88.md) | Post-tur: extractor profil (nano) + `lead_score` | M | 4h | 🟠 | G6-2 |
| [NX-89](NX-89.md) | WhatsApp outbound bogat (carduri/carusel/media) | M | 4h | 🟠 | NX-60, R2 |
| [NX-90](NX-90.md) | Typing indicator + spargere mesaj >200ch | S | 3.5h | 🟠 | #26 |
| [NX-91](NX-91.md) | Validator: cifre fără valută (numere halucinate) | S | 3h | 🟠 | G7-1 |
| [NX-92](NX-92.md) | Evals: LLM-as-judge + `conversation_evals` + `golden_tests` | L | 4h | 🟠 | G8-1 |
| [NX-93](NX-93.md) | Shadow mode (propune-nu-trimite) + candidați alias | S | 3.5h | 🟠 | NX-73 |
| [NX-94](NX-94.md) | Orders webhook: HMAC peste corpul brut | S | 2h | 🟠 | F2-2 |
| [NX-95](NX-95.md) | Detecție de limbă: bibliotecă în requirements | S | 2.5h | 🟢 | G5c |
| [NX-96](NX-96.md) | State 8KB: tăiere în cod (nu doar CHECK DB) | S | 2h | 🟢 | G6 |
| [NX-97](NX-97.md) | Provisioning `bot_runtime` LOGIN (apply_005 + env) | S | 1.5h | 🔴 ops | NX-50 |
| [NX-98](NX-98.md) | `search_products` live: fallback SQL + backfill `product_url` | M | 4h | 🔴 data | embed |

⚠️ **NX-76** = 7h > 4h → cardul propune split `NX-76a` (descarcă+Vision→text) / `NX-76b` (match catalog).
\* **NX-80** are overlap de fișier cu NX-79 (`commerce_tools.py`) → coordonare la merge.

---

## Lanțuri de dependențe (ce trebuie făcut înainte)

```
NX-71  ──▶ NX-70            (gating înainte ca scheduler-ul să-l consume)
NX-83  ──▶ NX-84            (serviciul de cron înainte de a programa cleanup-ul)
NX-73  ──▶ NX-93            (cititorul de alias înainte; shadow-ul îi produce candidați)
NX-79  ──▶ NX-80            (doar coordonare de fișier pe commerce_tools.py)
003 (în main) ──▶ NX-72
NX-51 (în main) ──▶ NX-85, NX-86, NX-87
```
Tot restul n-are dependențe noi între ele (doar pe lucruri **deja în main**).

## Ordine recomandată (valuri)

1. **Val 1 — câștiguri de securitate/operare ieftine, fără cod nou de pipeline:**
   **NX-97** (1.5h, închide scurgerea P0-A în prod) · **NX-94** (2h, HMAC orders) ·
   **NX-98** (4h, repară `search_products` pe demo) · **NX-95** (2.5h) · **NX-96** (2h).
2. **Val 2 — subsistemele goale (cel mai mare gap funcțional):**
   **NX-71 → NX-70** (proactiv) · **NX-72** (GDPR) · **NX-83 → NX-84** (cron + cleanup).
3. **Val 3 — straturile gratuite + agentul corect:**
   **NX-73**, **NX-74**, **NX-77** (deflectare ieftină) · **NX-78** (prompt din DB + caching).
4. **Val 4 — scale-out safety:** **NX-85**, **NX-86**, **NX-87**.
5. **Val 5 — media + tool-uri + UX outbound:** **NX-75/76**, **NX-79/80/81/82**, **NX-89/90**.
6. **Val 6 — calitate/observabilitate:** **NX-88**, **NX-91**, **NX-92**, **NX-93**.

## Hotspot-uri de conflict (NU lucra în paralel fără coordonare)

Aceste fișiere sunt atinse de multe carduri — secvențiază sau merge cu grijă:
- **`src/config.py` + `.env.example`** — ~15 carduri (adaugă fiecare setările lui; conflicte triviale).
- **`src/worker/runner.py` (`DEFAULT_STAGES`)** — NX-73, NX-74, NX-77, NX-75/76 (ordinea stagiilor).
- **`src/worker/stages/agent.py`** — NX-74, NX-78, NX-79, NX-91 (+ enablement NX-82).
- **`src/tools/base.py` + `tool_definitions.py`** — NX-74, NX-79, NX-80, NX-81, NX-82.
- **`src/tools/commerce_tools.py`** — NX-79 ⨯ NX-80.
- **`src/worker/processor.py`** — NX-75, NX-77, NX-79, NX-88, NX-93.
- **`src/agent/llm.py`** — NX-75, NX-76, NX-88, NX-92 (metode noi pe adaptorul unic).
- **`src/worker/consumer.py`** — NX-85, NX-86, NX-87, NX-90.

## Paralelizabile în siguranță (fișiere disjuncte)
- **NX-72** (GDPR, `src/gdpr/`) ⟂ orice altceva.
- **NX-94** (`signature.py`/`app.py`) ⟂ **NX-95** (`lang/detect.py`) ⟂ **NX-97** (ops/docs).
- **NX-70/71** (`src/proactive/`) ⟂ **NX-92** (`src/evals/`) ⟂ **NX-84** (`maintenance.py`).

---

## Backlog complet (expandat din `NX_backlog_compact.md`) — 23 carduri · ~139h

| ID | Titlu | Cmplx | Est | Temă | Dep cheie |
|----|-------|:-----:|:---:|------|-----------|
| [NX-03](NX-03.md) | Alerte consumer lag + outbox depth | S | 4h | observabilitate | — |
| [NX-06](NX-06.md) | CTWA referral → atribuire ads ⓢ | M | 6h | atribuire/ads | NX-60, F2-2 |
| [NX-07](NX-07.md) | Pacing proactiv + quiet hours ⓢ | M | 6h | proactiv | NX-70/71 |
| [NX-09](NX-09.md) | Retry failed re-engagement | S | 4h | proactiv | NX-70/71 |
| [NX-11](NX-11.md) | systemd units + healthchecks | S | 3h | ops/deploy | NX-83 |
| [NX-13](NX-13.md) | Registru procesatori + notă de informare | S | 3h | conformitate | T180, NX-41 |
| [NX-14](NX-14.md) | Supabase regiune UE + ADR OpenAI EU | S | 4h | conformitate | NX-50 |
| [NX-17](NX-17.md) | `schema_version` (v:1) în stream | S | 3h | infra/deploy | NX-60 |
| [NX-30](NX-30.md) | Promotions: tabel + tool + validator ⓢ | L | 20h | comerț | NX-91 |
| [NX-31](NX-31.md) | Export CRM: webhook + CSV zilnic ⓢ | L | 12h | integrare | NX-88, NX-52 |
| [NX-32](NX-32.md) | Cross-sell map + free-shipping gap ⓢ | L | 16h | comerț/upsell | NX-79, G6-2 |
| [NX-33](NX-33.md) | Funnel + cohorte Metabase ⓢ | L | 8h | analytics | F2-3, NX-72 |
| [NX-41](NX-41.md) | `create_tenant.py` idempotent ⓢ | L | 6h | onboarding | NX-52, T180 |
| [NX-42](NX-42.md) | pip-audit + renovate + digest pin | S | 4h | supply-chain | NX-02 |
| [NX-43](NX-43.md) | Whitelist chei `contacts.profile` | S | 4h | profil/GDPR | NX-88 |
| [NX-54](NX-54.md) | Tarife Meta per piață în config | S | 4h | facturare | G2c, NX-07 |
| [NX-20](NX-20.md) | Web W1 — Gateway SSE ⓢ | L | 10h | web widget | NX-60/61 |
| [NX-21](NX-21.md) | Web W2 — `widget.js` | M | 4h | web widget | NX-20 |
| [NX-22](NX-22.md) | Web W3 — canal web în gates/sender | M | 4h | web widget | NX-20, NX-90 |
| [NX-25](NX-25.md) | Web W3 — CORS/CSP + allowlist | S | 3h | web widget | NX-20 |
| [NX-23](NX-23.md) | Web W4 — identitate vizitator + merge | M | 4h | web widget | NX-20/22/25 |
| [NX-24](NX-24.md) | Web W5 — context pagină | S | 3h | web widget | NX-20/22 |
| [NX-26](NX-26.md) | Web W6 — golden web + load SSE | M | 4h | web widget | NX-20..25, G8-1 |

ⓢ = umbrella >4h, cu split `[PROPUNERE]` în card: **NX-06**(a/b) · **NX-07**(a/b) · **NX-30**(a/b/c/d) ·
**NX-31**(a/b/c) · **NX-32**(a/b/c) · **NX-33**(a/b/c) · **NX-41**(a/b) · **NX-20**(propune split SSE/sesiune).

**Lanțuri backlog:** NX-70/71 ▶ NX-07, NX-09 · NX-88 ▶ NX-43, NX-31 · NX-83 ▶ NX-11 ·
NX-91 ▶ NX-30 (validator discount) · **epic web E26 strict secvențial:** NX-20 ▶ {21, 22, 25} ▶ 23 ▶ 24 ▶ 26.
**Epicul web (NX-20..26, ~32h) = V1.5** — după primul client stabil pe WhatsApp; nu-l ataca înainte.

---

## Architecture review (lucruri noi față de plan)
- **NX-76** depășește 4h → spart în 76a/76b (în card).
- **⚠️ Coliziune de numere de migrare — de rezolvat înainte de orice DDL.** Mai multe carduri
  au ales independent același număr: **`009`** (NX-72 `gdpr_svc`, NX-86 inbound, NX-87 conversations,
  NX-41 faq-seed-unique) și **`006`** (NX-32 cross-sell; există deja `006` pentru semantic cache).
  **Alocă numere de migrare unice și crescătoare** (next liber ≈ `006`+) la momentul implementării —
  nu lua la literă numărul din card.
- **NX-76** (7h) și **NX-20** (10h) depășesc 4h → split propus în card (76a/b; SSE vs sesiune/auth).
- **NX-80/NX-81** ating integrări externe absente (curier/ERP, Google Calendar) → ambele
  conțin `[PROPUNERE]` cu stub determinist documentat până vine integrarea reală.
- **Epicul web E26 (NX-20..26)** introduce un al treilea canal (`channel_kind='web'`) + pachetul nou
  `src/web/` + `web/widget.js` (front-end static). E **V1.5** — păstrat în backlog, nu în drumul critic.
- Câteva carduri ating fișiere partajate cu lotul NX-70..98 (`context.py`, `agent.py`, `profile.py`,
  `signature.py`, `limits.py`) — vezi „Hotspot-uri de conflict"; secvențiază la implementare.
