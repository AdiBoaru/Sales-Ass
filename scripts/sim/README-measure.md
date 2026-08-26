# Măsurătoare: cât din plafonul de output mănâncă raționamentul

**De ce.** `MODEL_AGENT=gpt-5.6-luna` raționează implicit (`reasons_by_default=True` în
`_MODEL_PROFILES`), iar `LLM_REASONING_EFFORT_AGENT=high` se aplică pe apelurile **fără tool-uri**
— adică exact planul MainBrain (`complete_schema`) și repair-ul lui. Tokenii de raționament se scad
din **același** `max_completion_tokens` ca textul (măsurat, vezi `llm._note_truncation`), deci cei
800 impliciți se împart între gândire și JSON-ul structurat. Nimeni n-a calculat cât ia fiecare.

Epuizarea plafonului **nu arată ca o eroare**: furnizorul întoarce 200 cu conținut gol,
`complete_schema` îl transformă în `{}` (`content or "{}"`), iar turul degradează pe altă cauză
decât cea reală.

## De ce overrides pe comandă și nu în `.env`

`LLM_MAX_TOKENS_AGENT` e citit de suita de teste: `test_agent_call_includes_sampling_params` și
`test_agent_fara_tooluri_pastreaza_effortul_configurat_si_pierde_temperature` asertează forma
parametrilor pe settings-urile REALE. Un `0` pus în `.env` face imposibilă rularea testelor cât
timp măsori. Variabilele de mediu bat `.env` în pydantic-settings, deci prefixul de mai jos e
suficient.

## Comanda

Cele patru valori se schimbă **împreună**. Scoțând plafonul de TOKENI, constrângerea care leagă
devine plafonul de TIMP (`llm_call_cap_ms`, 8s pe încercare, activ fiindcă `TURN_DEADLINE_ENABLED`
e pornit local) — deci fără ridicarea lui ai schimba doar un răspuns truncat pe un timeout.

```bash
# 1. Pornește driverul warm CU overrides (Git Bash).
#    ATENȚIE (gotcha cunoscut): dacă rulează deja un server pe :8099, îl testezi pe ăla, cu
#    settings-urile VECHI. Oprește-l întâi și verifică portul.
netstat -ano | grep ":8099" || echo "portul e liber"

LLM_MAX_TOKENS_AGENT=0 \
LLM_CALL_CAP_MS=60000 \
TURN_HARD_DEADLINE_MS=60000 \
LLM_TIMEOUT_S=90 \
CONVERSATION_TRACE_ENABLED=true \
python scripts/sim/server.py
```

```bash
# 2. În alt terminal: un tur real de vânzare.
python scripts/sim/say.py --sender "sim:measure:plafon" --text "caut un ser pentru ten gras"
```

Linia de consum arată acum defalcarea:

```
💸 in 4.231 (cached 3.840 = 91% din input) · out 1.312 (gândire 1.180 din 1.312) · 3 apeluri · ...
```

`gândire 1.180 din 1.312` ⇒ cu plafonul implicit de 800 apelul n-ar fi avut loc: raționamentul
singur depășea bugetul, iar JSON-ul ar fi ieșit gol.

**Rulează pe credite OpenAI reale.** Câteva tururi ajung — nu e nevoie de suită.

## Ce compari

| Cifră | De unde | Ce înseamnă |
|---|---|---|
| `reasoning_tokens` vs `tokens_out` | linia `say.py`, event `llm_usage` | cât a rămas pentru text |
| `llm_output_truncated_empty` | `turn_latency` degradations | plafonul s-a atins ⇒ răspuns gol |
| `llm_output_cap_disabled` | idem | confirmă că rulezi FĂRĂ plafon |
| `llm_param_unsupported_temperature` | idem | confirmă că apelul de schemă chiar raționează |

## Planul care decide ce randează frontendul

`CONVERSATION_TRACE_ENABLED=true` persistă în `conversation_traces.diagnostics`:

| Cheie | Ce e |
|---|---|
| `brain_plan_raw` | JSON-ul BRUT al modelului, înainte de validare |
| `brain_plan` | planul VALIDAT (`AnswerPlanV2`) — sursa pentru `ground_answer` → `render_v2` |
| `brain_plan_failures` | codurile pe care a picat prima încercare |
| `brain_repair_raw` | ce a întors repair-ul |
| `brain_plan_fallback` | motivul pentru care s-a renunțat la plan |

Lanțul complet: `AnswerPlanV2` → `_attach_grounding` → `ground_answer` →
`src/channels/web/render_v2.py` → blocuri (`text` + `variant`, `comparison`, `action_row`, carduri
cu `image`/`actions`). Adică planul chiar decide ce componentă randează widgetul.

Înainte de asta, pe calea creierului unic planul nu se scria nicăieri: `agent_stage` iese la
`run_main_brain` înainte de `finalize`, deci nici măcar `rich_raw` nu se producea.

```sql
select turn_id,
       diagnostics->'brain_plan'         as plan_validat,
       diagnostics->'brain_plan_failures' as esecuri,
       diagnostics->'brain_plan_fallback' as motiv_fallback
from conversation_traces
where business_id = '6098812a-50fc-44bd-a1ba-bc77e6399158'
order by created_at desc
limit 5;
```

## După măsurătoare

`LLM_MAX_TOKENS_AGENT` implicit rămâne **800** în cod. Schimbarea lui e o decizie separată, luată
pe cifra de mai sus — nu un efect secundar al acestui experiment.
