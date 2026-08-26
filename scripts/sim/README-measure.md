# Cât din bugetul de output mănâncă raționamentul

> **Rezolvat pe 2026-08-26.** Măsurătoarea de mai jos a fost făcută, a găsit un defect activ în
> producție, iar plafonul a fost SCOS (`LLM_MAX_TOKENS_AGENT` implicit **0**). Documentul rămâne
> pentru cine repune vreodată un plafon, sau vrea să reproducă măsurătoarea pe alt model.

## Ce s-a măsurat și ce s-a găsit

`MODEL_AGENT=gpt-5.6-luna` raționează implicit, iar `LLM_REASONING_EFFORT_AGENT=high` se aplică pe
apelurile **fără tool-uri** — planul MainBrain, repair-ul lui, și calea rich din `finalize`. Tokenii
de raționament se scad din **același** `max_completion_tokens` ca textul.

Producție, tenant demo, 2026-08-26:

| Mesaj | Gândire | Rezultat |
|---|---|---|
| „caut un produs pentru ten gras" | **512** | încape în 800 → rich OK (616 chars JSON) |
| „pai daca fac dus mi se usuca pielea" | **1312** | depășește → conținut GOL → proză degradată |

**2 din 4** apeluri rich au întors gol. Turul căzut a răspuns cu o cremă de **mâini** la o cerere de
rutină de **față** — proza liberă pierduse constrângerea din primul mesaj.

Eșecul nu arăta ca un eșec: 200 + conținut gol → `content or "{}"` → `{}` → niciun `except` →
`rich_downgraded {all-items-dropped-by-membership}`, adică **altă cauză decât cea reală**.

## De ce plafonul a fost scos, nu ridicat

NX-125 îl pusese ca „un completion patologic să nu scape de ceiling". Premisa s-a rupt: un cap de
tokeni nu previne o buclă — bucla e în **runde de model** (`run_tool_loop`), nu în lungimea unui
răspuns. Deci plafonul plătea preț de calitate pentru o protecție pe care n-o oferea.

Ce rămâne ceiling, în unitatea corectă:

| Plafon | Stare |
|---|---|
| Cost ZILNIC per business (`DAILY_COST_CAP_USD`) | activ — pre-check în `processor`, taie LLM-ul |
| Timeout per apel (`LLM_TIMEOUT_S`, 30s) | activ, pe clientul `AsyncOpenAI` |
| `LLM_CALL_CAP_MS` (8s/încercare) | activ doar sub `TURN_DEADLINE_ENABLED` |

## Cum reproduci măsurătoarea

Nu mai e nevoie de overrides: `reasoning_tokens` se raportează la fiecare tur.

```bash
python scripts/sim/server.py            # alt terminal
python scripts/sim/say.py --sender "sim:measure:plafon" --text "caut un ser pentru ten gras"
```

Linia de consum arată perechea:

```
💸 in 4.231 (cached 3.840 = 91% din input) · out 1.312 (gândire 1.180 din 1.312) · 3 apeluri
```

Numărul brut nu spune nimic; ce contează e cât a rămas pentru text.

Gotcha: dacă rulează deja un server pe `:8099`, îl testezi pe ăla, cu settings-urile vechi.
`netstat -ano | grep ":8099"` înainte.

## Semnale

| Semnal | Unde | Ce înseamnă |
|---|---|---|
| `reasoning_tokens` vs `tokens_out` | event `llm_usage` | cât a rămas pentru text |
| `llm_output_truncated_empty` | `turn_latency` | **cineva a repus un plafon și e prea mic** |
| `llm_reasoning_tokens_unreported` | idem | furnizorul nu raportează gândirea ⇒ cifra de mai sus e falsă |
| `llm_param_unsupported_temperature` | idem | confirmă că apelul chiar raționează |

`reasoning_tokens` e **subset** al lui `tokens_out`, nu supliment. Nu-l aduna, nu-l factura separat.

## Dacă repui un plafon

Trebuie să acopere gândire **plus** text. Pe măsurătoarea de mai sus, turele conversaționale cereau
peste 1300 doar pentru gândire — deci orice cifră sub ~3000 e un pariu. Urmărește
`llm_output_truncated_empty`: dacă apare, plafonul taie răspunsuri, nu bucle.

## Planul care decide ce randează frontendul

Sub `SINGLE_BRAIN_ENABLED` + `CONVERSATION_TRACE_ENABLED`, `conversation_traces.diagnostics`
primește:

| Cheie | Ce e |
|---|---|
| `brain_plan_raw` | JSON-ul BRUT al modelului, înainte de validare |
| `brain_plan` | planul VALIDAT — sursa pentru `ground_answer` → `render_v2` |
| `brain_plan_failures` | codurile pe care a picat prima încercare |
| `brain_repair_raw` | ce a întors repair-ul |
| `brain_plan_fallback` | motivul pentru care s-a renunțat la plan |

Fără single brain, calea v1 depune `rich_raw` în același loc — util la fel: un `rich_raw` gol
înseamnă că modelul n-a emis nimic, indiferent ce spune eticheta de degradare.

```sql
select turn_id, client_text,
       diagnostics->'brain_plan' as plan,
       diagnostics->'rich_raw'   as rich,
       diagnostics->>'rich_downgraded' as motiv
from conversation_traces
where business_id = '6098812a-50fc-44bd-a1ba-bc77e6399158'
order by created_at desc limit 5;
```
