# Nativx Assistant — Manual complet „cap-coadă" (RO, for dummies)

> Scop: după ce citești acest document, poți explica **fiecare** componentă, urmări **orice** mesaj prin cod,
> depana în producție și modifica proiectul **fără să-l strici**.
>
> Regula de aur peste tot: **codul e sursa de adevăr**. Unde documentația (CLAUDE.md, docstring-uri)
> bate cu codul, câștigă codul — și îți spun de fiecare dată unde bate.
>
> Fiecare afirmație e ancorată cu `fișier:linie`. Complementar cu diagramele din
> [`ARCHITECTURE-WORKFLOWS.md`](ARCHITECTURE-WORKFLOWS.md) (10 diagrame Mermaid verificate pe cod).

---

## Cuprins

1. [Ce este proiectul (imaginea mare)](#1-ce-este-proiectul)
2. [Cele 7 procese și cum comunică](#2-cele-7-procese)
3. [Harta fișierelor pe subsisteme](#3-harta-fișierelor)
4. [TurnContext — obiectul care curge prin tot](#4-turncontext)
5. [INTRARE: webhook-ul (plafon de corp, semnătură, dedupe L1)](#5-intrare-webhook)
6. [COADA + WORKER: consumer, debounce, lock, reaper](#6-coada--worker)
7. [handle_turn — inima unui tur, pas cu pas](#7-handle_turn)
8. [PIPELINE-ul de 11 stagii, stagiu cu stagiu](#8-pipeline)
9. [Bugetul de context (mesaj lung → păstrăm doar o parte)](#9-buget-context)
10. [Cusătura LLM (retry, tokeni maximi, tool loop)](#10-llm)
11. [Căutarea de produse (hibrid lexical + semantic)](#11-căutare)
12. [Grounding-ul anti-halucinație (validator + compose)](#12-grounding)
13. [IEȘIRE: Sender TX + dispatcher + canale](#13-ieșire)
14. [Memoria conversației (state, istoric, rezumat, profil)](#14-memorie)
15. [Baza de date (pool-uri, RLS, tabele)](#15-baza-de-date)
16. [Guvernanța costului (3 niveluri)](#16-cost)
17. [Tratarea erorilor (niciodată tăcere)](#17-erori)
18. [Proactiv (mesaje inițiate de bot)](#18-proactiv)
19. [Referință de configurare (toate butoanele)](#19-config)
20. [Ghid de depanare pe subsisteme](#20-debugging)
21. [Exerciții + validare](#21-exerciții)

---

<a name="1-ce-este-proiectul"></a>
## 1. Ce este proiectul

**În trei propoziții:** un vânzător-robot pe WhatsApp (și web + Telegram) pentru magazine online din România.
Clientul scrie „ce ser cu vitamina C aveți sub 100 lei?", iar botul caută în catalogul magazinului și
răspunde ca un vânzător priceput — cu prețuri și linkuri **reale**, nu inventate. E **multi-tenant**: aceeași
aplicație servește mai multe magazine, fiecare izolat de celelalte prin `business_id`.

**Obsesia centrală:** botul nu are voie să inventeze prețuri, produse sau linkuri. Modelul AI produce doar
cuvinte + ID-uri de produs; **codul** pune faptele reale din baza de date. Asta se numește *grounding* și e
apărat de două mecanisme (validatorul + compose), pe care le vei înțelege complet la [capitolul 12](#12-grounding).

**Trei principii pe care le vei vedea peste tot:**
1. **Pipeline liniar** — mesajul trece printr-o „bandă rulantă" cu 11 stații, în ordine fixă, fără sărituri înapoi.
2. **LLM doar în 2 puncte** — triaj (model ieftin „nano") + agent (model „mini"). Tot restul e cod determinist.
3. **Niciodată tăcere** — mereu iese ceva spre client. Degradare: mini → retry → nano → template → om notificat.

**Stack:** Python 3.12 asyncio, FastAPI (webhook), Redis Streams (coadă), Postgres 16 pe Supabase (DB),
OpenAI (LLM), Meta Cloud API (WhatsApp), Telegram Bot API, widget web (SSE).

---

<a name="2-cele-7-procese"></a>
## 2. Cele 7 procese

Nu e „o aplicație". Sunt **7 procese** care rulează în paralel (din `docker-compose.yml`). Analogie: un
restaurant cu bucătărie, ospătari, oficiu de livrare și un manager de mentenanță — fiecare face un lucru.

| Proces | Comandă | Rol | Fișier entry point |
|---|---|---|---|
| `webhook` | `uvicorn src.webhook.app:app` | Poarta HTTP: primește de la Meta/web/shop | `src/webhook/app.py` |
| `worker` | `python -m src.worker.consumer` | **Creierul**: coadă → pipeline → răspuns | `src/worker/consumer.py` |
| `dispatcher` | `python -m src.worker.dispatcher` | **Singura ieșire**: outbox → canale | `src/worker/dispatcher.py` |
| `scheduler` | `python -m src.jobs.scheduler` | Mentenanță (rollup, embed, lifecycle, curățenie) | `src/jobs/scheduler.py` |
| `proactive` | `python -m src.proactive.scheduler` | Mesaje inițiate de bot | `src/proactive/scheduler.py` |
| `telegram-poller` | `python -m src.channels.telegram.poller` | Ascultă Telegram | `src/channels/telegram/poller.py` |
| `redis` | — | Coada + lock-uri + dedupe + contoare cost | (serviciu) |

**Cum comunică:** nimeni nu cheamă pe altul direct. Totul curge prin **coada Redis** (stream-ul `inbound`)
și prin **tabelul `outbox`**. De ce? Ca `webhook`-ul să răspundă în <50ms (Meta face retry agresiv), iar
procesarea grea (LLM, DB) să se facă separat în `worker`. E ca la restaurant: ospătarul ia comanda și pleacă
(bonul pe cui), nu stă lângă bucătar până e gata mâncarea.

```
INTRARE                COADĂ            CREIER (worker)                IEȘIRE
webhook  ─┐                             ┌─ Gates                       outbox ─┐
web      ─┼─> Redis stream ─> consumer ─┼─ Free layers (cache/FAQ)             ├─> dispatcher ─> WhatsApp
telegram ─┤   "inbound"                 ├─ Triaj (nano)                        │                Telegram
orders   ─┘                             ├─ Agent (mini + tools + validator)    │                Web (SSE)
                                        └─ Sender ─> outbox ───────────────────┘
                                              │
                                   Postgres (Supabase) + OpenAI
```

---

<a name="3-harta-fișierelor"></a>
## 3. Harta fișierelor pe subsisteme

```
src/
├── config.py              ← TOATE setările/bugetele/plafoanele (cap. 19)
├── models.py              ← TurnContext + toate dataclass-urile (cap. 4)
├── redis_bus.py           ← client Redis: XADD inbound, dedupe L1, lock-uri
├── meta_client.py         ← MetaClient (trimite pe WhatsApp)
│
├── webhook/               ← INTRAREA (margine subțire, fără DB)
│   ├── app.py             ← FastAPI: GET verify + POST inbound + POST orders
│   ├── signature.py       ← verifică X-Hub-Signature-256
│   ├── meta.py            ← parsează payload Meta → envelope neutru
│   ├── body_limit.py      ← plafon de mărime pe corp (anti-OOM)
│   └── orders.py          ← webhook comenzi → atribuire
│
├── worker/                ← CREIERUL
│   ├── consumer.py        ← bucla Redis: citește, rezolvă tenant, rutează
│   ├── processor.py       ← handle_turn: inima turului + Sender TX
│   ├── runner.py          ← motorul pipeline-ului (stagiile în ordine)
│   ├── context.py         ← bugetul de context pt prompturi (cap. 9)
│   ├── compose.py         ← compunerea rich + grounding (cap. 12)
│   ├── dispatcher.py      ← IEȘIREA: outbox → canale
│   ├── debounce.py        ← coalesce mesaje rapide (3s)
│   ├── callback.py        ← navigare carusel
│   ├── badges.py          ← badge-uri de card din semnale reale
│   ├── summarizer.py      ← rezumat rolling (nano)
│   ├── profile.py         ← extragere profil + lead_score (nano)
│   ├── reply_split.py     ← spargere text lung în 2
│   ├── limits.py          ← cost guard + rate limit (contoare Redis)
│   ├── order_gate.py      ← poarta de comandă (login web)
│   └── stages/            ← CELE 11 STAGII
│       ├── gates.py       ← 1: filtre (bot activ? risc? moderare? poză?)
│       ├── language.py    ← 2: detect RO/HU/EN
│       ├── clarify.py     ← 3: reia un slot în așteptare
│       ├── greeting.py    ← 4: salut → welcome determinist
│       ├── alias.py       ← 5: match exact pe fraze aprobate
│       ├── cache.py       ← 6: cache semantic
│       ├── faq.py         ← 7: răspuns din FAQ
│       ├── triage.py      ← 8: nano clasifică ruta
│       ├── handoff.py     ← 9: escaladare la om
│       └── agent.py       ← 10: mini + tool loop + validator (1411 linii!)
│
├── agent/
│   ├── llm.py             ← SINGURUL loc care vorbește cu OpenAI (cap. 10)
│   ├── prompt_builder.py  ← system prompt GENERAT din DB
│   ├── tool_definitions.py← schemele tool-urilor (OpenAI)
│   ├── usage.py           ← contorizare tokeni/cost
│   └── pricing.py         ← tarife LLM
│
├── tools/                 ← tool-urile agentului (cod determinist)
│   ├── catalog_tools.py   ← search_products, get_product_details, compare (cap. 11)
│   ├── commerce_tools.py  ← cart_add, checkout_link, back_in_stock
│   ├── orders_tools.py    ← check_order, reorder
│   ├── faq_tools.py       ← faq_lookup
│   ├── handoff_tools.py   ← request_human
│   └── taxonomy.py        ← mapare concerns
│
├── db/
│   ├── connection.py      ← pool-uri asyncpg + RLS + izolare (cap. 15)
│   └── queries/           ← SQL per domeniu (contacts, conversations, catalog, ...)
│
├── channels/              ← marginile de canal
│   ├── base.py            ← ChannelSender Protocol + Capability matrix
│   ├── media.py           ← download media (Vision)
│   ├── telegram/          ← client + poller
│   └── web/               ← sender SSE + render
│
├── web/                   ← gateway widget web
│   ├── app.py             ← /web/bootstrap, /messages, /stream, /chat
│   ├── session.py         ← sesiune HMAC
│   └── identity.py        ← login passthrough (JWT)
│
├── domain/                ← DomainPack (config per-vertical)
├── proactive/             ← motor + inițiatori + template-uri (cap. 18)
├── jobs/                  ← rollup, embed, lifecycle, cleanup
├── lang/                  ← detectare limbă
└── gdpr/                  ← ștergere/export
```

---

<a name="4-turncontext"></a>
## 4. TurnContext — obiectul care curge prin tot

📄 `src/models.py:477`

Într-un pipeline liniar ai nevoie de **un singur obiect** care poartă tot: cine e clientul, ce a scris, ce
s-a discutat, ce a decis fiecare stagiu. Ăsta e `TurnContext`. Analogie: o **tavă de spital** care circulă pe
bandă — fiecare stație pune ceva pe ea, nimeni nu ia de pe ea ce a pus altă stație.

### Regula ABSOLUTĂ

> **Fiecare câmp are EXACT un stagiu care-l scrie.** (proprietarul e notat în docstring, `models.py`)

```python
route: RouteDecision | None = None      # owner: Triaj          (models.py:494)
retrieval: RetrievalResult | None = None # owner: Retrieval/tools (models.py:495)
reply: Reply | None = None               # owner: orice stagiu → early exit (models.py:496)
halt: bool = False                       # owner: Gates (tăcere intenționată)  (models.py:497)
state_patch: dict = ...                  # owner UNIC: Agent      (models.py:505)
usage: TurnUsage | None = None           # owner: runner-ul       (models.py:509)
```

**DE CE atât de strict?** Dacă doi bucătari scriu pe același bon, nu mai știi cine a greșit. Dacă doar Triajul
scrie `route`, când `route` e greșit știi exact unde să te uiți. Debugging **liniar**, nu ghicit.

### Metodele-helper (le folosești mereu)

- **`emit(type_, **props)`** (`models.py:511`) — un stagiu adaugă un event de analytics fără să știe cum e
  scris. Injectează automat `turn_id` (`setdefault`, ca să nu suprascrie unul explicit). Asta e „black box
  recorder"-ul: toate event-urile unui tur au același `turn_id` → poți reconstrui traiectoria.
- **`set_reply(text, ...)`** (`models.py:527`) — setează `ctx.reply` → **oprește banda** (runner-ul iese).
- **`halt_silent(reason)`** (`models.py:520`) — **singura** tăcere permisă (a preluat un om). Emite `gate_halt`.
- **`set_clarify(text, field, resume_route, ...)`** (`models.py:590`) — pune o întrebare ȘI memorează slotul.
  Subtilitate: dacă re-întrebi **același** `field`, `attempts++` (anti-buclă → escaladare după N).
- **`set_rich_reply` / `set_comparison_reply` / `set_offer`** — variante pentru carduri / tabel / buton.

### ConversationState — hidratare DEFENSIVĂ

📄 `src/models.py:191` (`from_jsonb`)

State-ul vine din DB ca JSON. Codul îl transformă în obiect, dar **niciodată nu are încredere** în el:

```python
for p in raw.get("displayed_products") or []:
    pid = p.get("product_id") or p.get("id")
    if pid is None or p.get("name") is None or p.get("price") is None:
        continue   # produs incomplet → SĂRIT, nu crapă
    products.append(ProductRef(product_id=pid, name=p["name"], price=float(p["price"])))
```

**DE CE paranoia?** `from_jsonb` rulează la **FIECARE tur pe hot path**. Un cursor ne-numeric (state vechi /
editat manual) NU trebuie să crape turul — altfel conversația s-ar bloca permanent (încalcă „niciodată tăcere").

**Principiul 8 — state = ref-uri, nu obiecte:** stochezi `{product_id, name, price}`, nu tot produsul.
Bugetul e 8KB (impus în DB cu CHECK). Când ai nevoie de detalii, re-hidratezi din DB. Câmpurile din state:
`displayed_products`, `active_search` (pool + cursor de paginare), `pending_question`, `asked_intents`,
`constraints`, `search_constraints`, `cart`, `state_version`.

---

<a name="5-intrare-webhook"></a>
## 5. INTRARE: webhook-ul

📄 `src/webhook/app.py`

Marginea de intrare e **subțire și fără DB**. Face doar: verifică, deduplică rapid, pune pe coadă, răspunde 200.
De ce subțire? Meta face retry agresiv la timeout → trebuie ACK în <50ms.

### Middleware: plasa anti-OOM (`app.py:25`)

```python
@app.middleware("http")
async def _request_size_guard(request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None and int(cl) > get_settings().webhook_max_body_bytes:  # 256KB global
        return PlainTextResponse("payload too large", status_code=413)
```

**DE CE?** VPS-ul e mic (1vCPU, 4GB, 0 swap). Un POST uriaș ar putea OOM-ui procesul înainte să-l parsezi.
Cap global 256KB (webhook), rafinat per-endpoint (web = 16KB). Ăsta e **primul** filtru din Diagrama 3 (`CAP`).

### GET /webhook — handshake Meta (`app.py:62`)

Meta trimite `hub.mode=subscribe` + `hub.verify_token` + `hub.challenge`. Dacă token-ul corespunde, întorci
challenge-ul ca **text brut** (Meta compară exact). Altfel 403. Se face o singură dată, la configurare.

### POST /webhook — mesajele inbound (`app.py:80`)

Pașii, toți rapizi (fără LLM, fără DB):

```python
raw = await enforce_body_cap(request, ...)              # 1. plasă de mărime
signature = request.headers.get("X-Hub-Signature-256")
if not verify_meta_signature(app_secret, raw, signature):
    return 403                                           # 2. semnătura pe corpul BRUT
payload = json.loads(raw)                                # 3. JSON valid? altfel 400
for event in parse_webhook(payload):
    if await seen_before(redis, event.channel_account_id, event.provider_msg_id):
        continue                                         # 4. dedupe L1 (Redis) — retry Meta
    await enqueue_inbound(redis, event.to_dict())        # 5. XADD pe stream
for status in parse_statuses(payload):                   # statusurile NU se deduplică
    await enqueue_inbound(redis, status.to_dict())
return 200                                               # 6. ACK rapid
```

**Detalii pe care le vei fi întrebat:**
- **Semnătura pe corpul BRUT** (`signature.py`): dacă ai reserializa JSON-ul, ai schimba bytes → semnătura
  n-ar mai potrivi. De aceea verifici `raw`, nu `payload`.
- **Dedupe L1** (`redis_bus.py:53`, `seen_before`) = `SET NX EX` pe `(channel_account_id, provider_msg_id)`.
  E **primul strat** de dedupe. Al doilea (durabil, în DB) e în worker — vezi cap. 7. De ce două? Redis se
  poate goli (FLUSHALL/restart); DB-ul prinde ce scapă.
- **Statusurile nu se deduplică**: `delivered` și `read` au același `wamid` → dacă le-ai deduplica, ai pierde
  `read`.
- **Redis jos → 503** (`app.py:119`): NU pierdem tăcut. 503 → Meta reîncearcă, iar la retry dedupe-ul prinde
  ce-a intrat deja.

### POST /webhook/orders/{business_id} (`app.py:127`)

Webhook de comenzi de la platforma de shop. Margine subțire, ca Meta: verifică HMAC pe corpul brut, pune un
envelope `kind="order"` pe stream. Comenzile au `business_id` din path (autentificat de secret), deci NU trec
prin `resolve_channel`.

**Ce se întâmplă cu un mesaj TROLL / semnătură falsă?** 403 imediat, fără să atingem coada sau DB-ul.

---

<a name="6-coada--worker"></a>
## 6. COADA + WORKER

📄 `src/worker/consumer.py`

Webhook-ul a pus mesajul pe stream și a plecat. Workerul îl scoate și-l procesează. E o buclă infinită.

### run_consumer — bucla (`consumer.py:275`)

```python
while True:
    await consume_once(...)                # citește + procesează
    if now - last_reap >= 30s:
        await reap_pending(...)             # recuperează mesaje de la workeri morți
```

### consume_once — un ciclu (`consumer.py:187`)

```python
resp = await redis.xreadgroup(CONSUMER_GROUP, consumer_name, {STREAM_INBOUND: ">"}, count=10, block=2000)
```

**`XREADGROUP` explicat:** un teanc de bonuri (stream) + mai mulți ospătari (workeri) în același „grup". Redis
dă fiecare bon **unui singur** ospătar. `">"` = „bonuri pe care nu le-a văzut nimeni din grup". Asta permite să
rulezi 3 workeri fără dublă procesare. Fiecare are nume unic (`worker-<hostname>`, `consumer.py:324`).

Pentru fiecare mesaj (`consumer.py:210-230`):

```python
if event.get("kind") == "message":
    if typing_enabled:
        asyncio.create_task(_safe_typing(registry, event))  # "typing…" INSTANT, fire-and-forget
    await debouncer.add(event, msg_id)   # NU ACK aici — Debouncer-ul ACK-uiește DUPĂ flush
else:
    await process_event(pool, redis, event)  # status/callback/order → imediat
    await redis.xack(...)
```

> ⚠️ **Gaura #1 (NX-140), memoreaz-o:** dacă `process_event` aruncă o excepție (`consumer.py:228-230`),
> mesajul e **ACK-uit** și doar logat (`log.exception("eroare la procesarea mesajului")`). Meta a primit
> deja 200 → nu re-trimite → **clientul nu primește nimic**. E singura încălcare structurală a principiului 6.
> DE CE totuși ACK? Ca un mesaj „otrăvit" (care crapă mereu) să nu blocheze coada pentru toți. Fix corect:
> re-queue cu contor + fallback în outbox la epuizare. **La depanare „botul n-a răspuns": ăsta e primul grep.**

### _safe_typing — de ce ocolește outbox-ul (`consumer.py:59`)

„typing…" se trimite **direct** prin ChannelSender, nu prin outbox. Un typing livrat cu 3s întârziere (după ce
răspunsul a plecat) e inutil. Singura excepție documentată de la „un singur punct de ieșire" (săgeata punctată
în Diagrama 1). Gardat pe capabilitatea `TYPING`, nu pe canal.

### Debouncer — coalesce (`debounce.py`)

Dacă clientul scrie „vreau un ser" apoi „…cu vitamina C" apoi „…sub 100 lei" în 2 secunde, nu vrei 3 tururi.
Debouncer-ul așteaptă 3s de liniște, adună mesajele într-unul singur (N→1), apoi procesează. ACK-ul se face
**după flush reușit** (durabilitate — NX-87): dacă workerul moare înainte de flush, mesajele rămân ne-ACK-uite
și reaper-ul le recuperează.

### process_event — rezolvarea tenantului (`consumer.py:102`)

Aici „un envelope neutru" devine „un tur pentru businessul X". Trei ramuri:

1. **order** (`consumer.py:109`): `business_id` din envelope → `process_order` direct (fără resolve).
2. **resolve channel** (`consumer.py:123`):
   ```python
   async with admin_conn(pool) as conn:
       channel = await resolve_channel(conn, channel_kind, channel_account_id)
   ```
   **ĂSTA e SINGURUL query pe `admin_conn`** (pool privilegiat). Trebuie să afli `business_id` **înainte** să
   știi tenantul — e operația care-l derivă. Orice alt query pe admin_conn = bug de izolare.
3. **status** (`consumer.py:137`): idempotent (pe `provider_msg_id`) → fără lock, scriere directă.

### Lock per conversație (`consumer.py:157-169`)

```python
sender_key = f"{channel_kind}:{channel_account_id}:{sender_external_id}"
got = await acquire_conv_lock(redis, business_id, sender_key, token, ttl_s=30)
if locked is False:                    # altă replică procesează aceeași conversație
    await _requeue_busy(redis, event, settings)  # re-queue cu backoff
    return
```

**DE CE?** 2 workeri + 2 mesaje rapide → ambele citesc același `state`, ambele scriu → race. Lock-ul
serializează tururile **aceleiași** conversații. **Redis jos → fail-open** (procesează oricum: mai bine un risc
mic de race decât blocarea traficului).

> ⚠️ **Gaura #2 (NX-140):** `_requeue_busy` (`consumer.py:90`) — după `conv_lock_max_requeues` (default 10)
> reîncercări, evenimentul e **DROPAT** cu un log. A doua cale de pierdere tăcută.

### reap_pending — plasa de siguranță (`consumer.py:234`)

`XAUTOCLAIM` reclamă mesajele citite de un worker care a **murit** înainte de ACK (ne-ACK-uite > 60s) și le
reprocesează. Închide gaura „consumer mort între citire și ACK".

---

<a name="7-handle_turn"></a>
## 7. handle_turn — inima unui tur

📄 `src/worker/processor.py:418`

Cel mai important flux. Fan-out 45 (cel mai mare din cod). Rulează pe o conexiune **deja tenant-scoped**.

### Pas 0 — pregătire (linii 440-449)

```python
turn_id = str(uuid4())
verified_customer_ref = event.get("verified_customer_ref")   # login web (NX-129)
identity_external_id = verified_customer_ref or sender_external_id
```

`verified_customer_ref or sender_external_id`: dacă marginea a verificat o identitate stabilă (login eshop, JWT),
rezolvăm contactul pe EA (client stabil peste device-uri). Altfel, pe visitor_id-ul anonim.

### Pas 1 — Dedupe Layer 2 (linia 456)

```python
if provider_msg_id and not await claim_inbound(conn, business.id, provider_msg_id):
    return TurnResult(..., deduped=True)   # deja procesat
```

Al doilea strat de dedupe, **durabil în DB**. Prinde retry-uri Meta care scapă de Redis (FLUSHALL/restart).
**Guard ÎNAINTE de orice scriere** — un duplicat nu produce nici mesaj, nici outbox.

**Subtilitate (NX-86, claim-or-resume):** `claim_inbound` marchează „în lucru" (nu „gata"). Dacă turul crapă la
mijloc, `completed_at` rămâne NULL → reaper-ul îl poate relua. Abia `mark_inbound_completed` (în TX-ul de
outbox) îl finalizează.

### Pas 2 — Contact + Conversație + Mesaj (linii 460-488)

```python
contact = await get_or_create_contact(conn, business.id, channel_kind, identity_external_id, ...)
conv = await get_or_create_conversation(conn, business.id, contact.id, channel_id, ...)
await insert_message(conn, ..., Direction.INBOUND, Author.CONTACT, body=event.get("body"), ...)
await touch_last_inbound(conn, business.id, conv["id"])   # alimentează fereastra 24h
```

`touch_last_inbound` setează `last_inbound_at = now()` → alimentează fereastra de 24h (Meta permite mesaje
libere doar 24h de la ultimul inbound al clientului). E derivat, nu un flag.

### Pas 3 — Construiește TurnContext + încarcă memoria (linii 490-510)

```python
ctx = TurnContext(
    turn_id=turn_id, business=business, contact=contact, message=InboundMessage(...),
    conversation_id=conv["id"],
    history=await get_recent_messages(conn, ...),         # ultimele 8 mesaje
    state=ConversationState.from_jsonb(conv["state"]),    # memoria (ref-uri, defensiv)
    summary=await get_summary_for_context(conn, ...),     # rezumat rolling
    language=conv["locale"] or business.default_locale,
    bot_active=conv["bot_active"], handoff_until=conv["handoff_until"], ...
)
```

Astea 3 (`history` + `state` + `summary`) = **toată memoria** botului. Vezi cap. 14.

### Pas 4 — Cost guard (linii 523-525)

```python
if redis and cost_guard_enabled:
    await seed_daily_cost(conn, redis, business.id)   # reseed lazy din usage_daily
llm = await _llm_within_budget(ctx, redis, business, channel_kind=channel_kind)
```

`_llm_within_budget` (`processor.py:308`) verifică plafonul zilnic (business + per-contact). Depășit → întoarce
`None`. Cu `llm=None`, pipeline-ul rulează **degradat**: gates + cache merg, stagiile LLM sar. Niciodată nu crapă.

### Pas 5 — Rulează pipeline-ul (linia 528)

```python
await run_pipeline(ctx, PipelineDeps(conn=conn, redis=redis, llm=llm, media=media), stages)
```

Intră cele 11 stagii (cap. 8). Când se întoarce, `ctx.reply` e (poate) setat.

### Pas 6 — Fără reply (linii 534-545)

```python
if ctx.reply is None:
    if ctx.halt:  log.info("tăcere intenționată (handoff)")   # om preia — CORECT
    else:         log.info("tur procesat fără reply")
    if provider_msg_id: await mark_inbound_completed(...)      # finalizează claim-ul!
    return TurnResult(...)
```

`mark_inbound_completed` și aici: chiar dacă nu răspunzi, turul e DONE → altfel reaper-ul l-ar reprocesa la
infinit.

### Pas 7 — Disclaimer + fragmentare (linii 551-563)

```python
reply_text = ensure_disclaimer(ctx.reply.text, ctx.language)   # art. 50 AI Act, idempotent, azi OFF
is_rich = ctx.reply.rich is not None
has_products = bool(ctx.reply.products)
if is_rich or has_products or not deliver:
    fragments = [reply_text]                          # rich/carusel/sync = 1 fragment
else:
    fragments = split_reply(reply_text, limit=200)    # text lung → max 2 (citire ușoară pe telefon)
```

De ce rich/carusel nu se sparge? Spargerea lead-in-ului ar strica ordinea cardurilor.

### Pas 8 — TRANZACȚIA Sender (linii 565-667) — cel mai important bloc

**Totul atomic** — sau tot, sau nimic:

```python
async with conn.transaction():
    # 8a. Merge canonic de state
    new_state = conv["state"]
    if (is_rich or has_products) and ctx.reply.products:
        new_state = {**conv["state"], "displayed_products": ctx.reply.products}
    new_state = {**new_state, "pending_question": ctx.reply.pending_question}
    new_state = {**new_state,
        "constraints": ctx.state.constraints,               # slot-fill din clarify
        "asked_intents": ctx.state.asked_intents,           # anti-loop
        "search_constraints": ctx.state.search_constraints, # stiva de căutare (NX-133)
    }
    if not (is_rich or has_products):
        new_state = {**new_state, "active_search": None}     # reset sesiune căutare
    if ctx.state_patch:
        new_state = {**new_state, **ctx.state_patch}         # Agent are ULTIMUL cuvânt (cart etc.)

    # 8b. Mesaje outbound + rânduri outbox
    for i, frag in enumerate(fragments):
        out_msg_id = await insert_message(..., Direction.OUTBOUND, Author.BOT, body=frag,
            status="queued" if deliver else "sent",
            **(_message_usage_kwargs(ctx.usage) if i == 0 else {}))   # cost/tokeni pe primul frag
        if not deliver: continue
        payload = {"type": "text", "to": sender_external_id, "text": frag, ...}
        if i == 0 and is_rich:       payload["rich"] = asdict(ctx.reply.rich)
        elif i == 0 and has_products: payload["type"]="carousel"; payload["products"]=ctx.reply.products
        outbox_id = await enqueue_outbox(conn, ..., f"{turn_id}:{i}", payload)  # idempotency key

    # 8c. Patch state cu optimistic lock
    await patch_conversation_state(conn, ..., new_state, conv["state_version"], touch_outbound=True)

    # 8d. Finalizează dedupe-ul ÎN aceeași TX
    if provider_msg_id: await mark_inbound_completed(...)
```

**DE CE totul într-o tranzacție?** Analogie bancară: retragi din A, depui în B — dacă se întrerupe la mijloc,
banii dispar. Aici: mesaj outbound + outbox + patch state + finalizare dedupe = atomice. Altfel ai avea un
răspuns fără outbox (client nu primește nimic) sau state scris fără mesaj (memorie coruptă).

- **`idempotency_key = f"{turn_id}:{i}"`** (`processor.py:651`): outbox are UNIQUE pe cheie → un replay nu
  inserează dublul. turn:0 și turn:1 pentru cele 2 fragmente.
- **Optimistic lock pe `state_version`** (`processor.py:656`): scrie doar dacă `state_version` din DB e încă
  cel citit la început. Un „check-and-set" — a doua apărare peste lock (vezi cap. 15).

> 🐛 **Bug clasic „botul uită":** dacă un stagiu mută un câmp **in-place** pe `ctx.state` (ex. `constraints`),
> dar processor-ul nu-l listează în merge-ul canonic (8a), câmpul trăiește doar în memorie și se pierde tăcut
> la write-back. **Regula:** orice câmp mutat in-place de un stagiu TREBUIE listat explicit la 8a.

### Pas 9 — Post-tur async (linii 689-705)

```python
post_acc, post_token = usage.push()   # acumulator de usage SEPARAT
try:
    await _cache_writeback(...)              # scrie în semantic_cache (dacă cacheable)
    await _summarize_if_needed(...)          # rezumat rolling (nano)
    await _extract_profile_and_score(...)    # profil + lead_score (nano)
finally:
    usage.pop(post_token)
```

Rulează **DUPĂ** ce răspunsul e în outbox — nu întârzie livrarea. „Temele de casă": învață clientul, comprimă
istoricul, memorează răspunsul. Toate **best-effort** (crapă → log, turul a răspuns deja). Acumulator separat
ca apelurile nano de fundal să nu scape contabilității de cost (`phase="post_turn"`).

---

<a name="8-pipeline"></a>
## 8. PIPELINE-ul de 11 stagii

📄 `src/worker/runner.py:47` (motor), `runner.py:207` (`DEFAULT_STAGES`)

### Motorul (`run_pipeline`)

```python
for stage in stages:
    before = acc.snapshot()
    started = perf_counter()
    await stage(ctx, deps)                             # rulează stagiul
    ctx.emit("stage_completed", stage=name, latency_ms=...)   # măsoară
    _record_stage_delta(...)                           # cost/tokeni per stagiu
    if ctx.reply is not None or ctx.halt:              # EARLY EXIT
        ctx.emit("pipeline_early_exit", stage=name); break
```

**Genialitatea:** stagiile sunt funcții pure `async def stage(ctx, deps) -> None`. Runner-ul le rulează în
ordine, măsoară latența/tokenii **în jur** (`before`/`after`), iese la primul `reply` sau `halt`. **Stagiile
nu știu că sunt măsurate** (principiul 10). Adaugi un stagiu nou fără să atingi runner-ul.

> ⚠️ **Discrepanță doc↔cod:** docstring-ul (`runner.py:8-10`) încă zice „un singur stagiu real (echo_stage)".
> E STALE. CLAUDE.md zice „9 stagii" + „validator = stagiul 8". În realitate sunt **11 stagii** și validatorul
> e cod în interiorul agentului, nu un stagiu. Codul câștigă.

### Ordinea (de la ieftin la scump)

Ținta: 40-60% din trafic se oprește **înainte** de LLM. Stagiile sunt de la gratis la scump:

| # | Stagiu | Fișier | Ce face | Cost |
|---|---|---|---|---|
| 1 | `gates_stage` | `stages/gates.py:439` | filtre: activ? blocat? risc? moderare? poză? PII? | gratis (+moderare) |
| 2 | `language_stage` | `stages/language.py:27` | detectează RO/HU/EN | gratis |
| 3 | `clarify_resume_stage` | `stages/clarify.py:28` | reia un slot în așteptare | gratis |
| 4 | `greeting_stage` | `stages/greeting.py:184` | pur salut → welcome determinist | gratis |
| 5 | `alias_stage` | `stages/alias.py:46` | match EXACT pe fraze aprobate | gratis (index) |
| 6 | `cache_stage` | `stages/cache.py:89` | cache semantic | 1 embed |
| 7 | `faq_stage` | `stages/faq.py:45` | răspuns din FAQ | 1 embed |
| 8 | `triage_stage` | `stages/triage.py:212` | nano clasifică ruta | 1 nano |
| 9 | `handoff_stage` | `stages/handoff.py:39` | escaladare la om | gratis |
| 10 | `agent_stage` | `stages/agent.py:957` | mini + tool loop + validator | 1-4 mini |
| 11 | `fallback_stage` | `runner.py:169` | dacă nimic → clarificare (nu tăcere) | gratis |

### 8.1 Gates (`stages/gates.py:439`)

Primul stagiu real. 8 porți, în ordine, fiecare cu early-exit:

```python
if not ctx.bot_active:            ctx.halt_silent("bot_inactive"); return       # 1. kill-switch
if ctx.contact.is_blocked:        ctx.halt_silent("contact_blocked"); return    # 2. blocklist
if handoff_until > now():         ctx.halt_silent("handoff_active"); return     # 3. om a preluat
if await _rate_limited(...):      return                                        # 4. rate limit
if await _moderation_blocked(...):return                                        # 5. moderare (toxic)
reason = detect_risk(ctx.message.body)                                          # 6. risc (om/legal)
if reason and handoff_enabled_for(channel): request_human + reply; return
if content_type == "image":       await _route_image(...)                       # 7. poză → Vision
_apply_input_guardrails(ctx)                                                     # 8. clamp+PII+injection
```

**Detalii importante:**
- **Rate limit** (`_rate_limited`, `gates.py:323`): >20 mesaje/60s → un mesaj de throttle O SINGURĂ dată, apoi
  tăcere pe restul burst-ului. Fail-open dacă Redis e jos.
- **Moderare** (`_moderation_blocked`, `gates.py:348`): apel OpenAI moderation (gratuit). Flagged → răspuns
  neutru + contor de flag-uri pe 24h; peste prag (3) → blochează contactul. **Fail-open**: fără cheie/API jos
  → trece (indisponibilitatea moderării nu tace tot traficul). NICIODATĂ corpul în analytics — doar categoriile.
- **Risc** (`detect_risk`, `gates.py:122`): pattern-uri **deterministe** (nu LLM) RO+HU+EN pentru „vreau un om"
  și „avocat/reclamație". Escaladează DOAR pe canale cu handoff (`handoff_enabled_for`); pe web (fără operator)
  NU escaladează — botul asistă singur.
- **Vision** (`_route_image`, `gates.py:385`): poză → descriere text → devine `ctx.message.body`. NU setează
  reply, doar **îmbogățește** inputul → triajul rutează SALES. **Fail-soft**: orice eșec → păstrează caption-ul
  util al pozei ca text de căutare (nu aruncă intenția). „Imagine → text → search" (nu avem image-embedding).
- **Guardrails de input** (`_apply_input_guardrails`, `gates.py:255`) — **AICI e detaliul „mesaj lung → păstrăm
  doar o parte"**:
  ```python
  if len(body) > INBOUND_BODY_MAX:                 # 2000 caractere
      ctx.message.body = body[:INBOUND_BODY_MAX]   # TRUNCHIERE, nu rejection
      ctx.emit("body_truncated", chars=len(body))  # DOAR lungimea, nu corpul (P12)
  masked, counts = mask_pii(ctx.message.body)      # telefon/email/iban/card → [telefon] etc.
  n = screen_injection(ctx)                        # "ignore previous" etc. → doar contor
  ```
  - **Trunchiere la 2000 caractere** (`INBOUND_BODY_MAX`, `config.py:16`): un mesaj mai lung e **tăiat**, nu
    respins (P6). Aliniat cu validarea web (`max_length=2000`). Nota: e cap de *caractere*, plasă structurală.
  - **Mascare PII** (`mask_pii`, `gates.py:175`): telefonul/email/IBAN/card liber-tastate de client → `[telefon]`
    etc., ca să NU intre în prompt/analytics. Ordine: email → iban → card → telefon. Cardul e validat cu **Luhn**
    + lungime (15/16/19) + prefix IIN, ca să NU prindă un cod EAN de produs.
  - **Ecran injection** (`screen_injection`): „ignore previous instructions" etc. → doar **contor** (nu tăcem,
    nu escaladăm — ar premia abuzul). Apărarea reală = validatorul agentului.

### 8.2 Language (`stages/language.py:27`)

Detectează RO/HU/EN, setează `ctx.language`, persistă `conversations.locale` (scriere directă best-effort).
**DE CE aici, înaintea straturilor gratuite?** Toate lookup-urile în FAQ/cache/template includ `locale` — un
cache hit în limba greșită e un bug, nu un hit (principiul 11).

### 8.3 Clarify resume (`stages/clarify.py:28`)

Dacă `state.pending_question` e setat (am întrebat ceva la turul trecut), răspunsul scurt al clientului („200
lei") e consumat **determinist** aici: umple slotul, setează ruta de reluare. Dacă a re-întrebat prea des
(`attempts > clarify_max_attempts`, default 2) → escaladează (HANDOFF pe slot critic / SALES altfel). Rulează
ÎNAINTE de greeting/cache/triage ca „200 lei" să nu fie tratat ca salut sau re-triat de la zero.

### 8.4 Greeting (`stages/greeting.py:184`)

Pur salut („bună", „salut") → mesaj de welcome branded, **determinist** (fără LLM). Nu verifică ruta, doar
textul. Numele botului + sugestiile din `businesses.settings["welcome"]`.

### 8.5 Alias (`stages/alias.py:46`)

Match **EXACT** al frazei normalizate în `intent_aliases` (status='approved'). Cel mai ieftin strat (index
B-tree, zero token). Trei ieșiri: FAQ target → servește răspuns (early-exit); route/product/category → setează
**doar** `ctx.route` (fără reply), iar cache/FAQ/triaj îl **respectă** (skip); miss → mergi la cache. Gol pe
demo (se populează din shadow mode).

### 8.6 Cache semantic (`stages/cache.py:89`)

Query repetat → răspuns gata, fără LLM de generare. L1 exact (hash) → L2 cosine (embed + prag `cache_tau_high`
= 0.92). Două tiere: `static` (fără produse, TTL zile) și `dynamic` (recomandare cu produse, TTL minute +
snapshot de preț). La lookup pe dynamic se face **price-check**: dacă prețul s-a schimbat, e miss (nu servim
preț învechit). Query-urile „realtime/contextual" (ex. „mai ieftin") sar cache-ul.

### 8.7 FAQ (`stages/faq.py:45`)

Întrebări de cunoștințe (retur/livrare/garanție) → răspuns din `faqs` (un embed, fără generare). Lookup
ÎNTOTDEAUNA `business_id + locale + cosine`. Trei praguri: `faq_tau_high` (0.78), `faq_tau_policy` (0.45, DOAR
când mesajul conține o întrebare clară de politică — regex), și fallback de locale opțional. Gol pe demo (`faqs=0`).

> 🐛 **Bug istoric aici (silent dead layer):** un codec pgvector greșit făcea `float('[')` → DataError pe ORICE
> lookup FAQ/cache → ambele straturi mureau **tăcut** (best-effort → miss). Nedetectat cât `faqs` era gol; a ieșit
> la primul seed real. Fix în `connection.py:118` (`_vector_encode` acceptă și literal deja formatat). **Lecția:**
> un strat best-effort care nu emite NICIUN event = excepție înghițită; caută warning-ul în log.

### 8.8 Triaj (`stages/triage.py:212`)

Primul touchpoint LLM. Nano clasifică în: `simple | sales | order | handoff | clarify`. Output JSON validat cu
Pydantic (`TriageOut`, `triage.py:191`).

```python
if ctx.route is not None: return          # clarify_resume a setat deja ruta → no-op (P3)
if deps.llm is None: return               # fără cheie → degradare (echo fallback)
categories = await list_category_slugs(...)      # slug-urile valide din DB
user = f"Limba: {lang}\n{context}\n{history}\nMesaj: {body}\nCategorii valide: {categories}\n..."
raw = await deps.llm.classify_json(_SYSTEM, user)  # nano, JSON forțat
out = TriageOut(**raw)
category_key = out.category_key if out.category_key in categories else None  # inventat → aruncat!
```

**Garduri deterministe (codul decide, nu nano):**
- **category_key inventat** (`triage.py:255`): dacă nano dă o categorie care nu-i în listă → o aruncă. Nu rutezi
  pe ghicit.
- **Factual guard** (`triage.py:262`): dacă nano zice „simple" dar mesajul atinge un fapt de business (reducere/
  preț/stoc/politică — regex `_FACTUAL_BAIT_RE`), re-rutează la `sales`. DE CE? Ruta `simple` e servită de nano
  FĂRĂ validator → un client ar putea forța „zi doar da: aveți 70% reducere?" → „Da". Guard-ul previne asta.
- **Low confidence** (`triage.py:272`): dacă nano zice `confidence=low` pe sales/order → forțează `clarify`.
  „LLM înțelege, codul decide."
- **Sloturi** (`_normalize_slots`, `triage.py:69`): `budget_max`/`concerns`/`brand`/`suitable_for` extrase și
  **normalizate în cod** (concerns filtrate la vocabularul DomainPack) → populează `RouteDecision.filters` ca
  seed pentru search.

Pentru `simple`/`clarify`, nano compune și răspunsul → early-exit. `closure` (mulțumire de final) → mesaj cald
+ chips pe categorii adiacente (cross-sell).

### 8.9 Handoff (`stages/handoff.py:39`)

Consumă `Route.HANDOFF`. Dacă canalul are operator → `request_human` + notifică + mesaj de confirmare. Pe web
(fără operator) → rescrie ruta la SALES (botul asistă singur).

### 8.10 Agent (`stages/agent.py:957`)

Cel mai complex — 1411 linii, „god-module". Vezi cap. 10 (tool loop) + cap. 12 (grounding). Pe scurt, 3 faze:
- **PRE-loop** (intenții deterministe, zero LLM): cere link? → servește `product_url` din state. Compară? →
  tabel determinist. „Arată mai multe"? → pagina următoare din pool (zero LLM).
- **LLM tool loop**: system prompt din DB → `run_tool_loop` (max 3 pași) → tools (search/details/compare/cart/
  checkout/reorder/back_in_stock/faq/check_order/request_human).
- **POST-loop** (compunere deterministă): checkout fallback, cross-sell, „mai ieftin", superlativ, apoi
  finalize (rich sau proză) + validator. Modelul propune, **codul dispune** pe cifre/linkuri.

### 8.11 Fallback (`runner.py:169`)

Dacă niciun stagiu n-a produs reply (rută neacoperită, triaj fără răspuns, fără cheie OpenAI) → o întrebare de
clarificare, **NU tăcere** (principiul 6). ⚠️ Doar în română (încalcă P11 — R7 în backlog).

---

<a name="9-buget-context"></a>
## 9. Bugetul de context (mesaj lung → păstrăm doar o parte)

📄 `src/worker/context.py`

**Ăsta e exact detaliul pe care l-ai cerut.** LLM-ul are un buget de tokeni. Nu-i trimiți toată conversația —
o **tai la buget**, în cod (principiul 4 — bugetul e în cod, nu în prompturi, nu prin disciplină).

### Sunt DOUĂ tăieri de „mesaj lung"

1. **Mesajul CURENT prea lung** → tăiat la **2000 caractere** în Gates (`INBOUND_BODY_MAX`, vezi 8.1). Asta e
   protecție de input.
2. **Istoricul + contextul** → bugetat aici, în `context.py`. Astea intră în promptul de triaj + agent.

### Blocurile de context și bugetele lor

```python
def conversation_transcript(history, *, max_turns=6, max_chars=1200):
    prior = history[:-1]                              # fără mesajul curent (ultimul)
    lines = [f"{'Client' if inbound else 'Asistent'}: {body}" for m in prior[-max_turns:]]
    return "\n".join(lines)[-max_chars:]              # ultimele 6 tururi, tăiat la 1200 char
```

| Bloc | Funcție | Buget | Ce conține |
|---|---|---|---|
| Transcript | `conversation_transcript` (`context.py:23`) | **6 tururi / 1200 caractere** | mesajele anterioare „Client:/Asistent:" |
| Profil | `customer_profile_block` (`context.py:40`) | **300 caractere**, liste tăiate la 4 | ce știm despre client (fără PII de canal) |
| State | `state_block` (`context.py:59`) | **3 produse / 600 caractere** | produse arătate (id+nume+preț) + constrângeri |
| Rezumat | `summary_block` (`context.py:85`) | **600 caractere** (`summary_max_chars`) | rezumatul conversației lungi (>8 mesaje) |

**Cum se tai:**
- Transcript: ia **ultimele** 6 tururi (`prior[-6:]`), apoi taie la **ultimele** 1200 caractere (`[-1200:]`).
  Deci dacă un mesaj vechi e uriaș, se pierde întâi el (păstrezi ce e recent = relevant).
- Profil: sare valorile goale, listele la 4 elemente, apoi taie la 300.
- Rezumat: pentru conversații > 8 mesaje, un rezumat rolling (generat post-tur de nano) acoperă mesajele vechi
  care au ieșit din fereastra de 8, tăiat la 600.

**DE CE ordinea asta?** `context_blocks` (`context.py:96`) unește: **rezumat (fundal vechi) → profil → state**,
iar transcriptul recent e concatenat downstream. Ordine cronologică: vechi → recent. Blocul stă în mesajul
**USER** (dinamic), NU în system → promptul static rămâne byte-identic → **prompt caching** OpenAI (75-90%
discount pe partea statică).

### search_query — de ce follow-up-urile scurte funcționează (`context.py:106`)

```python
def search_query(history, current, *, n=2):
    users = [m.body for m in history if m.direction == INBOUND and m.body]
    return " ".join(users[-2:])   # ultimele 2 mesaje ale CLIENTULUI
```

Când clientul zice „ceva mai ieftin", căutarea nu ia doar „ceva mai ieftin" (fără sens izolat) — ia ultimele
2 mesaje ale lui → caută în contextul corect.

**Exemplu complet:** clientul scrie un paragraf de 3000 de caractere despre pielea lui + ce vrea.
1. Gates taie la 2000 caractere (emit `body_truncated`).
2. La turul următor, mesajul intră în `history`. Când construim promptul, transcriptul ia ultimele 6 tururi,
   apoi taie la 1200 caractere — deci din paragraful lung rămâne coada relevantă.
3. Dacă conversația trece de 8 mesaje, restul se comprimă într-un rezumat de max 600 caractere.

Așa „păstrăm doar o parte" — la fiecare nivel, cu un buget clar în cod.

---

<a name="10-llm"></a>
## 10. Cusătura LLM (retry, tokeni maximi, tool loop)

📄 `src/agent/llm.py` — **SINGURUL** loc care vorbește cu OpenAI (principiul 2).

`get_llm()` (`llm.py:359`) întoarce un singleton `LLMClient`, sau **None** dacă nu e cheie → pipeline-ul
degradează grațios. Testele injectează un fake (zero apeluri reale în CI).

### Plafonul de tokeni pe apel (detaliul „max token pe mesaj")

📄 `_sampling` (`llm.py:139`)

```python
def _sampling(self, *, agent: bool) -> dict:
    out = {}
    if agent:
        out["max_completion_tokens"] = s.llm_max_tokens_agent   # 800 (config.py:523)
    if s.llm_sampling_enabled:
        out["temperature"] = s.llm_temperature_agent if agent else s.llm_temperature_triage  # 0.7 / 0.2
    return out
```

- **`max_completion_tokens` = 800** pe TOATE apelurile de agent (`llm_max_tokens_agent`). E plafonul de
  **output** (câți tokeni poate scrie modelul). Un completion patologic / buclă nu scapă de ceiling.
- **`max_completion_tokens`, NU `max_tokens`** — ultimul e deprecat → 400 pe modelele gpt-5.4-*. (Ăsta a fost
  un bug real: agentul trimitea `max_tokens` → 400 → fallback „n-am înțeles" pe orice sales. Fix în PR #133.)
- **Triajul (agent=False) NU primește ceiling** — JSON scurt, nu are nevoie.
- **Vision** (`describe_image`, `llm.py:305`): `max_completion_tokens=256` + `detail:"low"` = cost tăiat (un
  tile, fără high-res).
- **Temperatura pe rol:** triaj 0.2 (determinist, clasificare), agent 0.7 (variație, copy ne-repetitiv).
  Corectitudinea NU depinde de temperatură (o asigură validatorul) → agentul poate fi urcat liber.

### Retry mărginit (`_with_retry`, `llm.py:45`)

```python
for attempt in range(max_retries + 1):   # llm_retry_max = 2
    try: return await factory()
    except openai.APIStatusError as e:
        if e.status_code < 500 and not RateLimitError: raise   # 4xx terminal → ridică imediat
        wait = _retry_after_seconds(e)                         # respectă Retry-After
    except (APITimeoutError, APIConnectionError): wait = None
    sleep_s = (wait or delay) + jitter                          # backoff exponențial + jitter
    await asyncio.sleep(sleep_s); delay *= 2
raise last                                                      # epuizat → ridică (caller degradează)
```

**Ce e tranzitoriu (retry-abil):** 429 (rate limit), 5xx, timeout, connection. **Ce e terminal (ridică imediat):**
400/401/403/404 (nu are sens să reîncerci un request greșit). La epuizare loghează `llm_api_failure` și ridică
→ caller-ul (stagiul) prinde și degradează.

### Metodele adaptorului

| Metodă | Ce face | Cine o folosește |
|---|---|---|
| `classify_json` (`llm.py:168`) | chat cu `response_format=json_object` | triaj |
| `complete_schema` (`llm.py:188`) | chat cu `json_schema` strict | agent (rich) |
| `complete` (`llm.py:211`) | chat text simplu | agent (proză) |
| `run_tool_loop` (`llm.py:227`) | bucla de tool-calling | agent |
| `moderate` (`llm.py:290`) | moderation (gratuit) | gates |
| `describe_image` (`llm.py:305`) | Vision poză→text | gates |
| `embed` (`llm.py:343`) | embeddings (1536 dim) | cache/FAQ/search/products |

### Tool loop (`run_tool_loop`, `llm.py:227`) — inima agentului

```python
for _ in range(max_steps):        # max 3 (CLAUDE.md: max 3 tool calls/tur)
    resp = await self._chat(agent=True, model=mdl, messages=messages, tools=tools, tool_choice="auto")
    msg = resp.choices[0].message
    if not msg.tool_calls:
        return msg.content        # modelul a răspuns cu text → gata
    messages.append({assistant + tool_calls})
    contents = await asyncio.gather(*(execute(tc.name, args) for tc in msg.tool_calls))  # CONCURENT
    for tc, content in zip(...): messages.append({"role":"tool", "content": content})
resp = await self._chat(agent=True, ...)   # cap atins → un ultim apel FĂRĂ tools (text forțat)
return resp.choices[0].message.content
```

**Analogie:** modelul e un vânzător care poate „cere de la depozit" (tool) până la 3 ori: „dă-mi seruri sub 100
lei" (search) → primește rezultate → „dă-mi detalii la #2" (details) → apoi scrie răspunsul. Formatul OpenAI
(tool_calls / rol `tool`) trăiește DOAR aici (adaptorul = singurul loc care vorbește OpenAI). Tool-urile cerute
într-un pas rulează **concurent** (`asyncio.gather`) ca să taie latența.

---

<a name="11-căutare"></a>
## 11. Căutarea de produse

📄 `src/tools/catalog_tools.py:383` (`search_products_tool`) — vezi Diagrama 5.

Motorul de căutare combină **lexical** (potrivire pe cuvinte) + **semantic** (potrivire pe înțeles).

### Fluxul

1. **Argumentele** de la model (categorie, filtre, buget, concerns).
2. **Map concerns** (`catalog_tools.py:399`): „ten gras" → atribut real, prin DomainPack.
3. **Moștenire sesiune** (`catalog_tools.py:417`): dacă e sesiune activă + fără categorie/concerns noi →
   moștenește filtrele → **paginare** (`continue_search_session`, pagina următoare, **zero cost LLM**).
4. **Scara de relaxare** (`build relax ladder`, `catalog_tools.py:439`): dacă nu găsești nimic cu toate
   filtrele, relaxezi filtrele pe rând (buget → concern → ... → categorie ultima).
5. **Embed query** (dacă disponibil, `catalog_tools.py:450`) → **UN** apel embed.
6. **Lexical** (FTS + pg_trgm, `catalog_tools.py:468`) + **Semantic** (pgvector HNSW, `catalog_tools.py:486`).
7. **Fuse** (`fuse_candidates`, `catalog_tools.py:507`): RRF (Reciprocal Rank Fusion) + blended rank (rating
   social-proof shrunk + disponibilitate + reducere + concern). Repară „un produs 4.6×148 recenzii ajunge sub
   unul 4.4×28".
8. **Diversify** (`catalog_tools.py:518`): prima pagină pe relevanță → terțe de preț + max 2/brand (nu top-N
   identice).
9. **Dedupe** vs `displayed_products`, cap 6. **Store session** (pool + fingerprint în state).
10. **ToolResult**: produse complete → validator; `llm_view` compact (max 6×8 câmpuri) → model.

**Degradare:** embed picat → doar lexical; semantic picat în tur → rămâne lexical. Niciodată nu crapă complet.

**Detaliu important — două vederi ale rezultatelor:** validatorul primește produsele **complete** (ca să verifice
prețuri/linkuri), dar modelul primește un `llm_view` **compact** (max 6 produse × 8 câmpuri) ca să nu-i umpli
contextul. Asta e tot buget de tokeni.

---

<a name="12-grounding"></a>
## 12. Grounding-ul anti-halucinație (apărarea load-bearing)

Aici trăiește obsesia centrală: **botul nu inventează**. Sunt **două** apărări, una per cale.

### Calea PROZĂ: validatorul (`agent.py:472-502`, `560-595`)

După ce agentul compune un răspuns text, validatorul verifică:
- Fiecare **preț** din reply există în `ctx.retrieval`? (`_PRICE_RE` + cifre bare fără valută, `validator_bare_numbers_enabled`)
- Fiecare **produs/link** menționat există în catalog?
- **Claim-uri neverificabile** (superlativ „best seller", stoc „pe stoc") → `validator_claims_enabled`.
- **Claim MEDICAL** (tratează/vindecă, sigur în sarcină, fără alergeni, recomandat de medic) →
  `safety_medical_guardrail_enabled` (răspundere juridică!).

Dacă invalid → **1 retry** cu prețurile permise → dacă tot invalid → **răspuns determinist din DB** (`agent.py:523`).
Zero prețuri inventate **structural** — nu prin disciplina modelului, ci prin cod.

### Calea RICH: compose (`compose.assemble`, `compose.py:329`) — vezi Diagrama 4c

Pentru carduri de produs, regula e și mai strictă: **modelul emite DOAR cuvinte + referințe `product_id`**.
Codul hidratează faptele:

1. Modelul întoarce JSON structurat (mini, `complete_schema`).
2. `assemble` hidratează după `product_id`.
3. **Membership grounding** (`MEM`): itemul referă un produs REAL adus? Nu → **aruncat**.
4. **Scrub câmp cu câmp** (`scrub_prose`/`scrub_intro`/`scrub_education`): cifre, `%`, claim-uri neverificabile
   → `field=None`.
5. **Medical** (`_unsafe_medical`): claim medical în câmp → **câmp aruncat** (P0 safety).
6. Badge din semnale reale (`badges.py`), pick determinist, chips.

**Exemplu:** modelul zice „serul ăsta cu vitamina C 15% reduce ridurile". Cod: `15%` → nu apare în date → scos.
„reduce ridurile" → claim medical → câmp aruncat. Rămâne doar ce e ancorat în catalog.

### NX-139 — axe de decizie grounded

Ultima adăugire: `decision_axes` + `spec_numbers` (`compose.py:661`, `704`). Axele REALE pe care variază setul
(tip de ten / fitment / material — per vertical, din DomainPack) intră ca input în compunere → intro-ul numește
axe reale, nu superficiale. Cifrele de specificație din datele produselor afișate („SPF 30", „50 ml") devin
permise în intro/education (grounded). Prețurile NU intră niciodată în setul permis.

---

<a name="13-ieșire"></a>
## 13. IEȘIRE: Sender TX + dispatcher + canale

### Sender = tranzacția din handle_turn (cap. 7, pas 8)

Reamintire: reply + outbox + patch state + finalizare dedupe = **o singură tranzacție atomică**. Ăsta e „un
singur punct de ieșire" (principiul 5). Nimic nu trimite mesaje în afară de outbox → dispatcher.

### Dispatcher (`src/worker/dispatcher.py:245`)

Proces separat. Buclă:
```python
rows = await claim_due(...)              # FOR UPDATE SKIP LOCKED (nu se calcă cu alt dispatcher)
for row in rows:
    sender = registry.get(row.channel_kind)
    render = choose_render(sender.capabilities, payload)   # rich/carousel/cards/template/text
    result = await sender.send(...)
    if ok: await mark_sent(...) + link provider_msg_id      # TX
    else:  await mark_failed(...) → backoff → dead
```

- **`FOR UPDATE SKIP LOCKED`**: dacă rulezi 2 dispatchere, fiecare ia rânduri diferite (nu dublă trimitere).
- **`choose_render`** (`dispatcher.py:101`): alege forma după **capabilitățile** canalului. WhatsApp are
  carusel; un canal fără rich primește `text` (floor aplatizat).
- **Visibility timeout self-healing**: dacă dispatcher-ul moare între claim și mark, rândul e „redeemed" după
  timeout (nu rămâne blocat).
- ⚠️ **Slăbiciune (R6):** dispatcher-ul e **secvențial** (rând cu rând, tenant cu tenant) + poll 2s la idle.
  Un tenant lent întârzie ceilalți; +0-2s latență pe calea async.

### Canalele (marginile de transport)

📄 `src/channels/base.py` — `ChannelSender` Protocol + `Capability` matrix (NX-115).

| Canal | Client | Capabilities | Notă |
|---|---|---|---|
| WhatsApp | `MetaClient` (`meta_client.py:28`) | TEXT, CAROUSEL, TEMPLATE, TYPING | canal PRIMAR de producție; fereastră 24h |
| Telegram | `TelegramClient` (`telegram/client.py:62`) | TEXT, RICH, TYPING | canal de TEST; edit carusel |
| Web | `WebSender` (`web/sender.py:34`) | TEXT, RICH, COMPARISON | publish SSE + backlog replay |

**DE CE „registry"?** Dispatcher-ul nu știe de canale — cere `registry.get(channel_kind)`. Adaugi un canal nou
implementând `ChannelSender` + înregistrându-l. Cuplajul de canal trăiește DOAR la 2 margini: ingestie (parser)
+ trimitere (sender). Pipeline-ul e agnostic de canal.

### Web widget — două moduri

- **Sincron** (`/web/chat`, `web/app.py:190`): `handle_turn(deliver=False)` → **fără outbox**, răspunsul HTTP e
  transportul. Frontendul primește direct.
- **Async** (`/web/messages` + SSE `/web/stream`): envelope pe stream (ca Telegram) → worker → dispatcher →
  WebSender publică pe SSE → browserul primește.

---

<a name="14-memorie"></a>
## 14. Memoria conversației

Vezi Diagrama 6. Memoria = 4 lucruri:

| Sursă | Ce e | Unde se încarcă | Unde se scrie |
|---|---|---|---|
| `history` | ultimele 8 mesaje | processor (`get_recent_messages`) | insert_message |
| `state` (jsonb ≤8KB) | ref-uri: produse afișate, cart, constrângeri, sesiune căutare | processor (`from_jsonb`) | Sender TX (`patch_conversation_state`) |
| `summary` | rezumat rolling (conversații >8 mesaje) | processor (`get_summary_for_context`) | post-tur (`summarizer`, nano) |
| `profile` + `lead_score` | ce știm despre client | processor (pe Contact) | post-tur (`extract_profile`, nano) |

**Ciclul de viață al state-ului:**
1. **START**: `from_jsonb` hidratează defensiv (cap. 4).
2. **ÎN prompturi**: `context_blocks` (cap. 9).
3. **DURANTĂ**: agentul mută in-place `constraints`, `search_constraints`; tool-urile cer `state_patch` (cart).
4. **SENDER TX**: merge canonic (cap. 7, pas 8a) + optimistic lock.
5. **POST-tur**: cache writeback + rezumat + profil.

**Regula de aur (principiul 8):** state = ref-uri `{id, name, price}`, NU obiecte complete. Buget 8KB (CHECK în
DB). Când ai nevoie de detalii → re-hidratezi din DB.

---

<a name="15-baza-de-date"></a>
## 15. Baza de date

📄 `src/db/connection.py` — vezi Diagrama 7.

### Două pool-uri, două căi

```python
admin_pool  (get_pool, connection.py:76)   → control plane + joburi. Rol PRIVILEGIAT.
bot_pool    (get_bot_pool, connection.py:207) → tenant path. Rol bot_runtime, FĂRĂ bypassrls.
```

- **`admin_conn`** (`connection.py:247`): folosit DOAR pentru `resolve_channel` (canal→business, precede tenantul)
  + joburi admin. RLS bypass-at aici → suprafață limitată intenționat.
- **`tenant_conn(business_id)`** (`connection.py:261`): hot path, RLS activ.

### tenant_conn — izolarea în detaliu

```python
async with pool.acquire() as conn:
    if _isolation_enabled():
        row = await conn.fetchrow(
            "select set_config('app.business_id', $1, false) as biz, current_user as usr", business_id)
        _check_isolation(row["usr"], row["biz"], business_id)   # rol==bot_runtime? GUC==expected?
    try:
        yield conn
    finally:
        await conn.execute("select set_config('app.business_id', '', false)")  # RESET la release
```

- **Setare + verificare într-un round-trip** (`set_config` întoarce valoarea, `current_user` confirmă rolul).
- **`_check_isolation`** (`connection.py:187`): conexiunea TREBUIE să fie `bot_runtime` ȘI să poarte exact
  `app.business_id = expected`. Orice abatere (rol greșit / GUC nesetat / reuse murdar de la alt tenant) →
  **IsolationError ÎNAINTE de primul query** + log CRITICAL.
- **RESET la release**: următorul checkout pe alt tenant nu vede GUC-ul vechi.

**DE CE rol de LOGIN, nu `SET ROLE`?** `SET ROLE` de sesiune se scurgea sub multiplexarea poolerului Supabase
(P0-A din audit). Cu login direct, identitatea e fixată de credențiale. RLS devine plasă: **un query fără filtru
de tenant → 0 rânduri, nu datele altui client**.

**Izolarea multi-tenant e în straturi:** `WHERE business_id=$1` în cod (primar) + RLS (plasă). Chiar dacă uiți
filtrul, RLS te salvează.

### Boot-gate (`consumer.py:315-322`)

```python
await assert_migrations_current(pool)   # migrare pending → BOOT REFUZAT
await get_bot_pool()                     # eager → parolă greșită crapă la boot, nu la primul mesaj
```

Workerul refuză să pornească pe o schemă incompletă sau cu rol greșit — crash loud.

### Tabelele principale (din `docs/schema_v2_production.sql`)

| Tabel | Ce stochează | Cine scrie | Cine citește |
|---|---|---|---|
| `businesses` | tenanții (config, vertical, plafon cost) | seed/admin | processor (load_business) |
| `channels` | canalele per tenant (kind, provider_account_id) | seed | resolve_channel |
| `contacts` | clienții (profile, lead_score, consent, is_blocked) | processor, post-tur | gates, context |
| `channel_identities` | **PII-ul de canal** (telefon E.164 + hash) — DOAR aici | get_or_create_contact | identity resolution |
| `conversations` | conversații (state jsonb, bot_active, handoff_until, last_inbound_at) | Sender TX | processor |
| `messages` [partiționat] | toate mesajele (direction, author, body, cost, tokens) | insert_message | history |
| `inbound_dedupe` | claim dedupe L2 (provider_msg_id, completed_at) | claim/mark | dedupe |
| `outbox` | coada de ieșire (idempotency_key, payload, status) | Sender TX | dispatcher |
| `message_status_events` | delivered/read/failed | record_status_event | — |
| `products` | catalogul (name, price, availability, attributes) | sync (nu worker) | search |
| `product_embeddings` | vectori 1536 (HNSW cosine) | embed job | semantic search |
| `faqs` | întrebări+răspunsuri (embedding) | seed | faq_stage |
| `semantic_cache` | răspunsuri cache-uite (embedding, TTL) | post-tur writeback | cache_stage |
| `intent_aliases` | fraze aprobate → target | shadow mode | alias_stage |
| `checkout_links` | linkuri de plată (ref_code → atribuire) | checkout_link tool | orders webhook |
| `orders` | comenzi (attribution) | orders webhook | check_order |
| `proactive_jobs` | joburi proactive (kind, scheduled_at) | initiators | proactive scheduler |
| `analytics_events` [partiționat] | event-uri (append-only, turn_id) | runner/processor | rollup |
| `usage_daily` | rollup zilnic (cost, tokeni) — sursa de facturare | rollup nocturn | dashboard/cost guard |

**Convenții:** toate tabelele tenant-scoped au `business_id` NOT NULL + index compus. Idempotență = UNIQUE pe
`(business_id, external_id)`. Hot tables (`messages`, `analytics_events`) partiționate pe lună. PII DOAR în
`channel_identities`.

---

<a name="16-cost"></a>
## 16. Guvernanța costului (3 niveluri)

📄 `src/worker/limits.py` + `processor.py:308-379`

LLM-ul e scump. Sistemul are 3 plafoane, cu contoare în Redis (rapid) + reseed din `usage_daily` (durabil):

1. **Business/zi** (`daily_cost_cap_usd`, default $5): `_llm_within_budget` (`processor.py:308`) pre-check. Peste
   plafon → `llm=None` → pipeline degradat.
2. **Contact/zi** (`contact_daily_cost_cap_usd`, opt-in): o conversație în buclă nu poate arde plafonul întregului
   tenant. Doar canale identificate.
3. **Vizitator web** (`web_cost_cap_per_visitor_usd`, $0.50): un token public furat nu golește bugetul.

**Enforcement post-increment fără TOCTOU** (`_record_turn_cost`, `processor.py:345`): costul EXACT al turului
(din `ctx.usage.cost_usd`, tokeni reali) se adaugă ATOMIC; dacă noul total ≥ plafon, emite `cost_guard_tripped`
→ turul URMĂTOR e blocat de pre-check. Facturarea reală = `usage_daily`, nu contorul (care e plasă).

**Buget PER TUR** (observabilitate, `runner.py:132`): dacă un tur depășește latența (5000ms) sau costul ($0.01),
runner-ul emite `turn_over_budget` cu stagiul cel mai lent. NU schimbă turul — doar alertează.

---

<a name="17-erori"></a>
## 17. Tratarea erorilor (niciodată tăcere)

Vezi Diagrama 9. Principiul 6 pus în practică pe fiecare tip de eroare:

| Eroare | Ce se întâmplă | Rezultat |
|---|---|---|
| LLM pică | retry mărginit → triaj route None / agent return → `fallback_stage` | întrebare de clarificare (nu tăcere) |
| Validator pică | 1 retry cu prețuri permise → răspuns determinist din DB | zero prețuri inventate |
| Cost guard atins | `llm=None` — gates+cache merg, stagiile LLM sar | degradare |
| Redis jos la webhook | 503 → Meta reîncearcă | dedupe prinde la retry |
| Cache/FAQ eroare | miss, turul continuă | best-effort |
| Analytics eroare | log only | observabilitatea nu blochează |
| Conv lock busy | requeue cu cap → **drop la epuizare** ⚠️ | NX-140 |
| **Excepție procesare** | **ACK + log = tur PIERDUT** ⚠️ | NX-140 (gaura #1) |
| Livrare pică | backoff → dead (vizibil) | nu tăcut |
| Handoff | `request_human` + `handoff_until` + notifică | tururile următoare = tăcere intenționată |

**Cele două găuri reale (NX-140):** excepție de procesare = ACK + drop, și lock blocat = drop. Ambele încalcă
„niciodată tăcere" exact pe calea de eroare. Fix: re-queue cu contor + fallback în outbox.

---

<a name="18-proactiv"></a>
## 18. Proactiv (mesaje inițiate de bot)

📄 `src/proactive/` — vezi Diagrama 10.

Mesaje pe care botul le trimite **primul** (coș abandonat, stoc revenit). Cele mai reglementate decizii
(consent + fereastra 24h Meta) — 100% cod determinist, zero LLM.

1. **FEED**: `jobs.scheduler` rulează sweepere (`sweep_abandoned_cart`, `sweep_back_in_stock`, `initiators.py`)
   → creează rânduri în `proactive_jobs`. ⚠️ `schedule_awb_update` + `schedule_follow_up` sunt **definite dar
   niciodată apelate** (seam-uri TODO).
2. **ENGINE** (`proactive/scheduler.py:181`): `claim_due_jobs` (FOR UPDATE SKIP LOCKED) → rezolvă conversație+
   canal+destinatar → `build_message_spec`.
3. **GATE** (`decide_proactive`, `templates.py:39`): consent pe acel tip? → în fereastra 24h? → dacă da, text
   liber; dacă nu, template aprobat în locale? → altfel `skipped_no_window`.
4. → `outbox` (idempotency `proactive:job_id`) → dispatcher.

---

<a name="19-config"></a>
## 19. Referință de configurare (toate butoanele)

📄 `src/config.py` — sursa unică. Nimic hardcodat, nimic din `os.environ` direct.

### Bugete/plafoane structurale

| Setare | Default | Ce controlează |
|---|---|---|
| `INBOUND_BODY_MAX` (constantă) | 2000 | caractere max pe corpul inbound (trunchiere în Gates) |
| `webhook_max_body_bytes` | 262144 (256KB) | plasă anti-OOM webhook |
| `web_max_body_bytes` | 16384 (16KB) | plasă anti-OOM web |
| `reply_split_chars` | 200 | prag de spargere reply în 2 |
| `llm_max_tokens_agent` | 800 | `max_completion_tokens` pe agent |
| `summary_max_chars` | 600 | buget bloc rezumat |
| `summary_threshold` | 20 | de la câte mesaje se sumarizează |

### Praguri LLM/cache/FAQ

| Setare | Default | Ce controlează |
|---|---|---|
| `llm_temperature_triage` / `_agent` | 0.2 / 0.7 | determinism vs variație |
| `llm_retry_max` | 2 | reîncercări pe tranzitoriu |
| `llm_timeout_s` | 30 | anti-hang |
| `cache_tau_high` | 0.92 | prag auto-accept cache |
| `faq_tau_high` / `_policy` / `_tool` | 0.78 / 0.45 / 0.66 | praguri FAQ |

### Plafoane de cost

| Setare | Default | Ce controlează |
|---|---|---|
| `daily_cost_cap_usd` | 5.0 | plafon zilnic business |
| `contact_daily_cost_cap_usd` | 0.0 (off) | plafon per-contact |
| `web_cost_cap_per_visitor_usd` | 0.50 | plafon per-vizitator web |
| `turn_latency_budget_ms` / `turn_cost_budget_usd` | 5000 / 0.01 | alerte per-tur |

### Kill-switch-uri importante (fail-safe)

`moderation_enabled`, `cache_enabled`, `faq_enabled`, `alias_enabled`, `vision_enabled`, `cost_guard_enabled`,
`rate_limit_enabled`, `conv_lock_enabled`, `search_sessions_enabled`, `card_badges_enabled`,
`safety_medical_guardrail_enabled`, `domain_pack_enabled`, `proactive_enabled`, `web_enabled`,
`web_identity_enabled`, `ai_disclaimer_enabled` (azi OFF), `rich_pick_web_enabled` (azi OFF — preferință client).

**Filozofia flag-urilor:** fiecare feature nou are un kill-switch fail-safe. OFF = comportamentul vechi,
byte-identic. Poți dezactiva orice în prod fără redeploy de cod (doar env).

---

<a name="20-debugging"></a>
## 20. Ghid de depanare pe subsisteme

### Cum depanează un senior aici

Nu se uită la cod întâi. Se uită la **event-urile din `analytics_events`** filtrate pe `turn_id` — reconstruiește
traiectoria exactă a turului (ce stagii au rulat, cât au durat, unde a ieșit). `turn_id` e injectat automat în
fiecare event (`ctx.emit`). E „black box recorder"-ul. Abia apoi pui breakpoint.

### Loguri cheie (ce să `grep`-uiești)

| Simptom | Grep | Ce înseamnă |
|---|---|---|
| Client n-a primit nimic | `eroare la procesarea mesajului` (`consumer.py:229`) | Excepție → ACK → tur pierdut (NX-140) |
| Client n-a primit nimic | `dropped după N reîncercări` (`consumer.py:96`) | Lock blocat → drop |
| Răspuns lent | `tur peste buget` (`runner.py:158`) | Latență/cost peste prag + stagiul lent |
| Bot „a amuțit" | `tăcere intenționată (handoff)` (`processor.py:537`) | Corect: a preluat un om |
| Mesaj de 2 ori | `dedupe_hit_db` (`processor.py:457`) | Corect: dedupe a prins retry |
| LLM eroare | `llm_api_failure` (`llm.py:74`) | Retry epuizat |
| Izolare | `isolation_assert_failed` (`connection.py:285`) | O conexiune neizolată la checkout — CRITIC |
| Straturi moarte | `register_vector_codec: codec pgvector neînregistrat` | FAQ/cache degradează la text-inline |

### Breakpoint-uri strategice

| Loc | Ce inspectezi |
|---|---|
| `processor.py:528` (`await run_pipeline`) | `ctx` ÎNAINTE de pipeline: history, state, language |
| `processor.py:534` (`if ctx.reply is None`) | `ctx.reply`, `ctx.halt`, `ctx.route` DUPĂ pipeline — de ce n-a răspuns |
| `processor.py:565` (deschidere TX) | `new_state` cum se construiește — prinzi „botul uită" |
| `triage.py:246` (`out = TriageOut(**raw)`) | ce a clasificat nano |
| `agent.py:957` (agent_stage intrare) | ruta + retrieval + intenții deterministe |
| `connection.py:280` (`_check_isolation`) | rolul + GUC-ul la checkout |

### Bug-uri tipice și race conditions

- **Race pe state**: 2 mesaje rapide fără lock → ultimul suprascrie. Verifică `conv lock: ocupat → re-queue`.
- **Optimistic lock conflict**: `patch_conversation_state` eșuează dacă `state_version` s-a schimbat → excepție
  în TX → ACK → tur pierdut. Cauză: 2 tururi concurente.
- **„Botul uită"**: câmp mutat in-place dar ne-merge-uit la 8a (`processor.py:580`). Regula: orice câmp in-place
  TREBUIE listat explicit.
- **Silent dead layer**: strat best-effort care nu emite niciun event = excepție înghițită. Caută warning-ul.
- **Conexiune ținută prin LLM** (R5/NX-141): `conn` ocupat cât agentul face 1-4 apeluri LLM → pool 10 → max ~10
  tururi concurente/proces. Simptom la scală: „pool exhausted".

### Probleme LLM tipice

- **Fallback „n-am înțeles" pe orice sales**: agentul trimitea `max_tokens` (deprecat) → 400. Fix: `max_completion_tokens`.
- **Cache poisoning**: un răspuns „n-am găsit" cache-uit (cacheable=True greșit) servit altui client → tăcere de
  facto. Regula: răspunsurile de fallback/refuz/clarify au `cacheable=False`.
- **Deployed build lag**: un „bug live" poate fi imaginea prod în urma lui `main`. Verifică commit-ul deployat
  înainte să vânezi codul.

---

<a name="21-exerciții"></a>
## 21. Exerciții + validare

### Exerciții de modificare (de la ușor la greu)

**E1 (ușor)** — Schimbă pragul de spargere reply de la 200 la 300 caractere. Ce fișier? Ce se schimbă în
comportament? *(Indiciu: `config.py`, `reply_split_chars`.)*

**E2 (ușor)** — Vreau să văd în log câte produse afișează fiecare tur. Unde pui linia, ce loghezi (fără PII)?

**E3 (mediu)** — Adaugă un câmp `last_category` în state. Enumeră TOATE locurile: (a) `ConversationState`
dataclass, (b) `from_jsonb` hidratare defensivă, (c) merge canonic în TX. De ce dacă sari peste (c) botul „uită"?

**E4 (mediu)** — Adaugă un tool nou `check_warranty(product_id)`. Ce fișiere atingi? *(Indiciu: `tools/`,
`tool_definitions.py`, registrul, promptul.)*

**E5 (mediu)** — Adaugă un stagiu nou de pipeline între cache și FAQ. Ce semnătură trebuie să aibă? Unde-l pui
în `DEFAULT_STAGES`? De ce nu trebuie să atingi runner-ul?

**E6 (greu)** — Repară gaura ACK-on-error (NX-140). La `consumer.py:228-230`, schițează: re-queue cu contor →
la epuizare fallback în outbox. De ce fallback-ul trebuie în outbox și nu trimis direct?

**E7 (greu)** — Rezolvă R5 (conexiune ținută prin LLM). Cum fazezi `handle_turn`: load → release conn → LLM
fără conn → TX pe conn proaspăt? Ce câștigi la scală?

### Validare (răspunde, nu-ți dau răspunsurile)

1. **(Grilă)** Câte puncte LLM are pipeline-ul? (a) 11; (b) 2; (c) 5; (d) 0.
2. **(A/F)** Webhook-ul așteaptă răspunsul OpenAI înainte de ACK 200.
3. **(Deschisă)** Explică cele DOUĂ tăieri de „mesaj lung" (unde + la ce valoare).
4. **(Ce se întâmplă dacă…)** Modelul scrie „79 lei" dar produsul are 129 în retrieval. Pe calea proză vs rich?
5. **(Debugging)** „Botul n-a răspuns deloc." Numește 2 locuri unde un tur se pierde tăcut + grep-ul pentru fiecare.
6. **(Arhitectură)** De ce e `resolve_channel` singurul query pe `admin_conn`?
7. **(Prezice)** Ștergi liniile `active_search=None` la `processor.py:592-593`. Ce bug apare?
8. **(Găsește bug-ul)** Muți merge-ul canonic DUPĂ `state_patch` în TX. Ce se strică?
9. **(Grilă)** `max_completion_tokens` vs `max_tokens`? De ce contează?
10. **(Race)** De ce ai nevoie ȘI de lock per conversație ȘI de optimistic lock pe `state_version`?

---

## Anexă — cele 12 principii (din CLAUDE.md, verificate în cod)

1. Pipeline liniar — niciun stagiu nu sare înapoi. ✅ (`runner.py`)
2. LLM doar în 2 puncte — triaj + agent. ✅ (`llm.py` = singura cusătură)
3. Un singur proprietar per câmp. ✅ (`models.py` docstrings)
4. Buget de context în cod, nu în prompturi. ✅ (`context.py`)
5. Un singur punct de ieșire — Sender → outbox → dispatcher. ✅ (excepție: typing)
6. Niciodată tăcere. ✅ (cu 2 găuri: NX-140)
7. business_id pe tot + RLS. ✅ (`connection.py`)
8. State = ref-uri, nu obiecte. ✅ (`models.py` ProductRef)
9. Promptul se generează din DB. ✅ (`prompt_builder.py`)
10. Observabilitate din runner. ✅ (`runner.py` măsoară, stagiile nu știu)
11. Limba e parte din cheie. ✅ (locale în FAQ/cache/template; excepție: fallback RO-only)
12. PII într-un singur loc — channel_identities. ✅ (+ mascare PII input)

**Concluzie:** sistemul e neobișnuit de disciplinat — principiile chiar sunt implementate. Datoriile reale:
concentrarea (`agent.py` + `processor.py` acumulează tot ce e nou) și gaura ACK-on-error (NX-140).
