"""Pre-flight pentru aprinderea creierului unic: acceptă modelul `tools` + `json_schema` ÎMPREUNĂ?

## De ce există fișierul ăsta

`SINGLE_BRAIN_ENABLED=true` mută compunerea răspunsului de pe `run_tool_loop` (proză) pe
`run_tool_loop_structured` (`src/agent/llm.py`), iar diferența NU e doar forma ieșirii: al doilea
trimite `tools` ȘI `response_format={"type": "json_schema", ...}` în ACELAȘI request.

Pe `gpt-5.6-luna`, fiecare jumătate e dovedită în producție, separat:
  • `json_schema` singur — calea rich (`complete_schema`) rulează azi pe luna;
  • `tools` singure — bucla de vânzare v1 rulează azi pe luna, cu `reasoning_effort=none` forțat.
Combinația lor n-a fost măsurată niciodată. `_MODEL_PROFILES` nici nu o cunoaște: tabelul de
capabilități știe doar de `reasoning_effort` și `temperature`.

Miza e asimetrică, de aceea proba e separată de orice altceva. `_with_retry` tratează 4xx non-429
ca TERMINAL, iar `agent_stage` înghite excepția și cade pe fallback. Deci un refuz al furnizorului
nu s-ar vedea ca o eroare: ar arăta ca un bot care răspunde politicos și nu mai vinde nimic, cu
triajul (nano) intact deasupra, adică un sistem care pare sănătos. S-a întâmplat exact așa pe
2026-08-24 (`bbb77b3`) și a doua oară cu `max_tokens` (PR #133).

## Ce probează, mai exact

Payload-ul REAL, nu unul de jucărie: schema de plan pe care o cere MainBrain
(`plan_schema_for_model()`) plus schemele REALE ale tool-urilor (`tool_schemas`). O schemă de
jucărie ar putea trece acolo unde cea reală pică — `strict: True` refuză construcții pe care un
obiect cu două câmpuri nu le are (adâncime, `anyOf`, opționale), iar planul V2 le are pe toate.

Mesajul e minim și cere explicit modelului să NU cheme niciun tool: ne interesează dacă requestul
e ACCEPTAT, nu ce răspunde. Un tur real ar costa ordine de mărime mai mult fără să adauge nimic la
întrebarea asta.

## Ce NU probează

Calitatea răspunsurilor sub creierul unic. Aia e a lui `scripts/sim/single_brain_drive.py --live`
și a gate-ului NX-246. Aici răspundem la o singură întrebare binară: pleacă requestul sau nu.

## Modurile (NX-320)

  * `--mode brain` (implicit): `tools` + `json_schema` pe chat, cererea creierului unic (mai sus);
  * `--mode loop`: `tools` singure pe chat, cererea buclei v1 de AZI (`none` forțat). E proba care
    contează la o schimbare de `MODEL_AGENT`, fiindcă asta e calea care servește clienții;
  * `--mode responses`: `tools` pe `/v1/responses` CU raționament (`--effort`, implicit `low`),
    cererea pe care o măsoară replay-ul NX-320. Cere un `function_call` real, ca să verifice și că
    răspunsul se poate citi, nu doar că requestul a trecut.

## Rulare (consumă credite — câteva sute de tokeni)

    python scripts/preflight_structured_tools.py
    python scripts/preflight_structured_tools.py --mode loop
    python scripts/preflight_structured_tools.py --mode responses --effort low
    python scripts/preflight_structured_tools.py --model gpt-5.4-nano   # altă familie
    python scripts/preflight_structured_tools.py --dry-run             # $0: doar construiește

Ieșire: `PASS` (se poate aprinde flagul) sau `FAIL` + mesajul EXACT al furnizorului.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":  # consola Windows e cp1252; raportul are diacritice
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent.answer_plan_runtime import plan_schema_for_model  # noqa: E402
from src.agent.llm import get_llm, model_profile  # noqa: E402
from src.agent.tool_definitions import tool_schemas  # noqa: E402
from src.config import get_settings  # noqa: E402

#: Tool-urile trimise în probă. Nu toate: cele două care poartă formele grele din registru
#: (`enum` completat per tenant, `anyOf` pe opționale). Dacă furnizorul respinge combinația, o
#: respinge deja pe astea.
_PROBE_TOOLS = ("search_products", "get_product_details")

#: Mesajul probei. Cere explicit un răspuns fără tool call: verificăm ACCEPTAREA requestului, nu
#: comportamentul modelului. Dacă totuși cheamă un tool, tot am aflat ce voiam (requestul a trecut).
_SYSTEM = (
    "Ești un asistent de test. Răspunde folosind EXACT schema cerută, cu valori goale sau "
    "neutre. NU chema niciun tool."
)
_USER = "Probă de compatibilitate. Întoarce un plan gol, valid conform schemei."


async def _probe(model: str) -> tuple[bool, str]:
    """Un singur apel. Întoarce `(a trecut, detaliu)`; excepția devine detaliu, nu stack trace."""
    llm = get_llm()
    if llm is None:
        return False, "fără client LLM (OPENAI_API_KEY lipsă în mediu)"

    schema = plan_schema_for_model()
    tools = tool_schemas(list(_PROBE_TOOLS))
    try:
        # Apelul trece prin `_chat`, deci primește EXACT aceleași optionale ca producția
        # (`_sampling`): `reasoning_effort=none` forțat de prezența tool-urilor, `temperature`
        # omisă în consecință. O probă care ar construi requestul de mână ar măsura altceva.
        resp = await llm._chat(  # noqa: SLF001 — proba e despre payload-ul de sârmă, nu despre API
            agent=True,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER},
            ],
            tools=tools,
            tool_choice="auto",
            response_format={"type": "json_schema", "json_schema": schema},
        )
    except Exception as e:  # noqa: BLE001 — orice refuz al furnizorului e REZULTATUL probei
        return False, f"{type(e).__name__}: {e}"

    msg = resp.choices[0].message
    if getattr(msg, "tool_calls", None):
        return True, "acceptat (modelul a cerut un tool, deci requestul a trecut)"
    body = (msg.content or "").strip()
    try:
        json.loads(body or "{}")
    except json.JSONDecodeError:
        # Requestul a fost acceptat, dar corpul nu e JSON — `run_tool_loop_structured` face
        # `json.loads` pe el, deci ăsta ar fi un eșec de altă natură, la fel de fatal.
        return False, f"acceptat, dar corpul NU e JSON valid: {body[:200]!r}"
    return True, f"acceptat, răspuns JSON valid ({len(body)} caractere)"


#: NX-320: pentru `loop`/`responses` vrem un `function_call` REAL, ca să verificăm și citirea lui.
#: Fără vocabular de domeniu (NX-264): proba e despre forma cererii, nu despre un vertical.
_USER_TOOL = "Caută în catalog cele mai bine cotate produse și arată-mi-le."


async def _probe_round(model: str, *, via: str, effort: str) -> tuple[bool, str]:
    """O rundă prin `LLMClient.tool_round`, adică prin exact calea pe care o folosesc bucla de
    azi (`via="chat"`, `none` forțat) și replay-ul NX-320 (`via="responses"`)."""
    llm = get_llm()
    if llm is None:
        return False, "fără client LLM (OPENAI_API_KEY lipsă în mediu)"
    tools = tool_schemas(list(_PROBE_TOOLS))
    try:
        rnd = await llm.tool_round(
            _SYSTEM_TOOL, _USER_TOOL, tools, via=via, effort=effort, model=model
        )
    except Exception as e:  # noqa: BLE001 — orice refuz al furnizorului e REZULTATUL probei
        return False, f"{type(e).__name__}: {e}"
    if not rnd.calls:
        return True, f"acceptat, dar modelul n-a cerut nicio unealtă (text: {rnd.text[:120]!r})"
    name, raw = rnd.calls[0]
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return False, f"acceptat, dar argumentele lui {name} NU sunt JSON: {raw[:200]!r}"
    return True, f"acceptat, {len(rnd.calls)} apel(uri) de unealtă, primul: {name} {raw[:160]}"


_SYSTEM_TOOL = "Ești asistentul unui magazin online. Folosește uneltele ca să cauți în catalog."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=None,
        help="modelul de probat (implicit: MODEL_AGENT din config)",
    )
    parser.add_argument("--mode", choices=("brain", "loop", "responses"), default="brain")
    parser.add_argument("--effort", default="low", help="doar pentru --mode responses")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="construiește payload-ul și iese, FĂRĂ apel (zero credite) — verifică proba însăși",
    )
    args = parser.parse_args()

    settings = get_settings()
    model = args.model or settings.model_agent
    profile = model_profile(model)

    print(
        f"mod              : {args.mode}"
        + (f" (effort={args.effort})" if args.mode == "responses" else "")
    )
    print(f"model            : {model}")
    print(
        "profil           : "
        + (
            f"params={sorted(profile.params)}, raționează implicit={profile.reasons_by_default}, "
            f"responses_tools={profile.responses_tools}"
            if profile
            else "NEDECLARAT în _MODEL_PROFILES (niciun opțional nu se trimite)"
        )
    )
    schema = plan_schema_for_model()
    tools = tool_schemas(list(_PROBE_TOOLS))
    print(f"tool-uri în probă: {', '.join(t['function']['name'] for t in tools)}")
    print(f"schemă           : {schema['name']} (strict={schema.get('strict')})")
    print()

    if args.dry_run:
        payload = {
            "tools": tools,
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        size = len(json.dumps(payload))
        print(f"DRY-RUN — payload construit, {size} octeți, niciun apel făcut.")
        print("Rulează fără `--dry-run` ca să afli verdictul furnizorului.")
        return 0

    if args.mode != "brain":
        via = "chat" if args.mode == "loop" else "responses"
        effort = "none" if args.mode == "loop" else args.effort
        ok, detail = asyncio.run(_probe_round(model, via=via, effort=effort))
        print(("PASS — " if ok else "FAIL — ") + detail)
        if not ok:
            print()
            print(
                "Un 4xx pe calea asta e TERMINAL în `_with_retry`: pe bucla live ar însemna fiecare"
            )
            print("tur de vânzare căzut tăcut pe fallback. Nu promova modelul / nu aprinde calea.")
        return 0 if ok else 1

    ok, detail = asyncio.run(_probe(model))
    if ok:
        print(f"PASS — {detail}")
        print()
        print("`tools` + `response_format=json_schema` coexistă pe modelul ăsta.")
        print("SINGLE_BRAIN_ENABLED=true poate fi aprins fără să omoare calea de vânzare.")
        return 0

    print(f"FAIL — {detail}")
    print()
    print("NU aprinde SINGLE_BRAIN_ENABLED: `run_tool_loop_structured` ar da 4xx pe FIECARE tur")
    print("de vânzare, iar 4xx e terminal în `_with_retry` — deci turul cade tăcut pe fallback,")
    print("cu triajul intact deasupra. Sistemul ar părea sănătos și n-ar mai vinde nimic.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
