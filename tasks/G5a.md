# G5a — Gates (stagiul 3): bot_active + handoff + risc → request_human
**Owner:** S · **Faza:** MVP · **Zi/Ord:** după R2 · **Branch:** `feat/G5a-gates` · **Complexitate:** M · **Estimare:** 4h

## Goal
Primul stagiu real de control din pipeline: înainte de orice LLM, decidem
DETERMINIST dacă botul are voie să răspundă. Trei porți — `bot_active` (kill-switch
per conversație), `handoff_until` (un om a preluat), risc (pattern-uri care cer
escaladare la om) — fiecare cu early-exit. Gate-ul e **agnostic de canal**: decide
doar „răspunde botul?"; CUM arată handoff-ul (tăcere pe WhatsApp/TG vs agent live
pe webchat) e treaba marginilor, nu a gate-ului. Introduce și mecanismul de
**tăcere intenționată** — singura excepție conștientă de la principiul 6.

## Business Context
Model de agenție managed: când botul nu trebuie să răspundă (operatorul preia, sau
clientul e nervos/cere om), tăcerea corectă e mai valoroasă decât un răspuns de bot.
Gate-ul îi dă operatorului controlul (preia/oprește botul pe o conversație) și
escaladează automat cazurile sensibile (plângeri, cereri legale) — protejează
relația cu clientul și reputația magazinului.

## Technical Description

### Poziție în pipeline
`gates_stage` devine PRIMUL în `DEFAULT_STAGES`, înaintea triajului:
`[gates_stage, triage_stage, agent_stage, fallback_stage]`. Rulează cod pur,
ZERO LLM. Orice poartă declanșată → early-exit (cu reply de bot SAU tăcere).

### Cele 3 porți (în ordine)
```python
async def gates_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    # 1. kill-switch: botul e oprit pe ACEASTĂ conversație → tăcere (omul scrie)
    if not ctx.bot_active:
        ctx.halt_silent("bot_inactive")
        return
    # 2. handoff activ: un om a preluat până la handoff_until → tăcere
    if ctx.handoff_until and ctx.handoff_until > datetime.now(tz=UTC):
        ctx.halt_silent("handoff_active")
        return
    # 3. risc → escaladează la om + UN mesaj de tranziție, apoi botul tace
    reason = detect_risk(ctx.message.body)
    if reason:
        await request_human(deps.conn, ctx, reason, source="risk")
        ctx.set_reply("Te conectez cu un coleg, revin imediat 🙂")
        return
```

### Tăcere intenționată (excepția de la principiul 6)
Azi `fallback_stage` scoate MEREU un reply („niciodată tăcere"). În handoff/bot
inactiv tăcerea e CORECTĂ. Adăugăm un mecanism explicit, nu un reply gol:
- `TurnContext.halt: bool` + metoda `halt_silent(reason: str)` care setează
  `halt=True` și emite `gate_halt {reason}`.
- `run_pipeline` face early-exit pe `ctx.reply is not None OR ctx.halt` →
  `fallback_stage` NU mai e atins.
- `processor.handle_turn`: la final, dacă `ctx.reply is None` (inclusiv pe halt)
  → NU scrie în `outbox` (deja se întâmplă). Cu `ctx.halt` doar logăm explicit
  „tăcere intenționată (reason)", ca să distingem de „bug: tur fără reply".

### request_human (cârlig agnostic, web-ready)
```python
async def request_human(conn, ctx, reason, *, source="risk", assigned_user_id=None):
    # fereastră de tăcere; agentul (consola, task viitor) o poate prelungi/curăța
    await set_handoff(conn, ctx.business.id, ctx.conversation_id,
                      window_minutes=HANDOFF_WINDOW_MIN,   # config, default 45
                      risk_flag=reason, assigned_user_id=assigned_user_id)
    ctx.emit("handoff_requested", reason=reason, source=source)
```
`set_handoff` (query nou, `db/queries/conversations.py`):
```sql
update conversations
   set handoff_until = now() + make_interval(mins => $3),
       risk_flags    = array_append(risk_flags, $4),
       assigned_user_id = coalesce($5, assigned_user_id)
 where business_id = $1 and id = $2
```
`assigned_user_id` rămâne **cârlig**: G5a NU auto-asignează (nu există pool de
agenți încă) — îl umple consola de agent (task de margine). `handoff_until` +
`risk_flags` + evenimentul sunt partea activă acum.

### detect_risk (pattern-uri RO, fără LLM)
Listă de regex/substring normalizate (lowercase, fără diacritice), grupate pe motiv:
- `human_request`: „vreau sa vorbesc cu un om", „operator", „agent uman", „om real".
- `legal_complaint`: „avocat", „anaf", „protectia consumatorului", „reclamatie",
  „instanta", „te dau in judecata".
Întoarce primul motiv găsit sau `None`. Extensibil (per-business în `settings` =
follow-up). NU folosește LLM (principiul 2).

### Câmpuri din conversație → TurnContext
`get_or_create_conversation` întoarce deja `bot_active`, `handoff_until`,
`risk_flags` (vezi `_CONV_COLS`). `processor.handle_turn` le pune pe `ctx`:
`bot_active`, `handoff_until` (owner: processor, la construirea ctx). Gate-ul
scrie DOAR `ctx.halt` (+ `ctx.reply` pe risc).

## Principii CLAUDE.md aplicabile
- **P1 (pipeline liniar):** gate = încă un stagiu în ordine fixă, early-exit, fără loop.
- **P2 (LLM doar triaj+agent):** detect_risk e 100% determinist; gate-ul nu cheamă LLM.
- **P3 (un owner per câmp):** gate scrie `ctx.halt`/`ctx.reply`; processor scrie
  `ctx.bot_active`/`ctx.handoff_until`. Niciun câmp cu doi scriitori.
- **P6 (niciodată tăcere) — excepție DOCUMENTATĂ:** `halt_silent` e singura cale
  de tăcere, intenționată, în handoff/bot inactiv (omul răspunde). Restul rămâne
  „mereu ceva iese".
- **P7 (business_id pe tot):** `set_handoff` are `where business_id = $1`.
- **P12 (PII):** corpul mesajului se inspectează pentru risc dar NU se loghează;
  logurile gate-ului conțin doar `reason` + id-uri.

## Implementation Steps
1. `models.py`: `TurnContext.halt: bool = False`, `bot_active: bool = True`,
   `handoff_until: datetime | None = None`; metoda `halt_silent(reason)`.
2. `runner.py`: early-exit pe `ctx.halt or ctx.reply`; adaugă `gates_stage` PRIM
   în `DEFAULT_STAGES`.
3. `worker/stages/gates.py`: `gates_stage`, `detect_risk`, `request_human`,
   `HANDOFF_WINDOW_MIN`/`RISK_PATTERNS`.
4. `db/queries/conversations.py`: `set_handoff(...)`.
5. `processor.handle_turn`: setează `ctx.bot_active`/`ctx.handoff_until` din `conv`;
   log explicit „tăcere intenționată" când `ctx.halt`.
6. `config.py`: `handoff_window_minutes` (default 45) + `.env.example`.
7. Teste (vezi mai jos) + `ruff check . && ruff format . && pytest -x -q`.

## Files To Create / Files To Modify
**Create:** `src/worker/stages/gates.py` · `tests/test_gates.py`
**Modify:** `src/models.py` · `src/worker/runner.py` · `src/worker/processor.py` ·
`src/db/queries/conversations.py` · `src/config.py` · `.env.example`

## Database Changes
None (DDL). `set_handoff` = UPDATE nou pe `conversations`, `where business_id = $1
and id = $2`. `bot_active`, `handoff_until`, `risk_flags`, `assigned_user_id`
există deja în schema_v2.

## API Changes
None.

## Events de emis (analytics)
- `gate_halt` {reason: 'bot_inactive' | 'handoff_active'} — pe tăcere intenționată.
- `handoff_requested` {reason, source: 'risk' | 'manual'} — pe escaladare la om.
(Channel-agnostic; consola de agent va emite ulterior `handoff_resolved`.)

## Dependencies
G2b (runner + processor + DEFAULT_STAGES) — în main. G3 (triage_stage) — în main.
Niciuna din PR-urile deschise (NX-04/53/R2) nu e necesară.

## Out of Scope
- **Canalul webchat** (ingestie WS/SSE → envelope) — task de margine separat.
- **WebChannelSender** (livrare real-time via Redis pub/sub) — task separat.
- **Consola de agent** (UI operator, mesaje `human_agent`, auto-asignare
  `assigned_user_id`, `handoff_resolved`) — task separat.
- **Detecția de limbă** (RO/HU/EN) — separat, perechea lui G5b (cache).
- **Identity resolution** — deja în `get_or_create_contact`.
- **Media routing** (STT/Vision) și **straturile gratuite** (alias/cache, G5b/G5c).

## Definition of Done
- [ ] `bot_active=false` → `ctx.halt`, NICIUN reply, NICIUN rând în outbox (test).
- [ ] `handoff_until` în viitor → `halt`; în trecut → pipeline-ul continuă (test).
- [ ] mesaj cu pattern de risc → `request_human` apelat + reply de tranziție +
  `handoff_until` setat în DB (test).
- [ ] mesaj normal → gate-ul nu oprește (continuă la triaj).
- [ ] `detect_risk` nu cheamă LLM (zero apeluri în test).
- [ ] `gates_stage` e primul în `DEFAULT_STAGES`.
- [ ] `ruff check . && ruff format . && pytest -x -q` verde.

## Test Cases
**Happy Path:**
1. `bot_active=False` → `gates_stage` setează `ctx.halt`, `ctx.reply is None`;
   `run_pipeline` se oprește înainte de triaj (triaj-mock neatins).
2. Mesaj normal („caut o cremă") → `detect_risk` None, gate nu setează halt/reply.

**Edge Cases:**
1. `handoff_until` = acum − 1 min (expirat) → gate NU oprește (botul reia).
2. `detect_risk` pe text cu diacritice/uppercase („Vreau să vorbesc cu un OM") →
   normalizat → match `human_request`.

**Failure Cases:**
1. Risc detectat dar `set_handoff` aruncă (conflict/DB) → eroarea se propagă
   (turul eșuează zgomotos, NU trimite reply de bot pe un handoff ne-setat).
2. `detect_risk` pe input gol/None → None (fără excepție), pipeline continuă.

> Teste fără DB unde se poate: `detect_risk` = unit pur; `gates_stage` cu
> `request_human`/`set_handoff` monkeypatch-uite (verifică apelul + reply), plus
> un test mic de integrare pe `set_handoff` (handoff_until chiar setat în DB).
> ZERO apeluri LLM.

## Cost
LLM: **ZERO** (gate determinist). Efect NET pozitiv pe cost: oprește tururile în
handoff/bot inactiv ÎNAINTE de triaj+agent → mai puține apeluri nano/mini pe
conversațiile preluate de om.
