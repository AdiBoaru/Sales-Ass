"""Teste unit pentru stagiul de Triaj (nano) — LLM mockuit, fără DB/apeluri reale."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages.triage import triage_stage


class FakeLLM:
    """Adaptor LLM fals: întoarce un payload canonic sau ridică o excepție."""

    def __init__(self, payload: dict | None = None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls = 0

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.payload or {}


class FakeConn:
    """Conn fals pentru `list_category_slugs` — întoarce slug-urile date."""

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    async def fetch(self, *args, **kwargs):
        return [{"slug": s} for s in self._slugs]


def _ctx(body: str | None = "salut") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )


def _deps(llm, slugs=("creme-fata", "balsamuri")) -> PipelineDeps:
    return PipelineDeps(conn=FakeConn(list(slugs)), redis=None, llm=llm)


async def test_simple_sets_route_and_reply():
    """route=simple → ctx.route setat + reply compus de nano (early exit)."""
    ctx = _ctx("mulțumesc!")
    llm = FakeLLM({"route": "simple", "category_key": None, "reply": "Cu plăcere!"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route is not None
    assert ctx.route.route == Route.SIMPLE
    assert ctx.reply is not None
    assert ctx.reply.text == "Cu plăcere!"
    assert any(e.type == "intent_detected" for e in ctx.events)


async def test_clarify_sets_reply():
    """route=clarify → întrebare de clarificare ca reply."""
    ctx = _ctx("ceva")
    llm = FakeLLM({"route": "clarify", "missing_field": "produs", "reply": "Ce produs cauți?"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.CLARIFY
    assert ctx.reply.text == "Ce produs cauți?"


async def test_sales_valid_category_no_reply():
    """route=sales cu categorie validă → category_key setat, FĂRĂ reply (agentul G4 răspunde)."""
    ctx = _ctx("vreau o cremă de față")
    llm = FakeLLM({"route": "sales", "category_key": "creme-fata", "reply": None})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES
    assert ctx.route.category_key == "creme-fata"
    assert ctx.reply is None


async def test_invented_category_is_dropped():
    """Categorie inventată (în afara listei DB) → aruncată (nu rutăm pe ghicit)."""
    ctx = _ctx("vreau ceva")
    llm = FakeLLM({"route": "sales", "category_key": "inexistent-xyz", "reply": None})
    await triage_stage(ctx, _deps(llm, slugs=("creme-fata",)))
    assert ctx.route.route == Route.SALES
    assert ctx.route.category_key is None


async def test_no_llm_is_noop():
    """Fără cheie OpenAI (llm=None) → no-op, echo fallback va răspunde."""
    ctx = _ctx("salut")
    await triage_stage(ctx, _deps(None))
    assert ctx.route is None
    assert ctx.reply is None


async def test_empty_body_is_noop():
    ctx = _ctx(body=None)
    await triage_stage(ctx, _deps(FakeLLM({"route": "simple", "reply": "x"})))
    assert ctx.route is None


async def test_llm_error_is_graceful():
    """Eroare de API / JSON invalid → degradare grațioasă (no-op), nu crash."""
    ctx = _ctx("salut")
    llm = FakeLLM(exc=ValueError("bad json"))
    await triage_stage(ctx, _deps(llm))
    assert ctx.route is None
    assert ctx.reply is None


async def test_invalid_route_value_is_graceful():
    """route necunoscut → ValidationError prinsă → no-op."""
    ctx = _ctx("salut")
    llm = FakeLLM({"route": "bla-bla", "reply": None})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route is None
