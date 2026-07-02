"""Teste unit pentru stagiul de Triaj (nano) — LLM mockuit, fără DB/apeluri reale."""

from src.domain.pack import DomainPack
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


# --- NX-116: confidence + sloturi structurate ------------------------------------------


def _ctx_dp(body, concern_map=None):
    ctx = _ctx(body)
    ctx.business.domain_pack = DomainPack(
        vertical="beauty_salon", concern_map=concern_map or {"ten gras": "oily"}
    )
    return ctx


async def test_slots_populate_route_filters():
    ctx = _ctx_dp("crema spf sub 200 pentru ten gras")
    llm = FakeLLM(
        {
            "route": "sales",
            "category_key": "creme-fata",
            "confidence": "high",
            "slots": {"budget_max": 200, "concerns": ["ten gras"], "suitable_for": "fata"},
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES
    assert ctx.route.filters["budget_max"] == 200.0
    assert ctx.route.filters["concerns"] == ["ten gras"]
    assert ctx.route.filters["suitable_for"] == "fata"


async def test_unknown_concern_dropped():
    ctx = _ctx_dp("ceva")
    llm = FakeLLM(
        {
            "route": "sales",
            "category_key": "creme-fata",
            "confidence": "high",
            "slots": {"concerns": ["ten gras", "inventat xyz"]},
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.filters["concerns"] == ["ten gras"]  # necunoscutul aruncat (vocab DomainPack)


async def test_negative_budget_dropped_rest_kept():
    ctx = _ctx("ceva")
    llm = FakeLLM(
        {
            "route": "sales",
            "category_key": "creme-fata",
            "confidence": "high",
            "slots": {"budget_max": -5, "brand": "Nivea"},
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert "budget_max" not in ctx.route.filters  # negativ aruncat
    assert ctx.route.filters["brand"] == "Nivea"  # restul rămâne


async def test_low_confidence_forces_clarify():
    ctx = _ctx("ceva bun")
    llm = FakeLLM({"route": "sales", "category_key": None, "confidence": "low", "reply": None})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.CLARIFY  # cod forțează clarify, nu sales pe ghicit
    assert ctx.reply is not None and ctx.reply.text  # întrebare generică (fallback)
    assert ctx.reply.pending_question is not None


async def test_low_confidence_does_not_override_handoff():
    ctx = _ctx("vreau un om")
    llm = FakeLLM({"route": "handoff", "confidence": "low"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.HANDOFF  # handoff e terminal, nu-l forțăm clarify


async def test_backcompat_no_confidence_slots():
    ctx = _ctx("vreau o crema")
    llm = FakeLLM({"route": "sales", "category_key": "creme-fata"})  # fără confidence/slots
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES  # med default → nu forțează clarify
    assert ctx.route.filters == {}


async def test_intent_detected_includes_confidence():
    ctx = _ctx("vreau o crema")
    llm = FakeLLM({"route": "sales", "category_key": "creme-fata", "confidence": "high"})
    await triage_stage(ctx, _deps(llm))
    ev = next(e for e in ctx.events if e.type == "intent_detected")
    assert ev.properties["confidence"] == "high"


async def test_purchase_intent_set_on_sales():
    """A2: purchase_intent=true pe sales → propagat în RouteDecision + intent_detected."""
    ctx = _ctx("îl iau, adaugă în coș")
    llm = FakeLLM({"route": "sales", "category_key": "creme-fata", "purchase_intent": True})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.purchase_intent is True
    ev = next(e for e in ctx.events if e.type == "intent_detected")
    assert ev.properties["purchase_intent"] is True


async def test_purchase_intent_forced_false_off_sales():
    """A2: purchase_intent are sens DOAR pe sales — pe clarify e forțat False."""
    ctx = _ctx("ceva")
    llm = FakeLLM({"route": "clarify", "missing_field": "produs", "purchase_intent": True})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.CLARIFY
    assert ctx.route.purchase_intent is False


async def test_purchase_intent_backcompat_defaults_false():
    """Nano vechi fără câmp → purchase_intent False (back-compat)."""
    ctx = _ctx("vreau o crema")
    llm = FakeLLM({"route": "sales", "category_key": "creme-fata"})
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.purchase_intent is False


# --- NX-136: închidere conversațională + chips pe categorii adiacente ---------


async def test_closure_attaches_sibling_chips(monkeypatch):
    """Închidere după produse → mesaj cald + chips pe categorii surori, reply necacheabil."""
    from src.models import ConversationState
    from src.worker.stages import triage as triage_mod

    async def fake_siblings(conn, business_id, slug, *, limit=4):
        assert slug == "creme-fata"  # sursa = state.search_constraints.category_key
        return ["Geluri de curățare", "Tonere"]

    monkeypatch.setattr(triage_mod, "sibling_categories", fake_siblings)

    ctx = _ctx("mulțumesc, asta vreau!")
    ctx.state = ConversationState(search_constraints={"category_key": "creme-fata"})
    llm = FakeLLM({"route": "simple", "reply": "Mă bucur că ai găsit ce trebuie!", "closure": True})
    await triage_stage(ctx, _deps(llm))

    assert ctx.route.route == Route.SIMPLE
    assert ctx.reply.text == "Mă bucur că ai găsit ce trebuie!"
    assert ctx.reply.cacheable is False  # depinde de categorie → nu otrăvește cache-ul
    assert ctx.reply.suggestions == ["Recomandă-mi și Geluri de curățare", "Recomandă-mi și Tonere"]
    ev = [e for e in ctx.events if e.type == "closure_served"]
    assert ev and ev[0].properties == {"chips": 2, "category": "creme-fata", "turn_id": ctx.turn_id}


async def test_closure_without_category_is_warm_message_only(monkeypatch):
    """Închidere fără categorie discutată (state gol) → mesaj cald, ZERO chips (P6), fără crash."""
    from src.worker.stages import triage as triage_mod

    async def boom(*a, **k):
        raise AssertionError("fără category_key → nu interoga surorile")

    monkeypatch.setattr(triage_mod, "sibling_categories", boom)

    ctx = _ctx("mersi, gata!")  # ctx.state implicit → search_constraints = {}
    llm = FakeLLM({"route": "simple", "reply": "Cu drag! O zi bună!", "closure": True})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply.text == "Cu drag! O zi bună!"
    assert ctx.reply.suggestions == []
    ev = [e for e in ctx.events if e.type == "closure_served"]
    assert ev and ev[0].properties["chips"] == 0 and ev[0].properties["category"] is None


async def test_simple_non_closure_has_no_chips():
    """`simple` fără closure (salut normal) → reply cacheabil, fără chips (byte-identic cu azi)."""
    ctx = _ctx("salut")
    llm = FakeLLM({"route": "simple", "reply": "Bună! Cu ce te pot ajuta?", "closure": False})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply.cacheable is True and ctx.reply.suggestions == []
    assert not any(e.type == "closure_served" for e in ctx.events)


async def test_closure_kill_switch_off(monkeypatch):
    """Kill-switch OFF → mesajul cald simplu, fără chips (comportamentul vechi pe `simple`)."""
    from src.config import get_settings
    from src.models import ConversationState

    monkeypatch.setattr(get_settings(), "closure_chips_enabled", False)
    ctx = _ctx("mulțumesc, asta vreau!")
    ctx.state = ConversationState(search_constraints={"category_key": "creme-fata"})
    llm = FakeLLM({"route": "simple", "reply": "Cu plăcere!", "closure": True})
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply.cacheable is True and ctx.reply.suggestions == []
    assert not any(e.type == "closure_served" for e in ctx.events)


async def test_purchase_intent_stays_sales_not_closure():
    """„asta vreau, adaugă în coș" = purchase_intent (sales), NU closure."""
    ctx = _ctx("asta vreau, adaugă în coș")
    llm = FakeLLM(
        {"route": "sales", "category_key": "creme-fata", "purchase_intent": True, "closure": False}
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES and ctx.route.purchase_intent is True
    assert not any(e.type == "closure_served" for e in ctx.events)
