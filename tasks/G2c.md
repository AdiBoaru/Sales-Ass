# G2c — Cost guard + rate limit (stagiul 2, hardening)
**Owner:** S · **Faza:** P0 · **Zi/Ord:** după G7-1 (tool-calling amplifică nevoia) · **Branch:** `feat/G2c-cost-guard-rate-limit` · **Complexitate:** M · **Estimare:** 4h

## Goal
Două protecții realtime peste moderation (NX-15), cu contoare Redis: (1) **cost guard** zilnic
per business — peste plafon, dezactivează apelurile LLM scumpe pentru restul zilei (degradare
grațioasă pe straturi gratuite); (2) **rate limit** per contact — peste prag de mesaje/fereastră,
throttle. P0 acum că tool-calling-ul (G7-1) face 2-4× apeluri mini/tur → un singur user
abuziv poate exploda costul.

## Business Context
Fără cost guard, o buclă/abuz pe un agent cu tool-calling poate genera lanțuri de apeluri mini
la nesfârșit → factură OpenAI exploadată. Rate limit oprește spam-ul susținut (peste debounce-ul
R1, care doar coalescă bursturi legitime). Redis = guard-ul realtime; **facturarea reală rămâne
`usage_daily`** (rollup nocturn, sursa de adevăr) — contorul Redis e o estimare-plasă.

## Technical Description

### Modul `src/worker/limits.py` (contoare Redis, best-effort)
- `rate_limit_count(redis, business_id, contact_id, window_s) -> int`: `INCR rate:{biz}:{contact}`
  + `EXPIRE` la primul (fereastră fixă). Întoarce noul count.
- `cost_over_budget(redis, business_id, cap_usd) -> bool`: `GET cost:{biz}:{YYYYMMDD}` ≥ cap.
- `cost_add(redis, business_id, amount_usd)`: `INCRBYFLOAT` + `EXPIRE` ~2 zile.
- `estimate_turn_cost(events, cost_triage_usd, cost_agent_usd) -> float`: estimare grosieră din
  evenimente — `intent_detected` (triaj nano) + `agent_recommended`/`tool_call` (agent mini,
  ×(1+nr tool_call)). Compatibil cu agentul RAG ȘI cu tool-calling (G7-1). Sursa de FACTURARE
  rămâne `usage_daily`; aici e doar plasă.
Toate cheile includ `business_id` (principiul 7). Eșec Redis → fail-open (nu blochează).

### Rate limit — în Gates (stagiul 3, înaintea moderării/LLM)
`gates_stage` capătă poarta `_rate_limited` DUPĂ handoff, ÎNAINTE de moderation (check Redis
ieftin înaintea apelului de moderation):
- count ≤ prag → trece.
- count == prag+1 (tocmai a depășit) → `set_reply(THROTTLE_MSG)` (o SINGURĂ dată) → early-exit.
- count > prag+1 (deja peste) → `halt_silent("rate_limited")` (tăcere, ca blocklist).
Emite `rate_limited {count}`. Fără Redis / dezactivat → no-op.

### Cost guard — în worker (processor), nu gate
`handle_turn` rezolvă LLM-ul prin `_llm_within_budget(ctx, redis, business)`:
- dacă `cost_over_budget(business.daily_cost_cap_usd or settings.daily_cost_cap_usd)` → emite
  `cost_guard_tripped` + întoarce **None** → pipeline-ul rulează cu `llm=None` (triaj/agent
  degradează grațios, cache L1 încă servește; altfel fallback). NU oprește gates (handoff/
  blocklist încă funcționează).
- altfel întoarce LLM-ul normal.
După pipeline: `_record_turn_cost(redis, business_id, ctx, llm_used)` adaugă estimarea în contor
DOAR dacă LLM-ul a fost disponibil (peste buget → nu adăugăm). `_cache_writeback` primește
același LLM guardat (peste buget → write-back sărit).

### De ce gate vs worker
Rate limit = decizie de „răspunde botul?" (early-exit) → se potrivește în Gates, lângă
moderation/blocklist. Cost guard = „cât de scump procesăm?" → dezactivează LLM-ul în worker
(reutilizează degradarea `llm=None`), păstrând gates intacte.

### Câmpuri TurnContext scrise
`ctx.reply`/`ctx.halt` (rate limit, via Gates — owner Gates). Cost guard scrie doar evenimente.

## Principii CLAUDE.md aplicabile
- **P7:** cheile Redis + query-urile includ `business_id`. **P6:** rate limit = excepție
  documentată (abuz), ca blocklist; cost guard degradează, nu tace tot.
- **P12:** evenimentele logă `count`/`cap_usd`, NICIODATĂ corpul/PII.
- **Stage 2 (CLAUDE.md):** „rate limit per user + cost guard zilnic per business (contor Redis;
  sursa de facturare = usage_daily)". Exact asta.

## Implementation Steps
1. `src/worker/limits.py`: cele 4 funcții (Redis best-effort).
2. `config.py` + `.env.example`: `cost_guard_enabled`, `cost_triage_usd`, `cost_agent_usd`,
   `rate_limit_enabled`, `rate_limit_max`, `rate_limit_window_seconds`.
3. `gates.py`: `THROTTLE_MSG` + `_rate_limited` + integrare în `gates_stage`.
4. `processor.py`: `_llm_within_budget` + `_record_turn_cost`; wire în `handle_turn`
   (+ `_cache_writeback` cu LLM guardat).
5. Teste: `tests/test_limits.py`, rate limit în `tests/test_gates.py`, cost guard
   `tests/test_cost_guard.py`.
6. `ruff check . && ruff format . && pytest -x -q` verde.

## Files To Create / Files To Modify
**Create:** `src/worker/limits.py` · `tests/test_limits.py` · `tests/test_cost_guard.py`
**Modify:** `src/config.py` · `.env.example` · `src/worker/stages/gates.py` ·
`src/worker/processor.py` · `tests/test_gates.py`

## Database Changes
**None.** Doar Redis (contoare). `businesses.daily_cost_cap_usd` există (plafon per business).

## API Changes
None.

## Events de emis (analytics)
- `rate_limited` {count} — la depășirea pragului per contact.
- `cost_guard_tripped` {cap_usd} — când businessul e peste plafonul zilnic (LLM dezactivat).

## Dependencies
**G5a** (gates_stage) — în main. **G2b** (processor/handle_turn + PipelineDeps.redis) — în main.
**NX-15** (pattern contor Redis în gates) — în main. (G7-1 nu e blocant — estimarea e
compatibilă cu RAG și tool-calling.)

## Out of Scope
- **usage_daily rollup** (facturare reală din analytics_events) — task separat.
- **Cost real din token usage OpenAI** — v1 = estimare din evenimente (plasă, nu facturare).
- **Alerte la depășire** (Slack) — NX-03.
- **Plafon/prag per-vertical sau dinamic** — v1 = config global + `businesses.daily_cost_cap_usd`.
- **Mesaj „sistem solicitat" dedicat la cost guard** — v1 = degradare pe fallback existent.

## Definition of Done
- [ ] `rate_limit_count` incrementează + setează EXPIRE la primul (fake redis) — test.
- [ ] Rate sub prag → gates trece; == prag+1 → throttle reply; > prag+1 → halt silent — test.
- [ ] Rate limit dezactivat / fără redis → no-op — test.
- [ ] `cost_over_budget` true → `_llm_within_budget` întoarce None + emite `cost_guard_tripped` — test.
- [ ] Sub buget → LLM normal; fără redis → guard off (LLM normal) — test.
- [ ] `estimate_turn_cost`: triaj+agent (RAG) și triaj+agent×(1+n) (tool-calling); fără LLM → 0 — test.
- [ ] `_record_turn_cost` adaugă în contor doar dacă LLM-ul a fost folosit — test.
- [ ] `ruff check . && ruff format . && pytest -x -q` verde.

## Test Cases
**Happy Path:**
1. Rate sub prag (count=5, max=20) → `_rate_limited` False, pipeline continuă.
2. `estimate_turn_cost` cu `intent_detected`+`agent_recommended` → triaj+agent; cu 2× `tool_call`
   → triaj + agent×3.

**Edge Cases:**
1. count == max+1 → throttle reply (o dată); count == max+5 → halt silent (fără reply).
2. Cost peste buget → LLM None, dar gates (bot_active/handoff) încă rulează.
3. `_record_turn_cost` cu `llm_used=False` → nu adaugă (peste buget nu acumulează).

**Failure Cases:**
1. Redis aruncă la `incr`/`get` → fail-open (rate limit/cost guard nu blochează turul).
2. `cost_add` aruncă → loghează, turul a răspuns deja.

> Teste fără Redis real: fake redis (incr/expire/get/incrbyfloat) + monkeypatch `get_llm`/
> `cost_over_budget`. ZERO apeluri reale.

## Cost
Adaugă 1-2 operații Redis O(1) per tur (incr/get). Economisește costul LLM peste plafon
(degradare) + oprește abuzul (rate limit) → NET reduce cheltuiala. Aici se materializează
protecția financiară pe care tool-calling-ul (G7-1) o face urgentă.
