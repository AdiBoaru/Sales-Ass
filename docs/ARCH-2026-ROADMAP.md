# Nativx Assistant — Roadmap arhitectural 2026 (consolidat)

> Sursă: trei audituri rulate 2026-06-21 — (1) **n8n-vs-noi** (adoption gap pe mecanica multi-tur),
> (2) **industry-benchmark-2026** (cum au făcut Sierra/Decagon/Intercom/Klarna/Shopify/Twilio/Rasa CALM),
> (3) **audit de cod 2026** (12 subsisteme citite la sursă, 30 de agenți). Acest doc le unifică într-o
> singură imagine pentru asistentul **single-brain (web + WhatsApp), multi-tenant, multi-vertical**.
> Cardurile noi: **NX-112 … NX-127** (în `tasks/`).

---

## 1. Rezumat executiv

**Suntem pe arhitectura corectă a anului 2026 și înaintea pieței pe partea grea.** Consensul industriei
(pipeline liniar, LLM doar la 2 puncte, validare pe output structurat ÎNAINTE de randare, channel
abstraction la 2 margini, izolare multi-tenant code-primary + RLS) **este deja arhitectura noastră**.
Cardurile noi nu schimbă filozofia — adâncesc exact ce le lipsește.

**Cele 3 urgențe (P0):**
1. **NX-112** — bug de persistență a state-ului: un slot umplut la clarificare (`clarify.py:46`) se pierde
   turul următor (processor-ul nu scrie `ctx.state.constraints`). Confirmat de toate 3 auditurile, netestat.
2. **NX-123** — fără tabel de tracking al migrărilor + runner ordonat: drift cod↔migrare (010-012 nu erau
   aplicate live) a crăpat primul mesaj al oricărui client nou în preprod. Risc de prod.
3. **NX-113** — retrieval-ul nu e hibrid (doar vector + ILIKE pe query întreg, fără RRF/rerank) → pică pe
   SKU/cod/exact-match exact unde se pierde conversia.

Plus P0-uri care trăiesc în carduri existente (de ridicat în prioritate): **NX-97** (compat-mode fără
`DATABASE_URL_BOT` rulează privilegiat sub pooler = bypass RLS), **NX-02** (revenue_attributed adună
comenzi anulate/refundate + valute mixte), **NX-09/NX-71** (fără producător de `proactive_jobs`).

## 2. Grade pe subsisteme (audit de cod 2026)

| Subsistem | Grad | Findings (severitate) |
|---|:---:|---|
| ingestion-security | **B** | 12 (P1:2 P2:5 P3:5) |
| worker-concurrency | **B** | 11 (P1:2 P2:6 P3:3) |
| routing-gates | **C** | 10 (P1:2 P2:5 P3:3) |
| agent-grounding | **B** | 11 (P1:2 P2:5 P3:4) |
| free-layers (alias/cache/FAQ/clarify) | **B** | 9 (P1:1 P2:4 P3:4) |
| retrieval-catalog | **C** | 11 (P1:2 P2:5 P3:4) |
| state-memory-profile | **C** | 9 (P1:2 P2:4 P3:3) |
| channels-rendering | **C** | 10 (P1:4 P2:3 P3:3) |
| commerce-proactive-jobs | **C** | 14 (**P0:2** P1:3 P2:6 P3:3) |
| db-schema-rls | **B** | 14 (**P0:1** P1:3 P2:8 P3:2) |
| config-observability-cost | **C** | 10 (P1:2 P2:5 P3:3) |
| tests-ci-evals | **C** | 10 (P1:3 P2:5 P3:2) |

Cel mai slab: **retrieval, state/memory, channels-rendering, commerce, observability, tests** (C) — exact
ariile pe care le țintesc cardurile noi. Cel mai solid: ingestie/securitate, worker, grounding, DB/RLS (B).

## 3. Fundație — Wave 0 (deblochează totul, fără să atingă „creierul")

- **NX-112** (P0, S) — fix persistență constraints + processor = singurul scriitor explicit de state.
- **NX-114** (P1, L) — schelet **DomainPack** (policy + taxonomie per-vertical ca config în DB) + **`Reply.offer`** neutru tipat.
- **NX-115** (P1, M) — **ChannelProfile** (capability matrix declarativ) care înlocuiește duck-typing-ul `hasattr`.
- **NX-123** (P0, M) — tracking migrări + runner ordonat + smoke-test de grant/policy.

Aceste 4 fac TOT restul corect: generic pe verticale (DomainPack), corect pe 2 canale (ChannelProfile +
Reply.offer), corect pe state (NX-112), sigur la deploy (NX-123).

## 4. Carduri noi — NX-112 … NX-127

| ID | Titlu | Wave | Prio | Efort | Sursă |
|---|---|:---:|:---:|:---:|---|
| [NX-112](../tasks/NX-112.md) | Persist clarify-filled constraints + processor = single state writer | Wave0 | P0 | S | cod+n8n+industry |
| [NX-113](../tasks/NX-113.md) | Hybrid retrieval: FTS/pg_trgm + vector via RRF + rerank determinist | Wave1 | P1 | XL* | cod+industry |
| [NX-114](../tasks/NX-114.md) | DomainPack: policy+taxonomie per-vertical ca config DB + `Reply.offer` neutru | Wave0 | P1 | L | industry+n8n+cod |
| [NX-115](../tasks/NX-115.md) | ChannelProfile capability matrix (înlocuiește `hasattr`) | Wave0 | P1 | M | industry+cod |
| [NX-116](../tasks/NX-116.md) | Triage confidence + sloturi structurate; escaladare anti-loop deterministă | Wave1 | P1 | M | industry+cod |
| [NX-117](../tasks/NX-117.md) | Scrub claim/superlativ/stoc pe calea de fallback prose (grounding) | Wave1 | P1 | M | cod |
| [NX-118](../tasks/NX-118.md) | Variant hydration pe read path + grounding nuanță/stoc | Wave1 | P2 | M | cod+n8n |
| [NX-119](../tasks/NX-119.md) | Search sessions: pool + paginare + dedup „nevăzute" („mai arată-mi") | Wave1 | P2 | L | n8n+cod |
| [NX-120](../tasks/NX-120.md) | Hardening ingestie DoS: cap body-size + cost/rate guard web fail-CLOSED | Wave1 | P1 | M | cod+industry |
| [NX-121](../tasks/NX-121.md) | Input guardrails la gate: clamp lungime + PII masking + screen injection | Wave1 | P2 | M | industry+cod |
| [NX-122](../tasks/NX-122.md) | Trace per-tur: stamp `turn_id` + capturare args/results tool-call | Wave2 | P1 | M | cod+industry |
| [NX-123](../tasks/NX-123.md) | Tracking migrări + runner ordonat + smoke-test grant/policy | Wave2 | P0 | M | cod |
| [NX-124](../tasks/NX-124.md) | Corectitudine filtre catalog: taxonomie-din-DB + index GIN + guard cache/FAQ | Wave3 | P2 | L | cod |
| [NX-125](../tasks/NX-125.md) | Cost guard precis per-tur + cap de cheltuială per-contact/visitor | Wave2 | P2 | M | cod |
| [NX-126](../tasks/NX-126.md) | Lot quick-wins grounding/gate: risk-reply non-cacheable, timeout+retry LLM, homoglife greeting | Wave1 | P2 | M | cod |
| [NX-127](../tasks/NX-127.md) | Paritate web rich-render: `WebSender.send_rich` peste SSE = o singură sursă de randare | Wave3 | P1 | M | cod |

\* **NX-113** e XL → are split `[PROPUNERE]` în card.

**Channel-aware & generic:** fiecare card tratează explicit (a) cum se comportă pe web (buton/card) vs
WhatsApp (CTA text), păstrând UN creier; și (b) cum rămâne agnostic de vertical (chei dinamice / config
DomainPack, fără vocabular hardcodat). Niciun card nou nu introduce un al 3-lea punct LLM (rerank-ul din
NX-113 e determinist).

## 5. Reconciliere cu backlog-ul existent (NU dubla)

Auditul a produs 48 de findings reconciliate: **20 noi** (cardate NX-112+), **18 extind** carduri
existente, **10 deja cardate**. De adăugat ca felii în cardurile existente (nu carduri noi):

| Finding | Status | Card | De adăugat acolo |
|---|---|---|---|
| State 8KB doar prin DB CHECK (overflow → reply tăcut) | already_carded | NX-96 | clamp determinist + event state-size |
| `StateConflict` ridicat dar neprins (retry nedocumentat) | extends | NX-85 | catch→reread→remerge→retry-once |
| Risk patterns RO-only, substring, „avocat"≈avocado | extends | NX-105 | locale-keyed + word-boundary + per-business (home generic = NX-114) |
| Sort preț/rating doar pe fereastra HNSW, nu set global | extends | NX-107 | sort pur-SQL pe setul filtrat complet |
| Re-queue exhaustion drop silent mesaj client (P6) | extends | NX-86 | never-drop → dead-letter pe epuizare |
| Conv-lock TTL 30s < worst-case tur | extends | NX-85 | aliniază TTL la CLAIM_TTL sau heartbeat |
| Outbox „dead" fără alertă | extends | NX-01/NX-86 | event + operator_alert pe tranziția dead |
| Rate-limit numără ture debounced, nu mesaje brute | extends | NX-87 | contor abuz la nivel de mesaj la ingestie |
| Identitate cross-channel → un `contact_id` | already_carded | NX-23 | generalizează la orice canal, nu doar web |
| Compat-mode (fără `DATABASE_URL_BOT`) = bypass RLS | extends | NX-97 | **P0** — închide compat-mode privilegiat |
| Fără test CI că RLS chiar blochează cross-tenant | extends | NX-92 | test de izolare sub `bot_runtime` |
| Fără auto-creare partiții (după 2026-07 → default) | extends | NX-84 | job de creare partiții |
| Telegram rich KeyError pe price/url lipsă; caption >1024 | extends | NX-89 | render defensiv |
| WhatsApp (primar) text-only: fără butoane/liste/carduri | already_carded | NX-89 | interactive messages (+ WhatsApp Flows ulterior) |
| Eval golden single-turn/RO/substring; fără LLM-judge prod | already_carded | NX-92 | trajectory/sesiune + LLM-judge + domain-checkers |
| Summarizer pierde ref-uri produs/preț | already_carded | NX-110 | — |
| Disclaimer (art.50) poate rata căi de fallback prose | extends | NX-111 | acoperă toate căile de reply |
| GDPR export ține tot în memorie, `result_ref`=NULL | extends | NX-72 | stream la storage + ref |
| revenue_attributed: anulate/refundate + valute mixte | already_carded | NX-02 | **P0** atribuire corectă |
| Fără producător `proactive_jobs`; out-of-24h drop silent | already_carded | NX-09/NX-71 | sweeper + template send |

## 6. Roadmap pe valuri + dependențe

```
Wave 0 (fundație):  NX-112 ─┐   NX-114 ──▶ NX-115 ──▶ NX-127
                    NX-123  │   (DomainPack)(ChannelProfile)(web rich parity)
                            └─▶ NX-119
Wave 1 (paritate):  NX-98(main) ──▶ NX-113 ──▶ NX-119
                    NX-117 ──▶ NX-118        NX-116 (citeste asked_intents din NX-112)
                    NX-120, NX-121, NX-126 (independente)
Wave 2 (honesty/obs): NX-122 ──▶ (eval continuu / NX-92)  ·  NX-125  ·  NX-123
Wave 3 (hardening):   NX-124  ·  NX-127  ·  (delivery fallback)
```

- **Wave 0** — fundație: NX-112 (P0 bug), NX-123 (P0 deploy), NX-114 + NX-115 (generic + 2-canale). *Începe aici.*
- **Wave 1** — paritate retrieval + grounding + safety: NX-113, NX-116, NX-117, NX-118, NX-119, NX-120, NX-121, NX-126.
- **Wave 2** — resolution honesty + observabilitate + cost: NX-122, NX-125 + eval continuu (extinde NX-92).
- **Wave 3** — hardening + merchandising: NX-124, NX-127, delivery fallback.

Lanțuri critice: `NX-114 → NX-115 → NX-127` (foundation rendering); `NX-98 → NX-113 → NX-119` (retrieval);
`NX-112 → NX-116/NX-119` (state deblochează anti-loop + sessions); `NX-117 → NX-118` (grounding text → variant).

## 7. Ce NU copiem / unde suntem deja lideri

**Lideri (păstrăm, nu regresăm):** validator determinist anti-halucinație pe output structurat ÎNAINTE de
randare (n8n nici nu-l are); izolare multi-tenant code-primary + RLS net (`bot_runtime` non-superuser);
channel abstraction la 2 margini (`channels/base.py`); free layers alias→cache→FAQ (40-60% deflecție);
prompt generat din DB; outbox idempotent; GDPR; golden meta-tests anti-security-theater.

**NU copiem (complexitate accidentală):** al 3-lea LLM (recovery agent / rerank LLM) — rerank determinist;
dual-router + shadow PersistedPendingV2 (~2000 linii JS) — mergem direct la pending tipat autoritar;
multi-agent „roiuri" (single-agent bate 64% la 1/15 tokens); MCP/ACP amânat deliberat (diferențiator, nu
supraviețuire); estate multi-tabel de debug — un `turn_trace` + tool args ajung.

## 8. Riscuri & decizii (recomandări de expert)

1. **Generic ACUM, dar incremental** — Wave 0 (ChannelProfile + DomainPack + Reply.offer) generic de la
   început; memoria long-term + search sessions rămân beauty-tuned până la al 2-lea tenant verticală-diferită.
2. **Paritate semantică, NU de UI** — web butoane/carduri, WhatsApp CTA text + (ulterior) Flows; validarea
   pe obiect semantic ÎNAINTE de randare → niciodată inconsistență de conținut între canale.
3. **Single-agent rămâne** — corect, nu imatur (date 2026). Supervisor→specialist doar la graniță reală de compliance.
4. **NX-113 e XL** — implementează split-ul `[PROPUNERE]`; ship behind `search_sort_mode_enabled`-style kill-switch.
5. **Migrări (NX-123) + compat-mode RLS (NX-97)** = risc P0 de prod — de prioritizat înaintea oricărui feature nou.
6. **Eval (extinde NX-92)** — începe cu domain-checkers (le ai deja ca funcții) pe `analytics_events`
   grupate pe `conversation_id`; adaugă LLM-judge + canary incremental. Evită „big-bang observability".

---

_Cardurile complete: `tasks/NX-112.md` … `tasks/NX-127.md`. Index backlog existent: `tasks/AUDIT-GAPS-INDEX.md`,
`tasks/NX-PREPROD-FIXES.md`. Verifică întotdeauna în `main` (git log + grep) că un card nu e deja făcut._
