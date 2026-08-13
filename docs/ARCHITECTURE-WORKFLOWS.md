# Nativx Assistant — Workflow Architecture (n8n-style)

> **Revizia auditată: `6bbeb6f` (2026-08-10).** Când citești asta pe alt commit, prima
> întrebare e cât de departe ai ajuns de aici: `git log --oneline 6bbeb6f..HEAD`.
>
> **Regulă de evidență:** fiecare muchie corespunde unui call/import real, citat `file:line`.
> Scheletul e verificat contra `arch_explorer/` (AST-derivat, regenerabil determinist:
> `python arch_explorer/analyze.py --repo . --root src` → **182 fișiere / 1272 noduri /
> 941 muchii**, plus `verify.py` care re-derivă graful printr-o metodă DIFERITĂ, regex vs AST,
> și cade dacă cele două nu sunt de acord).
>
> **Poartă anti-putrezire (CI):** `python scripts/verify_architecture_doc.py` compară blocurile
> ```` ```claim:...` ```` de la finalul documentului cu codul. Divergență → CI roșu. Ce garantează:
> LISTELE (stagii în ordine, tool-uri, procese, rute, flag-uri, migrări). Ce **nu** garantează:
> săgețile dintre ele. O diagramă poate avea toate stagiile corecte și o muchie greșită.
>
> Diagramele se randează în VS Code (extensia Mermaid Chart) sau pe GitHub.

<details>
<summary>Istoricul verificărilor (2026-07-02 → 2026-08-10)</summary>

- **2026-07-02, redactare inițială** (branch `feat/NX-139-decision-axes`, AST: 719 noduri /
  555 muchii). Verificat prin trace invers de execuție: fiecare entry point simulat până la
  terminare, 17 nepotriviri corectate (v. split-ul Diagram 4a/4b).
- **2026-07-02, runda 2 — audit adversarial**: sweep pe fișierele necitite la prima trecere →
  +Diagram 4c (compunerea rich / grounding) și Diagram 10 (proactiv), +18 noduri/muchii
  (rute de margine, price-check cache, typing-bypass, operator webhook, media download, XADD trim).
- **2026-08-10, resincronizare**. Documentul rămăsese în urmă cu ~6 săptămâni. Măsurat, nu
  presupus: din 229 de citări `file:line`, **201 încă aterizau în interval**; cele 27 în afară
  erau concentrate în exact două fișiere — `src/worker/stages/agent.py` (1411 → 423 linii) și
  `src/worker/processor.py`. Cauza: modularizarea agentului (NX-142/143/144) a spart monolitul
  în 19 module `src/agent/*`, deci **Diagram 4b a fost rescrisă din temelii**. Adăugate
  subsistemele apărute după 2026-07-02, absente complet: `src/safety/`, `src/knowledge/`,
  `src/commerce/`, `answer_plan`, `match_gate`, `query_spec`/`query_rewrite`,
  `search_documents`, rollup-ul de cerere. Restructurat pe niveluri + lentile.

</details>

---

## Cum se citește documentul

Un singur desen „cu tot proiectul" nu există aici, deliberat: sistemul are axe **ortogonale**
pe care un flowchart nu le poate purta simultan. În loc de asta, patru **niveluri** de zoom și
șase **lentile** peste același schelet.

**Regula etichetelor** (2026-08-10): prima linie a unui chenar spune CE se întâmplă, pe
românește, fără jargon; citarea `(fișier:linie)` din paranteză e dovada, nu mesajul.
Diagramele 1/2/7/8/9/10 rămân în limbaj de componente — publicul lor e operatorul.

**Regula de includere** (de ce un pas apare pe diagramă și altul nu): un pas primește nod dacă
face cel puțin unul dintre — *poate ieși devreme*, *cheamă un LLM*, *atinge DB/Redis/serviciu
extern*, *e stins/aprins de un flag*, *poate eșua într-un fel care schimbă răspunsul trimis*.
Restul e detaliu de implementare și stă în cod.

| Nivel | Întrebarea la care răspunde | Unde |
| --- | --- | --- |
| **1 · Context** | Cine vorbește cu sistemul și de ce servicii externe depinde | Diagram 1 |
| **2 · Procese** | Ce rulează, în ce container, și cine cu cine vorbește | Diagram 2 + tabelul de mai jos |
| **3 · Pipeline** | Cele 11 stagii, în ordine, cu toate ieșirile devreme | Diagram 4a |
| **4 · Un tur** | „Intră un mesaj — ce se întâmplă, pas cu pas" | Diagram 3 (+4b pentru interiorul agentului) |

**Lentilele** (aceleași stagii, întrebări diferite): Cost · Date · Eșec · Flag-uri · Izolare ·
Siguranță → secțiunea [Lentile](#lentile).

---

**Procese** ([docker-compose.yml:31-98](../docker-compose.yml)):

| Serviciu            | Comandă                                   | Rol                                                           |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------- |
| `webhook`         | `uvicorn src.webhook.app:app`            | Ingress HTTP (Meta + orders + /web/*)                         |
| `worker`          | `python -m src.worker.consumer`          | Consumer Redis Streams → pipeline                            |
| `dispatcher`      | `python -m src.worker.dispatcher`        | outbox → canale (singurul punct de trimitere)                |
| `scheduler`       | `python -m src.jobs.scheduler`           | Joburi mentenanță (7: rollup usage/demand, embed, lifecycle, cleanup, partiții, inițiatori)          |
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
    JOBS["jobs.scheduler — 7 joburi<br/>rollup_usage·rollup_demand·embed·lifecycle<br/>cleanup·partition_maintenance·initiators"]:::bg
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
  CONSUMER -. "typing direct — ocolește outbox<br/>consumer.py:63-81" .-> META_C
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
| Consumer             | `src/worker/consumer.py:333`         | `run_consumer()`     | XREADGROUP + debounce + reaper PEL                                          |
| handle_turn          | `src/worker/processor.py:200`        | `handle_turn()`      | Miezul turului: dedupe L2 → context → pipeline → outbox TX               |
| Pipeline             | `src/worker/runner.py:62`            | `run_pipeline()`     | Stagii în ordine fixă, early-exit pe reply/halt, măsoară                |
| Dispatcher           | `src/worker/dispatcher.py:325`       | `run_dispatcher()`   | Singurul punct de trimitere: outbox → ChannelSender                        |
| MetaClient           | `src/meta_client.py:28`              | `MetaClient`         | WhatsApp Cloud API send (text/template/typing)                              |
| TelegramClient       | `src/channels/telegram/client.py:78` | `TelegramClient`     | Bot API send + edit carusel                                                 |
| WebSender            | `src/channels/web/sender.py:34`      | `WebSender`          | Publish SSE pe`web:out:{visitor}` + backlog replay                        |
| Proactive            | `src/proactive/scheduler.py:253`     | `run_scheduler()`    | Joburi scadente → poartă consent/24h → outbox                            |
| Jobs scheduler       | `src/jobs/scheduler.py:132`          | `Job` loop           | rollup_usage · rollup_demand · embed_products · lifecycle · cleanup_dedupe · partition_maintenance · proactive_initiators |
| GET /webhook         | `src/webhook/app.py:62`              | `verify_webhook()`   | Handshake verificare Meta (token → challenge / 403)                        |
| GET /web/bootstrap   | `src/web/app.py:136`                 | `web_bootstrap()`    | Emite sesiunea vizitatorului (HMAC) + verificare Origin server-side         |
| Rich compose         | `src/worker/compose.py:344`          | `assemble()`         | Lanțul de grounding al căii bogate — v. Diagram 4c                       |
| Proactive gate       | `src/proactive/templates.py:83`      | `decide_proactive()` | Consent per kind + fereastra 24h + template aprobat — v. Diagram 10        |

---

## Diagram 2 — Application Startup

```mermaid
flowchart TD
  classDef proc fill:#aed6f1,stroke:#2874a6,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef gate fill:#f5b7b1,stroke:#922b21,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000

  subgraph WorkerBoot["worker: python -m src.worker.consumer"]
    W0["_main consumer.py:364"]:::proc
    W1["get_pool — admin pool"]:::step
    W2{"assert_migrations_current<br/>scripts/migrate.py"}:::gate
    W3["get_bot_pool EAGER"]:::step
    W4{"current_user == bot_runtime?<br/>connection.py:96"}:::gate
    W5["register pgvector codec<br/>connection.py:138"]:::step
    W6["get_redis"]:::step
    W7["build_registry — Meta/TG senders<br/>dispatcher.py:348"]:::step
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
    D0["_main dispatcher.py:369"]:::proc
    D1["get_pool + get_bot_pool eager"]:::step
    D2{"web_enabled?"}:::gate
    D3["get_redis → WebSender"]:::step
    D4["build_registry by credentials"]:::step
    D5["run_dispatcher loop READY"]:::proc
  end

  subgraph OtherBoot["scheduler / proactive / telegram-poller"]
    J0["jobs._main scheduler.py:224<br/>job list built by flags :122-162"]:::proc
    J1["loop: run due jobs + heartbeat<br/>scheduler.py:186-194"]:::step
    P0["proactive._main scheduler.py:262"]:::proc
    P1{"proactive_enabled? :265"}:::gate
    P2["get_pool + get_bot_pool eager<br/>:268-269"]:::step
    P3["run_scheduler loop READY"]:::proc
    PX["exit — process ends :266-267"]:::gate
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

Evidență: poarta de boot pe migrări `src/worker/consumer.py:373-386`; assert rol `bot_runtime` `src/db/connection.py:96-107`; pool eager („parolă greșită crapă la boot, nu la primul mesaj") `src/worker/consumer.py:388`.
**Shutdown** (audit adversarial): worker `finally: close_media + close_redis + close_pool` (`consumer.py:397-401`); dispatcher idem (`dispatcher.py:286-289`). Singletoni lazy la runtime (nu la boot): `get_llm`, `get_media_registry` (`channels/media.py:34-43`), `SessionSecretCache` (`web/session.py:98-101`).

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

  MSG["Mesaj primit<br/>(WhatsApp / Telegram / site)"]:::edge
  CAP{"Mesajul e anormal de mare?<br/>(apărare anti-OOM) (app.py:99-101)"}:::dec
  R413["Refuzat: prea mare (413)"]:::err
  SIG{"Semnătura Meta e autentică?<br/>(webhook/app.py:103)"}:::dec
  JSONV{"E JSON valid? (:106-109)"}:::dec
  R400["Refuzat: cerere stricată (400)"]:::err
  R403["Refuzat: semnătură falsă (403)"]:::err
  DED1{"L-am mai văzut? — Meta re-trimite<br/>agresiv; doar MESAJELE se deduplică<br/>(redis_bus.py:53)"}:::dec
  SKIP1["Ignorat: e o dublură"]:::step
  XADD["Pus la coadă în Redis<br/>(limită ~100k — sub avalanșă cele mai<br/>vechi se PIERD) (redis_bus.py:68-73)"]:::queue
  RDOWN{"Redis e căzut?"}:::dec
  R503["503 → Meta va re-încerca"]:::err
  ACK200["Confirmăm către Meta imediat<br/>(sub 50ms)"]:::step

  XREAD["Un worker ia mesajul din coadă<br/>(consumer.py:245)"]:::step
  KIND{"Ce fel de eveniment e?<br/>(process_event :123, :155-158)"}:::dec
  TYPING["Arătăm «scrie...» instant (:74-81)"]:::step
  DEB["Așteptăm 3s să termine de scris —<br/>o rafală de mesaje devine UNUL<br/>(debounce.py:57)"]:::step
  ORDERK["Comandă nouă de pe site → o legăm<br/>de conversație (webhook/orders.py:64)"]:::step
  RESOLVE["Aflăm CĂRUI magazin îi aparține<br/>(canal → business) (consumer.py:145)"]:::db
  STATUSK["Notăm statusul: livrat / citit / eșuat"]:::db
  LOCK{"Conversația e deja în lucru?<br/>(lacăt per conversație)"}:::dec
  REQUEUE["Ocupat → repunem la coadă<br/>cu pauză (:96-102)"]:::queue
  DROPQ["Prea multe reîncercări → mesaj<br/>PIERDUT (doar log) (:99-100)"]:::err
  ADM{"Magazinul are loc liber?<br/>(frână de concurență) (:197-210)"}:::dec
  REQA["Peste capacitate → repus la coadă,<br/>NIMIC pierdut (:106-119)"]:::queue
  LOADB["Încărcăm magazinul + configurația<br/>lui de vertical (consumer.py:222)"]:::db
  CBK{"E o apăsare de buton (carusel)?"}:::dec
  CAROUSEL["Navigare în carusel — drum determinist,<br/>fără AI (callback.py:71)"]:::step

  HT["Începe procesarea turului<br/>(processor.py:200)"]:::step
  DED2{"Dublură scăpată de Redis?<br/>(plasa a doua, în DB) (:238)"}:::dec
  SKIP2["Ignorat: deja procesat"]:::step
  CTX["Încărcăm: cine e clientul, conversația,<br/>ultimele 8 mesaje, starea, faptele știute"]:::db
  SEEDC["Pornim contorul de cost al zilei (:336)"]:::step
  GUARD{"Magazinul a depășit bugetul zilnic<br/>de AI? (:116)"}:::dec
  NOLLM["AI oprit pe azi → răspundem degradat,<br/>dar RĂSPUNDEM"]:::err
  PIPE["Rulăm cele 11 stagii → Diagram 4a"]:::step
  REPLY{"A ieșit un răspuns?"}:::dec
  HALT["Tăcere intenționată — logată (:361)"]:::step
  DISC["Adăugăm disclaimerul, dacă e cazul (:368)"]:::step
  SPLIT{"Text prea lung (și nu e cu carduri)?"}:::dec
  FRAG["Îl spargem în maxim 2 mesaje"]:::step
  TX["Salvăm TOTUL dintr-o mișcare: mesajele +<br/>coada de trimis + starea discuției<br/>(:382-492)"]:::db
  POST["După răspuns, în fundal: cache, rezumat,<br/>profil, fapte noi — cu conexiunea DB<br/>deja eliberată (consumer.py:236 · aftercare.py:444)"]:::step

  DISP["Dispecerul (alt proces) ia din coadă<br/>ce e de trimis"]:::step
  RENDER{"Alegem forma după ce POATE canalul:<br/>carduri / carusel / șablon / text<br/>(dispatcher.py:101)"}:::dec
  SEND["Trimitem prin canal (Meta / Telegram)"]:::edge
  SENT["Marcat trimis + legăm id-ul de la<br/>provider (dispatcher.py:215)"]:::db
  FAIL["Eșec → reîncercăm cu pauze crescânde<br/>→ marcat mort (dispatcher.py:211)"]:::err
  RP["Notăm când forma cerută ≠ forma<br/>livrată (dispatcher.py:69-98)"]:::step

  MSG --> CAP
  CAP -- da --> R413
  CAP -- nu --> SIG
  SIG -- nu --> R403
  SIG -- da --> JSONV
  JSONV -- nu --> R400
  JSONV -- da --> DED1
  DED1 -- da --> SKIP1
  DED1 -- nu --> XADD --> RDOWN
  RDOWN -- da --> R503
  RDOWN -- nu --> ACK200
  XADD -.-> XREAD --> KIND
  KIND -- mesaj --> TYPING --> DEB
  KIND -- comandă --> ORDERK
  KIND -- "status / buton" --> RESOLVE
  DEB -- "trimite după 3s de liniște" --> RESOLVE
  RESOLVE --> STATUSK
  RESOLVE --> LOCK
  LOCK -- ocupat --> REQUEUE
  REQUEUE -- "limita depășită" --> DROPQ
  LOCK -- liber --> ADM
  ADM -- "peste capacitate" --> REQA
  ADM -- admis --> LOADB --> CBK
  CBK -- da --> CAROUSEL
  CBK -- nu --> HT --> DED2
  DED2 -- "deja procesat" --> SKIP2
  DED2 -- "revendicat" --> CTX --> SEEDC --> GUARD
  GUARD -- da --> NOLLM --> PIPE
  GUARD -- nu --> PIPE
  PIPE --> REPLY
  REPLY -- nu --> HALT
  REPLY -- da --> DISC --> SPLIT
  SPLIT -- da --> FRAG --> TX
  SPLIT -- nu --> TX
  TX --> POST
  TX -.-> DISP --> RENDER --> SEND
  SEND -- ok --> SENT --> RP
  SEND -- eroare --> FAIL
```

Calea **sincronă** `/web/chat` diferă doar la capete: sesiune HMAC + rate-limit fail-closed + gard de buget (`src/web/app.py:197-240`) → `handle_turn(deliver=False)` — fără outbox, răspunsul HTTP e transportul (`src/web/app.py:241-252`, `src/worker/processor.py:345,382-492`).

---

## Diagram 4a — Pipeline Routing Workflow (cele 11 stagii)

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

  IN["Mesajul intră în pipeline"]:::stage

  subgraph G["1 · Porți — cod pur, fără AI (gates.py:439)"]
    G1{"Botul e oprit pe conversația asta?<br/>Clientul e blocat? A preluat un om?<br/>(:442-453)"}:::dec
    G2{"Trimite prea multe mesaje? (:323)"}:::dec
    G3{"Mesaj abuziv? — moderare automată (:348)"}:::llm
    MODB["Prea multe abateri în 24h →<br/>contactul e blocat (:305-318)"]:::err
    G4{"Semne de risc: amenințare legală,<br/>cere explicit un om? (:468)"}:::dec
    G5{"A trimis o poză? (:482)"}:::dec
    VIS["AI descrie poza → devine text de căutare<br/>(dacă pică, păstrăm descrierea clientului)<br/>(:385-436)"]:::llm
    GRD["Curățăm mesajul: tăiem excesul,<br/>mascăm datele personale (:487)"]:::stage
  end

  HALT["TĂCERE intenționată — nu răspundem<br/>(un om se ocupă / rafală de spam)"]:::err
  THR["Îi spunem O singură dată<br/>«hai mai încet» (:340-342)"]:::out
  NEU["Răspuns neutru, fără discuție"]:::out
  ESC["Chemăm un operator + mesaj de tranziție<br/>(gates.py:473-476)"]:::out

  LANG["2 · Detectăm limba: RO / HU / EN<br/>(language.py:27)"]:::stage

  subgraph CL3["3 · Reluăm clarificarea începută (clarify.py:28)"]
    CLR{"Îi pusesem o întrebare turul trecut?"}:::dec
    CONS["Răspunsul lui completează formularul:<br/>buget, nevoie, categorie (:41-52)"]:::stage
    ATT{"A răspuns vag de prea multe ori? (:59)"}:::dec
    RESUME["Reluăm de unde rămăsese<br/>(ruta salvată) (:69-73)"]:::stage
    ESCR["Prea multe încercări → om<br/>sau vânzare (:61-62)"]:::stage
  end

  GREET{"4 · E doar un salut? («bună»)<br/>(greeting.py:184)"}:::dec
  WELCOME["Mesaj de bun venit scris de noi — fără AI"]:::free
  ALIAS{"5 · Frază pe care o știm exact?<br/>(alias aprobat) (alias.py:46)"}:::dec
  AFAQ["Servim răspunsul pregătit dinainte (:64-72)"]:::free
  AROUTE["Știm doar ÎNCOTRO merge (ruta),<br/>nu și răspunsul (:73-82)"]:::stage
  SKIPS["Sărim cache / FAQ / triaj —<br/>ruta e deja știută<br/>(cache.py:93 · faq.py:49 · triage.py:214)"]:::stage
  CACHE{"6 · Am mai răspuns la o întrebare<br/>aproape identică? (cache semantic)<br/>preț/stoc se re-verifică, nu se servesc orb<br/>(cache.py:46-100)"}:::dec
  CHIT["Servim răspunsul din memorie — fără AI"]:::free
  FAQ{"7 · Se potrivește cu o întrebare<br/>frecventă? (prag + rerank) (faq.py:45-101)"}:::dec
  FHIT["Servim răspunsul din FAQ"]:::free

  TRI["8 · AI-ul MIC clasifică mesajul:<br/>vânzare / comandă / simplu / neclar<br/>(triage.py:212)"]:::llm
  TVAL{"Clasificarea e validă? Categoria<br/>chiar există la noi? (:247-255)"}:::dec
  TGUARD{"Întrebare cu cifre/fapte deghizată<br/>în «simplu»? (:261)"}:::dec
  TCONF{"AI-ul e nesigur pe clasificare? (:272)"}:::dec
  ROUTE{"Încotro merge mesajul?"}:::dec
  SIMPLE["AI-ul mic răspunde direct<br/>+ butoane de continuare (:298-308)"]:::out
  CLARIFY["Punem O întrebare de clarificare<br/>+ variante de răspuns (:309-319)"]:::out

  HOFF{"9 · Canalul are operator uman?<br/>(handoff.py:39)"}:::dec
  HESC["Anunțăm operatorul + îi confirmăm<br/>clientului (:44-49)"]:::out
  HSUP["Pe site nu e operator → tratăm<br/>ca vânzare (:40-42)"]:::stage

  AGENT["10 · AI-ul MARE: caută în catalog și<br/>compune recomandarea → Diagram 4b<br/>(stages/agent.py:267)"]:::llm
  FB["11 · Plasa finală: nimic n-a produs răspuns →<br/>punem o întrebare de clarificare<br/>(runner.py:169)"]:::out
  SEND["Răspunsul pleacă spre client<br/>(un singur punct de ieșire)"]:::out

  IN --> G1
  G1 -- da --> HALT
  G1 -- nu --> G2
  G2 -- "prima dată peste limită" --> THR
  G2 -- "rafala continuă (:343)" --> HALT
  G2 -- nu --> G3
  G3 -- da --> MODB --> NEU
  G3 -- nu --> G4
  G4 -- "da + canal cu operator" --> ESC
  G4 -- "da, fără operator → continuăm (:470)" --> G5
  G4 -- nu --> G5
  G5 -- da --> VIS --> GRD
  G5 -- nu --> GRD
  GRD --> LANG --> CLR
  CLR -- da --> CONS --> ATT
  ATT -- da --> ESCR --> GREET
  ATT -- nu --> RESUME --> GREET
  CLR -- nu --> GREET
  GREET -- da --> WELCOME
  GREET -- nu --> ALIAS
  ALIAS -- "țintă: răspuns FAQ" --> AFAQ
  ALIAS -- "țintă: rută / produs / categorie" --> AROUTE --> SKIPS --> ROUTE
  ALIAS -- nu --> CACHE
  CACHE -- da --> CHIT
  CACHE -- nu --> FAQ
  FAQ -- da --> FHIT
  FAQ -- nu --> TRI --> TVAL
  TVAL -- "invalid → fără rută" --> FB
  TVAL -- ok --> TGUARD
  TGUARD -- "da → vânzare" --> ROUTE
  TGUARD -- nu --> TCONF
  TCONF -- "da → clarificare" --> ROUTE
  TCONF -- nu --> ROUTE
  ROUTE -- "simplu + are răspuns" --> SIMPLE
  ROUTE -- "simplu, fără text (:298)" --> FB
  ROUTE -- clarificare --> CLARIFY
  ROUTE -- "cere om" --> HOFF
  HOFF -- da --> HESC --> SEND
  HOFF -- "nu — site" --> HSUP --> AGENT
  ROUTE -- "vânzare / comandă" --> AGENT
  AGENT -- "răspuns compus" --> SEND
  AGENT -- "AI-ul a picat → fără răspuns (:370)" --> FB
  FB --> SEND
  SIMPLE --> SEND
  CLARIFY --> SEND
  WELCOME --> SEND
  CHIT --> SEND
  FHIT --> SEND
  AFAQ --> SEND
```

---

## Diagram 4b — Agent Stage Internals (fazele A–F după modularizare)

Monolitul `agent.py` (1411 linii) a fost spart în 19 module `src/agent/*` (NX-142/143/144).
`agent_stage` ([src/worker/stages/agent.py:267](../src/worker/stages/agent.py)) a rămas **doar
regia**: A→B→C→D → `build_plan` → `render`.

| Fază | Unde trăiește | Ce decide |
| --- | --- | --- |
| **A · Regie + siguranță** | `stages/agent.py:267` · `_persist_safety_context:217` | Porți de intrare (fără LLM / rută ≠ sales,order / mesaj gol → no-op). Persistă contextul de siguranță ÎNAINTE de orice cale care servește produse |
| **B · Intenții pre-loop** | `deterministic.py:469` (`try_pre_intents`) | Link / comparație / detaliu / recenzii pe setul deja afișat → răspuns determinist, **$0 inferență** |
| **C · Compunerea promptului** | `prompt_builder` · `merge_constraints:126` · `context.py` | System GENERAT din DB (P9); stiva de constrângeri multi-tur; hint-uri per-tur (filtre, cumpărare, lead score) |
| **D · Bucla de tool-uri** | `llm.py:227` (`run_tool_loop`) · `tool_executor.py` (`ToolRun`) | Modelul alege tool-urile (max 3/tur, cap dur). `show_more` ocolește complet bucla → paginare deterministă |
| **E · Planner** | `planner.py:162` (`build_plan`) | Shaping determinist post-loop → `ResponsePlan`. Patru căi aduc produse din DB **în afara** `ToolRun` — fiecare cu gate de siguranță propriu |
| **F · Render** | `finalize.py:325` (`render`) | Comparație → rich → proză → order → no-result. Validator + retry + fallback determinist |

**Invariantul central:** modelul nu are ultimul cuvânt pe cifre și linkuri. Fazele E și F sunt cod
determinist peste `ctx.retrieval`; validatorul respinge orice preț/link/număr care nu e în retrieval.

**Privirea de sus — cele șase faze.** Patru din șase nu cheamă niciun model:

```mermaid
flowchart LR
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef free fill:#a3e4d7,stroke:#148f77,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef safe fill:#f5b7b1,stroke:#922b21,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000

  A["A · Pregătire + siguranță:<br/>ce restricții a declarat clientul<br/>(sarcină...) se rețin ACUM<br/>(agent.py:267-295)"]:::safe
  B["B · Scurtături fără AI:<br/>«dă-mi linkul», «compară-le»,<br/>«detalii», «ce zic recenziile»<br/>(deterministic.py:469)"]:::free
  C["C · Construim instrucțiunile<br/>pentru AI din datele magazinului<br/>+ tot ce știm din discuție<br/>(agent.py:299-350)"]:::step
  D["D · AI-ul MARE caută în catalog<br/>cu unelte controlate<br/>(maxim 3 căutări) (llm.py:227)"]:::llm
  E["E · CODUL decide ce arătăm<br/>de fapt (planner.py:162)"]:::step
  F["F · Compunem răspunsul + verificăm<br/>fiecare preț și link<br/>(finalize.py:325)"]:::llm
  X1["Răspuns imediat —<br/>zero cost AI"]:::out
  X2["«mai arată-mi» → pagina următoare<br/>din ce am găsit deja"]:::free

  A --> B
  B -- "scurtătură prinsă" --> X1
  B --> C --> D --> E --> F
  C -- "«mai arată-mi»" --> X2 --> E
```

**Faza E nu e un arbore — e o listă de precedență.** Primul caz care se potrivește câștigă;
orice cale care aduce produse din DB ocolind uneltele trece prin propriul filtru de siguranță:

```mermaid
flowchart TD
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef safe fill:#f5b7b1,stroke:#922b21,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000

  P1{"1 · Întreabă de comanda lui,<br/>dar nu e logat pe site? (:187)"}:::dec
  O1["Îi cerem să se logheze — fără cont<br/>nu-i putem căuta comanda"]:::out
  P2{"2 · Vrea să CUMPERE, dar AI-ul a uitat<br/>să-i facă linkul de plată? (:206-214)"}:::dec
  S2["Codul creează el linkul de plată,<br/>pe același drum contabilizat (:227)"]:::step
  P3{"3 · Tocmai a pus ceva în coș<br/>(și nu are link de plată)? (:236-242)"}:::dec
  S3["Îi arătăm produse care MERG ÎMPREUNĂ<br/>cu ce a luat, filtrate de siguranță (:251)<br/>a mers → gata; n-a mers → continuăm"]:::safe
  P4{"4 · Întreabă «care dintre ELE e cea mai...»?<br/>— superlativ pe ce i-am arătat (:282-288)"}:::dec
  S4["Recitim din catalog produsele deja arătate,<br/>ca să răspundem pe FAPTE, nu din memorie (:294)"]:::safe
  P5{"5 · Cere «ceva mai ieftin»? (:304-311)"}:::dec
  S5["Căutăm STRICT mai ieftin decât ce a văzut (:317)<br/>nu există → îi spunem cinstit + notăm golul<br/>de preț pentru comerciant (:331) → gata"]:::safe
  P6{"6 · AI-ul n-a adus produse, dar clientul<br/>vorbește despre cele deja arătate? (:352-359)"}:::dec
  S6["Le recitim din catalog — plasa care evită<br/>un «n-am găsit» absurd (:363)"]:::safe
  GATE["FILTRUL FINAL de siguranță: orice produs<br/>contraindicat (ex. sarcină) e scos AICI,<br/>orice ar fi adus căile de mai sus (:372)"]:::safe
  RETR["Setul final = SINGURA sursă pentru<br/>răspuns, carduri și memoria discuției (:373)"]:::step

  P1 -- da --> O1
  P1 -- nu --> P2
  P2 -- da --> S2 --> P3
  P2 -- nu --> P3
  P3 -- da --> S3 --> P4
  P3 -- nu --> P4
  P4 -- da --> S4 --> GATE
  P4 -- nu --> P5
  P5 -- da --> S5 --> GATE
  P5 -- nu --> P6
  P6 -- da --> S6 --> GATE
  P6 -- nu --> GATE
  GATE --> RETR
```

**Faza F — cum se alege forma răspunsului**, cu căderi în trepte (comparație → carduri → text):

```mermaid
flowchart TD
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef free fill:#a3e4d7,stroke:#148f77,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000

  APLAN{"Modul strict «answer plan» e pornit?<br/>(azi: OPRIT) (agent.py:396)"}:::dec
  AGUARD["Verificare suplimentară cu AI<br/>(answer_plan_guard.py:20)"]:::llm
  CMPD{"A cerut o COMPARAȚIE?"}:::dec
  CTAB["Tabel comparativ făcut de COD —<br/>AI-ul nu scrie nicio cifră în celule"]:::free
  PRD{"Avem produse de arătat?"}:::dec
  RICHC["AI-ul compune recomandarea<br/>structurată (carduri) (:266)"]:::llm
  ROK{"A ieșit ceva valid?"}:::dec
  RICHOUT["Trimitem cardurile + butonul de plată<br/>+ notăm ce am recomandat"]:::out
  DOWN["Cădem pe text simplu —<br/>motivul se înregistrează (:396)"]:::step
  VALID{"Verificăm textul: fiecare preț, link,<br/>cifră, afirmație de stoc sau sănătate<br/>(validator.py:195)"}:::dec
  RETRY["O reîncercare, cu explicația greșelii"]:::llm
  V2{"Acum e valid?"}:::dec
  DETR["Formulare scrisă de cod, fără cifre"]:::free
  PROSE["Text + carduri către client"]:::out
  ORD{"Era despre o comandă?"}:::dec
  GRND["Răspuns legat STRICT de datele<br/>comenzii lui (:149)"]:::llm
  TXTOK{"Text sigur, fără produse?<br/>(o clarificare, de pildă)"}:::dec
  NORES["«Nu am găsit» sigur + butoane de<br/>continuare — NU se salvează în cache (:211)"]:::out

  APLAN -- da --> AGUARD
  APLAN -- nu --> CMPD
  CMPD -- da --> CTAB
  CMPD -- nu --> PRD
  PRD -- "da + vânzare" --> RICHC --> ROK
  ROK -- da --> RICHOUT
  ROK -- nu --> DOWN --> VALID
  PRD -- "da + comandă" --> VALID
  VALID -- ok --> PROSE
  VALID -- invalid --> RETRY --> V2
  V2 -- da --> PROSE
  V2 -- nu --> DETR
  PRD -- "nu, dar există text" --> ORD
  ORD -- da --> GRND
  ORD -- nu --> TXTOK
  TXTOK -- da --> PROSE
  TXTOK -- nu --> NORES
  PRD -- "nu, fără text" --> NORES
```


**Cele patru căi care ocolesc `ToolRun`** (cross-sell, superlativ, „mai ieftin", rehidratare) aduc
produse direct din DB. Fiecare are `policy.gate` propriu, iar `retrieval_final`
([planner.py:372](../src/agent/planner.py)) e plasa de siguranță: idempotent pe un set deja filtrat,
dar prinde orice cale VIITOARE care uită gate-ul. Fără el, un `cart_add` perfect sigur putea trage
un complement contraindicat.

**`unmet_query` cu `reason="price_gap"`** ([planner.py:331](../src/agent/planner.py)) e marcat exact
unde se știe că turul a fost o intenție de preț. Din rollup n-ai cum să distingi post-hoc „n-am găsit
nimic" de „n-am găsit nimic mai ieftin" — de aceea faptul se scrie la sursă, nu se inferă.

Memoria (istoric / profil / state / rezumat) intră în prompturi prin `conversation_transcript` +
`context_blocks` ([src/worker/context.py:23](../src/worker/context.py)). System-promptul agentului e
GENERAT din DB ([src/agent/prompt_builder.py](../src/agent/prompt_builder.py), principiul 9).

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

  AXES["decision_axes + spec_numbers — NX-139<br/>axe REALE pe care variază setul<br/>compose.py:696 · :739"]:::step
  BNDL["_rich_bundle: facts per product<br/>facets from DomainPack — agent/finalize.py:229"]:::step
  JIN["structured JSON from mini<br/>complete_schema + _RICH_SCHEMA — agent/finalize.py:266"]:::llm

  ASM["assemble — hydrate by product_id refs<br/>compose.py:344"]:::step
  MEM{"item references a REAL<br/>retrieved product?"}:::dec
  DROPI["item dropped — membership grounding"]:::err

  SCRB["scrub field-by-field: intro / fit / education<br/>digits · % · unverifiable claims → field=None<br/>scrub_prose :132 · scrub_intro :161 · scrub_education :283"]:::step
  MED{"medical claim in field?<br/>_unsafe_medical :125"}:::dec
  DROPF["field DROPPED — P0 safety<br/>liability, not style"]:::err

  BDG["badge from REAL signals only<br/>deal > top, DomainPack thresholds<br/>badges.py:36-78 — gated card_badges_enabled"]:::free
  PICK["_select_pick deterministic :308<br/>suppressed when off-category :95"]:::step
  STK["drop unfounded stock line"]:::step
  CHIPS["suggestion chips<br/>client voice → NO scrub"]:::step
  FLAT["flatten / flatten_framing :500 · :534<br/>floor text for non-rich channels"]:::step
  OUTR["RichReply → set_rich_reply<br/>models.py:580"]:::out

  CMPB["build_comparison :757<br/>facet_summary :681 + axes + spec numbers<br/>lead determinist — ZERO LLM in cells"]:::free
  OUTC["Comparison → set_comparison_reply<br/>models.py:596"]:::out

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

Cine o folosește: `_finalize_rich` (agent/finalize.py:266-311, recomandare + cross-sell) și randorul web (`flatten_framing` la `channels/web/render.py:127`). Comparația (CMPB) e apelată din ambele căi de compare (`agent/deterministic.py:422, :455` și `agent/finalize.py:348`).

---

## Diagram 5 — Product Search Workflow

Calea LIVE a lui `search_products` ([catalog_tools.py:646](../src/tools/catalog_tools.py)).
NX-208 (query understanding) NU e aici: expansiunile rulează doar în shadow
(`query_spec_shadow`, OFF) și în benchmark-ul offline — calea servită primește query-ul modelului.

```mermaid
flowchart TD
  classDef step fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef out fill:#aed6f1,stroke:#2874a6,color:#000
  classDef safe fill:#f5b7b1,stroke:#922b21,color:#000

  Q["AI-ul cere o căutare în catalog<br/>(catalog_tools.py:646)"]:::step
  CONC["Traducem vorbele clientului în chei de<br/>catalog: «ten gras» → oily (:662)<br/>+ ingrediente cerute: «cu niacinamidă» (:665-670)"]:::step
  INH{"Căutare în aceeași discuție,<br/>fără categorie nouă? (:681)"}:::dec
  INHERIT["Păstrăm raftul curent — un typo<br/>(«mai ifetin») nu ne mai aruncă<br/>pe alt raft (:683-690)"]:::step
  FP{"E exact aceeași căutare<br/>ca data trecută? (:698)"}:::dec
  PAGE["Pagina următoare din ce am găsit<br/>deja — fără AI, fără cost (:594)"]:::step

  LADDER["Plan de rezervă: ce criterii slăbim,<br/>în ce ordine, dacă nu iese nimic (:702)"]:::step
  EMB{"Avem AI + vectori pregătiți? (:713)"}:::dec
  VEC["Înțelesul frazei → vector<br/>(UN singur apel) (:715)"]:::llm
  LEX["Căutare pe CUVINTE în catalog (:731)"]:::db
  SEM["Căutare pe ÎNȚELES în catalog (:749)"]:::db
  FUSE["Combinăm cele două liste<br/>într-un singur clasament (:770)"]:::step
  ANY{"Am găsit ceva la pasul ăsta?"}:::dec
  RELAX["Slăbim următorul criteriu secundar<br/>(brandul cerut NU se slăbește niciodată)"]:::step
  EXH{"Am epuizat planul de rezervă?"}:::dec
  EMPTY["Chiar nu există → AI-ul spune cinstit<br/>«nu am găsit» (finalize.py:203)"]:::out

  SAFE["FILTRU de contraindicații pe TOT setul,<br/>ÎNAINTE să-l ținem minte (:786)"]:::safe
  DIV{"Sortare pe relevanță și nu caută<br/>un produs anume? (:792)"}:::dec
  DIVER["Prima pagină acoperă ieftin / mediu /<br/>scump + branduri diferite —<br/>nu 6 clone (:798)"]:::step
  PAGE1["Prima pagină: maxim 6, fără cele<br/>deja arătate clientului (:807)"]:::step
  TEL["Notăm CEREREA pentru comerciant: ce s-a<br/>căutat și ce NU s-a găsit — produs lipsă /<br/>variantă lipsă / nimic (:826-897)"]:::step
  SESS["Ținem minte restul rezultatelor<br/>pentru «mai arată-mi» (:901-908)"]:::db
  BRAND{"A cerut un brand pe care<br/>nu-l avem deloc? (:921)"}:::dec
  BDISC["Spunem cinstit «nu lucrăm cu brandul X» —<br/>NU îi prezentăm altul drept al lui (:922-930)"]:::out
  RC["Fiecare produs primește motivul potrivirii +<br/>scoatem ce e nerecomandat pentru el (:952-955)<br/>produs numit inexistent → spunem clar (:934-942)"]:::step
  RES["Rezultat: produsele complete merg la<br/>verificator, AI-ul vede rezumatul<br/>compact (max 6 × 8 câmpuri)"]:::out

  Q --> CONC --> INH
  INH -- da --> INHERIT --> FP
  INH -- nu --> FP
  FP -- da --> PAGE --> RES
  FP -- nu --> LADDER --> EMB
  EMB -- da --> VEC --> LEX
  EMB -- nu --> LEX
  LEX --> SEM --> FUSE --> ANY
  ANY -- nu --> EXH
  EXH -- nu --> RELAX --> LEX
  EXH -- da --> EMPTY
  ANY -- da --> SAFE --> DIV
  DIV -- da --> DIVER --> PAGE1
  DIV -- nu --> PAGE1
  PAGE1 --> TEL --> SESS --> BRAND
  BRAND -- da --> BDISC
  BRAND -- nu --> RC --> RES
```

Degradare: `embed` picat → `query_vec=None` → lexical-only (`catalog_tools.py:716-717`); semantic
picat în tur → rămâne lexical (`:764-765`). Poziția gate-ului de siguranță (`:786`) e esențială:
filtrat DUPĂ pool, un produs contraindicat ar rămâne în `active_search.pool` și ar reapărea la
„arată-mi altele". `unmet_query` e emis și pe căile CU rezultate (produs numit absent → alternativele
se servesc, dar golul de catalog tot se înregistrează).

---

## Diagram 6 — Conversation Memory Workflow

```mermaid
flowchart TD
  classDef read fill:#aed6f1,stroke:#2874a6,color:#000
  classDef write fill:#a9dfbf,stroke:#1e8449,color:#000
  classDef llm fill:#d7bde2,stroke:#6c3483,color:#000
  classDef db fill:#d5dbdb,stroke:#566573,color:#000
  classDef dec fill:#f9e79f,stroke:#b7950b,color:#000

  subgraph Load["ÎNCEPUTUL TURULUI — ce ne amintim (processor.py:280-310)"]
    H["Ultimele 8 mesaje"]:::read
    ST["Starea discuției (max 8KB): ce i-am arătat,<br/>ce caută, coșul"]:::read
    SUM["Rezumatul discuției, dacă e lungă"]:::read
    PROF["Profilul clientului + scorul de interes"]:::read
    FCT["Fapte STABILE despre el: buget, brand<br/>preferat, restricții (processor.py:307)"]:::read
  end

  subgraph Use["CE VEDE AI-UL — context.py"]
    TRANS["Transcriptul compact<br/>(max 6 tururi / 1200 caractere) (:23)"]:::read
    BLOCKS["Blocurile: profil + fapte + stare<br/>(:40 · :87 · :155)"]:::read
  end

  subgraph Mutate["ÎN TIMPUL TURULUI"]
    AG["Agentul actualizează: ce caută clientul,<br/>căutarea activă"]:::write
    TP["Uneltele cer modificări — coșul<br/>(tools/base.py:38-40)"]:::write
    LNG["Limba detectată se salvează pe conversație<br/>(language.py:44)"]:::db
  end

  subgraph Persist["LA SALVARE — totul într-o singură tranzacție (processor.py:382-492)"]
    DP["Ce i-am ARĂTAT — doar id + nume + preț"]:::write
    PQ["Întrebarea pusă (sau ștearsă)"]:::write
    MERGE["Constrângerile adunate din discuție"]:::write
    ASR["Căutarea activă se resetează dacă<br/>răspunsul n-are produse"]:::write
    PATCH["Modificările uneltelor câștigă ULTIMELE"]:::write
    OPT["Salvat cu lacăt optimist — nu suprascriem<br/>alt worker care a apucat înainte"]:::db
  end

  subgraph PostTurn["DUPĂ RĂSPUNS, ÎN FUNDAL — conexiunea DB e deja eliberată (NX-161)<br/>consumer.py:236 → aftercare.py:444"]
    CW{"Răspunsul merită salvat<br/>și pentru alți clienți?"}:::dec
    CWB["În cache-ul semantic: zile pentru statice,<br/>minute pentru preț/stoc (aftercare.py:106)"]:::db
    SQ{"Discuția a depășit pragul<br/>de lungime?"}:::dec
    SUMGEN["AI-ul mic scrie rezumatul<br/>(aftercare.py:193)"]:::llm
    PE{"Turul a rulat normal?"}:::dec
    PEX["AI-ul mic extrage fapte noi despre client<br/>+ actualizează scorul de interes<br/>(aftercare.py:248-281)"]:::llm
  end

  H --> TRANS
  PROF --> BLOCKS
  ST --> BLOCKS
  FCT --> BLOCKS
  SUM --> TRANS
  TRANS --> AG
  BLOCKS --> AG
  AG --> MERGE
  TP --> PATCH
  DP --> PQ --> MERGE --> ASR --> PATCH --> OPT
  OPT --> CW
  CW -- da --> CWB
  OPT --> SQ
  SQ -- da --> SUMGEN
  OPT --> PE
  PE -- da --> PEX
```

Facts memory (NX-148/160) e LIVE: migrările 023/024 sunt aplicate, `conversation_facts_enabled=true`,
iar blocul de facts intră în prompturi între profil și state ([context.py:155](../src/worker/context.py)).
Doar facts cu `visibility='inject'` ajung în prompt — PII/medical filtrate la sursă.

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
    ADMIN["admin_pool — privileged<br/>get_pool :78 — control plane ONLY"]:::pool
    BOT["bot_pool — role bot_runtime, NO bypassrls<br/>get_bot_pool :209"]:::pool
  end

  subgraph Tenant["tenant_conn checkout — connection.py:270"]
    SETBIZ["set_config app.business_id"]:::step
    ISO{"isolation assert:<br/>role + GUC match? :191"}:::dec
    ISOERR["IsolationError — log CRITICAL<br/>reset GUC, raise"]:::err
    RLS["RLS policies filter every query<br/>wrong query → 0 rows, not other tenant"]:::db
    RESET["reset GUC on release :309"]:::step
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
  JOBS["admin jobs: rollup usage/demand · embed<br/>cleanup · lifecycle · partiții"]:::step
  SSC["in-process SessionSecretCache LRU+TTL<br/>+ NEGATIVE cache anti-flood<br/>web/session.py:73-101"]:::step

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

Tranzacția Sender (mesaje + outbox + state + dedupe-complete, atomic): `src/worker/processor.py:382-492`. Scrierile best-effort (analytics, cache, rezumat) rulează în `try/except` propriu, cu tranzacții imbricate unde e nevoie (`aftercare.py:72-87, :165, :227`) — un eșec se loghează, turul continuă.

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
    VISN["vision: photo → search query :305"]:::llm
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
    TOUT["TelegramClient send + edit carousel<br/>telegram/client.py:78"]:::ext
  end

  subgraph Web["Web widget — FE repo separat"]
    WIN["POST /web/messages · /chat HMAC session"]:::ext
    WOUT2["SSE /web/stream + backlog replay<br/>web/app.py:300"]:::ext
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
    L3["triage fail → route None<br/>triage.py:316"]:::deg
    L4["agent loop fail → return<br/>stages/agent.py:370"]:::deg
    L5["fallback_stage: clarify question<br/>NEVER silence — runner.py:169"]:::ok
  end

  subgraph ValErr["Validator failures — agent/validator.py:195"]
    V1{"reply valid? prices/links/claims"}:::dec
    V2["1 retry with allowed prices<br/>finalize.py:94-148"]:::deg
    V3{"valid now?"}:::dec
    V4["deterministic reply from DB products<br/>finalize.py:94-148"]:::ok
  end

  subgraph CostErr["Cost guard — limits.py:59-91 · processor.py:116"]
    C1{"daily cap business/contact hit?"}:::dec
    C2["llm=None: gates + cache still work<br/>LLM stages skip"]:::deg
  end

  subgraph InfraErr["Infra failures"]
    I1["Redis down at webhook → 503, Meta retries<br/>webhook/app.py:127-128"]:::err
    I2["cache/FAQ error → miss, turn continues<br/>cache.py:169"]:::deg
    I3["analytics fail → log only<br/>aftercare.py:86"]:::deg
    I4["conv lock busy → requeue capped<br/>consumer.py:96-102"]:::deg
    I5["consumer crash mid-turn → un-ACKed<br/>reaper XAUTOCLAIM re-claims :293"]:::ok
    I6["processing exception → ACK + log<br/>consumer.py:286-288 — WARNING: turn LOST"]:::err
    I7["requeue cap exceeded → event DROPPED<br/>consumer.py:99-100 — WARNING: turn LOST"]:::err
    I8["inbound stream maxlen ~100k trim →<br/>oldest LOST under backlog — redis_bus.py:68-73"]:::err
  end

  subgraph BgErr["Background process failures"]
    B1["proactive job crash → mark failed<br/>+ event, clean savepoint<br/>proactive/scheduler.py:214-227"]:::deg
    B2["maintenance job crash → log + skip,<br/>retry next interval — jobs/scheduler.py:117-118"]:::deg
    B3["poller getUpdates fail → log +<br/>sleep 3s + retry — poller.py:113-116"]:::deg
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
    SW1["sweep_abandoned_cart<br/>initiators.py:49"]:::bg
    SW2["sweep_back_in_stock<br/>initiators.py:77"]:::bg
    SEAM["event seams — DEFINED, NEVER CALLED:<br/>schedule_awb_update :160 · schedule_follow_up :190<br/>TODO: orders webhook should call them"]:::err
  end

  PJ[("proactive_jobs")]:::db

  subgraph Engine["ENGINE — proactive.scheduler loop :253"]
    CTL["control plane: tenants with due jobs<br/>scheduler.py:253-260"]:::step
    CLAIM["claim_due_jobs FOR UPDATE SKIP LOCKED<br/>:213 — savepoint per job :218-219"]:::step
    ROUTEP["resolve conversation + channel +<br/>recipient identity :113-120"]:::step
    RERR["no route → ProactiveRouteError<br/>→ mark failed :63-64, :120-124"]:::err
    BUILD["build_message_spec — builders.py<br/>text per kind :143"]:::step
    CANQ{"spec.cancel?<br/>:144"}:::dec
    CANC["mark cancelled :145-146"]:::out
  end

  subgraph Gate["GATE — decide_proactive, templates.py (NX-71)"]
    CONS{"consent by kind?<br/>marketing: abandoned_cart/follow_up<br/>transactional: awb/back_in_stock — templates.py:83"}:::dec
    SK1["skipped_no_optin"]:::out
    WIN{"in 24h window?<br/>is_in_24h_window"}:::dec
    TPL{"approved template<br/>in locale? P11"}:::dec
    SK2["skipped_no_window"]:::out
    FREE["mode=free → payload type=text<br/>scheduler.py:184-185"]:::step
    TMPL["mode=template → name+language+params<br/>scheduler.py:172-183"]:::step
  end

  OB[("outbox — idempotency proactive:job_id<br/>scheduler.py:186-194")]:::db
  SENTP["mark sent + proactive_enqueued event<br/>:195-200"]:::out
  FAILP["job exception → mark failed +<br/>proactive_failed event, clean savepoint<br/>:220-227"]:::err
  DISPP["dispatcher — TEMPLATE capability?<br/>WhatsApp: send_template · else degrade to text<br/>dispatcher.py:158-160, :218"]:::step

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
3. **„Validatorul (stagiul 8)" din CLAUDE.md nu e stagiu separat:** trăiește în [`src/agent/validator.py`](../src/agent/validator.py) (`validate_prose:195`) și e chemat din faza F a agentului, nu ca stagiu al pipeline-ului. Comportamentul e cel documentat, structura diferă. *(Actualizat 2026-08-10: până la NX-142 erau funcții private în monolitul `agent.py` — de aceea R2 din tabelul de refactoring apare ca rezolvat.)*
4. **arch_explorer e ușor stale pe branch-ul curent:** raportează `agent_stage` la linia 858 și `triage_stage` la 179; în cod sunt la `stages/agent.py:267` și `triage.py:212`. Necesită re-rulare `arch_explorer/analyze.py`.
5. **`webhook/status.py` NU există** — CLAUDE.md îl listează ca LIVE; statusurile sunt parsate în `webhook/meta.py:104` (`parse_statuses`) și scrise de worker (`consumer.py:137-146`).
6. **STT/Whisper NU e implementat** — CLAUDE.md promite „vocale → STT (Whisper)"; `audio` e doar un tip de media parsat cu caption drept body (`webhook/meta.py:27,36-39`), ne-rutat spre vreo transcriere (gates rutează DOAR `image`, `gates.py:482`).
7. **Tool-ul `delivery_eta` NU există** — CLAUDE.md îl listează; registry-ul real are 10 tool-uri (grep `@register` în `src/tools/`), fără `delivery_eta`.

## Puncte forte

- **Pipeline liniar cu un singur owner per câmp** — runner generic; observabilitatea (latency + tokens per stagiu + buget per tur) e în runner, nu în stagii (`src/worker/runner.py:47-105`).
- **Un singur punct de ieșire, tranzacțional** — reply + outbox + state + dedupe-complete într-o singură TX (`src/worker/processor.py:382-492`); dispatcher cu visibility-timeout self-healing (`src/worker/dispatcher.py:8-13`).
- **Validatorul e apărarea load-bearing anti-halucinație și anti-prompt-injection** — orice preț/link/produs trebuie să existe în `ctx.retrieval` (`src/agent/validator.py:78-104`); degradarea finală e un reply determinist din DB (`src/agent/fallbacks.py:23`).
- **Izolare multi-tenant în straturi** — rol LOGIN fără bypassrls + GUC per checkout + assert de izolare la fiecare checkout (`src/db/connection.py:187-204, 274-287`); boot-gate pe migrări (`src/worker/consumer.py:315-321`).
- **Fiabilitate pe coadă** — dedupe 2 straturi (Redis + DB claim-or-resume), ACK-after-flush (`src/worker/debounce.py:11-15`), reaper XAUTOCLAIM (`src/worker/consumer.py:293-330`).
- **Cost governance pe 3 niveluri** — business/zi, contact/zi, vizitator web, reseed din `usage_daily`, enforcement post-increment fără TOCTOU (`src/worker/processor.py:308-379`).

## Puncte slabe / riscuri

1. **⚠ Cel mai serios: excepție de procesare = tur pierdut tăcut.** La orice excepție în `process_event`, consumer-ul face ACK și doar loghează (`src/worker/consumer.py:286-288`; la fel reaper-ul, per-intrare, `:308`). Meta a primit deja 200 → nu re-trimite. Clientul nu primește NIMIC — încălcare a principiului 6 exact pe calea de eroare pe care principiul o vizează. **A doua cale de pierdere (găsită la trace-ul invers):** lock ocupat persistent → după `conv_lock_max_requeues` evenimentul e DROPAT cu un simplu log (`consumer.py:99-100`). → **NX-140**
2. **`agent.py` e un god-module** — 1200+ linii; `agent_stage` (`:957+`) amestecă 3 intenții deterministe, bucla LLM, login-wall, checkout-fallback, cross-sell; validatorul + 3 căi de finalize în același fișier.
3. **Cuplaj maxim în processor** — `handle_turn` are fan-out 45 (cel mai mare din sistem, `arch_explorer/GRAPH_REPORT.md`) și ~300 linii: orchestrare + politică de state-merge + sender + post-tur.
4. **Ciclu de import gestionat manual** — stagiile importă `PipelineDeps` din runner sub `TYPE_CHECKING` (`src/worker/stages/gates.py:37-38`); runner-ul importă stagiile la sfârșitul fișierului (`src/worker/runner.py:189-199`).
5. **Conexiune DB ținută pe durata apelurilor LLM** — pipeline-ul rulează pe `conn` din `bot_pool` (max_size=10, `src/db/connection.py:224-229`) în timp ce agentul face 1-4 apeluri LLM (`src/worker/processor.py:345`); la fel `/web/chat` (`src/web/app.py:223-243`). Plafon ~10 tururi concurente/proces; pooler Supabase capat la ~15 sesiuni. **Bottleneck-ul #1 de scalare.** → **NX-141**
6. **Dispatcher secvențial + poll de 2s** — rând cu rând, tenant cu tenant (`src/worker/dispatcher.py:233-241`), sleep 2s la idle (`:245-251`). Un tenant lent întârzie toți ceilalți; +0-2s latență pe calea async.
7. **Fallback-ul final e doar în română** — `src/worker/runner.py:173-176`, deși pipeline-ul e RO/HU/EN și triage are `_CLARIFY_FALLBACK` per-locale (`src/worker/stages/triage.py:313`). Încalcă P11.
8. **SSL fără verificare pe orice win32** — `src/db/connection.py:56-73` dezactivează verificarea certificatului condiționat de platformă, nu de env. Prod e Linux → risc latent.
9. **Cuplaj src→scripts la runtime** — `src/worker/consumer.py:315` importă `scripts.migrate`; a produs deja incidentul Dockerfile (imagine fără `scripts/` → crash-loop, PR #132).
10. **Duplicare mică de asamblare context** — `history_block`/`context_block` construite identic în triage (`triage.py:223-226`) și agent (`stages/agent.py:309-312`).

**Circular dependencies:** doar ciclul runner↔stages gestionat prin late-import (pct. 4). **Dead code (corectat la auditul adversarial):** `estimate_turn_cost` (`src/worker/limits.py:186`) — definit, referit doar într-un comentariu (`processor.py:133`), înlocuit de costul real NX-125 → de șters. **Unreachable seams (intenționate, TODO):** `schedule_awb_update` (`initiators.py:160-187`) și `schedule_follow_up` (`initiators.py:190`) — definite, niciun apelant; webhook-ul de comenzi ar trebui să le cheme la expediere.

---

---

# Lentile

Aceleași 11 stagii, șase întrebări diferite. O lentilă nu adaugă noduri — recolorează scheletul
din Diagram 4a. Deciziile arhitecturale se iau pe lentile, nu pe topologie.

## Lentila 1 · COST — unde se duc banii pe tur

Zece puncte în care sistemul cheamă un model. Restul e cod determinist.

| Punct | Model | Când | Evitabil prin |
| --- | --- | --- | --- |
| Moderation | `omni-moderation-latest` | Gates, fiecare mesaj text | `moderation_enabled` |
| Vision | `gpt-5.4-mini` | Gates, doar imagini | `vision_enabled` |
| Embedding query | `text-embedding-3-small` | Cache + FAQ lookup | — (ieftin, e chiar mecanismul de economie) |
| Triaj | `gpt-5.4-nano` | Orice mesaj care trece de straturile gratuite | alias / cache / FAQ hit |
| Triaj compune „simple" | `gpt-5.4-nano` | Rută `simple` | — |
| Buclă tool-uri | `gpt-5.4-mini` | Rute `sales` / `order` | intenții pre-loop · `show_more` |
| Compunere rich | `gpt-5.4-mini` | Recomandare cu produse | — |
| Finalizare proză (+1 retry) | `gpt-5.4-mini` | Când rich eșuează sau ruta e order | — |
| Extractor profil | `gpt-5.4-nano` | Post-tur, async | `profile_extraction_enabled` |
| Summarizer | `gpt-5.4-mini` | Conversații > 20 mesaje | `summary_enabled` |

**Căile cu cost zero de inferență** (ținta: 40-60% din trafic): alias exact · cache semantic ·
FAQ · salut determinist · intenții pre-loop (link/compare/detaliu/recenzie) · paginare
`show_more` · tabel comparativ · căutarea „mai ieftin" · toate mesajele de fallback.

**Plafoane impuse în cod**, nu în prompturi: max 3 tool calls/tur · istoric max 8 mesaje ·
state ≤ 8KB (CHECK în DB ca plasă) · max 6 produse × 8 câmpuri în tool results · cost guard
zilnic per business (`cost_guard_enabled`, contor Redis; sursa de facturare rămâne `usage_daily`).
Prefixul de system e byte-identic între tururi → prompt caching (75-90% discount) — orice hint
per-tur se pune în USER, nu în prefix.

## Lentila 2 · DATE — un singur proprietar per câmp

Principiul 3 din CLAUDE.md e verificabil: fiecare câmp din `TurnContext` are exact un stagiu
care îl scrie. Când două vor să scrie același câmp, arhitectura e greșită.

| Câmp | Proprietar unic | Notă |
| --- | --- | --- |
| `language` | `language_stage` | Cheie în TOATE lookup-urile (faqs / semantic_cache / wa_templates) |
| `route` | `triage_stage` | Excepții documentate: `alias_stage` setează ruta fără reply; `handoff_stage` o rescrie în SALES pe canale fără operator; `clarify_resume` o restaurează |
| `retrieval` | `planner.build_plan` | Singurul scriitor — de aceea gate-ul de siguranță final stă tot acolo |
| `reply` | orice stagiu (early exit) | Un singur punct de IEȘIRE (Sender), dar multe puncte de decizie |
| `halt` | `gates_stage` | Tăcere INTENȚIONATĂ, fără reply — distinctă de „n-am produs nimic" |
| `from_cache` | `cache_stage` | — |
| `state.safety` | `agent_stage` (faza A) | Persistat de processor din `state_patch` |
| `state.search_constraints` | `agent_stage` (faza C) | Stiva multi-tur |

**State = ref-uri, nu obiecte** (P8): `displayed_products` ține `{product_id, name, price}`.
De aceea toate cele patru căi din faza E **rehidratează din catalog** înainte să judece —
un ref nu-și trădează retinalul în nume.

## Lentila 3 · EȘEC — unde poate ieși tăcere

Principiul 6 („niciodată tăcere") are un lanț de degradare explicit. Fiecare treaptă e cod, nu
speranță.

```
rich eșuat ──→ rich_downgraded ──→ proză
proză invalidă ──→ 1 retry cu feedback ──→ formulare deterministă fără cifre
buclă LLM eșuată ──→ no-op ──→ fallback_stage (întrebare de clarificare)
fără cheie LLM / cost guard depășit ──→ llm=None ──→ pipeline degradat, tot cu reply
tool eșuat ──→ commerce_note ──→ chips-urile nu promit acțiunea refuzată
zero rezultate ──→ mesaj sigur, cacheable=False ──→ chips de continuare
```

**Cele două locuri unde tăcerea e corectă**, ambele intenționate: `halt` din Gates (om a preluat
conversația / burst de rate-limit) și dedupe (retry Meta pe un mesaj deja procesat).

**Cache poisoning** e clasa de bug care revine: orice reply de tip „n-am găsit" trebuie
`cacheable=False`. Altfel un `hit_count` care crește servește „n-am găsit" la orice query similar,
sărind agentul. S-a întâmplat live pe demo.

## Lentila 4 · FLAG-URI — ce rulează de fapt

**86 de flag-uri bool** în `Settings`; **71 ON**, **15 OFF** by default. Lista exactă e în blocul
`claim:flags` — CI-ul cade dacă divergeaza. Ce contează arhitectural:

**Stratul shadow** — rulează în paralel cu producția, observă, nu atinge `reply`:
`query_spec_shadow_enabled` · `match_gate_shadow_enabled` · `search_shadow_enabled`.
Toate OFF. Asta e o dimensiune întreagă a sistemului pe care nicio diagramă de dinainte n-o arăta:
măsurăm calitatea căutării fără să riscăm răspunsul.

**Construit dar neactivat**: `answer_plan_enabled` (+ `_critic`, `_max_quality`) ·
`injection_screen_enabled` · `web_identity_enabled` · `ai_disclaimer_enabled` ·
`faq_locale_fallback_enabled` · `replay_store_prompt_enabled` · `validator_stock_claims_enabled`.

**Atenție la citire**: `web_enabled` e OFF în default-urile din cod și ON în `.env.prod`.
Un flag OFF în `config.py` nu înseamnă OFF în producție — înseamnă „decizia se ia în env".

## Lentila 5 · IZOLARE — unde se poate scurge un tenant

Izolarea primară e `WHERE business_id = $1` în cod. RLS e plasa, nu mecanismul.

| Cale | Rol DB | Ce poate atinge |
| --- | --- | --- |
| `tenant_conn` (pipeline, joburi per-tenant) | `bot_runtime`, FĂRĂ `bypassrls` | Doar rândurile tenantului (`app.business_id` setat per checkout) |
| `admin_conn` (control plane) | privilegiat | **Excepție unică documentată**: `resolve_channel` (canal → business, rulează ÎNAINTE ca tenantul să fie cunoscut) + mentenanță non-PII |

**Orice alt query pe `admin_conn` e un bug de izolare.** CI-ul are un test dedicat
(`scripts/check_no_raw_conn.py` + jobul „Izolare concurentă NX-53").

**PII trăiește într-un singur loc**: `channel_identities` (telefon E.164 / id canal + hash).
Nici în `orders`, nici în loguri (redaction în logger), nici în `analytics_events` — de aceea
whitelist-ul de argumente pentru evenimentul `tool_call` exclude deliberat `search_products.query`
(ar putea ecoua fraza userului) și întoarce `{}` pentru tool-urile cu argumente PII.

## Lentila 6 · SIGURANȚĂ — gate-uri P0

Două familii, ambele cu kill-switch, ambele verificate prin mutație (o protecție inertă e teatru
de siguranță).

**Contraindicații** (`src/safety/policy.py`): `SafetyPolicy.for_turn` o dată per tur, aplicat în
**cinci** puncte — `cross_sell`, `attr_query`, `cheaper`, `rehydrate`, `retrieval_final` — plus
`state_prune` pe `displayed_products`. Contextul declarat (ex. sarcină) se **persistă** în
`state.safety`, pentru că istoricul e plafonat la 8 mesaje: fără persistare, o declarație de la
turul 9 dispărea și produsul contraindicat reintra.

**Claim-uri medicale** (`validator._safety_ok`, `safety_medical_guardrail_enabled`): niciun claim
terapeutic („tratează", „sigur în sarcină", „fără alergeni", „recomandat de medic"). Pe proză:
invalid → retry → fallback. Pe calea bogată: scrub → DROP. Răspundere juridică, nu preferință de ton.

---

# Contract verificat

Blocurile de mai jos sunt comparate cu codul de `scripts/verify_architecture_doc.py`, rulat în CI.
Nu le edita de mână: `python scripts/verify_architecture_doc.py --emit-claims` le regenerează.

Ce se verifică: **listele**. Ce nu: **săgețile**. O diagramă poate avea toate stagiile corecte și
o muchie greșită între ele — pentru muchii rămân `arch_explorer/verify.py` și cititorul.

```claim:stages
gates_stage
language_stage
clarify_resume_stage
greeting_stage
alias_stage
cache_stage
faq_stage
triage_stage
handoff_stage
agent_stage
fallback_stage
```

```claim:tools
cart_add
check_order
checkout_link
compare_products
faq_lookup
get_product_details
reorder
request_human
search_products
subscribe_back_in_stock
```

```claim:processes
dispatcher
proactive
redis
scheduler
telegram-poller
webhook
worker
```

```claim:routes
GET /bootstrap
GET /r/{business_id}/{ref_code}
GET /stream
GET /webhook
POST /chat
POST /messages
POST /webhook
POST /webhook/orders/{business_id}
```

```claim:jobs
cleanup_dedupe
cleanup_web_turns
embed_products
lifecycle
partition_maintenance
proactive_initiators
rollup_demand
rollup_usage
```

```claim:flags
admission_distributed_enabled = true
admission_enabled = true
ai_disclaimer_enabled = false
alias_enabled = true
answer_plan_critic_enabled = false
answer_plan_enabled = false
answer_plan_max_quality = false
attr_query_enabled = true
cache_enabled = true
card_badges_enabled = true
catalog_projection_v2_enabled = true
catalog_reason_codes_enabled = true
cheaper_intent_enabled = true
cheapest_alternatives_enabled = true
checkout_intent_fallback_enabled = true
closure_chips_enabled = true
compare_coherence_guard_enabled = true
compare_intent_enabled = true
comparison_facets_enabled = true
content_status_filter_enabled = true
conv_lock_enabled = true
conversation_facts_enabled = true
cost_guard_enabled = true
cross_sell_enabled = true
db_op_metrics_enabled = true
db_query_timing_enabled = false
decision_axes_enabled = true
demand_rollup_enabled = true
detail_intent_enabled = true
domain_pack_enabled = true
embed_job_enabled = true
facet_search_enabled = true
faq_enabled = true
faq_locale_fallback_enabled = false
faq_policy_gate_on_faq_kind = true
faq_rerank_enabled = true
injection_screen_enabled = false
input_pii_mask_enabled = true
lead_score_hint_enabled = true
lexical_rank_v2_enabled = false
lifecycle_job_enabled = true
link_intent_enabled = true
llm_sampling_enabled = true
match_gate_shadow_enabled = false
memory_canonicalize_enabled = true
memory_open_capture_enabled = true
memory_safe_injection_enabled = true
memory_v2_enabled = true
moderation_enabled = true
no_result_alternatives_enabled = true
partition_job_enabled = true
pool_metrics_enabled = true
proactive_enabled = true
proactive_initiators_enabled = true
profile_extraction_enabled = true
query_spec_shadow_enabled = false
rate_limit_enabled = true
relations_first_enabled = true
replay_store_prompt_enabled = false
response_style_enabled = true
response_telemetry_enabled = true
review_intent_enabled = true
rich_facets_enabled = true
rich_pick_deterministic_enabled = true
rich_pick_relevance_gate_enabled = true
rich_pick_web_enabled = false
rich_review_anchor_enabled = false
safety_contraindications_enabled = true
safety_medical_guardrail_enabled = true
search_blended_rank_enabled = true
search_category_hard_enabled = true
search_category_tree_enabled = true
search_diversify_enabled = true
search_offcategory_guard_enabled = true
search_sessions_enabled = true
search_shadow_enabled = false
search_sort_mode_enabled = true
short_ack_guard_enabled = true
spec_digits_grounded_enabled = true
summary_enabled = true
triage_factual_guard_enabled = true
turn_budget_alerts_enabled = true
typing_enabled = true
validator_bare_numbers_enabled = true
validator_claims_enabled = true
validator_stock_claims_enabled = false
vision_enabled = true
web_admission_enabled = true
web_demo_access_enabled = false
web_enabled = false
web_identity_enabled = false
web_session_origin_binding = false
web_session_v2_enabled = false
web_session_v2_required = false
web_turn_ledger_enabled = false
web_turn_lock_enabled = true
welcome_enabled = true
```

```claim:migrations
003_bot_runtime_role.sql
004_inbound_dedupe.sql
005_bot_runtime_login.sql
006_semantic_cache_v2.sql
007_semantic_cache_invalidation.sql
008_order_items_insert.sql
009_gdpr_svc_role.sql
010_conversations_one_open.sql
011_bot_runtime_read_aliases_faqs.sql
012_inbound_dedupe_completion.sql
013_usage_cached_tokens.sql
014_schema_migrations.sql
015_fts_index.sql
016_analytics_turn_trace.sql
017_faqs_embedding_model.sql
018_orders_external_customer_ref.sql
019_proactive_job_dedupe.sql
020_messages_latency_seconds.sql
021_outbox_priority_dispatcher.sql
022_analytics_turn_id_index.sql
023_conversation_facts.sql
024_conversation_facts_memory_v2.sql
025_usage_split_attribution.sql
026_variant_gtin_net_content.sql
027_product_relations.sql
028_product_content_status.sql
029_product_embeddings_versioned.sql
032_commerce_and_content.sql
033_ro_unaccent_search.sql
034_semantic_cache_prompt_version.sql
035_evidence_and_derived_signals.sql
036_search_documents_shadow.sql
037_partition_maintenance.sql
038_demand_daily.sql
039_public_read_categories.sql
040_web_turns.sql
```


---

# Refactoring — plan cu evidență

> **Stare la resincronizarea din 2026-08-10.** Tabelul de mai jos e din 2026-07-02; citările lui
> către `agent.py` (liniile 472-1216 de ATUNCI) trimit la un fișier care între timp a fost spart în 19 module.
> Verificat pe cod, nu presupus:
>
> - **R2 REZOLVAT** — validatorul trăiește în [`src/agent/validator.py`](../src/agent/validator.py).
> - **R3 REZOLVAT** — intențiile deterministe în [`src/agent/deterministic.py`](../src/agent/deterministic.py) (pre-loop) și `planner.py` (post-loop).
> - **R4 DESCHIS** — `src/worker/deps.py` nu există; ciclul runner↔stages e tot pe late-import.
> - **R7 DESCHIS** — `fallback_stage` are și azi textul RO hardcodat (încalcă P11).
> - R1/R5/R6/R8 — neverificate la această trecere; tratează-le ca necunoscute, nu ca deschise.


| #  | Problemă                                                                      | Evidență                                  | Soluție                                                                                      | Impact                                                                                  | Card             |
| -- | ------------------------------------------------------------------------------ | ------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ---------------- |
| R1 | Excepție de procesare → ACK + drop, client fără răspuns                   | `consumer.py:228-230`                     | Re-queue cu contor; la epuizare → fallback reply în outbox + event`turn_failed`, apoi ACK | Erorile tranzitorii se auto-vindecă; cele permanente produc mesaj + semnal, nu tăcere | **NX-140** |
| R2 | Validatorul (apărarea critică) trăiește ca funcții private în god-module | `agent.py` @2026-07-02, 472-502 + 560-688               | Mutare pură în`src/worker/validator.py`                                                   | Testare izolată; auditabilitate de securitate pe un fișier                            | —               |
| R3 | Intențiile deterministe împletite în`agent_stage`                         | `agent.py` @2026-07-02, 984-1027 + 1174-1216            | `stages/intents.py` cu registru `[(predicate, handler)]` înaintea buclei LLM             | Intent nou = fișier nou + intrare în registru                                         | —               |
| R4 | Ciclu import runner↔stages prin late-import                                   | `runner.py:189-199`, `gates.py:37-38`   | `PipelineDeps` în `src/worker/deps.py`                                                   | Graf de importuri aciclic real                                                          | —               |
| R5 | Conexiune DB ținută prin apelurile LLM → plafon ~10 tururi concurente       | `processor.py:345`, `connection.py:227` | Fazează handle_turn: load → release conn → LLM fără conn → TX pe conn proaspăt         | Concurența limitată de LLM, nu de pool                                                | **NX-141** |
| R6 | Dispatcher serial + poll 2s                                                    | `dispatcher.py:233-251`                   | gather bounded per tenant; tenanti în paralel; idle 0.5s                                     | Latență de livrare constantă sub load mixt                                           | —               |
| R7 | fallback_stage RO-only (încalcă P11)                                         | `runner.py:173-176`                       | Dict per-locale ro/hu/en pe`ctx.language`                                                   | Paritate multilingvă pe toate ieșirile                                                | —               |
| R8 | Docstring-uri/documentație stale                                              | `runner.py:8-10`, CLAUDE.md „Structura"  | Update + re-rulare`arch_explorer/analyze.py`                                                | Previne decizii greșite pe documentație veche                                         | —               |

**Concluzie:** sistemul e neobișnuit de disciplinat — principiile din CLAUDE.md chiar sunt implementate (pipeline liniar, single-exit, validator structural, RLS în straturi), iar mecanismele de fiabilitate sunt de calitate de producție. Cele două datorii reale: **concentrarea** (agent.py + processor.py acumulează tot ce e nou — R2/R3/R4 le dezamorsează ieftin) și **gaura ACK-on-error** (R1, singura încălcare structurală a propriilor principii). R5 e singurul subiect veritabil de scalare.
