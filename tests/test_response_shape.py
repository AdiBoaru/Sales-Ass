"""NX-159 felia 1 — telemetria de calitate a formei răspunsului (response_shape + completeness_gap).

Testează helper-ele PURE (`reply_shape`/`completeness_gaps`) direct + integrarea prin `run_pipeline`
(hook global post-reply, excludere `halt`, kill-switch). Fără DB/LLM → rulează în CI.
"""

from src.agent import response_quality as rq
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    Offer,
    Reply,
    RichReply,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps, run_pipeline


def _ctx(body: str | None = "salut") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="wamid.1", body=body),
        conversation_id="conv1",
    )


# --- reply_shape (pur) --------------------------------------------------------


def test_shape_short_reply_flags_under_20():
    ctx = _ctx()
    ctx.reply = Reply(text="Da.")
    s = rq.reply_shape(ctx, "triage_stage")
    assert s["chars"] == 3
    assert s["under_20"] is True
    assert s["has_question"] is False
    assert s["stage"] == "triage_stage"


def test_shape_rich_reply():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.reply = Reply(
        text="Am ales câteva variante potrivite pentru tine. Vrei ceva mai hidratant?",
        rich=RichReply(intro="x", items=[], pick=None, education=None, chips=[], disclaimer=""),
        products=[{"id": "p1"}],
        suggestions=["Una mai ieftină"],
    )
    s = rq.reply_shape(ctx, "agent_stage")
    assert s["is_rich"] is True
    assert s["has_products"] is True
    assert s["has_suggestions"] is True
    assert s["has_question"] is True
    assert s["under_20"] is False
    assert s["route"] == "sales"


def test_shape_never_leaks_text_or_pii():
    ctx = _ctx()
    ctx.reply = Reply(text="Comanda ta ORD-123 pentru 0722123456 e pe drum.")
    s = rq.reply_shape(ctx, "agent_stage")
    # P12: DOAR forma — niciun câmp nu conține fragmente din text.
    assert all(not isinstance(v, str) or "ORD-123" not in v for v in s.values())
    assert "0722" not in str(s.values())


# --- completeness_gaps (pur) --------------------------------------------------


def test_gap_sales_with_products_no_next_step():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.reply = Reply(text="Îți recomand crema X.", products=[{"id": "p1"}])
    assert rq.completeness_gaps(ctx) == ["next_step"]


def test_no_gap_sales_with_products_and_question():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.reply = Reply(
        text="Îți recomand crema X. Vrei ten uscat sau gras?", products=[{"id": "p1"}]
    )
    assert rq.completeness_gaps(ctx) == []


def test_gap_sales_no_results_no_alternative():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.reply = Reply(text="Momentan n-am găsit produse potrivite.")  # fundătură
    assert rq.completeness_gaps(ctx) == ["alternative"]


def test_no_gap_sales_no_results_with_suggestions():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.reply = Reply(text="N-am găsit exact asta.", suggestions=["Alt brand", "Mărește bugetul"])
    assert rq.completeness_gaps(ctx) == []


def test_gap_clarify_without_question():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.CLARIFY)
    ctx.reply = Reply(text="Am nevoie de mai multe detalii.")
    assert rq.completeness_gaps(ctx) == ["question"]


def test_no_gap_order_with_offer():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.ORDER)
    ctx.reply = Reply(
        text="Comanda ta e pe drum.", offer=Offer(kind="open_url", label="x", url="u")
    )
    assert rq.completeness_gaps(ctx) == []


def test_gap_order_cold_no_field_asked():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.ORDER)
    ctx.reply = Reply(text="Verific comanda.")  # nici date, nici câmp cerut
    assert rq.completeness_gaps(ctx) == ["asked_field"]


def test_no_route_no_gap():
    ctx = _ctx()
    ctx.reply = Reply(text="salut!")  # welcome/cache — fără rută → fără gap
    assert rq.completeness_gaps(ctx) == []


# --- integrare prin runner ----------------------------------------------------


async def test_runner_emits_response_shape_on_reply():
    async def stage(ctx, deps):
        ctx.set_reply("Da.")

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [stage])
    ev = [e for e in ctx.events if e.type == "response_shape"]
    assert ev and ev[0].properties["under_20"] is True
    assert ev[0].properties["stage"] == "stage"


async def test_runner_no_shape_on_halt():
    async def gate(ctx, deps):
        ctx.halt = True  # tăcere intenționată — niciun reply

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [gate])
    assert not any(e.type == "response_shape" for e in ctx.events)


async def test_runner_emits_completeness_gap():
    async def stage(ctx, deps):
        ctx.route = RouteDecision(route=Route.SALES)
        ctx.set_reply("Momentan n-am găsit produse potrivite.", cacheable=False)

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [stage])
    ev = [e for e in ctx.events if e.type == "completeness_gap"]
    assert ev and ev[0].properties["missing"] == ["alternative"]
    assert ev[0].properties["intent"] == "sales"


async def test_runner_telemetry_disabled(monkeypatch):
    from src.worker import runner as rnr

    monkeypatch.setattr(
        rnr,
        "get_settings",
        lambda: __import__("types").SimpleNamespace(
            response_telemetry_enabled=False,
            turn_budget_alerts_enabled=False,
        ),
    )

    async def stage(ctx, deps):
        ctx.set_reply("Da.")

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [stage])
    assert not any(e.type == "response_shape" for e in ctx.events)
