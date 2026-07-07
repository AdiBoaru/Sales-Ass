"""NX-144 felia 1 — `build_plan` (faza E, `src/agent/planner.py`). Testează SHAPING-ul determinist
post-loop scos din `agent_stage`: mode + set de produse pe fixture-urile cheaper/cross-sell/compare,
plus ramurile care răspund direct (`handled=True`: login / „deja cel mai ieftin"). ZERO DB/OpenAI
reale — `ToolRun` e umplut manual, iar query-urile de catalog sunt monkeypatch-uite pe `planner`."""

import pytest

from src.agent import planner as planner_mod
from src.agent.planner import _ATTR_QUERY_RE, ResponsePlan, build_plan
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_executor import ToolRun
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps

SHOWN = [
    ProductRef(product_id="p-mid", name="Pure Arc Daily", price=80.99),
    ProductRef(product_id="p-hi", name="Ardent Lab Calm", price=97.99),
]
PROD = {
    "id": "p-cheap",
    "name": "Rhea Organics Soft",
    "brand": "Rhea",
    "price": 18.99,
    "url": "https://shop/cheap",
    "ai_summary": "parfum lejer",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["lejer"],
}
INP = PromptInputs.build("D", "ecommerce", "ro", ["Parfumuri"], [])


class _FakeLLM:
    """`complete_schema` → recomandare rich (cross-sell); `complete` → proză de retry."""

    def __init__(self, *, rich=None):
        self._rich = rich

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "ok"

    async def complete_schema(self, system, user, schema, *, model=None):
        return self._rich


def _ctx(body="ceva", *, displayed=(), purchase=False):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(displayed_products=list(displayed)),
    )
    ctx.language = "ro"
    ctx.route = RouteDecision(route=Route.SALES, purchase_intent=purchase)
    return ctx


def _deps(llm=None):
    return PipelineDeps(conn=object(), redis=None, llm=llm or _FakeLLM())


async def _plan(ctx, run, deps, *, final="", retrieved=None, is_order=False, show_more=False):
    return await build_plan(
        ctx,
        deps,
        run,
        INP,
        final=final,
        retrieved=list(retrieved or []),
        is_order=is_order,
        show_more=show_more,
        query=ctx.message.body,
        history="",
        tool_names=["search_products", "checkout_link"],
    )


# --- mode + produse pe fixture (DoD) ---------------------------------------------------------


async def test_products_present_mode_rich():
    ctx = _ctx()
    run = ToolRun(ctx, _deps())
    plan = await _plan(ctx, run, _deps(), retrieved=[dict(PROD)])
    assert plan.handled is False
    assert plan.mode == "rich"
    assert [p["id"] for p in plan.products] == ["p-cheap"]
    assert ctx.retrieval is not None and ctx.retrieval.source == "tools"


async def test_compare_sets_comparison_mode():
    ctx = _ctx()
    deps = _deps()
    run = ToolRun(ctx, deps)
    run.compared = [dict(PROD), dict(PROD, id="p2")]
    plan = await _plan(ctx, run, deps, retrieved=[dict(PROD)])
    assert plan.mode == "comparison"
    assert plan.compared == run.compared


async def test_empty_retrieval_mode_fallback():
    ctx = _ctx()
    deps = _deps()
    plan = await _plan(ctx, ToolRun(ctx, deps), deps, retrieved=[])
    assert plan.handled is False
    assert plan.mode == "fallback"
    assert plan.products == []


async def test_cheaper_sets_products(monkeypatch):
    async def fake_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        assert max_excl == 80.99  # baseline = cel mai ieftin AFIȘAT
        return [dict(PROD)]

    monkeypatch.setattr(planner_mod, "search_cheaper_than", fake_cheaper)
    ctx = _ctx("ceva mai ieftin", displayed=SHOWN)
    deps = _deps()
    plan = await _plan(ctx, ToolRun(ctx, deps), deps)
    assert plan.handled is False
    assert [p["id"] for p in plan.products] == ["p-cheap"]
    assert ctx.retrieval.relevance is None  # calea deterministă nu setează off-category


async def test_cheaper_none_handled_with_reply(monkeypatch):
    async def empty_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        return []

    monkeypatch.setattr(planner_mod, "search_cheaper_than", empty_cheaper)
    ctx = _ctx("ceva mai ieftin", displayed=SHOWN)
    deps = _deps()
    plan = await _plan(ctx, ToolRun(ctx, deps), deps)
    assert plan == ResponsePlan(handled=True)  # ramura a răspuns direct → render sărit
    assert ctx.reply is not None and ctx.reply.cacheable is False
    assert "cea mai ieftină" in ctx.reply.text.lower()


async def test_order_gated_login_handled():
    ctx = _ctx()
    deps = _deps()
    run = ToolRun(ctx, deps)
    run.order_gated_login = True
    plan = await _plan(ctx, run, deps, is_order=True)
    assert plan == ResponsePlan(handled=True)
    assert ctx.reply is not None and ctx.reply.cacheable is False


async def test_cross_sell_handled_with_rich(monkeypatch):
    async def fake_complementary(conn, business_id, anchor_id, *, exclude_ids=None, limit=4):
        return [dict(PROD, id="c1"), dict(PROD, id="c2")]

    monkeypatch.setattr(planner_mod, "get_complementary_products", fake_complementary)
    rich_json = {
        "intro": "merg bine:",
        "items": [
            {"product_id": "c1", "pro_index": 0, "fit_clause": "x"},
            {"product_id": "c2", "pro_index": 0, "fit_clause": "y"},
        ],
        "pick": {"product_id": "c1", "justification": "z"},
        "education": None,
        "suggestions": ["mai vreau"],
    }
    ctx = _ctx()
    deps = _deps(_FakeLLM(rich=rich_json))
    run = ToolRun(ctx, deps)
    run.added_product = dict(PROD, id="p1", name="Ser Aqua")
    plan = await _plan(ctx, run, deps)
    assert plan.handled is True
    assert ctx.reply is not None and ctx.reply.rich is not None
    assert ctx.reply.rich.pick is None  # fără pick între complementare
    ev = [e for e in ctx.events if e.type == "cross_sell"]
    assert ev and ev[0].properties["n"] == 2


# --- _ATTR_QUERY_RE (superlativ pe setul afișat, NU căutare nouă) ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "care dintre ele e cea mai ieftină?",
        "care e cea mai ușoară dintre astea",
        "which of these is best?",
        "melyik a legolcsóbb?",
    ],
)
def test_attr_query_matches(text):
    assert _ATTR_QUERY_RE.search(text) is not None


@pytest.mark.parametrize("text", ["arată-mi ceva mai ieftin", "vreau un parfum de vară"])
def test_attr_query_ignores_new_search(text):
    assert _ATTR_QUERY_RE.search(text) is None
