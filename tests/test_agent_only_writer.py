"""NX-297 felia 1 — nano își pierde pixul: `simple`/`clarify` ajung la agent, nu la client.

Poarta e o SINGURĂ retrogradare de rută în triaj. Testele de aici verifică exact cele două stări:
stins = comportamentul de azi, byte-identic; aprins = nano clasifică, nu răspunde.
"""

from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, Route, TurnContext
from src.worker.runner import PipelineDeps, run_pipeline
from src.worker.stages.triage import triage_stage


class FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        self.calls += 1
        return self.payload


class FakeConn:
    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    async def fetch(self, *args, **kwargs):
        return [{"slug": s} for s in self._slugs]


def _ctx(body: str) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


def _deps(llm) -> PipelineDeps:
    return PipelineDeps(conn=FakeConn(["creme-fata", "balsamuri"]), redis=None, llm=llm)


def _on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_only_writer_enabled", True)


# ── stins: comportamentul de azi ───────────────────────────────────────────────────────────────


async def test_off_clarify_still_answered_by_nano():
    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "clarify", "reply": "Pentru ce tip de ten?", "missing_field": "intent"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.text == "Pentru ce tip de ten?"
    assert ctx.route.route == Route.CLARIFY
    assert not any(e.type == "triage_demoted" for e in ctx.events)


async def test_off_simple_still_answered_by_nano():
    ctx = _ctx("mulțumesc!")
    llm = FakeLLM({"route": "simple", "reply": "Cu plăcere!"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply is not None and ctx.reply.text == "Cu plăcere!"
    assert ctx.route.route == Route.SIMPLE


# ── aprins: nano clasifică, agentul răspunde ───────────────────────────────────────────────────


async def test_on_clarify_is_demoted_to_sales_without_reply(monkeypatch):
    """Turul NU se închide: fără reply, runner-ul merge mai departe spre agent."""
    _on(monkeypatch)
    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "clarify", "reply": "Pentru ce tip de ten?", "missing_field": "intent"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply is None
    assert ctx.route.route == Route.SALES
    demoted = [e for e in ctx.events if e.type == "triage_demoted"]
    assert len(demoted) == 1 and demoted[0].properties["original"] == "clarify"


async def test_on_simple_is_demoted_too(monkeypatch):
    _on(monkeypatch)
    ctx = _ctx("mulțumesc!")
    llm = FakeLLM({"route": "simple", "reply": "Cu plăcere!"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply is None
    assert ctx.route.route == Route.SALES


async def test_on_keeps_the_slots_nano_extracted(monkeypatch):
    """Retrogradarea pierde PIXUL, nu OCHII: sloturile și categoria supraviețuiesc, fiindcă
    agentul le folosește ca seed de căutare, iar stiva de constrângeri se hrănește din ele."""
    _on(monkeypatch)
    ctx = _ctx("vreau ceva pentru ten uscat, sub 100 lei")
    llm = FakeLLM(
        {
            "route": "clarify",
            "reply": "Pentru ce tip de ten?",
            "category_key": "creme-fata",
            "slots": {"budget_max": 100, "concerns": ["ten uscat"]},
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES
    assert ctx.route.category_key == "creme-fata"
    assert ctx.route.filters.get("budget_max") == 100


async def test_on_does_not_invent_purchase_intent(monkeypatch):
    """`purchase_intent` e calculat DOAR pe sales-ul real al nano-ului. Un `clarify` retrogradat
    nu are voie să devină intenție de cumpărare: ar declanșa oferta deterministă de checkout."""
    _on(monkeypatch)
    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "clarify", "reply": "?", "purchase_intent": True})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.purchase_intent is False


async def test_on_the_turn_actually_reaches_the_agent(monkeypatch):
    """Proba care contează: pe pipeline-ul REAL, un `clarify` nu mai încheie turul.

    Unit-testele de mai sus arată că triajul nu scrie. Asta arată că runner-ul merge mai departe
    și că răspunsul pe care îl vede clientul e al agentului — adică exact ce cumpără felia."""
    _on(monkeypatch)

    async def _agent_like_stage(ctx, deps):
        ctx.set_reply("Pentru ten uscat îți recomand crema X, 89 lei.")

    _agent_like_stage.__name__ = "agent_stage"

    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "clarify", "reply": "Pentru ce tip de ten?", "missing_field": "intent"})
    await run_pipeline(ctx, _deps(llm), [triage_stage, _agent_like_stage])

    assert ctx.reply.text == "Pentru ten uscat îți recomand crema X, 89 lei."
    exits = [e for e in ctx.events if e.type == "pipeline_early_exit"]
    assert [e.properties["stage"] for e in exits] == ["agent_stage"]


async def test_off_the_turn_still_stops_at_triage():
    """Perechea obligatorie: cu flagul stins, turul se oprește la triaj, ca azi."""

    async def _agent_like_stage(ctx, deps):
        ctx.set_reply("nu ar trebui să ajungă aici")

    _agent_like_stage.__name__ = "agent_stage"

    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "clarify", "reply": "Pentru ce tip de ten?", "missing_field": "intent"})
    await run_pipeline(ctx, _deps(llm), [triage_stage, _agent_like_stage])

    assert ctx.reply.text == "Pentru ce tip de ten?"
    exits = [e for e in ctx.events if e.type == "pipeline_early_exit"]
    assert [e.properties["stage"] for e in exits] == ["triage_stage"]


async def test_on_leaves_sales_and_order_untouched(monkeypatch):
    """Poarta atinge EXACT două rute. Dacă ar atinge `order`, zidul de login ar dispărea tăcut."""
    _on(monkeypatch)
    for value, expected in (("sales", Route.SALES), ("order", Route.ORDER)):
        ctx = _ctx("unde e comanda mea")
        await triage_stage(ctx, _deps(FakeLLM({"route": value, "confidence": "high"})))
        assert ctx.route.route is expected
        assert not any(e.type == "triage_demoted" for e in ctx.events)
