# Agent stage — arhitectură țintă (modularizarea `agent.py`)

Status: **plan aprobat 2026-07-06**. Referință: ARCH-REVIEW-2026-07 §5.5 (scor agent 6.5),
rafinat după citirea completă a `agent_stage`. Taskuri: NX-142 (done) → NX-143 → NX-144.

## 1. Ce este `agent_stage` de fapt

Stagiul 7 al pipeline-ului liniar (CLAUDE.md) este el însuși un **pipeline intern de 6 faze**.
Un singur `TurnContext` intră; stagiul poate seta `ctx.reply` / `ctx.rich_reply` și iese.

| Fază | Rol | Natură | Azi (în `agent.py`) |
|---|---|---|---|
| **A · ADMIT** | guard: `llm`? rută `SALES/ORDER`? `query` nevid? | pur | `agent_stage` intro |
| **B · PRE-INTENT** | intenții deterministe *înainte* de LLM (`link`/`compare`/`show_more`) → early-exit, **$0 inferență** | pur | `_handle_link_intent`, `_handle_compare_intent`, gating inline |
| **C · PREPARE** | `merge_constraints` (owner: `ctx.state.search_constraints`) + asamblare prompt/context/hints | pur + I/O | `merge_constraints`, `_filters_hint`, `_lead_score_hint`, `_load_prompt_inputs` |
| **D · GENERATE** | `run_tool_loop` (LLM) **sau** paginare `show_more` → text brut + produse/linkuri/sume acumulate | LLM | closure `execute` + `deps.llm.run_tool_loop` |
| **E · SHAPE** | intenții deterministe *după* tools: checkout-fallback, cross-sell, `attr_query`, `cheaper`, rehidratare → decide `products` + **modul de răspuns** | pur + I/O | blocul mare post-buclă |
| **F · RENDER** | alege renderer (comparison / rich / prose / order-grounded / fallback) → validează → retry → fallback → `ctx.set_*reply` | orchestrare | `_finalize`, `_finalize_grounded`, `_finalize_rich`, ramurile finale |

## 2. Descoperirea cheie: deterministul e în DOUĂ locuri

Audit-ul §5.5 propune un singur `deterministic.py`. Dar logica deterministă are **două roluri
distincte**, cu intrări și poziții diferite:

- **B (pre-loop)** = „pot răspunde *fără LLM deloc*" — intrare `query`+`state`, early-exit.
- **E (post-loop)** = „date fiind produsele întoarse de tools/LLM, *cum modelez răspunsul*" —
  intrare `query`+`retrieved`+`state`.

**E împletit cu F (unele ramuri din E fac `_finalize_rich` + `return` direct) ESTE planner-ul
implicit.** Deci `deterministic.py` ține DOAR B; post-loop-ul (E) devine **`planner.py`**.

## 3. Cele două contracte load-bearing

Refactorul stă sau cade pe două value-objects care fac seam-urile explicite (P3 — un owner/câmp):

### `ToolRun` (seam D→E) — `src/agent/tool_executor.py`
Înlocuiește cei **10 acumulatori `nonlocal`** din closure-ul `execute`:
`retrieved`, `generated_links`, `grounded_prices`, `compared`, `order_views`, `order_gated`,
`added_cart`, `search_rel`, `failed_commerce`, `checkout_offer`. Devin câmpuri explicite ale unui
dataclass cu metoda `.execute(name, args)`. **Invariant de securitate:** `business_id` din `ctx`,
niciodată din `args` — și seam-ul pe care se așază NX-150 (tool authorization).

### `ResponsePlan` (seam E→F) — `src/agent/planner.py`
Value-object **mic**, NU orchestrator:
```
ResponsePlan(
    mode: Literal["comparison","rich","prose","order","fallback"],
    products: list[dict],            # setul final (dedup/rehydrated/cheaper/cross-sell)
    offers: PlanOffers,              # checkout_offer, cross_sell flag, commerce_note
    relevance: RelevanceMeta | None, # off-category signal pt compose
    reply_override: Reply | None,    # ramurile care răspund direct (cheapest_already/no_more/login)
)
```
Faza E produce planul determinist; faza F îl **randează** (`finalize.py`). Asta desface
interleaving-ul E/F: „decide ce" separat de „randează cum".

## 4. Module țintă (rafinare a celor 8 din audit)

```
src/agent/
  stage.py         # A→C wiring + orchestrare fazelor. Țintă < 120 linii.
  deterministic.py # B: DOAR pre-loop (link/compare/show_more) + regexuri + gating
  tool_executor.py # D: ToolRun (execute + acumulatori + _safe_tool_args + tool selection)
  planner.py       # E: build_plan(ctx, deps, run) -> ResponsePlan  [NX-144]
  finalize.py      # F: render(plan) → validate/retry/fallback (folosește validator + compose)
  validator.py     # ✅ NX-142
  fallbacks.py     # replici pure: _deterministic_reply/_no_result/_cheapest/_no_more + _dedupe/_card
```

Abateri **intenționate** de la audit:
- **`tool_loop.py` NU se creează** — bucla LLM rămâne în `deps.llm.run_tool_loop` (P2/P4).
- **`composer.py` nu se duplică** — `src/worker/compose.py` e deja composer-ul (flattening text/rich).
- **post-loop → `planner.py`, nu `deterministic.py`** (vezi §2).

## 5. Ownership (CLAUDE.md P3/P5/P10)

| Câmp / efect | Owner (fază) | Notă |
|---|---|---|
| `ctx.state.search_constraints` | C (`merge_constraints`) | scriitor unic; persistat de processor |
| `ctx.state_patch` (cart) | D (`ToolRun.execute` via tool `state_patch`) | tool-urile mută, executor-ul acumulează |
| `ctx.retrieval` | E (planner, la final) | scris o dată, după shaping |
| `ctx.reply` / `ctx.rich_reply` / comparison | F (finalize) **sau** E via `reply_override` | tot prin `set_*reply` → Sender/outbox (P5) |
| `tool_call` / `constraints_merged` / `cheaper_followup` … | faza care le produce, prin `ctx.emit` (turn_id) | observabilitate din locul acțiunii (P10) |

## 6. Plan de taskuri, ordine & regulă de risc

| Task | Livrează | Comportament |
|---|---|---|
| **NX-142** ✅ | `validator.py` | byte-identic |
| **NX-143** | `deterministic.py` (pre-loop) + `tool_executor.py` (`ToolRun`) + `fallbacks.py` (replici pure) | **byte-identic** |
| **NX-144** | felia 1: `planner.py` (`ResponsePlan`, faza E) + `finalize.py` (faza F) — **byte-identic**; felia 2: response templates per intent + answer-completeness checks — **singura schimbare de comportament** | mixt (delimitat pe felii) |

Regula de risc (audit: „nu recomand rescriere mare dintr-o dată"):
- **Serial pe `agent.py`** — NX-142/143/144 ating același fișier; NU în paralel (backlog 2026-07 §exec).
- **Byte-identic până la planner** — NX-143 + felia 1 din NX-144 sunt mutări pure; golden/hallucination
  neschimbate. Singura schimbare de comportament (templates/completeness) e izolată în felia 2 NX-144,
  în spatele kill-switch-urilor existente de calitate.
- **Fiecare extract are teste izolate** înainte de a fi cablat în `stage.py`.

Deblochează: **NX-150** (tool authz pe `tool_executor.py`), **NX-145** (golden multi-tur peste
`stage.py` subțire).
