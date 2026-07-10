# Design brief: conexiunile DB nu se țin cât aștepți LLM/API (2026-07)

> **Teza centrală (citește-o întâi).** Nu vrem un „refactor frumos". Vrem **schimbarea
> proprietății resursei**: conexiunea DB nu mai aparține *turului*, ci *operației scurte*. Un tur
> poate dura 20s, dar DB-ul trebuie atins în ferestre de milisecunde/zeci de milisecunde.
>
> Doc scris ca să fie citibil de cineva FĂRĂ context. Structură + invariante = design canonic
> (agreat). Secțiunile „Ground-truth" și corecțiile ⚠️ = verificat față de codul real (2026-07).

---

## 1. Problema

Azi `handle_turn` ține **o singură conexiune tenant-scoped** din `bot_pool` (max=10) pe tot turul:
`load → gates → limbă → free layers → triaj → agent/tool loop → commit → aftercare`.

Tur de sales măsurat (`scripts/sim/pool_probe.py`, reproductibil):
- **held** (conn pinned): ~18.9s
- **db_active**: ~4.0s (pe dev/probe; ~31 round-trip-uri × ~130ms latență de rețea Windows→Supabase)
- **idle-held**: ~**79%**, majoritar așteptând OpenAI (agent_stage singur ~8.7s)

Pe dev, `db_active` e inflat de latența de rețea; pe VPS co-locat (conn directă, ~1ms/query)
`db_active` scade la ~30-200ms → idle% urcă spre ~98%. **Direcția e sigură; magnitudinea reală
e mai mare decât cifra de dev.**

### Eșuează prin CONCURENȚĂ, nu prin debit mediu

Framing-ul „tururi/secundă" e înșelător. Cheia e **câte conversații stau simultan „într-un tur"**.
Legea lui Little: `L = λ × W`, cu `W = 18.9s`:

| Sarcină (λ) | Conexiuni ocupate simultan (L) | din pool=10 |
|---|---|---|
| 0.1 tururi/s | ~1.9 | 19% |
| 0.3 tururi/s | ~5.7 | **57%** |
| 0.5 tururi/s | ~9.5 | **95% — plin** |

Fiecare tur squattează o conexiune ~19s (79% doar așteptând rețeaua), deci **poolul se umple din
DURATĂ, nu din VOLUM**. ~10 conversații concurente = stare normală într-o seară aglomerată sau la
un broadcast promoțional. La media pilotului ești ok; la primul burst cu câțiva tenanți (poolul e
partajat) ești peste plafon — exact când contează să nu pice botul.

**Concluzie:** conexiunea DB trebuie să devină resursă efemeră, folosită doar pentru
query/tranzacție. Timpul de așteptare LLM/API trebuie să trăiască în AFARA poolului DB.

---

## 2. Arhitectura țintă

Introdu un **provider DB tenant-scoped**, nu un `deps.conn` viu lung.

```python
# Model curent
PipelineDeps(conn=conn, redis=redis, llm=llm, media=media)

# Model țintă
PipelineDeps(db=db_provider, redis=redis, llm=llm, media=media)

# Unde db_provider e:
async with deps.db() as conn:      # checkout scurt, doar cât ține operația
    ...                            # query/tranzacție
# conn eliberat la ieșirea din with; între operații — ZERO conn ținut
```

Fiecare checkout TREBUIE să treacă tot prin `tenant_conn`, deci:
`app.business_id` setat de fiecare dată · assert de izolare (NX-04) rulează · GUC resetat la
release · RLS rămâne defense-in-depth.

**Punte de compatibilitate (⚠️ obligatorie — vezi Corecția 4):** `deps.db()` trebuie să facă
fallback la `yield self._static_conn` când un conn static e injectat (teste). Astfel un stagiu
migrat la `deps.db()` trece în testele legacy care dau `conn=fake`, fără rescriere simultană a
33 de fișiere de test.

---

## 3. Invariante dure (orice soluție le respectă)

1. **Niciodată nu ține conn peste un apel LLM/API/sleep.** Lista COMPLETĂ de metode `LLMClient`
   (⚠️ Corecția 1 — brief-ul inițial acoperea doar 3): `embed`, `classify_json`, `complete`,
   `complete_schema`, `run_tool_loop`, `moderate`, `describe_image` (Vision), + STT/transcribe +
   orice fetch media extern + backoff/retry.
2. **Commit-ul Sender rămâne atomic.** outbound message(s) + outbox row(s) + patch state +
   `mark_inbound_completed` = O singură tranzacție explicită.
3. **Fără model mixt nested.** Nu ține conn-ul vechi în timp ce achiziționezi altul via `deps.db()`.
   Un segment e ori legacy-conn, ori provider-based — niciodată ambele.
4. **business_id explicit pe fiecare query.** RLS e plasă, nu filtrul primar de tenant.
5. **Fără `pool.acquire()` direct în hot path.** Munca de tenant trece doar prin `tenant_conn`/provider.
6. **Idempotența intactă.** `claim_inbound` înainte de write-uri · `mark_inbound_completed` doar
   după procesarea terminală cu succes · idempotency key outbox neschimbat.

---

## 4. Ground-truth: cele 11 stagii reale și ce țin pe conn

Pipeline-ul real (`src/worker/runner.py :: DEFAULT_STAGES`) e mai bogat decât „free layers →
triage → agent". Ce ține fiecare stagiu (verificat în cod):

| # | Stagiu | DB (deps.conn) | LLM / I/O extern (idle-held) | Off-conn? |
|---|---|---|---|---|
| 1 | `gates` | block_contact, request_human | **moderate + Vision (describe_image) + download media Meta** ⚠️ | **DA — ratat de brief** |
| 2 | `language` | set_conversation_locale (write) ⚠️ | euristic (fără LLM) | scurt |
| 3 | `clarify_resume` | citește/consumă slot | — | scurt |
| 4 | `greeting` | — (determinist) | — | n/a |
| 5 | `alias` | match exact intent_aliases | — | scurt |
| 6 | `cache` | lookup + touch/evict semantic | **embed** (dacă semantic) | **DA** |
| 7 | `faq` | lookup semantic | **embed** | **DA** |
| 8 | `triage` | citește category slugs | **classify_json** | **DA** |
| 9 | `handoff` | request_human | — | scurt |
| 10 | `agent` | build prompt (categories+aliases) + tool DB | **run_tool_loop + thinking între tools** | **DA — grosul** |
| 11 | `fallback` | — | — | n/a |

**Idle-held recuperabil, în ordine de mărime:** agent (thinking, ~8.7s) > gates pe voice/image
(Whisper/Vision/download) > cache/faq embed > triage classify > aftercare (post-commit).

---

## 5. Strategia de migrare (Opțiunea C ca drum → converge la Opțiunea A)

Incremental, fiecare fază livrabilă + măsurabilă pe probe. Nu refactoriza tot hot path-ul deodată.

### Faza 0 — Instrumentare (PRIMA, ieftin, non-invaziv)
Metrici de PROD (probe-ul a măsurat HOLD pe dev; prod trebuie WAIT):
`db_pool_acquire_wait_ms` · `db_conn_held_ms` · `db_query_active_ms` (dacă practic) ·
pool in-use/idle/waiters · queue delay/tur · LLM wait/stagiu. **p50/p95/p99, nu doar medii.**
**Trigger de îngrijorare:** p95 acquire wait > 100-250ms în burst · p99 → secunde · in-use lipit
de max pe tururi LLM-heavy.

### Faza 1 — Aftercare off-conn (risc MINIM)
Post-tur (cache write-back, summarizer, profil/facts) după commit → checkout-uri scurte proaspete.
LLM fără conn; fiecare write DB cu checkout/tranzacție proprie; eșecuri best-effort + observabile.
Risc minim fiindcă reply-ul e DEJA commis. (Deja izolat în helpere `_cache_writeback` etc.)

### Faza 2 — Batching la load (latență + round-trips)
`history` + `summary` + `facts` (+ data_version) sunt citiri INDEPENDENTE → `asyncio.gather` pe
checkout-uri separate SAU combină SQL unde e mai curat. Scade latența, `db_active`, ferestrele de acquire.

### Faza 3 — Gates off-conn (⚠️ adăugat — ratat de brief; mare pe voice/image)
Separă DB de LLM/media în gates: citește flagurile (bot_active/handoff) cu conn scurt → eliberează
→ `moderate`/Vision/**download media** fără conn → re-acquire scurt doar pt block_contact/request_human.

### Faza 4 — Free layers fără conn ținut (cache/FAQ/triaj)
- **Cache:** canonicalize în memorie → lookup exact conn scurt → release → (semantic) `embed` fără
  conn → lookup semantic conn scurt → touch/evict conn scurt.
- **FAQ:** canonicalize → `embed` fără conn → lookup semantic conn scurt.
- **Triaj:** citește category slugs conn scurt → release → `classify_json` fără conn → (chips)
  citește categorii-frați conn scurt.

### Faza 5 — Agent: prompt inputs off-conn
Înainte de LLM: încarcă category names + routing aliases (+ orice input de prompt) → **eliberează
conn ÎNAINTE de `run_tool_loop`**.

### Faza 6 — Agent tool loop conn-per-op (grosul, risc maxim)
```python
async def search_products_tool(ctx, deps, args):
    async with deps.db() as conn:
        return await search_products(conn, ctx.business.id, ...)
```
Fiecare tool: acquire scurt → DB → release → întoarce la loop-ul LLM. **Thinking-ul între tool
calls nu ține niciun conn.**

### Faza 7 — Processor pe granițe unit-of-work (coeziv, FĂRĂ explozie de clase)
`load_turn(db, event) -> ctx + metadata` · `run_pipeline(ctx, deps_fără_conn_viu)` ·
`commit_reply(db, ctx, metadata)` · `run_aftercare(db, ctx)`. Funcții coezive, nu 4 clase cosmetice.

---

## 6. Model de scalare + starea finală

**Curent:** `conns necesare ≈ tururi/s × secunde_tur_complet`. La 33/s × 18.9s → **~624 conexiuni
ținute**. Imposibil.
**Țintă:** `conns necesare ≈ tururi/s × secunde_db_active`. La 33/s × 0.05-0.20s → **~1.7-6.6
conexiuni active**. Ordinul de mărime se schimbă complet. (Sizing real cere p95/p99 + burst tests.)

**Stare finală:** un tur poate fi in-flight 10-20s, dar conn ținut doar în ferestre scurte de
query/tranzacție. **Dimensiunea poolului controlează concurența DB reală, nu concurența LLM.**
Concurența LLM se controlează separat (semafoare/rate limits); coada/backpressure protejează
tenanții unul de altul.

---

## 7. Controale suplimentare pentru businessuri mari (ortogonal — nu le rezolvă conn-per-op)

Pentru ~2000 useri/min mai trebuie: limite de concurență per-business · lock/ordonare
per-conversație · semafor concurență LLM · metrici queue depth · retry/backoff/circuit breakers ·
cost guard per business · scheduling corect între tenanți · cache/free layers înainte de LLM ·
timeout cu fallback grațios · garanția „niciodată tăcere". (Multe sunt TODO-uri deja notate în CLAUDE.md.)

---

## 8. Riscuri / de revizuit

- **Granițe de tranzacție:** niciun helper existent nu presupune conn/sesiune stabilă între apeluri
  multiple (verifică înainte de conn-per-op).
- **Concurență optimistă:** LLM-ul rulează între load și commit → state-ul conversației se poate
  schimba. `state_version` RĂMÂNE garda (optimistic lock) — nu o slăbi.
- **Migrare mixtă:** evită fazele care țin un conn și achiziționează altul (invariant 3).
- **⚠️ Suprafață de teste (Corecția 4):** `PipelineDeps(conn=...)` × **114 în 33 fișiere**. Necesită
  puntea de compat (`deps.db()` yield la conn static injectat), nu rescriere big-bang.
- **RLS:** fiecare checkout de provider trece prin `tenant_conn`; zero pool raw.
- **Performanță:** overhead acquire/release de măsurat, dar a_teptat << LLM wait.

---

## 9. Întrebări pentru Codex (a doua părere)

1. **Puntea de compat:** `deps.db()` cu fallback la conn static injectat pentru teste — sau
   preferi un fake provider explicit în teste (mai curat, dar atinge 33 de fișiere)?
2. **Ordinea:** gates off-conn (Faza 3) contează doar pe voice/image — o urci mai devreme dacă
   pilotul are trafic vocal, sau o lași după free layers?
3. **Invariant 3 (fără model mixt) sub `conn` + `db` coexistente pe `PipelineDeps`:** cum îl
   enforce-uim mecanic în timpul migrării (un stagiu migrat nu mai trebuie să primească conn viu)?
4. **Agentul (Faza 6) primul sau ultimul?** E grosul (8.7s) dar și riscul maxim; feliile 1-5 dau
   destul headroom pentru pilot, sau atacăm direct agentul?
5. **Ratăm ceva?** (helper cu conn stabil între apeluri, tranzacție lungă în pipeline, STT/Whisper
   necartografiat în gates.)

---

## 10. Decizie recomandată

**Implementează Opțiunea C** (migrare incrementală: instrumentare → aftercare → load batching →
gates off-conn → free layers/triaj → agent prompt inputs → agent tools conn-per-op). **Documentează
Opțiunea A ca arhitectură finală:** cod de pipeline/tool care depinde de un **provider DB
tenant-scoped**, nu de o conexiune vie lungă.

## 11. Referințe
Cod: `src/worker/processor.py`, `src/db/connection.py`, `src/worker/runner.py` (`PipelineDeps`,
`DEFAULT_STAGES`), `src/worker/stages/*`, `src/tools/*`, `src/agent/llm.py`.
Măsurare: `scripts/sim/pool_probe.py`. Istoric: NX-50/NX-04/NX-86, `docs/db_connections.md`.
