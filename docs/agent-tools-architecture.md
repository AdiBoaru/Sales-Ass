# Epicul „Tool-uri Agent" — Arhitectură + Analiză de producție (2026)

_Document de design. Decide CUM convertim agentul din RAG (retrieve-then-generate) într-un
agent cu tool-calling determinist (max 3 apeluri/tur), CE tool-uri construim și în ce ordine,
și CE mai lipsește pentru producție reală în 2026. Sursă pentru cardurile epicului._

---

## 0. TL;DR

- Agentul de azi ([`agent.py`](worker/stages/agent.py)) e **RAG**: 1 search + 1 apel mini
  cu validator de preț inline. Nu există tool-calling. „Tool-urile" = un EPIC, nu un task.
- Construim întâi **fundația** (contract de tool + registry + buclă de function-calling cu
  cap dur de 3 + validator extins), apoi adăugăm tool-uri în 4 faze (read → comerț →
  comenzi/logistică → programări/proactiv).
- **Faza 1 (acest epic, prima felie):** framework + `search_products` (ca tool) +
  `get_product_details` + `compare_products`. Read-only, date gata (D3), demo-vizibil,
  zero regresie pe fluxul de vânzare (păstrăm fallback-ul determinist + validatorul).
- **Producție 2026:** gap-urile reale nu sunt tool-urile, ci: **cost guard + rate limit**
  (anti-runaway), **lock per conversație + XAUTOCLAIM** (scalare orizontală), **evals +
  golden CI** (regresii de selecție de tool), **conformitate EU AI Act + GDPR** (transparență
  Art. 50, EU residency, DPA), **observabilitate** (lag/outbox alerts, tracing, billing rollup).

---

## PARTEA I — ARHITECTURA EPICULUI

### 1. Starea actuală vs țintă

**Acum (RAG, stagiul 7):**
```
embed(query) → search_products_semantic → mini.complete(produse) → validator preț (retry→fallback)
```
Un singur apel de generare. `ctx.retrieval` = produsele retrievate. Validator inline
(`_prices_ok`) verifică DOAR prețurile.

**Țintă (tool-calling, CLAUDE.md stagiul 7):**
```
mini decide → [tool_call(s)] → execută determinist → feed rezultate → repetă (≤3) →
text final → validator (preț + produs + link)
```
Agentul (nu routerul) decide mutarea de vânzare; tool-urile sunt cod determinist; LLM-ul doar
ALEGE ce tool cu ce argumente. „MAX 3 tool calls per tur — limită dură în cod."

### 2. Contractul de tool + registry

```python
# src/tools/base.py
@dataclass
class ToolResult:
    """Rezultatul unui tool. `data` = structura completă (pt ctx.retrieval/validator);
    `llm_view` = reprezentarea COMPACTĂ trimisă înapoi modelului (max 6 produse × 8 câmpuri,
    principiul 'state = ref-uri, nu obiecte')."""
    ok: bool
    data: dict[str, Any]           # intern: produse complete, pt validator
    llm_view: str | dict           # ce vede modelul (trunchiat, fără PII)
    error: str | None = None

Tool = Callable[[TurnContext, ...], Awaitable[ToolResult]]

# semnătura uniformă (CLAUDE.md): async def tool(ctx, **params) -> ToolResult
TOOL_REGISTRY: dict[str, Tool] = {}            # nume → implementare
def enabled_tools(business) -> list[str]: ...  # per-business (settings/feature flags)
```

- **Per-business:** tool-urile sunt activate per tenant (`businesses.settings` sau o coloană
  dedicată). Un tenant beauty fără programări n-are `book_appointment`. Schemele OpenAI se
  generează DOAR pentru tool-urile active → prompt mai mic + suprafață redusă.
- **Argumente validate cu Pydantic** ÎNAINTE de execuție (ca triajul) — modelul poate halucina
  argumente; le respingem determinist, nu rulăm tool cu input invalid.
- **`business_id` injectat din `ctx`, NU din argumentele modelului** (principiul 7 + izolare):
  modelul NU primește și NU poate seta `business_id`. Orice query în tool = `where business_id
  = ctx.business.id`. RLS (`bot_runtime`) = plasa.

### 3. Bucla de tool-calling (design detaliat)

```python
# src/worker/stages/agent.py (rescris) — pseudo
async def agent_stage(ctx, deps):
    if route != SALES or deps.llm is None: return
    tools = tool_schemas(enabled_tools(ctx.business))   # prefix STATIC → prompt caching
    messages = [system, user(history, query)]
    retrieved = []                                      # acumulează pt validator
    for step in range(MAX_TOOL_CALLS):                  # cap DUR = 3
        resp = await deps.llm.chat_with_tools(messages, tools)
        if resp.tool_calls:
            # OpenAI poate cere MAI MULTE tool calls într-un singur pas → execută CONCURENT
            results = await gather(*[run_tool(ctx, deps, tc) for tc in resp.tool_calls])
            retrieved += collect_products(results)
            messages += tool_messages(resp.tool_calls, results)  # feed compact (llm_view)
            continue
        break                                            # model a dat text final
    else:
        # a atins capul de 3 → forțează un răspuns final FĂRĂ tools
        resp = await deps.llm.chat_with_tools(messages, tools=None)
    ctx.retrieval = RetrievalResult(products=dedupe(retrieved))
    reply = validate_or_retry(resp.text, ctx.retrieval)   # stagiul 8 extins
    ctx.set_reply(reply, products=_card_products(retrieved))
```

Decizii cheie:
- **Cap dur 3** în cod (nu în prompt). La atingere → un ultim apel `tools=None` care obligă text.
- **Execuție concurentă** a tool-call-urilor din același pas (`asyncio.gather`) → tai latența.
- **Adaptor nou** `chat_with_tools(messages, tools)` în `src.agent.llm` (singurul loc OpenAI):
  întoarce `{text | tool_calls}`. Folosește **Structured Outputs / strict function schemas**
  (OpenAI 2024+) → argumente valide din construcție, mai puține retry-uri.
- **Idempotența tool-urilor de scriere** (faza 2+): `cart_add`/`checkout_link` cu cheie
  idempotentă (turn_id) → un retry al buclei nu dublează coșul.

### 4. Integrarea cu pipeline-ul

- **`ctx.retrieval`** rămâne owner-ul tool-urilor (agentul îl acumulează). Stagiul 8 (validator)
  citește de acolo.
- **Validator EXTINS** (azi doar preț): conform CLAUDE.md stagiul 8 — (a) fiecare PREȚ din
  reply ∈ retrieval, (b) fiecare PRODUS menționat ∈ retrieval, (c) fiecare LINK ∈
  `products.product_url` (nu inventat). Invariant: ZERO prețuri/produse/linkuri halucinate.
  Retry 1 cu feedback → fallback determinist (păstrăm pattern-ul actual).
- **Buget de context impus în cod:** `llm_view` per tool ≤ 6 produse × 8 câmpuri; istoricul
  ≤ 8 mesaje (G6); nu trimitem obiecte complete înapoi modelului (principiul 8).
- **Prompt caching OpenAI:** prefixul (system + scheme de tool) e byte-identic per business →
  75-90% discount pe tokenii de input cache-uiți. De aceea schemele se generează STABIL
  (ordine fixă), nu dinamic per tur.
- **Cache semantic (G5b)** rămâne ÎNAINTE de agent: un „cremă ten uscat <80 lei" repetat se
  servește din cache (dynamic, price-check) fără să intre în bucla de tool-calling.
- **Observabilitate (principiul 10):** runner-ul emite `tool_call {name, latency_ms, ok}` și
  `agent_steps {n}` — FĂRĂ argumente cu PII (doar nume + hash). Stagiile nu știu că-s măsurate.

### 5. Taxonomia tool-urilor + fazare

| Tool | Tip | Tabele | Fază |
|---|---|---|---|
| `search_products` | read | products, embeddings | **1** (wrap pe cel existent) |
| `get_product_details` | read | products, product_review_summaries (D3) | **1** |
| `compare_products` | read | products (+variants) | **1** |
| `faq_lookup` | read | faqs (locale) | 1.5 (gol până la seed FAQ) |
| `check_order` | read | orders, shipments | 3 |
| `delivery_eta` | read | integrare curier/ERP | 3 |
| `reorder` | read | orders (contact) | 3 |
| `cart_add` | **write** | (state) | 2 |
| `checkout_link` | **write** | checkout_links (ref_code, atribuire) | 2 |
| `subscribe_back_in_stock` | **write** | back_in_stock_subscriptions | 4 |
| `book_appointment` | **write+extern** | appointments + Google Calendar | 4 |
| `request_human` | write | conversations (handoff) | ✅ există (gates) |

**Faze:**
- **Faza 1 — read core (acest PR):** framework + `search_products` + `get_product_details` +
  `compare_products`. Demo-vizibil (detalii + comparație cu rating/recenzii D3), zero write,
  zero regresie.
- **Faza 2 — bucla de bani:** `cart_add` + `checkout_link` (+ `webhook/orders.py` match
  `ref_code` → atribuire). Aici botul VINDE măsurabil.
- **Faza 3 — comenzi/logistică:** `check_order` + `delivery_eta` + `reorder` (rută `order`).
- **Faza 4 — programări/proactiv:** `book_appointment` + `subscribe_back_in_stock` (leagă de
  schedulerul proactiv).

### 6. Cost & latență

- **Cost:** un tur cu tool-calling = 1 (decizie) + N (răspuns la fiecare pas cu tools) + 1
  (final) apeluri mini. Cap 3 → până la ~4 apeluri/tur vs 1-2 azi → **~2-4× cost/tur de
  vânzare**. Atenuat de: (1) **prompt caching** pe prefixul static, (2) **cache semantic G5b**
  servește repetările fără agent, (3) cap dur 3, (4) doar `sales` ajunge la agent (triajul
  filtrează simple/order).
- **Latență:** mai multe round-trip-uri → p95 mai mare. Atenuat de execuția **concurentă** a
  tool-urilor din același pas + typing indicator instant (stagiul 9). Țintă: păstrează p95 sub
  pragul de UX prin a NU permite lanțuri de 3 apeluri seriale când 1 pas cu 2 tools paralele
  ajunge.

### 7. Eșecuri & degradare (principiul 6 — niciodată tăcere)

| Eșec | Tratare |
|---|---|
| Model alege tool inexistent / argumente invalide | Pydantic respinge → `ToolResult(ok=False, error)` feed înapoi → modelul reîncearcă (în cap) sau fallback |
| Tool aruncă (DB/extern) | `ToolResult(ok=False)` → bucla continuă / fallback determinist |
| Buclă infinită de tools | Cap DUR 3 → ultim apel `tools=None` (text forțat) |
| Validator pică de 2× | Fallback determinist (listă cu prețuri reale) — pattern actual |
| Fără cheie OpenAI / API jos | no-op → echo/fallback (degradare grațioasă existentă) |
| Tool de scriere + retry buclă | Cheie idempotentă (turn_id) → nu dublează |

### 8. Testare & evals

- **Tool-uri:** unit determinist (DB real în tranzacție rollback-uită, ca testele integration).
- **Bucla agent:** FAKE LLM care SCRIPTează `tool_calls` apoi text final (replay/mock, T140) →
  ZERO apeluri reale în CI. Testează: cap-3, execuție concurentă, feed-ul de rezultate,
  fallback.
- **Validator extins:** preț + produs + link halucinat → blocat.
- **Golden (`tests/golden/`):** conversații tool-driven (ex. „compară X cu Y" → un singur
  `compare_products`, răspuns grounded).
- **Evals (producție):** `conversation_evals` + `golden_tests` (LLM-as-judge + gate CI) — vezi
  Partea II §6. Critice când agentul ia ACȚIUNI: o regresie de selecție de tool trebuie prinsă
  înainte de prod.

---

## PARTEA II — ANALIZĂ DE PRODUCȚIE (2026)

> Întrebarea reală: ce ne trebuie ca să rulăm asta cu clienți reali în 2026, dincolo de
> tool-uri. Gap-urile sunt mai mult operaționale/legale decât de „feature".

### 1. Model & provider (2026)

- **Pinning + deprecare:** fixează versiunile de model (nu `-latest` în prod, exceptând
  moderation); urmărește calendarul de deprecare OpenAI; un model nou = re-rulează golden+evals
  înainte de switch (NU schimba orbește).
- **Structured Outputs / strict function-calling:** obligatoriu pentru argumentele tool-urilor
  → mai puține retry-uri de argumente invalide, comportament determinist.
- **Prompt caching:** prefix static (system + scheme) → discount mare; măsoară hit-rate-ul de
  cache de prompt în analytics.
- **Fallback de provider (reziliență):** opțional, un al doilea provider pentru `complete`/
  `chat_with_tools` la outage OpenAI. Cost de complexitate; de evaluat după primul client.
- **EU residency la model:** vezi §2 — în 2026 e și o decizie de cost (+~10% pe nano), și una
  legală.

### 2. Conformitate EU — AI Act + GDPR (2026, foarte relevant)

- **EU AI Act (în vigoare 2026):** un chatbot de vânzări = sistem cu **risc limitat** →
  obligație de **transparență (Art. 50)**: userul TREBUIE să știe că vorbește cu un AI.
  Disclosure vizibil (deja notat pentru web widget; trebuie și pe WhatsApp/Telegram — un rând
  în primul mesaj / la `/start`). Fără manipulare, fără dark patterns în tool-urile de checkout.
- **GDPR (deja proiectat, de finalizat):** PII DOAR în `channel_identities` (✅), redaction în
  loguri (✅), `gdpr_erase_contact` (✅ în schema, wrapper `src/gdpr/erase.py` ❌ de scris),
  **EU data residency** (Supabase regiune UE + OpenAI EU processing — NX-14, decizie datată),
  **registru de procesatori + DPA** (OpenAI/Meta/Supabase — NX-13), retenție prin drop de
  partiții (cleanup ❌).
- **Tool-urile ridică miza:** `book_appointment`/`checkout_link` ating date personale și
  acțiuni cu efect → minimizare (doar ce trebuie în argumente), consimțământ (`contacts.consent`)
  pentru proactiv, audit (`audit_log`) pe acțiunile de scriere.

### 3. Fiabilitate / SLO — gap-uri reale

| Capabilitate | Stare | De ce e critic în prod |
|---|---|---|
| Webhook ACK <50ms + retry Meta | ✅ | Meta face retry agresiv |
| Dedupe 2 straturi + outbox idempotent | ✅ | Zero dublări la retry |
| Dispatcher retry/backoff + dead-letter | ✅ | Livrare garantată |
| **Cost guard zilnic per business** | ❌ | **Anti-runaway**: o buclă/abuz poate exploda costul (tool-calling = 2-4× apeluri) |
| **Rate limit per user** | ❌ | Anti-spam/abuz (peste moderation) |
| **Lock per conversație** | ❌ | Scalare ORIZONTALĂ (>1 consumer) fără dezordine |
| **XAUTOCLAIM (consumer mort)** | ❌ | Mesaje „stuck" recuperate |
| Dead-letter pe dedupe claim-first | ❌ | Crash între claim și final = mesaj pierdut |

> Tool-calling-ul AMPLIFICĂ nevoia de **cost guard + rate limit**: fără ele, un singur user
> abuziv poate declanșa lanțuri de 3 apeluri mini la nesfârșit. Astea devin P0 înainte de
> primul client real.

### 4. Securitate

- **Izolare multi-tenant:** RLS + `bot_runtime` (NX-50/04 ✅), `business_id` din `ctx` NU din
  argumentele modelului. Un tool nu poate citi alt tenant nici dacă modelul halucinează un id.
- **Prompt injection prin tool args:** modelul mediază, dar argumentele sunt validate Pydantic
  + scoped pe `business_id` → injection nu trece granița de tenant; cel mai rău = un query
  prost în PROPRIUL tenant (zero rezultate, nu leak).
- **Tool-uri de scriere = suprafață nouă:** `checkout_link`/`cart_add` — validare strictă a
  argumentelor (produse reale, preț din catalog), audit_log, idempotență. NICIODATĂ acțiuni
  ireversibile fără confirmare (ex. nu plasează comanda; doar generează link).
- **Secrete:** `credentials_ref` (secret manager), nu în DB (✅ design). Webhook semnat (✅).
- **Moderation gate (NX-15 ✅)** + abuse blocklist — prima linie pe inbound.

### 5. Infra & scaling (2026)

- **Acum:** `docker compose` pe VPS (țintă), Supabase + Redis. OK pentru primii clienți.
- **Postgres:** pooling (Supavisor/pgbouncer) + cele 2 pool-uri NX-50 (bot_runtime vs admin);
  partiționare lunară (✅ messages/analytics) + drop de partiții (cleanup ❌).
- **Redis:** durabil AOF (✅); pentru prod serios — HA (Sentinel) sau managed.
- **Worker orizontal:** mai mulți consumeri în grup (✅ XREADGROUP) DAR cere **lock per
  conversație** (❌) pentru ordine. Webhook = stateless, scalează ușor.
- **Billing/usage:** `usage_daily` (rollup nocturn ❌) = sursa de adevăr pentru facturare +
  cost guard. Fără el, nu poți factura retainer-ul pe consum real.

### 6. Calitate / evals / observabilitate

- **Evals (P0 pentru un agent cu acțiuni):** `conversation_evals` + `golden_tests`
  (LLM-as-judge + gate CI). O regresie în selecția de tool (ex. cheamă `checkout_link` când
  trebuia `compare_products`) e invizibilă fără evals. De construit ÎMPREUNĂ cu fazele de tool.
- **Shadow mode:** un tool nou rulează în umbră (loghează ce AR fi făcut) înainte de live →
  măsori precizia selecției fără risc. (Și alimentează `intent_aliases` pt alias lookup.)
- **Observabilitate operațională (NX-03 ❌):** alerte pe consumer lag (`XINFO GROUPS`) + outbox
  depth; tracing per tur (turn_id → spans pe stagii); error tracking (Sentry); dashboard
  (Metabase NX-33). `analytics_events` (✅) e baza, dar nu înlocuiește APM.
- **Hallucination guard:** validatorul extins (preț+produs+link) = invariantul structural.
  Asta e diferențiatorul de încredere al produsului — de păstrat strict pe toate tool-urile.

### 7. Readiness business/ops

- **Onboarding:** `create_tenant.py` idempotent (NX-41 ❌) — business + canal + plafoane + FAQ
  seed + secret webhook comenzi. Devine API-ul de onboarding la scară.
- **Config per-tenant:** tool-uri active, `supported_locales` (G5c ✅), tarife Meta (NX-54 ❌),
  praguri cost/rate.
- **Human-in-the-loop:** consolă de agent (inbox) pentru handoff (`assigned_user_id` = cârlig
  ✅, UI ❌). Esențial: AI-ul escaladează, omul preia.
- **Runbooks + on-call:** ce faci la outage OpenAI / Redis / Supabase; cum repornești workerii;
  cum oprești un tenant (kill-switch `bot_active` ✅).

### 8. Drumul spre producție — done vs TODO

**✅ Gata (fundație solidă):** pipeline 9 stagii (RAG), webhook semnat + dedupe 2L, dispatcher
retry/dead-letter, RLS + bot_runtime (NX-50/04), gates + moderation (G5a/NX-15), cache semantic
(G5b), debounce (R1), detecție limbă (G5c), context builder (G6), catalog + embeddings + D3.

**❌ Înainte de primul client real (P0/P1):**
1. **Cost guard + rate limit** (amplificate de tool-calling) — P0.
2. **Lock per conversație + XAUTOCLAIM** (scalare) — P1.
3. **Evals + golden CI** (regresii de tool) — P1, în paralel cu fazele de tool.
4. **`usage_daily` rollup** (facturare + cost guard) — P1.
5. **Conformitate EU:** transparență Art. 50 pe canale + `gdpr/erase.py` + EU residency
   (NX-14) + registru DPA (NX-13) — P1, înainte de date reale.
6. **Observabilitate ops** (NX-03 alerte + tracing + Sentry) — P1.
7. **WhatsApp e2e** (canalul real, T013/T015 manual) + deploy VPS — P1.

**Tool-urile însele (acest epic):** valoare de produs, dar fundația de mai sus = ce face
diferența între „demo impresionant" și „serviciu pe care îl poți vinde și opera în 2026".

---

## 9. Recomandare de secvențiere

1. **Faza 1 tool-uri** (framework + read core) — acum, livrabil incremental.
2. **Cost guard + rate limit** — imediat după (tool-calling le face urgente).
3. **Evals + golden CI** — în paralel cu Faza 2 tool-uri.
4. **Faza 2 (bucla de bani)** + `usage_daily` rollup — valoare business măsurabilă.
5. **Conformitate EU + observabilitate ops + WhatsApp e2e** — poarta spre primul client.
6. **Faza 3/4 tool-uri** + lock per conversație (scalare) — pe măsură ce crește traficul.
