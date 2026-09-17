"""NX-145 felia 3 — Regression harness peste cazurile golden.

Rulează TOATE cazurile (single-tur `tests/golden/cases.json` + multi-tur
`tests/golden/conversations.json`) prin pipeline-ul REAL cu un LLM SCRIPTAT + stub-uri DB
(ZERO OpenAI/DB real, ca gate-ul CI din `tests/test_golden.py`) și scrie un SNAPSHOT
determinist: per caz/tur → rută, tool-uri chemate, product IDs observate, `cacheable`,
pass/fail. Un al doilea mod DIFF-uiește rularea curentă față de un snapshot de baseline →
orice schimbare de prompt/model/pipeline care mută rutarea/tool-urile/produsele apare
explicit (semnalul pe care testele clasice nu-l prind).

Utilizare (rulare directă: prefixează `PYTHONPATH=.`):
    PYTHONPATH=. python scripts/eval_regression.py --out reports/snapshot.json   # scrie snapshot
    PYTHONPATH=. python scripts/eval_regression.py --baseline reports/snapshot.json  # DIFF
    PYTHONPATH=. python scripts/eval_regression.py                               # rulează + sumar

Exit code 1 dacă (a) un caz e ROȘU sau (b) `--baseline` găsește un DIFF → utilizabil în CI
manual/nocturn. Judge-ul LLM de naturalețe (`scripts/sim/halu_run.py`) rămâne separat
(cost + nedeterminism) — aici e strict determinist.

Fiecare caz se rulează pe AMBELE contracte, ca gate-ul CI: `v1` (`run_tool_loop` → text) și
`brain` (`run_tool_loop_structured` → `AnswerPlanV2`). Cheia de snapshot poartă calea
(`<caz>` / `<caz>@brain`), fiindcă altfel aprinderea lui `SINGLE_BRAIN_ENABLED` în producție ar
fi arătat în diff ca „s-a schimbat totul", fără să se poată spune ce anume.

NB: harness-ul scriptat NU se reimplementează aici și nici nu se importă din `tests/` — trăiește
în `src/evals/scripted_llm.py`, de unde îl iau amândouă consumatoarele. Copia locală de dinainte
rămăsese fără metodele căii structurate, deci scriptul măsura v1 și când produsul rula brain.
Sursa de adevăr a semnalelor de snapshot (`_ctx_tool_names` / `_ctx_product_ids`) vine din
`src/evals/golden.py` ca să nu divergem de checker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from src.agent import deterministic as deterministic_mod
from src.agent import planner as planner_mod
from src.config import get_settings
from src.evals.golden import (
    GoldenCase,
    _ctx_product_ids,
    _ctx_tool_names,
    advance_turn,
    evaluate_reply,
    load_cases,
)
from src.evals.scripted_llm import ScriptedLLM, brain_gap
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import clear_embeddings_cache
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, run_pipeline
from src.worker.stages import agent as agent_mod
from src.worker.stages import cache as cache_mod

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "golden" / "cases.json"
CONVERSATIONS_PATH = ROOT / "tests" / "golden" / "conversations.json"


# --- stub-uri DB (aceleași ca gate-ul CI), aplicate prin ExitStack -----------


class _Patcher:
    """`monkeypatch.setattr`-like peste un `ExitStack` (restaurează la ieșire)."""

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self._stack = stack

    def setattr(self, target: Any, name: str, value: Any) -> None:
        old = getattr(target, name)
        self._stack.callback(setattr, target, name, old)
        setattr(target, name, value)


def _apply_stubs(patch: _Patcher, get_fx, *, single_brain: bool = False) -> None:
    async def fake_categories(conn, business_id):
        return list(get_fx().get("categories", []))

    async def fake_search(conn, business_id, vec, **kwargs):
        return list(get_fx().get("catalog", []))

    async def fake_lexical(conn, business_id, **kwargs):
        return []

    async def has_emb(conn, business_id):
        return True

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        by_id = {p["id"]: p for p in get_fx().get("catalog", [])}
        return [by_id[i] for i in ids if i in by_id]

    async def fake_search_cheaper_than(conn, business_id, ref_ids, baseline, **kwargs):
        return list(get_fx().get("cheaper", []))

    async def fake_complementary(conn, business_id, product_id, **kwargs):
        return list(get_fx().get("complementary", []))

    async def none_lookup(*args, **kwargs):
        return None

    async def no_prompt_inputs(conn, business_id, **kwargs):
        return []

    # NX-78: agent_stage citește categorii + aliase din DB pt promptul generat (P9); pe conn
    # fals → stub → prompt generic (oglindește fixture-ul autouse din tests/conftest.py).
    patch.setattr(agent_mod, "list_category_names", no_prompt_inputs)
    patch.setattr(agent_mod, "list_routing_aliases", no_prompt_inputs)
    patch.setattr(ct, "has_embeddings", has_emb)
    patch.setattr(ct, "search_products_semantic", fake_search)
    patch.setattr(ct, "search_products_lexical", fake_lexical)
    patch.setattr(ct, "get_products_by_ids", fake_by_ids)
    # NX-144: re-hidratarea grounded a produselor afisate s-a mutat din agent_stage in
    # src/agent/planner.py (build_plan) => stub pe binding-ul din planner.
    # PRE-loop deterministic intents (`link` / `compare`) import `get_products_by_ids` direct.
    patch.setattr(deterministic_mod, "get_products_by_ids", fake_by_ids)
    patch.setattr(planner_mod, "get_products_by_ids", fake_by_ids)
    patch.setattr(planner_mod, "search_cheaper_than", fake_search_cheaper_than)
    patch.setattr(planner_mod, "get_complementary_products", fake_complementary)
    patch.setattr(cache_mod, "exact_lookup", none_lookup)
    patch.setattr(cache_mod, "semantic_lookup", none_lookup)
    patch.setattr(get_settings(), "moderation_enabled", True)
    # Calea e a HARNESSULUI, nu a `.env`-ului mașinii — aceeași regulă ca în gate-ul CI. Fără
    # pinul ăsta, snapshot-ul depindea de cine îl rulează, iar diff-ul dintre două rulări putea
    # însemna „s-a schimbat produsul" sau „s-a schimbat mașina", fără cale de a le deosebi.
    patch.setattr(get_settings(), "single_brain_enabled", single_brain)
    # Brațul semantic e OFF în producție (2026-09-08), dar fixturile golden stubează
    # `search_products_semantic` — cu el stins, stubul nu se cheamă deloc și fiecare caz de
    # vânzare raportează „zero produse observate". Gate-ul CI îl aprinde printr-un fixture autouse
    # (`tests/conftest.py`); scriptul trebuie să pinuiască ACELAȘI lucru, altfel cele două
    # instrumente care rulează aceleași cazuri dau rezultate diferite — și l-am fi citit ca
    # regresie de produs. Cache-ul de `has_embeddings` e per tenant, deci se golește odată cu el.
    patch.setattr(get_settings(), "search_semantic_enabled", True)
    clear_embeddings_cache()


# --- snapshot + rulare -------------------------------------------------------


def _build_ctx(case_id: str, body: str, language: str) -> TurnContext:
    return TurnContext(
        turn_id=f"reg-{case_id}",
        business=BusinessConfig(id="biz-golden", slug="golden", name="Golden"),
        contact=Contact(id="contact-golden", business_id="biz-golden"),
        message=InboundMessage(provider_msg_id=f"m-{case_id}-0", body=body),
        conversation_id=f"conv-{case_id}",
        language=language,
    )


#: Cele două contracte pe care rulează fiecare caz. Sufixul intră în cheia de snapshot; `v1` NU
#: primește sufix, ca baseline-urile scrise înainte de dualizare să rămână comparabile.
_PATHS: tuple[tuple[str, bool], ...] = (("v1", False), ("brain", True))

#: Cazurile care PICĂ pe calea `brain` și de ce. Nu sunt scuze: sunt măsurători, cu cauza scrisă
#: lângă ele, iar `tests/test_eval_regression.py` cere ca mulțimea să fie EXACT asta — un roșu nou
#: pică gate-ul, iar o reparație îl pică la fel (ca `xfail(strict=True)`), deci nu se poate repara
#: tăcut și nici degrada tăcut.
#:
#: GOALĂ azi, și a fost goală prin reparare, nu prin ștergere. Cele cinci divergențe de la
#: dualizare aveau O SINGURĂ cauză: sub `single_brain_enabled`, `agent_stage` se întorcea înainte
#: de `planner.build_plan`, deci Faza E nu mai rula deloc — «ceva mai ieftin» rămânea pe seama
#: modelului, rehidratarea produselor afișate dispărea, iar fallback-ul servea text fără carduri,
#: așa că turul următor pierdea referința. Gate-ul le-a raportat una câte una pe măsură ce au fost
#: reparate; lista a ajuns aici fiindcă nu mai are ce să conțină.
KNOWN_BRAIN_DIVERGENCES: dict[str, str] = {}


def _key(case_key: str, path: str) -> str:
    return case_key if path == "v1" else f"{case_key}@{path}"


def _snapshot(ctx: TurnContext, passed: bool, failures: list[str], path: str) -> dict[str, Any]:
    return {
        "path": path,
        "route": ctx.route.route.value if ctx.route is not None else None,
        "tools": sorted(set(_ctx_tool_names(ctx))),
        "product_ids": sorted(_ctx_product_ids(ctx)),
        "cacheable": ctx.reply.cacheable if ctx.reply is not None else None,
        "passed": passed,
        "failures": failures,
    }


def _gap_entry(path: str, reason: str) -> dict[str, Any]:
    """Un gol DECLARAT al harnessului: `passed` e None, nu False.

    Distincția e aceeași ca la NX-238 („n-am măsurat" ≠ „am măsurat și a picat"): un caz fără
    text scriptat pe calea brain n-a fost rulat, deci a-l trece la roșu ar umfla raportul cu un
    eșec inexistent, iar a-l trece la verde ar ascunde o gaură de acoperire."""
    return {
        "path": path,
        "route": None,
        "tools": [],
        "product_ids": [],
        "cacheable": None,
        "passed": None,
        "failures": [],
        "gap": reason,
    }


async def _run_single(case: GoldenCase, *, path: str, single_brain: bool) -> dict[str, Any]:
    with contextlib.ExitStack() as stack:
        patch = _Patcher(stack)
        _apply_stubs(patch, lambda: case.fixtures, single_brain=single_brain)
        ctx = _build_ctx(case.id, case.input, case.language)
        deps = PipelineDeps(
            conn=object(),
            redis=None,
            llm=ScriptedLLM(case.fixtures, business_id="biz-golden", locale=case.language),
        )
        await run_pipeline(ctx, deps, DEFAULT_STAGES)
        res = evaluate_reply(ctx, case.expect, case_id=case.id)
        return _snapshot(ctx, res.passed, res.failures, path)


async def _run_conversation(
    case: GoldenCase, *, path: str, single_brain: bool
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    holder = {"fx": case.turns[0].fixtures}
    with contextlib.ExitStack() as stack:
        patch = _Patcher(stack)
        _apply_stubs(patch, lambda: holder["fx"], single_brain=single_brain)
        ctx = _build_ctx(case.id, case.turns[0].input, case.language)
        deps = PipelineDeps(
            conn=object(),
            redis=None,
            llm=ScriptedLLM(lambda: holder["fx"], business_id="biz-golden", locale=case.language),
        )
        for i, turn in enumerate(case.turns):
            if i > 0:
                msg = InboundMessage(provider_msg_id=f"m-{case.id}-{i}", body=turn.input)
                advance_turn(ctx, msg)
            holder["fx"] = turn.fixtures
            await run_pipeline(ctx, deps, DEFAULT_STAGES)
            res = evaluate_reply(ctx, turn.expect, case_id=f"{case.id}#{i}")
            out[_key(f"{case.id}#{i}", path)] = _snapshot(ctx, res.passed, res.failures, path)
    return out


async def _run_all() -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    singles = load_cases(CASES_PATH)
    conversations = load_cases(CONVERSATIONS_PATH) if CONVERSATIONS_PATH.exists() else []
    for path, single_brain in _PATHS:
        for case in singles:
            gap = brain_gap(case.id) if single_brain else None
            snapshot[_key(case.id, path)] = (
                _gap_entry(path, gap)
                if gap
                else await _run_single(case, path=path, single_brain=single_brain)
            )
        for case in conversations:
            gap = brain_gap(case.id) if single_brain else None
            if gap:
                snapshot[_key(case.id, path)] = _gap_entry(path, gap)
                continue
            snapshot.update(await _run_conversation(case, path=path, single_brain=single_brain))
    return snapshot


# --- diff --------------------------------------------------------------------

_SIGNAL_KEYS = ("route", "tools", "product_ids", "cacheable", "passed")


def _diff(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(set(baseline) - set(current)):
        lines.append(f"- {key}: DISPĂRUT din rularea curentă")
    for key in sorted(set(current) - set(baseline)):
        lines.append(f"+ {key}: NOU în rularea curentă")
    for key in sorted(set(baseline) & set(current)):
        b, c = baseline[key], current[key]
        changes = [
            f"{k}: {b.get(k)!r} → {c.get(k)!r}" for k in _SIGNAL_KEYS if b.get(k) != c.get(k)
        ]
        if changes:
            lines.append(f"~ {key}: " + "; ".join(changes))
    return lines


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    with contextlib.suppress(Exception):  # consola Windows (cp1252) → forțează utf-8
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Golden regression snapshot + diff (NX-145 felia 3)")
    ap.add_argument("--out", type=Path, help="scrie snapshot-ul JSON aici")
    ap.add_argument("--baseline", type=Path, help="DIFF rularea curentă față de acest snapshot")
    args = ap.parse_args()

    current = asyncio.run(_run_all())
    total = len(current)
    red = {k: v["failures"] for k, v in current.items() if v["passed"] is False}
    gaps = {k: v["gap"] for k, v in current.items() if v.get("gap")}
    known = {k: v for k, v in red.items() if k in KNOWN_BRAIN_DIVERGENCES}
    unknown = {k: v for k, v in red.items() if k not in KNOWN_BRAIN_DIVERGENCES}
    green = total - len(red) - len(gaps)
    print(f"golden regression: {green}/{total} verzi (ambele contracte)")
    for k, failures in unknown.items():
        print(f"  ROȘU {k}: {failures}")
    for k in known:
        print(f"  DIVERGENȚĂ CUNOSCUTĂ {k}: {KNOWN_BRAIN_DIVERGENCES[k]}")
    for k, reason in gaps.items():
        print(f"  NERULAT {k}: {reason}")
    red = unknown

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"version": 1, "cases": current}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"snapshot scris în {args.out} ({total} intrări)")

    exit_code = 1 if red else 0
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")).get("cases", {})
        diff = _diff(baseline, current)
        if diff:
            print(f"\nDIFF vs {args.baseline} ({len(diff)} schimbări):")
            for line in diff:
                print(f"  {line}")
            exit_code = 1
        else:
            print(f"\nfără schimbări vs {args.baseline}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
