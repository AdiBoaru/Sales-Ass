"""NX-159 felia 2 — thin-path repair: căile subțiri non-rich nu mai închid sec.

Testează predicatele deterministe (`_is_short_ack`, `_thin_path_chips`) + integrarea prin `render`
pe calea no-result (chips de continuare atașate). Fără DB/LLM real → rulează în CI.
"""

from src.agent import finalize as fz
from src.agent.fallbacks import _is_short_ack, _thin_path_chips
from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Reply,
    TurnContext,
)
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("D", "ecommerce", "ro", ["Parfumuri"], [])


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return "Uite o variantă."

    async def complete_schema(self, system, user, schema, *, model=None):
        raise RuntimeError("no structured output")


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau ceva"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=_LLM())


def _plan(**kw):
    base = dict(products=[], final="", is_order=False, query="vreau ceva", history="", inp=INP)
    base.update(kw)
    return ResponsePlan(handled=False, **base)


# --- predicate pure -----------------------------------------------------------


def test_is_short_ack_true_on_bare_ack():
    assert _is_short_ack("Da.") is True
    assert _is_short_ack("Ok") is True
    assert _is_short_ack("Cu plăcere!") is True


def test_is_short_ack_false_when_has_question():
    # Scurt DAR cu întrebare = nano a întrebat ceva → nu e fundătură.
    assert _is_short_ack("Vrei X?") is False


def test_is_short_ack_false_when_long():
    assert _is_short_ack("Îți recomand crema hidratantă potrivită pentru ten uscat.") is False


def test_is_short_ack_empty():
    assert _is_short_ack("") is False
    assert _is_short_ack(None) is False


def test_thin_path_chips_per_locale():
    assert len(_thin_path_chips("ro")) == 3
    assert _thin_path_chips("en") != _thin_path_chips("ro")
    assert _thin_path_chips("xx") == _thin_path_chips("ro")  # necunoscut → ro


# --- _attach_no_result_alternatives (gating) ----------------------------------


def test_attach_no_result_sets_suggestions():
    ctx = _ctx()
    ctx.reply = Reply(text="Momentan n-am găsit produse potrivite.")
    fz._attach_no_result_alternatives(ctx)
    assert ctx.reply.suggestions == _thin_path_chips("ro")


def test_attach_no_result_noop_without_reply():
    ctx = _ctx()
    fz._attach_no_result_alternatives(ctx)  # reply None → no-op, fără excepție
    assert ctx.reply is None


def test_attach_no_result_disabled(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        fz, "get_settings", lambda: SimpleNamespace(no_result_alternatives_enabled=False)
    )
    ctx = _ctx()
    ctx.reply = Reply(text="n-am găsit")
    fz._attach_no_result_alternatives(ctx)
    assert ctx.reply.suggestions == []


# --- integrare prin render (no-result sales) ----------------------------------


async def test_render_no_result_sales_attaches_chips():
    ctx = _ctx()
    # Fără produse, fără text → no-result terminal (else) pe SALES → chips de continuare.
    await render(ctx, _deps(), _plan(final="", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.suggestions == _thin_path_chips("ro")


async def test_render_no_result_order_no_chips():
    ctx = _ctx()
    # ORDER no-result → cere numărul comenzii (propriul flux), fără chips generice de sales.
    await render(ctx, _deps(), _plan(final="", is_order=True, mode="fallback"))
    assert ctx.reply is not None and ctx.reply.suggestions == []
    assert "comenzii" in ctx.reply.text.lower()


async def test_render_ungrounded_price_no_products_attaches_chips():
    ctx = _ctx()
    # Preț negroundat fără produse → mesaj sigur necacheabil + chips de continuare.
    await render(ctx, _deps(), _plan(final="Costă doar 999 lei!", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.cacheable is False
    assert "999" not in ctx.reply.text
    assert ctx.reply.suggestions == _thin_path_chips("ro")
