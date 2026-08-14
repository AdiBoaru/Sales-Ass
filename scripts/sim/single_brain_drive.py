"""NX-239 — manual drive: 12 conversații multi-turn prin pipeline-ul REAL cu MainBrain aprins.

Două moduri:
  • STUB (default, $0): LLM scriptat + port de retrieval fals — validează cap-coadă orchestrarea
    (control plane → demote → brain → validator → render) fără OpenAI și fără DB.
  • --live: LLM REAL (cheia din .env) + DB reală (Supabase) pe traseul current live NX-238.
    NU se rulează din CI/Claude — o rulează Adi (consumă credite). Capturile NU conțin CoT/PII:
    doar plan counts, coduri de validator, evidence refs, call counts.

Output: reports/nx239/drive.json (+ un rezumat pe stdout).

Rulare:
    python scripts/sim/single_brain_drive.py            # stub, offline
    python scripts/sim/single_brain_drive.py --live     # provider real (o rulează Adi)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import get_settings  # noqa: E402
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, run_pipeline  # noqa: E402

REPORT_DIR = Path("reports/nx239")

#: Cele 12 scenarii cerute de card (formulări proprii; multi-turn = listă de mesaje).
SCENARIOS: list[dict[str, Any]] = [
    {"name": "greeting_plus_ask", "turns": ["Salut! Aveți seruri pentru ten uscat?"]},
    {
        "name": "mixed_faq_recommendation",
        "turns": ["Cât costă livrarea? Și vreau o cremă hidratantă sub 100 lei"],
    },
    {
        "name": "budget_corrected",
        "turns": ["Vreau un ser sub 150 lei", "De fapt nu 150, maxim 70 de lei"],
    },
    {"name": "review_ask", "turns": ["Ce părere au clienții despre serul cu vitamina C?"]},
    {"name": "comparison", "turns": ["Arată-mi două seruri", "Compară-le pe primele două"]},
    {"name": "ordinal_reference", "turns": ["Arată-mi seruri", "Spune-mi mai multe despre prima"]},
    {
        "name": "safety_pregnancy",
        "turns": ["Sunt însărcinată, ce cremă antirid pot folosi?"],
        "safety": True,
    },
    {"name": "no_match", "turns": ["Vreau un parfum cu feromoni de balenă sub 5 lei"]},
    {"name": "unknown_stock", "turns": ["Serul LumaDerm e pe stoc în toate variantele?"]},
    {"name": "provider_timeout", "turns": ["Caută-mi un ser bun"], "port_error": True},
    {"name": "cart_action", "turns": ["Arată-mi un ser", "Adaugă-l în coș"]},
    {"name": "handoff", "turns": ["Vreau să vorbesc cu un om, am o reclamație serioasă"]},
]


def _ctx(body: str, turn_id: str, business_id: str | None = None) -> TurnContext:
    # stub: tenant scriptat "b1" (planurile false îl numesc); live: tenantul demo real.
    business_id = business_id or "6098812a-50fc-44bd-a1ba-bc77e6399158"
    return TurnContext(
        turn_id=turn_id,
        business=BusinessConfig(id=business_id, slug="nativex-demo", name="Sole Demo"),
        contact=Contact(id="drive-contact", business_id=business_id),
        message=InboundMessage(provider_msg_id=f"drive-{turn_id}", body=body),
        conversation_id="drive-conv",
    )


def _capture(ctx: TurnContext) -> dict[str, Any]:
    """Proiecția PII-safe a turului: coduri + numere, fără CoT/text de client."""
    plan = ctx.answer_plan
    events = [
        e
        for e in ctx.events
        if e.type
        in (
            "control_plane_decision",
            "turn_obligations",
            "main_brain_call",
            "main_brain_tool_rounds_bucket",
            "answer_plan_validation",
            "conversation_quality",
            "clarification_decision",
            "no_results",
            "constraint_handling",
            "critic_triggered",
            "repair",
            "retrieval_gate",
            "tool_call",
        )
    ]
    return {
        "reply_set": ctx.reply is not None,
        "reply_len": len(ctx.reply.text) if ctx.reply else 0,
        "plan": None
        if plan is None
        else {
            "schema_version": plan.schema_version,
            "n_obligations": len(getattr(plan, "obligations", ())),
            "n_claims": len(getattr(plan, "claims", ())),
            "n_recommendations": len(getattr(plan, "recommendations", ())),
            "has_clarification": getattr(plan, "clarification", None) is not None,
            "no_results": getattr(getattr(plan, "no_results", None), "reason_class", None),
            "evidence_refs": sorted(
                {eid for rec in getattr(plan, "recommendations", ()) for eid in rec.evidence_ids}
            ),
        },
        "events": [
            {"type": e.type, **{k: v for k, v in e.properties.items() if k != "turn_id"}}
            for e in events
        ],
        "llm_calls": ctx.usage.calls if ctx.usage else 0,
    }


async def _drive_stub() -> list[dict[str, Any]]:
    """Modul STUB: reutilizează exact harness-ul din tests/test_single_brain_e2e.py."""
    from src.agent import brain as brain_mod
    from src.worker.stages import agent as agent_stage_mod
    from src.worker.stages import triage as triage_mod
    from tests.test_single_brain import _FakePort, _plan_dict
    from tests.test_single_brain_e2e import _PRODUCT, _ScriptedLLM

    class _FakeConn:
        """Conn permisiv pt căile care scriu direct (gates/handoff) în modul stub."""

        async def execute(self, *a, **kw):
            return "OK"

        async def fetch(self, *a, **kw):
            return []

        async def fetchrow(self, *a, **kw):
            return None

        async def fetchval(self, *a, **kw):
            return None

    settings = get_settings()
    settings.single_brain_enabled = True

    async def _empty(conn, business_id):
        return []

    triage_mod.list_category_slugs = _empty
    agent_stage_mod.list_category_names = _empty
    agent_stage_mod.list_routing_aliases = _empty

    from src.agent.brain_models import extract_obligations

    product2 = {**_PRODUCT, "id": "p2", "name": "Ser HidraPlus", "price": 69.0}

    def _stub_plan(message: str, *, port_error: bool, safety: bool = False) -> dict[str, Any]:
        """Planul scriptat DECLARĂ exact obligațiile pe care le-ar extrage codul — drive-ul stub
        verifică ORCHESTRAREA (demote/validare/render), nu calitatea modelului."""
        obligations = [
            {"kind": o.kind, "key": o.key}
            for o in extract_obligations(message, safety_active=safety)
        ][:8]
        plan = _plan_dict(obligations=obligations)
        kinds = {o["kind"] for o in obligations}
        if port_error or "action" in kinds:
            # onest: mutația/căutarea n-a reușit în stub → no_results, nu confirmare falsă
            plan.update(
                selected_products=[],
                recommendations=[],
                direct_answer="",
                no_results={
                    "reason_class": "dependency_unavailable",
                    "criteria": [],
                    "alternatives": [],
                },
            )
        elif "compare" in kinds:
            plan.update(
                selected_products=[
                    {
                        "product_id": "p1",
                        "variant_id": None,
                        "evidence_ids": ["product:p1:identity"],
                    },
                    {
                        "product_id": "p2",
                        "variant_id": None,
                        "evidence_ids": ["product:p2:identity"],
                    },
                ],
                recommendations=[],
                comparison={
                    "product_ids": ["p1", "p2"],
                    "axes": ["pret"],
                    "cells": [
                        {
                            "product_id": "p1",
                            "axis": "pret",
                            "value": 89.0,
                            "evidence_id": "product:p1:price",
                        },
                        {
                            "product_id": "p2",
                            "axis": "pret",
                            "value": 69.0,
                            "evidence_id": "product:p2:price",
                        },
                    ],
                },
            )
        return plan

    results = []
    for scenario in SCENARIOS:
        port = _FakePort(
            products=(_PRODUCT, product2),
            raise_error=TimeoutError("drive") if scenario.get("port_error") else None,
        )
        brain_mod.build_port = lambda ctx, deps, sel, _p=port, **kw: _p
        turns = []
        for i, message in enumerate(scenario["turns"]):
            plan = _stub_plan(
                message,
                port_error=scenario.get("port_error", False),
                safety=scenario.get("safety", False),
            )
            llm = _ScriptedLLM(triage_out={"route": "sales", "confidence": "high"}, plan=plan)
            ctx = _ctx(message, f"{scenario['name']}-{i}", business_id="b1")
            await run_pipeline(ctx, PipelineDeps(conn=_FakeConn(), llm=llm), DEFAULT_STAGES)
            turns.append(_capture(ctx))
        results.append({"scenario": scenario["name"], "mode": "stub", "turns": turns})
    return results


async def _drive_live() -> list[dict[str, Any]]:
    """Modul LIVE: LLM real + DB reală. O rulează Adi (consumă credite OpenAI)."""
    from src.agent.llm import get_llm
    from src.db.connection import get_pools
    from src.db.provider import DbProvider

    settings = get_settings()
    settings.single_brain_enabled = True
    llm = get_llm()
    if llm is None:
        raise SystemExit("--live cere OPENAI_API_KEY în .env")
    pools = await get_pools()
    results = []
    for scenario in SCENARIOS:
        turns = []
        for i, message in enumerate(scenario["turns"]):
            ctx = _ctx(message, f"{scenario['name']}-{i}")
            deps = PipelineDeps(llm=llm, db=DbProvider(pools, ctx.business.id))
            await run_pipeline(ctx, deps, DEFAULT_STAGES)
            turns.append(_capture(ctx))
        results.append({"scenario": scenario["name"], "mode": "live", "turns": turns})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="provider real (o rulează Adi)")
    args = parser.parse_args()

    results = asyncio.run(_drive_live() if args.live else _drive_stub())
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / "drive.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in results if all(t["reply_set"] for t in r["turns"]))
    print(f"{ok}/{len(results)} scenarii cu reply pe fiecare tur → {out}")
    for r in results:
        outcomes = [
            e.get("outcome")
            for t in r["turns"]
            for e in t["events"]
            if e["type"] == "main_brain_call"
        ]
        print(f"  {r['scenario']}: main_brain={outcomes or '-'}")


if __name__ == "__main__":
    main()
