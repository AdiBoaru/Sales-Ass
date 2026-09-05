"""Bugetul de tokeni al unui tur: cât cântă FIECARE prompt, pe calea v1 și pe single brain.

De ce există: „creierul unic e mai scump" e o intuiție, iar `docs/QUALITY-OVERHAUL-2026.md` (D15)
cere măsurători. Scriptul numără tokenii REALI ai prompturilor construite din DB-ul tenantului
(categorii, aliase, pachet de domeniu), nu ai unui exemplu scris de mână.

READ-ONLY și GRATUIT: doar `SELECT` pe control plane, ZERO apeluri la OpenAI. Tokenizarea e
locală (`tiktoken`, encoding `o200k_base`).

    python scripts/prompt_budget_probe.py                      # sole-ro
    python scripts/prompt_budget_probe.py --business <uuid>
    python scripts/prompt_budget_probe.py --json               # pentru diff între rulări

Ce e MĂSURAT și ce e IPOTEZĂ: secțiunea „prefix static" e numărată; secțiunea „model de cost"
compune tokenii măsurați cu ipoteze DECLARATE (mesajul clientului, tool results, lungimea
outputului). Ipotezele se schimbă din flag-uri, ca să nu treacă drept măsurătoare.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tiktoken  # noqa: E402

from src.agent import prompt_builder  # noqa: E402
from src.agent.answer_plan_runtime import ANSWER_PLAN_V2_SCHEMA  # noqa: E402
from src.agent.pricing import cost_for, has_rates  # noqa: E402
from src.agent.tool_definitions import tool_schemas  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.domain import vocab_examples  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.tools import (  # noqa: E402,F401 — importul populează TOOL_REGISTRY prin decoratori
    catalog_tools,
    commerce_tools,
    faq_tools,
    orders_tools,
)
from src.tools.base import enabled_tools  # noqa: E402

SOLE = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
ENC = tiktoken.get_encoding("o200k_base")


def tok(text: str) -> int:
    return len(ENC.encode(text))


def tok_json(obj: object) -> int:
    return tok(json.dumps(obj, ensure_ascii=False))


def _triple_quoted(path: Path, name: str) -> str:
    """Constanta de prompt din sursă. Citită din FIȘIER, nu importată: `_PLAN_V2_SYSTEM` și
    `_SYSTEM` (triaj) sunt private, iar un import ar lega scriptul de detaliul lor de nume."""
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*=\s*(\"\"\"|''')(.*?)\1", src, re.S | re.M)
    return m.group(2) if m else ""


async def _tenant_prompt_inputs(business_id: str):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow(
            "select name, vertical, default_locale, settings from businesses where id = $1",
            business_id,
        )
        if biz is None:
            raise SystemExit(f"business inexistent: {business_id}")
        categories = [
            r["name"]
            for r in await conn.fetch(
                "select name from categories where business_id = $1 order by name", business_id
            )
        ]
        aliases = [
            (r["phrase_norm"], r["target_kind"])
            for r in await conn.fetch(
                "select phrase_norm, target_kind from intent_aliases "
                "where business_id = $1 and status = 'approved' order by phrase_norm",
                business_id,
            )
        ]
    settings = biz["settings"]
    if isinstance(settings, str):
        settings = json.loads(settings or "{}")
    settings = settings or {}

    class _Business:
        vertical = biz["vertical"]

    _Business.settings = settings
    pack = load_domain_pack(_Business)
    examples = vocab_examples.from_pack(pack) if pack else vocab_examples.EMPTY_EXAMPLES
    style = getattr(pack, "response_style", None) if pack else None
    inp = prompt_builder.PromptInputs.build(
        biz["name"],
        biz["vertical"],
        biz["default_locale"] or "ro",
        categories,
        aliases,
        response_style=style,
        need_examples=examples.needs,
    )
    return inp, _Business, examples, len(categories), len(aliases)


def _measure(inp, business, examples) -> dict[str, int]:
    sales = enabled_tools(business, "sales")
    order = enabled_tools(business, "order")
    union = list(dict.fromkeys(sales + order))
    return {
        "system_agent": tok(prompt_builder.build_agent_system(inp)),
        "system_rich": tok(prompt_builder.build_rich_system(inp)),
        "system_reco": tok(prompt_builder.build_reco_system(inp)),
        "system_compare": tok(prompt_builder.build_compare_system(inp)),
        "system_triage": tok(_triple_quoted(ROOT / "src/worker/stages/triage.py", "_SYSTEM")),
        "system_plan_v2": tok(_triple_quoted(ROOT / "src/agent/brain.py", "_PLAN_V2_SYSTEM")),
        "tools_sales": tok_json(tool_schemas(sales, examples)),
        "tools_union": tok_json(tool_schemas(union, examples)),
        "plan_schema": tok_json(ANSWER_PLAN_V2_SCHEMA),
        "n_tools_sales": len(sales),
        "n_tools_union": len(union),
    }


def _cost_model(m: dict[str, int], args) -> dict[str, float]:
    """Costul unui tur cu carduri, compus din tokenii măsurați + ipotezele DECLARATE din `args`.

    `cache_ratio` se aplică DOAR prefixului cache-uibil, iar schema de `response_format` FACE
    parte din el: ghidul OpenAI de prompt caching listează Structured Outputs în prefixul
    cache-uit (prag 1.024 tokeni, tarif 0,1x). Prima versiune a scriptului presupunea contrariul
    și, din cauza asta, arăta creierul unic cu +31% pe cache cald în loc de aproximativ paritate.
    `--schema-uncached` păstrează scenariul pesimist, ca ipoteza să rămână testabilă, nu ștearsă.

    Ce NU se cache-uiește rămâne partea per tur: mesajul, istoricul care crește, tool results."""
    agent, nano, c = args.model_agent, args.model_triage, args.cache_ratio
    user, tool_res = args.user_tokens, args.tool_result_tokens

    v1_static = m["system_agent"] + m["tools_sales"]
    v1 = cost_for(nano, m["system_triage"] + 300, int(m["system_triage"] * c), args.out_triage)
    for i in range(args.rounds):
        v1 += cost_for(agent, v1_static + user + i * tool_res, int(v1_static * c), 120)
    v1 += cost_for(
        agent, m["system_rich"] + tool_res + user, int(m["system_rich"] * c), args.out_prose
    )

    v2_static = m["system_agent"] + m["system_plan_v2"] + m["tools_union"] + m["plan_schema"]
    v2_cacheable = v2_static - (m["plan_schema"] if args.schema_uncached else 0)
    v2 = 0.0
    for i in range(args.rounds):
        last = i == args.rounds - 1
        v2 += cost_for(
            agent,
            v2_static + user + i * tool_res,
            int(v2_cacheable * c),
            args.out_plan if last else 120,
        )
    v2_no_shadow = v2
    v2 += cost_for(nano, m["system_triage"] + 300, int(m["system_triage"] * c), args.out_triage)
    return {"v1": v1, "v2": v2, "v2_fara_shadow": v2_no_shadow}


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--business", default=SOLE)
    p.add_argument("--json", action="store_true", help="ieșire mașină, pentru diff între rulări")
    p.add_argument("--rounds", type=int, default=2, help="IPOTEZĂ: runde de model pe tur")
    p.add_argument("--user-tokens", type=int, default=900, help="IPOTEZĂ: istoric+context+mesaj")
    p.add_argument("--tool-result-tokens", type=int, default=1200, help="IPOTEZĂ: 6 produse")
    p.add_argument("--out-plan", type=int, default=700, help="IPOTEZĂ: output AnswerPlanV2")
    p.add_argument("--out-prose", type=int, default=350, help="IPOTEZĂ: output proză v1")
    p.add_argument("--out-triage", type=int, default=90, help="IPOTEZĂ: output triaj")
    p.add_argument("--cache-ratio", type=float, default=0.0, help="fracția de prefix din cache")
    p.add_argument(
        "--schema-uncached",
        action="store_true",
        help="scenariu PESIMIST: schema `response_format` în afara prefixului cache-uit "
        "(infirmat de ghidul OpenAI, păstrat ca să rămână testabil)",
    )
    args = p.parse_args()
    settings = get_settings()
    args.model_agent = settings.model_agent
    args.model_triage = settings.model_triage

    inp, business, examples, n_cat, n_alias = await _tenant_prompt_inputs(args.business)
    m = _measure(inp, business, examples)
    costs = _cost_model(m, args)
    await close_pool()

    if args.json:
        print(json.dumps({"tokens": m, "cost_usd": costs}, ensure_ascii=False, indent=2))
        return

    v1_call = m["system_agent"] + m["tools_sales"]
    v2_call = m["system_agent"] + m["system_plan_v2"] + m["tools_union"] + m["plan_schema"]
    print(f"business={args.business}  categorii={n_cat}  aliase_aprobate={n_alias}")
    print(f"model_agent={args.model_agent}  model_triage={args.model_triage}")
    if not has_rates(args.model_agent):
        print(
            f"  ⚠ {args.model_agent} NU are tarife (`src/agent/pricing.py`) → cade pe fallback-ul "
            "`mini`. Orice cifră de cost de mai jos, inclusiv plafonul zilnic, e o presupunere. "
            "Repară cu `LLM_PRICING_JSON`."
        )

    print("\n=== TOKENI MĂSURAȚI (retrimiși la FIECARE apel de model) ===")
    for label, key in (
        ("system agent (generat din DB)", "system_agent"),
        ("system triaj nano (v1)", "system_triage"),
        ("system rich compose (v1)", "system_rich"),
        ("system reco/retry (v1)", "system_reco"),
        ("system compare (v1)", "system_compare"),
        ("_PLAN_V2_SYSTEM (single brain)", "system_plan_v2"),
        (f"tool schemas SALES ({m['n_tools_sales']})", "tools_sales"),
        (f"tool schemas SALES∪ORDER ({m['n_tools_union']})", "tools_union"),
        ("ANSWER_PLAN_V2_SCHEMA (response_format)", "plan_schema"),
    ):
        print(f"  {m[key]:6d}  {label}")

    delta = v2_call - v1_call
    print("\n=== PREFIX STATIC PER APEL ===")
    print(f"  {v1_call:6d}  v1 (agent + tools)")
    print(
        f"  {v2_call:6d}  v2 (agent + plan_v2 + tools∪ + schema)  "
        f"{delta:+d} ({100 * delta / max(v1_call, 1):+.0f}%)"
    )
    print(
        f"  {m['plan_schema']:6d}  din care SCHEMA "
        f"({100 * m['plan_schema'] / max(v2_call, 1):.0f}% din apelul v2)"
    )

    print(f"\n=== MODEL DE COST / TUR CU CARDURI (cache_ratio={args.cache_ratio}) ===")
    print(
        "    ipoteze: rounds=%d user=%d tool_res=%d out_plan=%d out_prose=%d"
        % (args.rounds, args.user_tokens, args.tool_result_tokens, args.out_plan, args.out_prose)
    )
    v1c, v2c = costs["v1"], costs["v2"]
    print(f"  ${v1c:.6f}  v1 (triaj + {args.rounds} runde + compose rich)")
    print(f"  ${v2c:.6f}  v2 (brain + shadow nano)   {100 * (v2c - v1c) / max(v1c, 1e-9):+.0f}%")
    print(f"  ${costs['v2_fara_shadow']:.6f}  v2 fără shadow nano")


if __name__ == "__main__":
    asyncio.run(main())
