"""NX-144 felia 1 — `render` (faza F, `src/agent/finalize.py`). Testează DISPATCH-ul scos din
`agent_stage`: rich → proză (downgrade), proză passthrough, fallback la preț negroundat, status
comandă grounded, no-result. Grounding-ul (validator) rămâne poarta; aici verificăm regia
render→validate→recover. ZERO DB; LLM scriptat."""

from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("D", "ecommerce", "ro", ["Parfumuri"], [])
PROD = {
    "id": "p1",
    "name": "Rhea Soft",
    "brand": "Rhea",
    "price": 18.99,
    "url": "https://shop/p1",
    "product_url": "https://shop/p1",
    "ai_summary": "parfum lejer",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["lejer"],
}
RICH_JSON = {
    "intro": "recomand:",
    "items": [{"product_id": "p1", "pro_index": 0, "fit_clause": "lejer"}],
    "pick": {"product_id": "p1", "justification": "cel mai bun"},
    "education": None,
    "suggestions": ["altă variantă?"],
}


class _LLM:
    def __init__(self, *, rich=None, prose="Uite o variantă bună."):
        self._rich = rich
        self._prose = prose

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return self._prose

    async def complete_schema(self, system, user, schema, *, model=None):
        if self._rich is None:
            raise RuntimeError("no structured output")
        return self._rich


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau un parfum"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


def _deps(llm):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _plan(**kw):
    base = dict(products=[], final="", is_order=False, query="vreau un parfum", history="", inp=INP)
    base.update(kw)
    return ResponsePlan(handled=False, **base)


async def test_rich_success_sets_rich_reply():
    ctx = _ctx()
    await render(ctx, _deps(_LLM(rich=RICH_JSON)), _plan(products=[dict(PROD)], mode="rich"))
    assert ctx.reply is not None and ctx.reply.rich is not None
    ev = [e for e in ctx.events if e.type == "agent_recommended"]
    assert ev and ev[0].properties.get("rich") is True


async def test_rich_failure_downgrades_to_prose():
    ctx = _ctx()
    # complete_schema aruncă → rich None → downgrade la proză (_finalize).
    await render(ctx, _deps(_LLM(rich=None)), _plan(products=[dict(PROD)], final="", mode="rich"))
    assert ctx.reply is not None and ctx.reply.rich is None  # proză, nu rich
    assert any(e.type == "rich_downgraded" for e in ctx.events)


async def test_prose_passthrough_when_no_products_and_valid():
    ctx = _ctx()
    # Fără produse, text de clarificare fără preț inventat → servit verbatim.
    await render(ctx, _deps(_LLM()), _plan(final="Ce buget ai în minte?", mode="prose"))
    assert ctx.reply is not None and ctx.reply.text == "Ce buget ai în minte?"


async def test_invalid_price_no_products_falls_back_safe():
    ctx = _ctx()
    # Preț negroundat fără produse care să-l susțină → mesaj sigur, necacheabil (anti-poisoning).
    await render(ctx, _deps(_LLM()), _plan(final="Costă doar 999 lei!", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.cacheable is False
    assert "999" not in ctx.reply.text


async def test_order_grounded_uses_facts():
    ctx = _ctx()
    plan = _plan(
        is_order=True, final="Comanda ta e livrată.", order_views=["status: livrata"], mode="order"
    )
    await render(ctx, _deps(_LLM()), plan)
    assert ctx.reply is not None and ctx.reply.text  # status servit, non-tăcere


async def test_no_products_no_final_no_result():
    ctx = _ctx()
    await render(ctx, _deps(_LLM()), _plan(final="", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.cacheable is False
