# Nativx Assistant — Workflow Architecture (n8n-style)

> Generat prin reverse-engineering pe codul real (branch `feat/NX-139-decision-axes`, 2026-07-02).
> Schelet verificat contra `arch_explorer/` (AST-derivat: 719 noduri / 555 muchii) și a codului sursă.
> **Regulă de evidență:** fiecare muchie corespunde unui call/import real, citat `file:line`.
> **Verificat prin trace invers de execuție** (2026-07-02): fiecare entry point simulat până la
> terminare, 17 nepotriviri corectate (v. Diagram 4a/4b split).
> **Runda 2 — audit adversarial** (2026-07-02): sweep pe fișierele necitite la prima trecere →
> +Diagram 4c (compunerea rich / grounding) și Diagram 10 (proactiv), +18 noduri/muchii
> (rute de margine, price-check cache, typing-bypass, operator webhook, media download,
> XADD trim). Cod mort și seams neapelate marcate în audit. Simplificări asumate, notate:
> Vision fail-soft păstrează caption-ul (`gates.py:397-434`), debounce coalescing N→1
> (`debounce.py:76-79`), `_persist_events`/`_record_turn_cost` între pipeline și reply
> (`processor.py:529-532`), kill-switch `DB_ISOLATION_ASSERT=off` (`connection.py:181-184`).
> Diagramele se randează în VS Code (extensia Mermaid Chart) sau pe GitHub.

**Procese** ([docker-compose.yml:31-98](../docker-compose.yml)):

| Serviciu            | Comandă                                   | Rol                                                           |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------- |
| `webhook`         | `uvicorn src.webhook.app:app`            | Ingress HTTP (Meta + orders + /web/*)                         |
| `worker`          | `python -m src.worker.consumer`          | Consumer Redis Streams → pipeline                            |
| `dispatcher`      | `python -m src.worker.dispatcher`        | outbox → canale (singurul punct de trimitere)                |
| `scheduler`       | `python -m src.jobs.scheduler`           | Joburi mentenanță (rollup/embed/lifecycle/cleanup)          |
| `proactive`       | `python -m src.proactive.scheduler`      | proactive_jobs → outbox                                      |
| `telegram-poller` | `python -m src.channels.telegram.poller` | Long polling Telegram (canal TEST)                            |
| `redis`           | —                                         | Streams · locks · dedupe L1 · cost counters · SSE pub/sub |

DB = Supabase Postgres 16 (extern), RLS pe rol `bot_runtime`.

---

## Diagram 1 — Overall Architecture

```mermaid
flowchart LR
  classDef user fill:#f9e79f,stroke:#b7950b,color:#000
  classDef edge fill:#aed6f1,stroke:#2874a6,color:#000
  classDef queue fill:#f5b7b1,stroke:#922b21,color:#000
  classDef worker fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef ai fill:#d7bde2,stroke:#6c3483,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef ext fill:#fad7a0,stroke:#af601a,color:#000
  classDef bg fill:#a3e4d7,stroke:#148f77,color:#000

  subgraph Users["Users"]
    WA_USER["WhatsApp customer"]:::user
    TG_USER["Telegram tester"]:::user
    WEB_USER["Website visitor widget"]:::user
    SHOP["Shop platform orders"]:::ext
  end

  subgraph Ingress["Ingress Edges — thin, no DB"]
    WEBHOOK["GET /webhook verify · POST /webhook<br/>signature + dedupe L1"]:::edge
    ORDERS["POST /webhook/orders/:biz<br/>HMAC"]:::edge
    WEB_API["GET /web/bootstrap session<br/>POST /web/messages async · POST /web/chat sync"]:::edge
    POLLER["Telegram poller<br/>getUpdates loop"]:::edge
  end

  subgraph Queue["Redis Backbone"]
    STREAM[("Stream 'inbound'<br/>XADD / XREADGROUP")]:::queue
    LOCKS[("conv locks · dedupe L1<br/>cost counters · SSE pub/sub")]:::queue
  end

  subgraph Worker["Worker process"]
    CONSUMER["Consumer + Debouncer 3s"]:::worker
    PROCESSOR["handle_turn<br/>contact→conv→pipeline→outbox TX"]:::worker
    PIPELINE["Pipeline 11 stages<br/>gates→free layers→triage→agent"]:::ai
  end

  subgraph Egress["Egress"]
    OUTBOX[("outbox table")]:::db
    DISPATCHER["Dispatcher<br/>claim_due → send → mark"]:::worker
    META_C["MetaClient"]:::ext
    TG_C["TelegramClient"]:::ext
    WEB_S["WebSender → SSE"]:::ext
  end

  subgraph Background["Background processes"]
    JOBS["jobs.scheduler<br/>rollup·embed·lifecycle·cleanup·initiators"]:::bg
    PROACTIVE["proactive.scheduler<br/>proactive_jobs → outbox"]:::bg
  end

  subgraph Data["Data plane"]
    PG[("Supabase Postgres 16<br/>RLS bot_runtime + admin pool")]:::db
    OPENAI["OpenAI API<br/>nano triage · mini agent<br/>embed · vision · moderation"]:::ai
  end

  WA_USER --> WEBHOOK --> STREAM
  SHOP --> ORDERS --> STREAM
  WEB_USER --> WEB_API
  WEB_API -- "async envelope" --> STREAM
  WEB_API -- "sync in-process" --> PROCESSOR
  TG_USER --> POLLER --> STREAM
  STREAM --> CONSUMER --> PROCESSOR --> PIPELINE
  PIPELINE <--> OPENAI
  PIPELINE <--> PG
  PROCESSOR --> OUTBOX --> DISPATCHER
  DISPATCHER --> META_C --> WA_USER
  DISPATCHER --> TG_C --> TG_USER
  DISPATCHER --> WEB_S -- "SSE /web/stream" --> WEB_USER
  JOBS --> PG
  JOBS --> PROACTIVE
  PROACTIVE --> OUTBOX
  CONSUMER <--> LOCKS
  CONSUMER -. "typing direct — bypasses outbox<br/>consumer.py:59-78" .-> META_C
```

**Metadata (noduri cheie):**

| Nod                  | Fișier                                | Funcție               | Responsabilitate                                                            |
| -------------------- | -------------------------------------- | ---------------------- | --------------------------------------------------------------------------- |
| POST /webhook        | `src/webhook/app.py:81`              | `receive_webhook()`  | Verifică semnătura Meta, dedupe L1, XADD, ACK 200 <50ms                   |
| POST /webhook/orders | `src/webhook/app.py:127`             | `receive_order()`    | HMAC pe corp brut → envelope`kind=order` pe stream                       |
| POST /web/chat       | `src/web/app.py:190`                 | `web_chat()`         | Pipeline in-process, răspuns în același HTTP request                     |
| POST /web/messages   | `src/web/app.py:157`                 | `web_message()`      | Envelope neutru pe stream; reply prin SSE                                   |
| Telegram poller      | `src/channels/telegram/poller.py:70` | `poll_once()`        | getUpdates → envelope neutru pe stream                                     |
| Stream inbound       | `src/redis_bus.py:66`                | `enqueue_inbound()`  | XADD cu maxlen ~100k                                                        |
| Consumer             | `src/worker/consumer.py:275`         | `run_consumer()`     | XREADGROUP + debounce + reaper PEL                                          |
| handle_turn          | `src/worker/processor.py:418`        | `handle_turn()`      | Miezul turului: dedupe L2 → context → pipeline → outbox TX               |
| Pipeline             | `src/worker/runner.py:47`            | `run_pipeline()`     | Stagii în ordine fixă, early-exit pe reply/halt, măsoară                |
| Dispatcher           | `src/worker/dispatcher.py:245`       | `run_dispatcher()`   | Singurul punct de trimitere: outbox → ChannelSender                        |
| MetaClient           | `src/meta_client.py:28`              | `MetaClient`         | WhatsApp Cloud API send (text/template/typing)                              |
| TelegramClient       | `src/channels/telegram/client.py:62` | `TelegramClient`     | Bot API send + edit carusel                                                 |
| WebSender            | `src/channels/web/sender.py:34`      | `WebSender`          | Publish SSE pe`web:out:{visitor}` + backlog replay                        |
| Proactive            | `src/proactive/scheduler.py:181`     | `run_scheduler()`    | Joburi scadente → poartă consent/24h → outbox                            |
| Jobs scheduler       | `src/jobs/scheduler.py:36`           | `Job` loop           | rollup_usage · embed_products · lifecycle · cleanup_dedupe · initiators |
| GET /webhook         | `src/webhook/app.py:62`              | `verify_webhook()`   | Handshake verificare Meta (token → challenge / 403)                        |
| GET /web/bootstrap   | `src/web/app.py:136`                 | `web_bootstrap()`    | Emite sesiunea vizitatorului (HMAC) + verificare Origin server-side         |
| Rich compose         | `src/worker/compose.py:329`          | `assemble()`         | Lanțul de grounding al căii bogate — v. Diagram 4c                       |
| Proactive gate       | `src/proactive/templates.py:39`      | `decide_proactive()` | Consent per kind + fereastra 24h + template aprobat — v. Diagram 10        |

---

## Diagram 2 — Application Startup

```mermaid
flowchart TD
  classDef proc fill:#aed6f1,stroke:#2874a6,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef gate fill:#f5b7b1,stroke:#922b21,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000

  subgraph WorkerBoot["worker: python -m src.worker.consumer"]
    W0["_main consumer.py:306"]:::proc
    W1["get_pool — admin pool"]:::step
    W2{"assert_migrations_current<br/>scripts/migrate.py"}:::gate
    W3["get_bot_pool EAGER"]:::step
    W4{"current_user == bot_runtime?<br/>connection.py:96"}:::gate
    W5["register pgvector codec<br/>connection.py:138"]:::step
    W6["get_redis"]:::step
    W7["build_registry — Meta/TG senders<br/>dispatcher.py:254"]:::step
    W8["ensure_group XGROUP MKSTREAM<br/>consumer.py:81"]:::step
    W9["run_consumer loop READY"]:::proc
    WFAIL["BOOT REFUSED — crash loud"]:::gate
  end

  subgraph WebhookBoot["webhook: uvicorn src.webhook.app"]
    A0["FastAPI app import app.py:22"]:::proc
    A1["body-size middleware app.py:25"]:::step
    A2{"web_enabled?"}:::gate
    A3["mount /web router + CORS<br/>app.py:159-177"]:::step
    A4["serve READY"]:::proc
  end

  subgraph DispatcherBoot["dispatcher: python -m src.worker.dispatcher"]
    D0["_main dispatcher.py:275"]:::proc
    D1["get_pool + get_bot_pool eager"]:::step
    D2{"web_enabled?"}:::gate
    D3["get_redis → WebSender"]:::step
    D4["build_registry by credentials"]:::step
    D5["run_dispatcher loop READY"]:::proc
  end

  subgraph OtherBoot["scheduler / proactive / telegram-poller"]
    J0["jobs._main scheduler.py:197<br/>job list built by flags :122-162"]:::proc
    J1["loop: run due jobs + heartbeat<br/>scheduler.py:186-194"]:::step
    P0["proactive._main scheduler.py:190"]:::proc
    P1{"proactive_enabled? :193"}:::gate
    P2["get_pool + get_bot_pool eager<br/>:196-197"]:::step
    P3["run_scheduler loop READY"]:::proc
    PX["exit — process ends :194-195"]:::gate
    T0["poller._main poller.py:118"]:::proc
    T1{"telegram_bot_token? :122"}:::gate
    T2["get_me → account_id :129-130"]:::step
    T3["run_poller loop READY"]:::proc
    TX2["exit — no token :123-124"]:::gate
  end

  subgraph Config["Configuration"]
    CFG["Settings pydantic-settings<br/>config.py:19 — .env"]:::db
  end

  CFG -.-> W0
  CFG -.-> A0
  CFG -.-> D0
  CFG -.-> J0
  J0 --> J1
  P0 --> P1
  P1 -- no --> PX
  P1 -- yes --> P2 --> P3
  T0 --> T1
  T1 -- no --> TX2
  T1 -- yes --> T2 --> T3
  W0 --> W1 --> W2
  W2 -- "pending migration" --> WFAIL
  W2 -- ok --> W3 --> W4
  W4 -- "wrong role" --> WFAIL
  W4 -- ok --> W5 --> W6 --> W7 --> W8 --> W9
  A0 --> A1 --> A2
  A2 -- yes --> A3 --> A4
  A2 -- no --> A4
  D0 --> D1 --> D2
  D2 -- yes --> D3 --> D4
  D2 -- no --> D4
  D4 --> D5
```

Evidență: poarta de boot pe migrări `src/worker/consumer.py:315-321`; assert rol `bot_runtime` `src/db/connection.py:96-107`; pool eager („parolă greșită crapă la boot, nu la primul mesaj") `src/worker/consumer.py:322`.
**Shutdown** (audit adversarial): worker `finally: close_media + close_redis + close_pool` (`consumer.py:331-334`); dispatcher idem (`dispatcher.py:286-289`). Singletoni lazy la runtime (nu la boot): `get_llm`, `get_media_registry` (`channels/media.py:34-43`), `SessionSecretCache` (`web/session.py:98-101`).

---

## Diagram 3 — User Message Workflow (async: WhatsApp / Telegram / web-SSE)

```mermaid
flowchart TD
  classDef edge fill:#aed6f1,stroke:#2874a6,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef queue fill:#f5b7b1,stroke:#922b21,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef err fill:#f1948a,stroke:#922b21,color:#000

  MSG["Inbound message"]:::edge
  CAP{"body over cap?<br/>enforce_body_cap app.py:99-101"}:::dec
  R413["413 payload too large"]:::err
  SIG{"signature valid?<br/>webhook/app.py:103"}:::dec
  JSONV{"valid JSON?<br/>app.py:106-109"}:::dec
  R400["400 bad request"]:::err
  R403["403 forbidden"]:::err
  DED1{"message seen before? dedupe L1 Redis<br/>MESSAGES only — statuses skip :112-118<br/>redis_bus.py:53"}:::dec
  SKIP1["skip — Meta retry"]:::step
  XADD["XADD stream inbound<br/>maxlen ~100k trim — oldest LOST under backlog<br/>redis_bus.py:68-73"]:::queue
  RDOWN{"Redis down?"}:::dec
  R503["503 → Meta retries"]:::err
  ACK200["ACK 200 under 50ms"]:::step

  XREAD["consumer XREADGROUP<br/>consumer.py:198"]:::step
  KIND{"event kind?<br/>consumer.py:219"}:::dec
  TYPING["typing indicator fire-and-forget<br/>consumer.py:222"]:::step
  DEB["Debouncer 3s coalesce<br/>debounce.py:57"]:::step
  ORDERK["process_order — attribution<br/>webhook/orders.py:64"]:::step
  RESOLVE["resolve_channel → business_id<br/>admin_conn, consumer.py:123"]:::db
  STATUSK["record_status_event<br/>delivered/read/failed"]:::db
  LOCK{"conv lock acquired?<br/>consumer.py:159"}:::dec
  REQUEUE["requeue + backoff, capped<br/>consumer.py:90"]:::queue
  DROPQ["over requeue cap → event DROPPED<br/>log only — consumer.py:95-96"]:::err
  LOADB["load_business + DomainPack attach<br/>consumer.py:173 · businesses.py:60"]:::db
  CBK{"callback?"}:::dec
  CAROUSEL["handle_callback — carousel nav<br/>callback.py:36"]:::step

  HT["handle_turn processor.py:418"]:::step
  DED2{"claim_inbound dedupe L2 DB?<br/>processor.py:456"}:::dec
  SKIP2["deduped — return"]:::step
  CTX["contact + conversation + insert inbound msg<br/>+ history max 8 + state + summary"]:::db
  SEEDC["seed_daily_cost lazy reseed from usage_daily<br/>processor.py:523-524"]:::step
  GUARD{"cost guard over budget?<br/>processor.py:525"}:::dec
  NOLLM["llm=None — degraded pipeline"]:::err
  PIPE["run_pipeline — see Diagram 4"]:::step
  REPLY{"reply produced?"}:::dec
  HALT["intentional silence / no-reply logged<br/>processor.py:534-545"]:::step
  DISC["ensure_disclaimer processor.py:551"]:::step
  SPLIT{"text over limit + not rich?"}:::dec
  FRAG["split into max 2 fragments"]:::step
  TX["TX: outbound messages + outbox rows<br/>+ state patch + mark_inbound_completed<br/>processor.py:565-667"]:::db
  POST["post-turn async: cache writeback<br/>+ summarizer + profile extract<br/>processor.py:689-699"]:::step

  DISP["dispatcher claim_due FOR UPDATE SKIP LOCKED"]:::step
  RENDER{"choose_render by capabilities<br/>dispatcher.py:101"}:::dec
  SEND["send via ChannelSender<br/>rich/carousel/cards/template/text"]:::edge
  SENT["mark_sent + link provider_msg_id TX<br/>dispatcher.py:215"]:::db
  FAIL["mark_failed → backoff → dead<br/>dispatcher.py:211"]:::err
  RP["render_path event if requested ≠ delivered<br/>dispatcher.py:69-98"]:::step

  MSG --> CAP
  CAP -- yes --> R413
  CAP -- no --> SIG
  SIG -- no --> R403
  SIG -- yes --> JSONV
  JSONV -- no --> R400
  JSONV -- yes --> DED1
  DED1 -- yes --> SKIP1
  DED1 -- no --> XADD --> RDOWN
  RDOWN -- yes --> R503
  RDOWN -- no --> ACK200
  XADD -.-> XREAD --> KIND
  KIND -- message --> TYPING --> DEB
  KIND -- order --> ORDERK
  KIND -- "status/callback" --> RESOLVE
  DEB -- "flush after 3s idle" --> RESOLVE
  RESOLVE --> STATUSK
  RESOLVE --> LOCK
  LOCK -- busy --> REQUEUE
  REQUEUE -- "cap exceeded" --> DROPQ
  LOCK -- ok --> LOADB --> CBK
  CBK -- yes --> CAROUSEL
  CBK -- no --> HT --> DED2
  DED2 -- "already done" --> SKIP2
  DED2 -- claimed --> CTX --> SEEDC --> GUARD
  GUARD -- yes --> NOLLM --> PIPE
  GUARD -- no --> PIPE
  PIPE --> REPLY
  REPLY -- no --> HALT
  REPLY -- yes --> DISC --> SPLIT
  SPLIT -- yes --> FRAG --> TX
  SPLIT -- no --> TX
  TX --> POST
  TX -.-> DISP --> RENDER --> SEND
  SEND -- ok --> SENT --> RP
  SEND -- error --> FAIL
```

Calea **sincronă** `/web/chat` diferă doar la capete: sesiune HMAC + rate-limit fail-closed + gard de buget (`src/web/app.py:197-240`) → `handle_turn(deliver=False)` — fără outbox, răspunsul HTTP e transportul (`src/web/app.py:241-252`, `src/worker/processor.py:560,613-621`).

---

## Diagram 4a — Pipeline Routing Workflow (stagiile 1-9 + fallback)

Ordinea stagiilor: `DEFAULT_STAGES`, `src/worker/runner.py:207-219`. Internele stagiului Agent → Diagram 4b.
*(Corectat după trace-ul invers: alias route-hit continuă spre agent, clarify are escaladare pe attempts, greeting rulează și după resume, handoff are ramura web→SALES, rate-limit are tăcere pe burst.)*

```mermaid
flowchart TD
  classDef stage fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef free fill:#a3e4d7,stroke:#148f77,color:#000
  classDef err fill:#f1948a,stroke:#922b21,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000

  IN["TurnContext"]:::stage

  subgraph G["1· Gates — deterministic, gates.py:439"]
    G1{"bot_active? blocked?<br/>handoff active? :442-453"}:::dec
    G2{"rate limited? :323"}:::dec
    G3{"moderation flagged?<br/>OpenAI moderation :348"}:::llm
    MODB["flag counter 24h — over threshold<br/>block_contact :305-318"]:::err
    G4{"risk pattern?<br/>human/legal :468"}:::dec
    G5{"image? :482"}:::dec
    VIS["Vision describe → search text<br/>fail-soft keeps caption :385-436"]:::llm
    GRD["input guardrails: clamp + PII mask<br/>+ injection screen :487"]:::stage
  end

  HALT["HALT — intentional silence"]:::err
  THR["throttle message, once :340-342"]:::out
  NEU["neutral reply"]:::out
  ESC["request_human + transition msg<br/>gates.py:473-476"]:::out

  LANG["2· language_stage — RO/HU/EN<br/>language.py:27"]:::stage

  subgraph CL3["3· clarify_resume — clarify.py:28"]
    CLR{"pending_question in state?"}:::dec
    CONS["fill constraints slot + asked_intents<br/>clarify.py:41-52"]:::stage
    ATT{"attempts over max?<br/>clarify.py:59"}:::dec
    RESUME["route = resume_route<br/>clarify.py:69-73"]:::stage
    ESCR["route = HANDOFF if field=intent<br/>else SALES — clarify.py:61-62"]:::stage
  end

  GREET{"4· pure greeting?<br/>greeting.py:184 — does NOT check route"}:::dec
  WELCOME["deterministic welcome, no LLM"]:::free
  ALIAS{"5· exact alias match?<br/>alias.py:46"}:::dec
  AFAQ["serve FAQ answer<br/>alias.py:64-72"]:::free
  AROUTE["set ctx.route ONLY — no reply<br/>alias.py:73-82"]:::stage
  SKIPS["cache / FAQ / triage SKIP — route set<br/>cache.py:93 · faq.py:49 · triage.py:214"]:::stage
  CACHE{"6· semantic cache hit?<br/>cache.py:89 — L1 exact → L2 cosine<br/>realtime/contextual bypass :100<br/>dynamic hit → price-check, stale evict = miss :46-78"}:::dec
  CHIT["serve cached answer, no LLM"]:::free
  FAQ{"7· FAQ embed match?<br/>faq.py:45 — tau high / policy tau :64-77<br/>locale fallback :88-101"}:::dec
  FHIT["serve FAQ answer"]:::free

  TRI["8· triage — GPT-5.4-nano JSON<br/>triage.py:212"]:::llm
  TVAL{"valid JSON + category real?<br/>triage.py:247-255"}:::dec
  TGUARD{"factual bait on simple?<br/>triage.py:261"}:::dec
  TCONF{"confidence low?<br/>triage.py:272"}:::dec
  ROUTE{"route?"}:::dec
  SIMPLE["reply by nano + closure chips<br/>triage.py:298-308"]:::out
  CLARIFY["clarify + suggestions + persist slot<br/>triage.py:309-319"]:::out

  HOFF{"9· handoff — channel has operator?<br/>handoff.py:39"}:::dec
  HESC["request_human + notify_operator<br/>+ confirm msg — handoff.py:44-49"]:::out
  HSUP["route rewritten to SALES<br/>handoff.py:40-42"]:::stage

  AGENT["10· agent_stage — Diagram 4b<br/>agent.py:957"]:::llm
  FB["11· fallback_stage — clarify question<br/>RO-only — runner.py:169"]:::out
  SEND["Reply → Sender TX"]:::out

  IN --> G1
  G1 -- yes --> HALT
  G1 -- no --> G2
  G2 -- "first over cap" --> THR
  G2 -- "burst continues :343" --> HALT
  G2 -- no --> G3
  G3 -- yes --> MODB --> NEU
  G3 -- no --> G4
  G4 -- "yes + operator channel" --> ESC
  G4 -- "yes, no operator — suppressed :470" --> G5
  G4 -- no --> G5
  G5 -- yes --> VIS --> GRD
  G5 -- no --> GRD
  GRD --> LANG --> CLR
  CLR -- yes --> CONS --> ATT
  ATT -- yes --> ESCR --> GREET
  ATT -- no --> RESUME --> GREET
  CLR -- no --> GREET
  GREET -- yes --> WELCOME
  GREET -- no --> ALIAS
  ALIAS -- "faq target" --> AFAQ
  ALIAS -- "route/product/category" --> AROUTE --> SKIPS --> ROUTE
  ALIAS -- miss --> CACHE
  CACHE -- hit --> CHIT
  CACHE -- miss --> FAQ
  FAQ -- hit --> FHIT
  FAQ -- miss --> TRI --> TVAL
  TVAL -- "invalid → route None" --> FB
  TVAL -- ok --> TGUARD
  TGUARD -- "yes → sales" --> ROUTE
  TGUARD -- no --> TCONF
  TCONF -- "yes → clarify" --> ROUTE
  TCONF -- no --> ROUTE
  ROUTE -- "simple + reply" --> SIMPLE
  ROUTE -- "simple, no reply text :298" --> FB
  ROUTE -- clarify --> CLARIFY
  ROUTE -- handoff --> HOFF
  HOFF -- yes --> HESC --> SEND
  HOFF -- "no — web" --> HSUP --> AGENT
  ROUTE -- "sales / order" --> AGENT
  AGENT -- "reply composed" --> SEND
  AGENT -- "no reply — LLM fail :1148" --> FB
  FB --> SEND
  SIMPLE --> SEND
  CLARIFY --> SEND
  WELCOME --> SEND
  CHIT --> SEND
  FHIT --> SEND
  AFAQ --> SEND
```

---

## Diagram 4b — Agent Stage Internals (agent.py:957)

Intențiile deterministe PRE-loop scurtcircuitează bucla LLM (cost zero); compunerea POST-loop e cod determinist peste `ctx.retrieval` — modelul nu are ultimul cuvânt pe cifre/linkuri.

```mermaid
flowchart TD
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef free fill:#a3e4d7,stroke:#148f77,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000

  IN2["route = sales / order"]:::step

  subgraph PRE["PRE-loop deterministic intents — no LLM"]
    LNK{"link request on displayed?<br/>:984"}:::dec
    SLNK["serve product_url from state<br/>+ Offer — :894"]:::free
    CMPI{"compare on displayed set?<br/>:1001"}:::dec
    HCMP{"2+ valid products fetched?<br/>:933"}:::dec
    CTAB["deterministic comparison table"]:::free
    SM{"show-more on active session?<br/>:1019"}:::dec
  end

  MERGEC["merge_constraints stack — sales only<br/>:1098-1112"]:::step
  PAGE2["continue_search_session :1135"]:::free
  PGOK{"page has products?"}:::dec
  NOMORE["no-more message :1139"]:::out

  subgraph LOOPG["LLM tool loop"]
    PROMPT["build_agent_system from DB<br/>prompt_builder.py:279"]:::step
    LOOP["run_tool_loop max 3 steps<br/>llm.py:227"]:::llm
    EXEC["execute → run_tool + bookkeeping :1041-1086<br/>10 tools: search·details·compare·cart·checkout<br/>reorder·back_in_stock·faq·check_order·request_human"]:::step
  end

  subgraph POST["POST-loop deterministic composition"]
    LOGIN{"check_order hit login wall?<br/>:1155"}:::dec
    LWALL["login-required msg"]:::out
    CKF{"purchase intent + cart lines,<br/>no link yet? :1174"}:::dec
    CKEXEC["code calls checkout_link itself<br/>:1195"]:::step
    XS{"cart_add ok + no link? :1204"}:::dec
    XSELL["cross-sell complementary cards<br/>:1213-1236"]:::out
    ATQ{"superlative on displayed set?<br/>:1246"}:::dec
    HYD1["rehydrate displayed set :1255"]:::step
    CHP{"cheaper intent? :1265"}:::dec
    CHS["search_cheaper_than :1276"]:::step
    CH0{"anything cheaper?"}:::dec
    CHMSG["already-cheapest msg :1283"]:::out
    RHD{"no products + displayed +<br/>text ungrounded? :1291"}:::dec
    HYD2["get_products_by_ids :1300"]:::step
    CMPD{"model called compare_products?<br/>:1314"}:::dec
    CTAB2["deterministic table :1315-1324"]:::out
  end

  subgraph FIN["Finalize + validator"]
    PRD{"products retrieved?"}:::dec
    RICHC["_finalize_rich structured JSON :1330<br/>+ decision axes NX-139 input :704-720<br/>grounding chain → Diagram 4c"]:::llm
    ROK{"rich has items?"}:::dec
    RICHOUT["rich reply: cards + chips<br/>+ checkout Offer :1340-1348"]:::out
    VALID{"_valid: prices / links / bare numbers /<br/>claims / medical — agent.py:472"}:::dec
    RETRY["1 retry, allowed prices only :587"]:::llm
    V2{"valid now?"}:::dec
    DETR["deterministic reply :523"]:::free
    PROSE["prose reply + cards :1379"]:::out
    ORD{"order route?"}:::dec
    GRND["_finalize_grounded on order views<br/>:1389"]:::llm
    TXTOK{"text valid without products?<br/>:1398"}:::dec
    NORES["safe no-result / login msg<br/>:1405 · :1406-1411"]:::out
  end

  IN2 --> LNK
  LNK -- yes --> SLNK
  LNK -- no --> CMPI
  CMPI -- yes --> HCMP
  HCMP -- yes --> CTAB
  HCMP -- "no → fall through :1009" --> MERGEC
  CMPI -- no --> MERGEC
  MERGEC --> SM
  SM -- yes --> PAGE2 --> PGOK
  PGOK -- no --> NOMORE
  PGOK -- "yes, skip loop" --> LOGIN
  SM -- no --> PROMPT --> LOOP <--> EXEC
  LOOP --> LOGIN
  LOGIN -- yes --> LWALL
  LOGIN -- no --> CKF
  CKF -- yes --> CKEXEC --> XS
  CKF -- no --> XS
  XS -- yes --> XSELL
  XSELL -- "no complement / rich fail → continue :1238" --> ATQ
  XS -- no --> ATQ
  ATQ -- yes --> HYD1 --> CMPD
  ATQ -- no --> CHP
  CHP -- yes --> CHS --> CH0
  CH0 -- no --> CHMSG
  CH0 -- yes --> CMPD
  CHP -- no --> RHD
  RHD -- yes --> HYD2 --> CMPD
  RHD -- no --> CMPD
  CMPD -- yes --> CTAB2
  CMPD -- no --> PRD
  PRD -- "yes + sales" --> RICHC --> ROK
  ROK -- yes --> RICHOUT
  ROK -- "no → prose downgrade :1350" --> VALID
  PRD -- "yes + order" --> VALID
  VALID -- ok --> PROSE
  VALID -- invalid --> RETRY --> V2
  V2 -- yes --> PROSE
  V2 -- no --> DETR
  PRD -- "no, but final text" --> ORD
  ORD -- yes --> GRND
  ORD -- no --> TXTOK
  TXTOK -- yes --> PROSE
  TXTOK -- no --> NORES
  PRD -- "no, no text" --> NORES
```

Memoria (istoric/profil/state/rezumat) intră în prompturi prin `conversation_transcript` + `context_blocks` (`src/worker/context.py:23`; folosite la `triage.py:223-226` și `agent.py:1088-1091`). System-promptul agentului e GENERAT din DB (`src/agent/prompt_builder.py:279`, principiul 9).

---

## Diagram 4c — Rich Composition & Grounding (compose.py — adăugată la auditul adversarial)

Echivalentul validatorului pentru calea BOGATĂ: modelul emite DOAR cuvinte + referințe `product_id`; codul hidratează faptele. Garanția anti-halucinație a căii care servește majoritatea recomandărilor.

```mermaid
flowchart TD
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef free fill:#a3e4d7,stroke:#148f77,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef err fill:#f1948a,stroke:#922b21,color:#000

  AXES["decision_axes + spec_numbers — NX-139<br/>axe REALE pe care variază setul<br/>compose.py:661 · :704 — gated :708"]:::step
  BNDL["_rich_bundle: facts per product<br/>facets from DomainPack — agent.py:724"]:::step
  JIN["structured JSON from mini<br/>complete_schema + _RICH_SCHEMA — agent.py:727"]:::llm

  ASM["assemble — hydrate by product_id refs<br/>compose.py:329"]:::step
  MEM{"item references a REAL<br/>retrieved product?"}:::dec
  DROPI["item dropped — membership grounding"]:::err

  SCRB["scrub field-by-field: intro / fit / education<br/>digits · % · unverifiable claims → field=None<br/>scrub_prose :131 · scrub_intro :160 · scrub_education :268"]:::step
  MED{"medical claim in field?<br/>_unsafe_medical :124"}:::dec
  DROPF["field DROPPED — P0 safety<br/>liability, not style"]:::err

  BDG["badge from REAL signals only<br/>deal > top, DomainPack thresholds<br/>badges.py:36-78 — gated card_badges_enabled"]:::free
  PICK["_select_pick deterministic :293<br/>suppressed when off-category :95"]:::step
  STK["drop unfounded stock line :260"]:::step
  CHIPS["suggestion chips :232<br/>client voice → NO scrub"]:::step
  FLAT["flatten / flatten_framing :466 · :499<br/>floor text for non-rich channels"]:::step
  OUTR["RichReply → set_rich_reply<br/>models.py:540"]:::out

  CMPB["build_comparison :722<br/>facet_summary :646 + axes + spec numbers<br/>deterministic lead :590 — ZERO LLM in cells"]:::free
  OUTC["Comparison → set_comparison_reply<br/>models.py:556"]:::out

  AXES --> JIN
  BNDL --> JIN
  JIN --> ASM --> MEM
  MEM -- no --> DROPI
  MEM -- yes --> SCRB --> MED
  MED -- yes --> DROPF --> BDG
  MED -- no --> BDG
  BDG --> PICK --> STK --> CHIPS --> FLAT --> OUTR
  CMPB --> OUTC
```

Cine o folosește: `_finalize_rich` (agent.py:688-731, recomandare + cross-sell) și randorul web (`flatten_framing` la `channels/web/render.py:150`). Comparația (CMPB) e apelată din ambele căi de compare (agent.py:933, :1315).

---

## Diagram 5 — Product Search Workflow

```mermaid
flowchart TD
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000

  Q["search_products args from model<br/>catalog_tools.py:383"]:::step
  CONC["map concerns via DomainPack<br/>catalog_tools.py:399"]:::step
  INH{"active session +<br/>no new category/concerns?"}:::dec
  INHERIT["inherit session filters<br/>catalog_tools.py:417-427"]:::step
  FP{"same filters fingerprint<br/>+ stored pool?"}:::dec
  PAGE["continue_search_session<br/>next page, zero LLM cost<br/>catalog_tools.py:343"]:::step

  LADDER["build relax ladder<br/>catalog_tools.py:439"]:::step
  EMB{"LLM + embeddings available?"}:::dec
  VEC["embed query — ONE call<br/>catalog_tools.py:450"]:::llm
  LEX["lexical search FTS + pg_trgm<br/>catalog_tools.py:468"]:::db
  SEM["semantic search pgvector HNSW<br/>catalog_tools.py:486"]:::db
  FUSE["fuse_candidates RRF + blended rank<br/>catalog_tools.py:507"]:::step
  ANY{"results at this step?"}:::dec
  RELAX["relax next filter in ladder"]:::step
  EXH{"ladder exhausted?"}:::dec
  EMPTY["empty ToolResult → AGENT composes<br/>no-result msg — agent.py:1411"]:::out

  DIV{"sort=relevance + not named product?"}:::dec
  DIVER["diversify pool: price tertiles + brands<br/>catalog_tools.py:518"]:::step
  DEDUP["dedupe vs displayed_products, cap 6"]:::step
  SESS["store session pool + fingerprint in state"]:::db
  RES["ToolResult: full products → validator<br/>compact llm_view max 6x8 → model"]:::out

  Q --> CONC --> INH
  INH -- yes --> INHERIT --> FP
  INH -- no --> FP
  FP -- yes --> PAGE --> RES
  FP -- no --> LADDER --> EMB
  EMB -- yes --> VEC --> LEX
  EMB -- no --> LEX
  LEX --> SEM --> FUSE --> ANY
  ANY -- no --> EXH
  EXH -- no --> RELAX --> LEX
  EXH -- yes --> EMPTY
  ANY -- yes --> DIV
  DIV -- yes --> DIVER --> DEDUP
  DIV -- no --> DEDUP
  DEDUP --> SESS --> RES
```

Degradare: `embed` picat → `query_vec=None` → lexical-only (`catalog_tools.py:447-454`); semantic picat în tur → rămâne lexical (`catalog_tools.py:501-502`).

---

## Diagram 6 — Conversation Memory Workflow

```mermaid
flowchart TD
  classDef read fill:#aed6f1,stroke:#2874a6,color:#000
  classDef write fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000

  subgraph Load["TURN START — load memory processor.py:490-510"]
    H["history: last 8 messages<br/>get_recent_messages"]:::read
    ST["state jsonb max 8KB<br/>displayed_products refs + constraints + cart"]:::read
    SUM["rolling summary<br/>get_summary_for_context"]:::read
    PROF["contact profile + lead_score"]:::read
  end

  subgraph Use["IN PROMPTS — context.py"]
    TRANS["conversation_transcript max 6 turns / 1200ch<br/>context.py:23"]:::read
    BLOCKS["profile block :40 + state block :59"]:::read
  end

  subgraph Mutate["DURING TURN"]
    AG["agent mutates in-place:<br/>search_constraints, active_search"]:::write
    TP["tools request state_patch<br/>cart_add → ctx.state_patch<br/>tools/base.py:38-40"]:::write
    LNG["language_stage persists conv.locale<br/>direct DB write, best-effort — language.py:44"]:::db
  end

  subgraph Persist["SENDER TX — processor.py:565-598, in code order"]
    DP["displayed_products ← reply.products<br/>:567-570"]:::write
    PQ["pending_question set or cleared :573"]:::write
    MERGE["canonical merge: constraints +<br/>asked_intents + search_constraints :580-587"]:::write
    ASR["active_search reset when reply<br/>has no products :592-593"]:::write
    PATCH["state_patch wins LAST :597-598"]:::write
    OPT["optimistic lock state_version<br/>patch_conversation_state :656"]:::db
  end

  subgraph PostTurn["POST-TURN async, best-effort — processor.py:689-699"]
    CW{"cacheable reply?"}:::dec
    CWB["semantic_cache upsert<br/>static days / dynamic minutes + price snapshot<br/>processor.py:116"]:::db
    SQ{"messages over threshold?"}:::dec
    SUMGEN["generate_summary NANO<br/>summarizer.py:66 → conversation_summaries<br/>honest watermark"]:::llm
    PE{"route ran + not shadow?"}:::dec
    PEX["extract_profile NANO → whitelist patch<br/>+ deterministic lead_score<br/>processor.py:244"]:::llm
  end

  H --> TRANS
  PROF --> BLOCKS
  ST --> BLOCKS
  SUM --> TRANS
  TRANS --> AG
  BLOCKS --> AG
  AG --> MERGE
  TP --> PATCH
  DP --> PQ --> MERGE --> ASR --> PATCH --> OPT
  OPT --> CW
  CW -- yes --> CWB
  OPT --> SQ
  SQ -- yes --> SUMGEN
  OPT --> PE
  PE -- yes --> PEX
```

---

## Diagram 7 — Database Workflow

```mermaid
flowchart TD
  classDef pool fill:#aed6f1,stroke:#2874a6,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef err fill:#f1948a,stroke:#922b21,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000

  subgraph Pools["Two pools — connection.py"]
    ADMIN["admin_pool — privileged<br/>get_pool :76 — control plane ONLY"]:::pool
    BOT["bot_pool — role bot_runtime, NO bypassrls<br/>get_bot_pool :207"]:::pool
  end

  subgraph Tenant["tenant_conn checkout — connection.py:261"]
    SETBIZ["set_config app.business_id"]:::step
    ISO{"isolation assert:<br/>role + GUC match? :187"}:::dec
    ISOERR["IsolationError — log CRITICAL<br/>reset GUC, raise"]:::err
    RLS["RLS policies filter every query<br/>wrong query → 0 rows, not other tenant"]:::db
    RESET["reset GUC on release :294"]:::step
  end

  subgraph Reads["Hot reads — bot_pool"]
    RCAT["products + product_embeddings HNSW<br/>faqs · semantic_cache · intent_aliases"]:::db
    RCONV["contacts · conversations · messages partitioned"]:::db
  end

  subgraph Writes["Writes — bot_pool"]
    WMSG["messages inbound/outbound"]:::db
    WOUT["outbox — idempotency_key turn:i"]:::db
    WSTATE["conversations.state + state_version"]:::db
    WDED["inbound_dedupe claim → completed<br/>claim-or-resume NX-86"]:::db
    WANA["analytics_events INSERT-only, best-effort"]:::db
    WCACHE["semantic_cache / summaries — savepoints"]:::db
    WSTS["message_status_events → messages.status"]:::db
    WLOC["conversations.locale — language stage :44"]:::db
  end

  CTRL["resolve_channel: provider_account_id → business_id<br/>the ONLY pre-tenant lookup — channels.py"]:::step
  JOBS["admin jobs: rollup·embed·cleanup·lifecycle"]:::step
  SSC["in-process SessionSecretCache LRU+TTL<br/>+ NEGATIVE cache anti-flood<br/>web/session.py:67-101"]:::step

  SSC -- miss --> ADMIN
  ADMIN --> CTRL
  ADMIN --> JOBS
  BOT --> SETBIZ --> ISO
  ISO -- fail --> ISOERR
  ISO -- ok --> RLS --> RESET
  RLS --> RCAT
  RLS --> RCONV
  RLS --> WMSG
  RLS --> WOUT
  RLS --> WSTATE
  RLS --> WDED
  RLS --> WANA
  RLS --> WCACHE
  RLS --> WSTS
  RLS --> WLOC
```

Tranzacția Sender (mesaje + outbox + state + dedupe-complete, atomic): `src/worker/processor.py:565-667`. Scrierile best-effort rulează în savepoint propriu (`processor.py:159-161, 218, 278`).

---

## Diagram 8 — External Services Workflow

```mermaid
flowchart LR
  classDef sys fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef ext fill:#fad7a0,stroke:#af601a,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef q fill:#f5b7b1,stroke:#922b21,color:#000

  subgraph OpenAI["OpenAI — the ONLY LLM seam, agent/llm.py"]
    NANO["chat nano: triage · summarizer · profile<br/>classify_json :168"]:::llm
    MINI["chat mini: agent tool loop :227<br/>compose :211 · schema :188"]:::llm
    EMBD["embeddings: cache/FAQ/search/products"]:::llm
    MOD["moderation — free, gates"]:::llm
    VISN["vision: photo → search query :109"]:::llm
    RTY["bounded retry _with_retry :45<br/>respects Retry-After"]:::sys
  end

  subgraph Meta["Meta Cloud API"]
    MIN["webhook inbound signed<br/>X-Hub-Signature-256"]:::ext
    MOUT["MetaClient send text/template/typing<br/>meta_client.py:28"]:::ext
    MMED["media download GET — fetch_media<br/>meta_client.py:137 · media.py:34-43"]:::ext
  end

  subgraph Operator["Operator (handoff)"]
    OPW["notify webhook POST, best-effort, no PII<br/>handoff_tools.py:28-47"]:::ext
  end

  subgraph TG["Telegram Bot API"]
    TIN["long polling getUpdates<br/>poller.py:70"]:::ext
    TOUT["TelegramClient send + edit carousel<br/>telegram/client.py:62"]:::ext
  end

  subgraph Web["Web widget — FE repo separat"]
    WIN["POST /web/messages · /chat HMAC session"]:::ext
    WOUT2["SSE /web/stream + backlog replay<br/>web/app.py:279"]:::ext
  end

  subgraph Shop["Shop platform"]
    OIN["orders webhook HMAC<br/>webhook/app.py:127 → attribution"]:::ext
  end

  subgraph RedisSvc["Redis"]
    RS["streams · locks · dedupe L1<br/>cost counters · SSE pub/sub · TG offset"]:::q
  end

  SYS["Nativx pipeline"]:::sys

  MIN --> SYS
  TIN --> SYS
  WIN --> SYS
  OIN --> SYS
  MMED --> SYS
  SYS --> OPW
  SYS --> MOUT
  SYS --> TOUT
  SYS --> WOUT2
  SYS <--> RS
  SYS <--> NANO
  SYS <--> MINI
  SYS <--> EMBD
  SYS <--> MOD
  SYS <--> VISN
  NANO -.-> RTY
  MINI -.-> RTY
```

> %% UNVERIFIED: repo-ul frontend al widget-ului („Sales MVP Frontend") e extern acestui codebase;
> contractul JSON e documentat în `docs/FRONTEND-CONTRACT-IZI.md`, dar implementarea FE nu poate fi
> verificată de aici.

---

## Diagram 9 — Error Handling Workflow

```mermaid
flowchart TD
  classDef err fill:#f1948a,stroke:#922b21,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef ok fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef deg fill:#fad7a0,stroke:#af601a,color:#000

  subgraph LLMErr["LLM failures"]
    L1["API error / 429"]:::err
    L2{"bounded retry ok?<br/>llm.py:45"}:::dec
    L3["triage fail → route None<br/>triage.py:247-251"]:::deg
    L4["agent loop fail → return<br/>agent.py:1148"]:::deg
    L5["fallback_stage: clarify question<br/>NEVER silence — runner.py:169"]:::ok
  end

  subgraph ValErr["Validator failures — agent.py:560"]
    V1{"reply valid? prices/links/claims"}:::dec
    V2["1 retry with allowed prices :587"]:::deg
    V3{"valid now?"}:::dec
    V4["deterministic reply from DB products :595"]:::ok
  end

  subgraph CostErr["Cost guard — processor.py:308"]
    C1{"daily cap business/contact hit?"}:::dec
    C2["llm=None: gates + cache still work<br/>LLM stages skip"]:::deg
  end

  subgraph InfraErr["Infra failures"]
    I1["Redis down at webhook → 503, Meta retries<br/>webhook/app.py:119"]:::err
    I2["cache/FAQ error → miss, turn continues<br/>cache.py:146"]:::deg
    I3["analytics fail → log only<br/>processor.py:112"]:::deg
    I4["conv lock busy → requeue capped<br/>consumer.py:90"]:::deg
    I5["consumer crash mid-turn → un-ACKed<br/>reaper XAUTOCLAIM re-claims :234"]:::ok
    I6["processing exception → ACK + log<br/>consumer.py:228 — WARNING: turn LOST"]:::err
    I7["requeue cap exceeded → event DROPPED<br/>consumer.py:95-96 — WARNING: turn LOST"]:::err
    I8["inbound stream maxlen ~100k trim →<br/>oldest LOST under backlog — redis_bus.py:68-73"]:::err
  end

  subgraph BgErr["Background process failures"]
    B1["proactive job crash → mark failed<br/>+ event, clean savepoint<br/>proactive/scheduler.py:148-162"]:::deg
    B2["maintenance job crash → log + skip,<br/>retry next interval — jobs/scheduler.py:101-108"]:::deg
    B3["poller getUpdates fail → log +<br/>sleep 3s + retry — poller.py:113-115"]:::deg
  end

  subgraph SendErr["Delivery failures — dispatcher"]
    S1{"send failed?"}:::dec
    S2["mark_failed → backoff retry :211"]:::deg
    S3{"attempts exhausted?"}:::dec
    S4["status dead — visible, not silent"]:::err
    S5["dispatcher dead between claim/mark →<br/>visibility timeout, row redeemed"]:::ok
  end

  subgraph Human["Human handoff"]
    H1["risk pattern / route handoff"]:::dec
    H2["request_human: set handoff_until + notify<br/>gates.py:473"]:::ok
    H3["next turns: gates → intentional silence"]:::deg
  end

  L1 --> L2
  L2 -- no --> L3 --> L5
  L2 -- no --> L4 --> L5
  V1 -- invalid --> V2 --> V3
  V3 -- no --> V4
  C1 -- yes --> C2
  S1 -- yes --> S2 --> S3
  S3 -- yes --> S4
  H1 --> H2 --> H3
```

---

## Diagram 10 — Proactive Lifecycle (adăugată la auditul adversarial)

Cele mai reglementate decizii din sistem (consent + fereastra 24h Meta) — 100% cod determinist, zero LLM (`templates.py`).

```mermaid
flowchart TD
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef err fill:#f1948a,stroke:#922b21,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef bg fill:#a3e4d7,stroke:#148f77,color:#000

  subgraph Feed["FEED — jobs.scheduler runs initiators"]
    SW1["sweep_abandoned_cart<br/>initiators.py:136-143"]:::bg
    SW2["sweep_back_in_stock<br/>initiators.py:145-146"]:::bg
    SEAM["event seams — DEFINED, NEVER CALLED:<br/>schedule_awb_update :160 · schedule_follow_up :190<br/>TODO: orders webhook should call them"]:::err
  end

  PJ[("proactive_jobs")]:::db

  subgraph Engine["ENGINE — proactive.scheduler loop :181"]
    CTL["control plane: tenants with due jobs<br/>scheduler.py:170-171"]:::step
    CLAIM["claim_due_jobs FOR UPDATE SKIP LOCKED<br/>:141 — savepoint per job :146"]:::step
    ROUTEP["resolve conversation + channel +<br/>recipient identity :65-75"]:::step
    RERR["no route → ProactiveRouteError<br/>→ mark failed :49-51"]:::err
    BUILD["build_message_spec — builders.py<br/>text per kind :77"]:::step
    CANQ{"spec.cancel?<br/>:78"}:::dec
    CANC["mark cancelled :79-81"]:::out
  end

  subgraph Gate["GATE — decide_proactive, templates.py (NX-71)"]
    CONS{"consent by kind?<br/>marketing: abandoned_cart/follow_up<br/>transactional: awb/back_in_stock — :39"}:::dec
    SK1["skipped_no_optin"]:::out
    WIN{"in 24h window?<br/>is_in_24h_window"}:::dec
    TPL{"approved template<br/>in locale? P11"}:::dec
    SK2["skipped_no_window"]:::out
    FREE["mode=free → payload type=text<br/>scheduler.py:117-119"]:::step
    TMPL["mode=template → name+language+params<br/>scheduler.py:106-116"]:::step
  end

  OB[("outbox — idempotency proactive:job_id<br/>scheduler.py:120-122")]:::db
  SENTP["mark sent + proactive_enqueued event<br/>:123-129"]:::out
  FAILP["job exception → mark failed +<br/>proactive_failed event, clean savepoint<br/>:148-162"]:::err
  DISPP["dispatcher — TEMPLATE capability?<br/>WhatsApp: send_template · else degrade to text<br/>dispatcher.py:117-119"]:::step

  SW1 --> PJ
  SW2 --> PJ
  SEAM -.-> PJ
  PJ --> CTL --> CLAIM --> ROUTEP
  ROUTEP -- fail --> RERR
  ROUTEP -- ok --> BUILD --> CANQ
  CANQ -- yes --> CANC
  CANQ -- no --> CONS
  CONS -- no --> SK1
  CONS -- yes --> WIN
  WIN -- yes --> FREE
  WIN -- no --> TPL
  TPL -- no --> SK2
  TPL -- yes --> TMPL
  FREE --> OB
  TMPL --> OB
  OB --> SENTP
  CLAIM -. "exception" .-> FAILP
  OB --> DISPP
```

---

# Audit arhitectural (evidence-based)

## Discrepanțe documentație ↔ implementare (implementarea câștigă)

1. **CLAUDE.md e în urmă:** secțiunea „Structura proiectului" spune `stages/ … TODO: gates, free_layers; echo=fallback` — dar `gates.py`, `greeting.py`, `alias.py`, `cache.py`, `faq.py`, `clarify.py`, `handoff.py`, `language.py` există și sunt LIVE în `src/worker/runner.py:207-219`.
2. **Docstring stale în runner:** `src/worker/runner.py:8-10` descrie „un singur stagiu real (`echo_stage`)" — `echo_stage` nu există în `DEFAULT_STAGES`; pipeline-ul are 11 stagii.
3. **„Validatorul (stagiul 8)" din CLAUDE.md nu e stagiu separat:** e implementat ca funcții interne ale stagiului Agent (`src/worker/stages/agent.py:472-502`, `560-595`) — comportamentul e cel documentat, structura diferă.
4. **arch_explorer e ușor stale pe branch-ul curent:** raportează `agent_stage` la linia 858 și `triage_stage` la 179; în cod sunt la `agent.py:957` și `triage.py:212`. Necesită re-rulare `arch_explorer/analyze.py`.
5. **`webhook/status.py` NU există** — CLAUDE.md îl listează ca LIVE; statusurile sunt parsate în `webhook/meta.py:104` (`parse_statuses`) și scrise de worker (`consumer.py:137-146`).
6. **STT/Whisper NU e implementat** — CLAUDE.md promite „vocale → STT (Whisper)"; `audio` e doar un tip de media parsat cu caption drept body (`webhook/meta.py:27,36-39`), ne-rutat spre vreo transcriere (gates rutează DOAR `image`, `gates.py:482`).
7. **Tool-ul `delivery_eta` NU există** — CLAUDE.md îl listează; registry-ul real are 10 tool-uri (grep `@register` în `src/tools/`), fără `delivery_eta`.

## Puncte forte

- **Pipeline liniar cu un singur owner per câmp** — runner generic; observabilitatea (latency + tokens per stagiu + buget per tur) e în runner, nu în stagii (`src/worker/runner.py:47-105`).
- **Un singur punct de ieșire, tranzacțional** — reply + outbox + state + dedupe-complete într-o singură TX (`src/worker/processor.py:565-667`); dispatcher cu visibility-timeout self-healing (`src/worker/dispatcher.py:8-13`).
- **Validatorul e apărarea load-bearing anti-halucinație și anti-prompt-injection** — orice preț/link/produs trebuie să existe în `ctx.retrieval` (`src/worker/stages/agent.py:486-489`); degradarea finală e un reply determinist din DB (`agent.py:523`).
- **Izolare multi-tenant în straturi** — rol LOGIN fără bypassrls + GUC per checkout + assert de izolare la fiecare checkout (`src/db/connection.py:187-204, 274-287`); boot-gate pe migrări (`src/worker/consumer.py:315-321`).
- **Fiabilitate pe coadă** — dedupe 2 straturi (Redis + DB claim-or-resume), ACK-after-flush (`src/worker/debounce.py:11-15`), reaper XAUTOCLAIM (`src/worker/consumer.py:234-272`).
- **Cost governance pe 3 niveluri** — business/zi, contact/zi, vizitator web, reseed din `usage_daily`, enforcement post-increment fără TOCTOU (`src/worker/processor.py:308-379`).

## Puncte slabe / riscuri

1. **⚠ Cel mai serios: excepție de procesare = tur pierdut tăcut.** La orice excepție în `process_event`, consumer-ul face ACK și doar loghează (`src/worker/consumer.py:228-230`; la fel reaper-ul `:267-269`). Meta a primit deja 200 → nu re-trimite. Clientul nu primește NIMIC — încălcare a principiului 6 exact pe calea de eroare pe care principiul o vizează. **A doua cale de pierdere (găsită la trace-ul invers):** lock ocupat persistent → după `conv_lock_max_requeues` evenimentul e DROPAT cu un simplu log (`consumer.py:95-96`). → **NX-140**
2. **`agent.py` e un god-module** — 1200+ linii; `agent_stage` (`:957+`) amestecă 3 intenții deterministe, bucla LLM, login-wall, checkout-fallback, cross-sell; validatorul + 3 căi de finalize în același fișier.
3. **Cuplaj maxim în processor** — `handle_turn` are fan-out 45 (cel mai mare din sistem, `arch_explorer/GRAPH_REPORT.md`) și ~300 linii: orchestrare + politică de state-merge + sender + post-tur.
4. **Ciclu de import gestionat manual** — stagiile importă `PipelineDeps` din runner sub `TYPE_CHECKING` (`src/worker/stages/gates.py:37-38`); runner-ul importă stagiile la sfârșitul fișierului (`src/worker/runner.py:189-199`).
5. **Conexiune DB ținută pe durata apelurilor LLM** — pipeline-ul rulează pe `conn` din `bot_pool` (max_size=10, `src/db/connection.py:224-229`) în timp ce agentul face 1-4 apeluri LLM (`src/worker/processor.py:528`); la fel `/web/chat` (`src/web/app.py:223-243`). Plafon ~10 tururi concurente/proces; pooler Supabase capat la ~15 sesiuni. **Bottleneck-ul #1 de scalare.** → **NX-141**
6. **Dispatcher secvențial + poll de 2s** — rând cu rând, tenant cu tenant (`src/worker/dispatcher.py:233-241`), sleep 2s la idle (`:245-251`). Un tenant lent întârzie toți ceilalți; +0-2s latență pe calea async.
7. **Fallback-ul final e doar în română** — `src/worker/runner.py:173-176`, deși pipeline-ul e RO/HU/EN și triage are `_CLARIFY_FALLBACK` per-locale (`src/worker/stages/triage.py:313`). Încalcă P11.
8. **SSL fără verificare pe orice win32** — `src/db/connection.py:56-73` dezactivează verificarea certificatului condiționat de platformă, nu de env. Prod e Linux → risc latent.
9. **Cuplaj src→scripts la runtime** — `src/worker/consumer.py:315` importă `scripts.migrate`; a produs deja incidentul Dockerfile (imagine fără `scripts/` → crash-loop, PR #132).
10. **Duplicare mică de asamblare context** — `history_block`/`context_block` construite identic în triage (`triage.py:223-226`) și agent (`agent.py:1088-1091`).

**Circular dependencies:** doar ciclul runner↔stages gestionat prin late-import (pct. 4). **Dead code (corectat la auditul adversarial):** `estimate_turn_cost` (`src/worker/limits.py:186`) — definit, referit doar într-un comentariu (`processor.py:355`), înlocuit de costul real NX-125 → de șters. **Unreachable seams (intenționate, TODO):** `schedule_awb_update` (`initiators.py:160-187`) și `schedule_follow_up` (`initiators.py:190`) — definite, niciun apelant; webhook-ul de comenzi ar trebui să le cheme la expediere.

---

# Refactoring — plan cu evidență

| #  | Problemă                                                                      | Evidență                                  | Soluție                                                                                      | Impact                                                                                  | Card             |
| -- | ------------------------------------------------------------------------------ | ------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ---------------- |
| R1 | Excepție de procesare → ACK + drop, client fără răspuns                   | `consumer.py:228-230`                     | Re-queue cu contor; la epuizare → fallback reply în outbox + event`turn_failed`, apoi ACK | Erorile tranzitorii se auto-vindecă; cele permanente produc mesaj + semnal, nu tăcere | **NX-140** |
| R2 | Validatorul (apărarea critică) trăiește ca funcții private în god-module | `agent.py:472-502, 560-688`               | Mutare pură în`src/worker/validator.py`                                                   | Testare izolată; auditabilitate de securitate pe un fișier                            | —               |
| R3 | Intențiile deterministe împletite în`agent_stage`                         | `agent.py:984-1027, 1174-1216`            | `stages/intents.py` cu registru `[(predicate, handler)]` înaintea buclei LLM             | Intent nou = fișier nou + intrare în registru                                         | —               |
| R4 | Ciclu import runner↔stages prin late-import                                   | `runner.py:189-199`, `gates.py:37-38`   | `PipelineDeps` în `src/worker/deps.py`                                                   | Graf de importuri aciclic real                                                          | —               |
| R5 | Conexiune DB ținută prin apelurile LLM → plafon ~10 tururi concurente       | `processor.py:528`, `connection.py:227` | Fazează handle_turn: load → release conn → LLM fără conn → TX pe conn proaspăt         | Concurența limitată de LLM, nu de pool                                                | **NX-141** |
| R6 | Dispatcher serial + poll 2s                                                    | `dispatcher.py:233-251`                   | gather bounded per tenant; tenanti în paralel; idle 0.5s                                     | Latență de livrare constantă sub load mixt                                           | —               |
| R7 | fallback_stage RO-only (încalcă P11)                                         | `runner.py:173-176`                       | Dict per-locale ro/hu/en pe`ctx.language`                                                   | Paritate multilingvă pe toate ieșirile                                                | —               |
| R8 | Docstring-uri/documentație stale                                              | `runner.py:8-10`, CLAUDE.md „Structura"  | Update + re-rulare`arch_explorer/analyze.py`                                                | Previne decizii greșite pe documentație veche                                         | —               |

**Concluzie:** sistemul e neobișnuit de disciplinat — principiile din CLAUDE.md chiar sunt implementate (pipeline liniar, single-exit, validator structural, RLS în straturi), iar mecanismele de fiabilitate sunt de calitate de producție. Cele două datorii reale: **concentrarea** (agent.py + processor.py acumulează tot ce e nou — R2/R3/R4 le dezamorsează ieftin) și **gaura ACK-on-error** (R1, singura încălcare structurală a propriilor principii). R5 e singurul subiect veritabil de scalare.
