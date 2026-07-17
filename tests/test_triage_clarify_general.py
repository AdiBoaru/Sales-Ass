"""NX-176a — clarify conversațional pentru cereri sub-specificate (GENERAL).

Aceste teste blochează CONTRACTUL de cod al căii clarify (nano scriptat): când triajul întoarce
`clarify` cu `missing_field` + `suggestions` + o replică conversațională, codul le persistă corect
(pending_question pentru resume + suggestions pe reply pentru contractul web). Judecata nano ÎNSĂȘI
(„vreau un laptop" → clarify, „cremă pentru riduri" → sales) e comportament de PROMPT — se verifică
live cu `scripts/sim/web_audit.py` (DB + OpenAI), NU aici (FakeLLM e scriptat).
"""

from src.domain.pack import DomainPack
from src.models import BusinessConfig, Contact, InboundMessage, Route, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages.triage import triage_stage


class FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def classify_json(self, system: str, user: str, *, model: str | None = None) -> dict:
        return self.payload


class FakeConn:
    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    async def fetch(self, *args, **kwargs):
        return [{"slug": s} for s in self._slugs]


def _ctx(body: str) -> TurnContext:
    ctx = TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )
    ctx.business.domain_pack = DomainPack(vertical="beauty_salon", concern_map={"ten gras": "oily"})
    return ctx


def _deps(llm, slugs=("creme-fata", "machiaj")) -> PipelineDeps:
    return PipelineDeps(conn=FakeConn(list(slugs)), redis=None, llm=llm)


async def test_underspecified_clarify_persists_slot_and_suggestions():
    """Cerere sub-specificată (nano → clarify) → slot persistat pt resume + chips pe reply (web)."""
    ctx = _ctx("fă-mi o rutină de machiaj")
    llm = FakeLLM(
        {
            "route": "clarify",
            "missing_field": "routine_look",
            "reply": "Super alegere! Ca să-ți fac o rutină care ți se potrivește, ce ai în minte "
            "— natural de zi, ceva de seară, sau pentru un eveniment?",
            "suggestions": ["Natural de zi", "Seară / glam", "Eveniment", "Rapid 5 min"],
        }
    )
    await triage_stage(ctx, _deps(llm))

    assert ctx.route.route == Route.CLARIFY
    # replica de consultant trece neatinsă (conversațională, cu „?")
    assert "?" in ctx.reply.text and len(ctx.reply.text) > 40
    # slotul semantic persistat → turul următor îl reia determinist (NX-130)
    assert ctx.reply.pending_question is not None
    assert ctx.reply.pending_question["field"] == "routine_look"
    assert ctx.reply.pending_question["resume_route"] == Route.SALES.value
    # chips = alternative SECUNDARE, ajung pe reply (render_web le trece prin _web_chips)
    assert ctx.reply.suggestions == ["Natural de zi", "Seară / glam", "Eveniment", "Rapid 5 min"]
    assert ctx.reply.cacheable is False  # specific contextului → nu otrăvește cache-ul


async def test_clarify_suggestions_capped_at_4():
    """Nano dă 6 chips → codul păstrează max 4 (butoane, nu listă)."""
    ctx = _ctx("vreau un laptop")
    llm = FakeLLM(
        {
            "route": "clarify",
            "missing_field": "use_case",
            "reply": "Sigur! Depinde mult de cum îl folosești — la ce te gândești?",
            "suggestions": [
                "Gaming",
                "Birou / office",
                "Ușor de cărat",
                "Sub 3000 lei",
                "Foto/video",
                "Programare",
            ],
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.CLARIFY
    assert len(ctx.reply.suggestions) == 4  # [:4] în cod


async def test_clarify_drops_empty_suggestions():
    """Chips goale/whitespace → aruncate la persistare."""
    ctx = _ctx("ceva de ten")
    llm = FakeLLM(
        {
            "route": "clarify",
            "missing_field": "need",
            "reply": "Cu drag! Ce te preocupă mai mult la ten?",
            "suggestions": ["Hidratare", "  ", "Anti-rid", ""],
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.reply.suggestions == ["Hidratare", "Anti-rid"]


async def test_specific_request_stays_sales():
    """Contract: când nano judecă că e destul context → sales. Codul respectă ruta."""
    ctx = _ctx("cremă pentru riduri sub 200")
    llm = FakeLLM(
        {
            "route": "sales",
            "category_key": "creme-fata",
            "confidence": "high",
            "slots": {"budget_max": 200, "concerns": []},
        }
    )
    await triage_stage(ctx, _deps(llm))
    assert ctx.route.route == Route.SALES
    assert ctx.reply is None  # agentul răspunde, nu întrebăm
