"""NX-386 (`kernel.v7.1`) — un produs numit pe care resolverul nu l-a găsit scris întreg nu mai
degradează actul în „n-am găsit exact".

Turele reale (setul wide-2026-10-07): `w4_alergie_sarcina_nu#1` «beauty of joseon relief sun
contine alcool?» ⇒ „Nu am găsit exact produsul pe care l-ai numit" deasupra exact acelui produs;
`w4_compara_numit_apoi_cos#1` «compara skin1004 centella ampoule cu purito centella» ⇒ s-a căutat
doar primul nume; `w5_precomanda_epuizat#1` «cushionul tirtir rosu» pe „Mask Fit Red Cushion"
(cuvintele nu se potrivesc: rămâne căutarea, cu dezvăluirea).

Zero model, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.turn_planner import PlannedTurn, plan_turn
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import AmbiguityDecision, TurnPlan
from src.conversation.references import name_in_results
from src.conversation.state_v2 import ConversationStateV2
from src.tools import catalog_tools
from src.tools.base import ToolResult
from tests.kernel import fixture_catalog as fc
from tests.kernel import stage_harness as sh
from tests.test_kernel_planner import _gate, _interp, _ref, _state

ROWS = [
    {"id": "a", "name": "BEAUTY OF JOSEON Relief Sun: Rice + Probiotics SPF50+ 50 ml"},
    {"id": "b", "name": "BEAUTY OF JOSEON Glow Serum: Propolis + Niacinamide"},
    {"id": "c", "name": "SKIN1004 Madagascar Centella Air-Fit Suncream"},
]


# --- potrivirea pe rezultate ------------------------------------------------------------------


def test_a_partial_name_found_in_the_results_is_that_product():
    assert name_in_results("beauty of joseon relief sun", ROWS, "ro") == ("a",)


def test_a_brand_alone_names_all_its_products():
    """«joseon» e în două nume: ambiguu pe amândouă (un `find` nu-l promovează, cap 1)."""
    assert name_in_results("joseon", ROWS, "ro") == ("a", "b")
    assert name_in_results("beauty of joseon cushion", ROWS, "ro") == ()


def test_all_the_words_scattered_in_a_name_are_not_the_name():
    """Recenzia: pe rezultatele unei căutări, „toate cuvintele" e o dovadă slabă (setul a fost adus
    după ele); doar fraza întreagă numește un produs."""
    rows = [
        {"id": "1", "name": "Crema de zi cu efect hidratant si calmant"},
        {"id": "2", "name": "Ser hidratant pentru crema de noapte"},
    ]
    assert name_in_results("crema hidratant", rows, "ro") == ()


def test_words_the_product_does_not_carry_keep_the_search():
    rows = [{"id": "t", "name": "TIRTIR Mask Fit Red Cushion"}]
    assert name_in_results("cushionul tirtir rosu", rows, "ro") == ()


# --- plannerul --------------------------------------------------------------------------------


def _plan(kind, names, resolved):
    refs = [{"id": f"r{i}", "text": n, "kind": "name", "name": n} for i, n in enumerate(names, 1)]
    targets = [r["id"] for r in refs]
    interp = _interp(acts=[{"kind": kind, "targets": targets}], references=refs)
    return plan_turn(
        interp,
        _state("telefoane"),
        (),
        resolved,
        _gate(),
        changed=False,
        pack=fc.pack("electronics"),
        vocab=fc.vocabulary("electronics"),
        locale="ro",
    )


def test_a_detail_on_a_name_not_found_carries_the_act_and_the_name():
    planned = _plan("detail", ["xiaomi phone 3"], [_ref("r1", "not_found", [], kind="name")])
    plan = planned.plans[planned.primary]
    assert (plan.executor, plan.then, plan.names) == ("search", "detail", ["xiaomi phone 3"])
    assert (0, "not_exact_match") in planned.disclosures


def test_a_compare_on_two_names_not_found_carries_both():
    resolved = [
        _ref("r1", "not_found", [], kind="name"),
        _ref("r2", "not_found", [], kind="name"),
    ]
    planned = _plan("compare", ["xiaomi phone 3", "apple phone 2"], resolved)
    plan = planned.plans[planned.primary]
    assert plan.then == "compare" and plan.names == ["xiaomi phone 3", "apple phone 2"]


def test_a_lost_target_without_a_name_is_not_promoted():
    """Recenzia: o țintă `stale` lângă un nume negăsit ar ieși din act fără dezvăluire."""
    resolved = [
        _ref("r1", "stale", ["el-01"], kind="name", reason="not_in_catalog"),
        _ref("r2", "not_found", [], kind="name"),
    ]
    planned = _plan("compare", ["samsung phone 1", "apple phone 2"], resolved)
    plan = planned.plans[planned.primary]
    assert plan.then is None and (0, "not_exact_match") in planned.disclosures


def test_a_compare_with_one_name_found_keeps_the_exact_target():
    resolved = [
        _ref("r1", "exact", ["el-01"], kind="name"),
        _ref("r2", "not_found", [], kind="name"),
    ]
    planned = _plan("compare", ["samsung phone 1", "apple phone 2"], resolved)
    plan = planned.plans[planned.primary]
    assert (plan.product_ids, plan.names) == (["el-01"], ["apple phone 2"])


# --- executorul -------------------------------------------------------------------------------


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


def _search_stub(monkeypatch, cat, by_name):
    calls = []

    async def search(ctx, deps, args, **kw):
        calls.append(args.product_name)
        ctx.state_patch["active_search"] = {"fp": "x", "pool": ["el-09"], "cursor": 0, "page": 0}
        ctx.emit("product_search", n=1)
        ids = by_name.get(args.product_name, [])
        return ToolResult(ok=True, products=sh.product_rows(cat, ids))

    monkeypatch.setattr(catalog_tools, "run_planned_search", search)
    return calls


def _named(then, names, exact=()):
    plan = TurnPlan(
        executor="search",
        product_ids=list(exact),
        search_args=catalog_tools.SearchArgs(query=names[0], product_name=names[0]),
        depends_on=None,
        then=then,
        names=list(names),
    )
    return PlannedTurn(plans=(plan,), primary=0, disclosures=((0, "not_exact_match"),))


def _outcome():
    return GateOutcome(decision=AmbiguityDecision(verdict="act", reason="none", question=None))


def _deps():
    return NS(db=sh.RecordingDb(), llm=sh.StageLLM())


async def test_the_named_product_found_by_search_gets_the_detail(monkeypatch, electronics):
    calls = _search_stub(monkeypatch, electronics, {"xiaomi phone 3": ["el-03", "el-09"]})
    served = []

    async def details(ctx, deps, pid):
        served.append(pid)
        ctx.set_reply(f"detaliu {pid}", cacheable=False)

    monkeypatch.setattr(kx.det, "serve_details", details)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "xiaomi phone 3 are 5g?")
    planned = _named("detail", ["xiaomi phone 3"])
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    not_exact = kx.kernel_sentence(electronics.pack, "ro", "not_exact_match")
    assert served == ["el-03"] and not_exact not in ctx.reply.text
    assert calls == ["xiaomi phone 3"]
    # căutarea de rezolvare nu lasă sesiune și nici evenimente de căutare
    assert "active_search" not in ctx.state_patch
    assert not [e for e in ctx.events if e.type == "product_search"]
    assert [e for e in ctx.events if e.type == "kernel_name_resolved"]


async def test_two_names_resolved_are_compared(monkeypatch, electronics):
    _search_stub(
        monkeypatch,
        electronics,
        {"xiaomi phone 3": ["el-03", "el-01"], "apple phone 2": ["el-02", "el-08"]},
    )
    seen = []

    async def comparison(ctx, deps, ids, **kw):
        seen.append(list(ids))
        ctx.set_reply("comparatie", cacheable=False)
        return True

    monkeypatch.setattr(kx.det, "serve_comparison", comparison)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "compara")
    planned = _named("compare", ["xiaomi phone 3", "apple phone 2"])
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert seen == [["el-03", "el-02"]]


async def test_the_found_products_are_judged_by_the_answer_policy(monkeypatch, electronics):
    """Recenzia (I12): produsele găsite prin căutare intră la politica comparației ca parteneri."""
    _search_stub(
        monkeypatch,
        electronics,
        {"xiaomi phone 3": ["el-03"], "apple phone 2": ["el-02"]},
    )
    judged = []

    async def comparison(ctx, deps, ids, *, withhold=None, **kw):
        if withhold is not None:
            withhold([{"id": i} for i in ids])
        ctx.set_reply("comparatie", cacheable=False)
        return True

    def policy_for(partners, rows):
        judged.append(tuple(partners))
        return None

    monkeypatch.setattr(kx.det, "serve_comparison", comparison)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "compara")
    planned = _named("compare", ["xiaomi phone 3", "apple phone 2"])
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome(), policy_for) is True
    assert judged == [("el-03", "el-02")]


async def test_a_find_is_promoted_only_on_one_product(monkeypatch, electronics):
    """Un `find` pe un nume care prinde două produse rămâne lista (căutarea de azi)."""
    _search_stub(monkeypatch, electronics, {"phone": ["el-01", "el-02"]})
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "aveti phone?")
    planned = _named("find", ["phone"])
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert not [e for e in ctx.events if e.type == "kernel_name_resolved"]


async def test_a_name_the_results_do_not_carry_keeps_todays_search(monkeypatch, electronics):
    _search_stub(monkeypatch, electronics, {"lenovo phone 77": ["el-05"]})
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "lenovo phone 77")
    planned = _named("detail", ["lenovo phone 77"])
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    not_exact = kx.kernel_sentence(electronics.pack, "ro", "not_exact_match")
    assert ctx.reply.text.startswith(not_exact)
    assert not [e for e in ctx.events if e.type == "kernel_name_resolved"]


# --- NX-381 pe actul promovat (merge cu NX-380/381) ---------------------------------------------


def test_the_question_travels_with_the_promoted_detail():
    refs = [{"id": "r1", "text": "xiaomi phone 3", "kind": "name", "name": "xiaomi phone 3"}]
    acts = [{"kind": "detail", "targets": ["r1"], "question": "are 5g?"}]
    planned = plan_turn(
        _interp(acts=acts, references=refs),
        _state("telefoane"),
        (),
        [_ref("r1", "not_found", [], kind="name")],
        _gate(),
        changed=False,
        pack=fc.pack("electronics"),
        vocab=fc.vocabulary("electronics"),
        locale="ro",
    )
    plan = planned.plans[planned.primary]
    assert (plan.then, plan.question) == ("detail", "are 5g?")


async def test_the_promoted_detail_answers_the_question(monkeypatch, electronics):
    _search_stub(monkeypatch, electronics, {"xiaomi phone 3": ["el-03"]})
    answered = []

    async def answer(ctx, deps, pid, question):
        answered.append((pid, question))
        ctx.set_reply("Da, are 5G.", cacheable=False)
        return True

    monkeypatch.setattr(kx, "_answer_detail", answer)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "xiaomi phone 3 are 5g?")
    planned = _named("detail", ["xiaomi phone 3"])
    plan = planned.plans[0].model_copy(update={"question": "are 5g?"})
    planned = PlannedTurn(plans=(plan,), primary=0, disclosures=planned.disclosures)
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert answered == [("el-03", "are 5g?")]


async def test_with_the_composer_the_promoted_detail_is_composed(monkeypatch, electronics):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "composer_detail_enabled", True)
    _search_stub(monkeypatch, electronics, {"xiaomi phone 3": ["el-03"]})
    composed = []

    async def compose_detail(ctx, deps, pid, question):
        composed.append((pid, question))
        ctx.set_reply("Da, are 5G.", cacheable=False)
        return True

    monkeypatch.setattr(kx, "_compose_detail", compose_detail)
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "xiaomi phone 3 are 5g?")
    planned = _named("detail", ["xiaomi phone 3"])
    plan = planned.plans[0].model_copy(update={"question": "are 5g?"})
    planned = PlannedTurn(plans=(plan,), primary=0, disclosures=planned.disclosures)
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert composed == [("el-03", "are 5g?")]
