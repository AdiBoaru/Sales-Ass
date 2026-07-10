# Design brief: conexiunile DB nu se țin cât aștepți LLM/API (2026-07)

> **Teza centrală.** Nu vrem un „refactor frumos". Vrem **schimbarea proprietății resursei**:
> conexiunea DB nu mai aparține *turului*, ci *operației scurte*. Un tur poate dura 20s, dar DB-ul
> trebuie atins în ferestre de milisecunde. **Corolar (adăugat după review Codex):** azi poolul DB
> de 10 e *frâna accidentală* a întregului sistem — o scoatem din rolul greșit de bodyguard al
> OpenAI-ului și punem o **frână explicită (admission control)** în locul corect, înaintea LLM-ului.
>
> **Status:** direcție APROBATĂ de Codex (review 2026-07). Structură + invariante = design canonic.
> Secțiunile ⚠️ / ✅ = verificate în cod.

> **Intuiție (analogie).** Ospătar = conexiune DB, cuptor = OpenAI, client = conversație. Azi
> ospătarul stă blocat lângă cuptor 20 min cât se face mâncarea → 10 ospătari, 10 clienți, gata.
> Corect: ia comanda, o duce la bucătărie, pleacă la alți clienți, revine doar să servească. DAR:
> dacă eliberezi ospătarii, nu trimite 500 de comenzi într-o bucătărie care gătește 30 deodată →
> pui un **manager la intrare** (semafor) care lasă max N comenzi active. Asta construim.

---

## 1. Problema

`handle_turn` ține **o singură conexiune tenant-scoped** din `bot_pool` (max=10) pe tot turul:
`load → gates → limbă → free layers → triaj → agent/tool loop → commit → aftercare`.

Tur de sales măsurat (`scripts/sim/pool_probe.py`, reproductibil):
- **held** (conn pinned): ~18.9s · **db_active**: ~4.0s (dev; ~31 round-trip × ~130ms rețea) ·
  **idle-held**: ~**79%**, majoritar așteptând OpenAI (agent_stage ~8.7s).

Pe VPS co-locat (conn directă, ~1ms/query) db_active → ~30-200ms, idle% → ~98%. Direcția e sigură;
magnitudinea reală e mai mare decât pe dev.

### Eșuează prin CONCURENȚĂ, nu prin debit
Cheia nu e „tururi/secundă", ci **câte conversații stau simultan „într-un tur"**. Little: `L = λ × W`,
`W = 18.9s`:

| Sarcină (λ) | Conexiuni ocupate simultan (L) | din pool=10 |
|---|---|---|
| 0.1 tururi/s | ~1.9 | 19% |
| 0.3 tururi/s | ~5.7 | **57%** |
| 0.5 tururi/s | ~9.5 | **95% — plin** |

Poolul se umple din DURATĂ, nu din VOLUM. ~10 conversații concurente = stare normală la un burst promoțional.

### ✅ Poolul de 10 = admission control ACCIDENTAL (verificat în cod)
Workerul e **concurent între expeditori**: debouncer-ul spawnează `asyncio.create_task(_flush_later)`
per expeditor ([debounce.py:65](../src/worker/debounce.py#L65)) → fiecare flush → `process_event` →
lock per conversație ([consumer.py:157](../src/worker/consumer.py#L157)) → `tenant_conn` + `handle_turn`
([consumer.py:172](../src/worker/consumer.py#L172)). **Spawnarea e NEMĂRGINITĂ** (lock-ul serializează
doar *aceeași* conversație). Deci:

```
50 expeditori simultan → 50 flush tasks → 50 handle_turn
  → 10 intră în tenant_conn (rulează) · 40 blochează pe pool.acquire()
```

**Poolul de 10 e singura frână de concurență din sistem.** Nu doar „protecție DB" — e admission
control implicit. **Consecință critică:** conn-per-op FĂRĂ o frână de înlocuire *scoate singura
frână* → 50 handle_turn ajung simultan la OpenAI → DB liber, dar LLM/cost/memorie explodează.

---

## 2. Arhitectura țintă — TREI straturi

Corecția centrală post-Codex: separăm clar trei preocupări care azi sunt încurcate în poolul de 10.

| Strat | Ce face | Scope |
|---|---|---|
| **1. DB ownership fix** | conn scurtă per operație; LLM/API fără conn | ACEST epic (ținta) |
| **2. Admission control minim** | semafor global de tururi concurente (+ opțional per-business) | ACEST epic (frâna obligatorie) |
| **3. Fair scheduling complet** | coadă per tenant, fairness, circuit breakers, rate shaping | **EPIC SEPARAT** |

**Forma bună a fluxului:**
```
Redis stream / debouncer → ADMISSION CONTROL bounded → handle_turn fără conn lung
  → DB provider per operație scurtă → LLM/API fără conn → commit atomic scurt → outbox dispatcher
```

### 2.1 DB provider (nu `deps.conn` viu)
```python
# Curent
PipelineDeps(conn=conn, redis=redis, llm=llm, media=media)
# Țintă
PipelineDeps(db=db_provider, redis=redis, llm=llm, media=media)
async with deps.db() as conn:   # checkout scurt, doar cât ține operația
    ...                          # între operații (LLM) — ZERO conn ținut
```
Fiecare checkout trece tot prin `tenant_conn` → `app.business_id` setat · assert izolare (NX-04) ·
GUC resetat · RLS defense-in-depth.

**Punte de compat (⚠️ obligatorie + cu ieșire mecanică):** `deps.db()` face fallback la
`yield` la un conn static injectat (teste legacy, `PipelineDeps(conn=...)` × 114 în 33 fișiere). DAR:
- **guard CI** care PICĂ dacă `PipelineDeps(conn=` apare în `src/` non-test după ce infra aterizează
  (face „deprecated ASAP" mecanic, nu aspirațional — rezolvă și invariantul „fără model mixt");
- **testele NOI folosesc provider fake explicit**, nu conn static.

---

## 3. Invariante dure

1. **Niciodată conn ținut peste LLM/API/sleep.** Lista COMPLETĂ `LLMClient` (⚠️ verificată):
   `embed`, `classify_json`, `complete`, `complete_schema`, `run_tool_loop`, `moderate`,
   `describe_image` (Vision) + STT + fetch media extern + backoff/retry.
2. **Commit Sender atomic.** outbound + outbox + patch state + `mark_inbound_completed` = 1 TX
   ([processor.py:728-835](../src/worker/processor.py#L728)).
3. **Fără model mixt nested.** Un segment e ori legacy-conn, ori provider — niciodată ambele.
4. **business_id explicit pe fiecare query.** RLS = plasă, nu filtru primar.
5. **Fără `pool.acquire()` direct în hot path.** Doar `tenant_conn`/provider.
6. **Idempotență intactă.** `claim_inbound` înainte de write-uri · `mark_inbound_completed` doar la
   succes terminal · idempotency key outbox neschimbat.
7. **Admission (nou):** semaforul se ia ÎNAINTE de secțiunea LLM-heavy, **fără conn DB ținut**, și
   **NU în interiorul `tenant_conn`** (altfel repeți exact problema). Niciun apel LLM în afara
   graniței de admission.
8. **Concurență optimistă:** LLM rulează între load și commit → `state_version`
   ([conversations.py:108](../src/db/queries/conversations.py#L108)) RĂMÂNE garda optimistic-lock.

---

## 4. Ground-truth: cele 11 stagii reale și ce țin pe conn

| # | Stagiu | DB (deps.conn) | LLM / I/O extern (idle-held) | Off-conn? |
|---|---|---|---|---|
| 1 | `gates` | block_contact, request_human | **moderate + Vision + download media Meta** | **DA** |
| 2 | `language` | set_conversation_locale (write) | euristic (fără LLM) | scurt |
| 3 | `clarify_resume` | consumă slot | — | scurt |
| 4 | `greeting` | — | — | n/a |
| 5 | `alias` | match exact intent_aliases | — | scurt |
| 6 | `cache` | lookup + touch/evict | **embed** (dacă semantic) | **DA** |
| 7 | `faq` | lookup semantic | **embed** | **DA** |
| 8 | `triage` | citește category slugs | **classify_json** | **DA** |
| 9 | `handoff` | request_human | — | scurt |
| 10 | `agent` | prompt inputs + tool DB | **run_tool_loop + thinking între tools** | **DA — grosul** |
| 11 | `fallback` | — | — | n/a |

> ⚠️ **Notă gates (corecție Codex):** `bot_active`/`handoff_until` sunt DEJA în `ctx` (încărcate în
> processor la [processor.py:665](../src/worker/processor.py#L665), înainte de pipeline). Gates NU
> citește flaguri din DB → formulare corectă: **moderation/Vision/download fără conn; doar
> `block_contact` + `request_human` cu checkout scurt.**

---

## 5. Strategia de migrare (Opțiunea C → converge la Opțiunea A)

Incremental, fiecare fază livrabilă + măsurabilă pe probe. Nu refactoriza tot hot path-ul deodată.

### Faza 0A — Instrumentare (PRIMA, ieftin)
Metrici PROD, **nu doar DB** (probe-ul a măsurat HOLD pe dev; prod trebuie WAIT + queue + LLM):
`db_pool_acquire_wait_ms` · `db_conn_held_ms` · pool in-use/idle/waiters · `turn_admission_wait_ms` ·
`turn_inflight` · LLM concurrency/wait per stagiu · per-tenant wait. **p50/p95/p99.**
Trigger: p95 acquire wait > 100-250ms în burst · p99 → secunde · in-use lipit de max pe tururi LLM.

### Faza 0B — Infra `deps.db()` + compat bridge (fără migrare de stagii)
Doar abstracția provider + puntea de compat + guard CI + teste mici. Zero stagii mutate încă.

### Faza 0C — Frâna de admission (plasă, ÎNAINTE de a elibera poolul) ⚠️ obligatorie
Semafor global de tururi concurente (configurabil, inițial conservator) + opțional per-business.
Introdus AICI, nu la Faza 6 — fiindcă deja aftercare/cache/triage off-conn încep să elibereze poolul,
iar frâna trebuie să existe ca plasă înainte ca ceva să crească concurența efectivă LLM. **DoD jos.**

### Faza 1 — Aftercare off-conn (risc MINIM)
Post-tur (cache write-back, summarizer, profil/facts) → checkout-uri scurte proaspete; LLM fără conn;
fiecare write DB cu TX proprie; best-effort + observabil. Reply-ul e DEJA commis → risc minim.

### Faza 2 — Batching la load (fără gather implicit ⚠️ corecție Codex)
`history`+`summary`+`facts`(+data_version) sunt independente, DAR: în prod (query ~1ms) `gather` pe
checkout-uri separate nu cumpără nimic și crește presiunea instantanee pe pool (un tur cere 3
conexiuni deodată). **Prefer:** combină SQL unde e natural → citiri secvențiale pe UN checkout scurt.
`gather` DOAR pentru operații lente/independente, DUPĂ metrici.

### Faza 3 — Gates off-conn (mare pe voice/image)
Separă: moderation/Vision/**download media** fără conn → re-acquire scurt doar pt
`block_contact`/`request_human`. (NU „citește flaguri" — sunt în ctx.)

### Faza 4 — Free layers fără conn (cache/FAQ/triaj)
Cache: canonicalize în memorie → lookup exact conn scurt → release → (semantic) `embed` fără conn →
lookup semantic conn scurt → touch/evict conn scurt. FAQ: canonicalize → `embed` fără conn → lookup
conn scurt. Triaj: citește slugs conn scurt → release → `classify_json` fără conn → (chips) frați conn scurt.

### Faza 5 — Agent: prompt inputs off-conn
Încarcă category names + routing aliases → **eliberează conn ÎNAINTE de `run_tool_loop`**.

### Faza 6 — Agent tool loop conn-per-op (grosul, risc maxim)
```python
async def search_products_tool(ctx, deps, args):
    async with deps.db() as conn:
        return await search_products(conn, ctx.business.id, ...)
```
Fiecare tool: acquire scurt → DB → release. Thinking-ul între tool calls nu ține niciun conn.
**Prerechizit dur: Faza 0C (semaforul) TREBUIE livrată înainte de asta.**

### Faza 7 — Curățare: elimină `deps.conn` din pipeline/tool code
Guard CI devine hard-fail. `PipelineDeps` rămâne doar cu `db` provider.

---

## 6. DoD — frâna de admission (Faza 0C)

```
Înainte de orice fază care lasă multe tururi să ruleze fără conn DB ținut, adaugă turn admission bounded:
- semafor global de tururi inbound, configurabil
- semafor opțional per-business, dacă business_id e cunoscut
- metric: turn_admission_wait_ms
- metric: turn_inflight
- metric: admission_rejected_or_deferred
- NICIUN conn DB ținut cât aștepți semaforul
- NICIUN apel LLM în afara graniței de admission
- semaforul NU e în interiorul tenant_conn — se ia înainte de secțiunea LLM-heavy, fără conn ținut
```

---

## 7. Model de scalare + stare finală
**Curent:** `conns ≈ tururi/s × secunde_tur_complet`. 33/s × 18.9s → **~624 conexiuni**. Imposibil.
**Țintă:** `conns ≈ tururi/s × secunde_db_active`. 33/s × 0.05-0.20s → **~1.7-6.6 conexiuni**.
**Stare finală:** dimensiunea poolului controlează concurența DB reală; concurența LLM se controlează
SEPARAT (semafor); coada/backpressure protejează tenanții. Workerii și poolul scalează independent.

---

## 8. Riscuri / de revizuit
- **⚠️ Debouncer nemărginit (finding separat, datorie latentă):** task per expeditor via `create_task`
  fără semafor. Pt pilot, semaforul de admission e suficient. Pt scară: debouncer → buffer per-sender
  → enqueue în coadă internă BOUNDED → N workeri interni consumă (fiecare respectă admission). Epic separat.
- **Granițe de tranzacție:** niciun helper nu presupune conn/sesiune stabilă între apeluri (verifică).
- **Suprafață teste:** `PipelineDeps(conn=...)` × 114 în 33 fișiere → punte compat, nu rename big-bang.
- **RLS:** fiecare checkout provider prin `tenant_conn`; zero pool raw.
- **Performanță:** overhead acquire/release de măsurat, dar << LLM wait.

---

## 9. Verdict Codex (review 2026-07) — RESOLVED
> „Aprob direcția: conn-per-op e targetul corect, dar DOAR împreună cu bounded admission control.
> Poolul DB nu mai e frâna sistemului; introducem o frână explicită înainte de LLM-heavy turn
> execution. Fairness complet per tenant = epic separat, dar semaforul minim e prerechizit pt Faza 6
> și ideal intră din Faza 0B/0C."

Toate cele 5 întrebări deschise → rezolvate: (1) compat bridge + guard CI; (2) gates după free
layers, mai devreme dacă traficul vocal contează; (3) enforcement mecanic prin guard CI; (4) agentul
ultimul, dar semaforul mai devreme; (5) debouncer = datorie separată.

## 10. Decizie
**Opțiunea C** (0A metrici → 0B infra provider → **0C admission semafor** → aftercare → load batching
→ gates → free layers → agent prompt → agent tools → curățare). **Opțiunea A = arhitectura finală.**
Fairness per-tenant complet = **epic separat** cu dependență explicită.

## 11. Referințe
`src/worker/processor.py`, `src/db/connection.py`, `src/worker/runner.py`, `src/worker/consumer.py`,
`src/worker/debounce.py`, `src/worker/stages/*`, `src/tools/*`, `src/agent/llm.py`.
Măsurare: `scripts/sim/pool_probe.py`. Istoric: NX-50/NX-04/NX-86/NX-85, `docs/db_connections.md`.
