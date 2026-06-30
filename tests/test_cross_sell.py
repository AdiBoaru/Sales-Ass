"""#7b — cross-sell la add-to-cart (model iZi). ZERO DB/OpenAI: cart_add + complementarele +
recomandarea rich sunt monkeypatch-uite/scriptate.

Acoperă: după ce modelul adaugă un produs în coș, sugerăm produse COMPLEMENTARE ca CARDURI prin
calea rich (intro = confirmare DETERMINISTĂ a coșului, fără pick, produsul adăugat EXCLUS); fără
complementare → cade în fluxul normal (fără cross-sell)."""

from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.tools import commerce_tools as cm
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as ag
from src.worker.stages.agent import _cart_confirm_msg, agent_stage

P1 = {
    "id": "p1",
    "name": "Ser hidratant Aquafil",
    "brand": "Ivatherm",
    "price": 117.99,
    "availability": "in_stock",
    "top_pros": ["hidratare"],
    "ai_summary": "ser",
}
C1 = {
    "id": "c1",
    "name": "Contur ochi Aqua",
    "brand": "Ivatherm",
    "price": 75.99,
    "url": "u/c1",
    "availability": "in_stock",
    "rating": 4.3,
    "top_pros": ["hidratează zona ochilor"],
    "ai_summary": "contur",
}
C2 = {
    "id": "c2",
    "name": "Cremă anti-aging UNA",
    "brand": "Ivatherm",
    "price": 135.99,
    "url": "u/c2",
    "availability": "in_stock",
    "rating": 4.8,
    "top_pros": ["pentru riduri"],
    "ai_summary": "crema",
}

# Recomandarea rich a modelului peste setul COMPLEMENTAR (c1/c2). Intro-ul va fi SUPRASCRIS
# determinist (confirmarea coșului); pick-ul va fi scos în branch-ul de cross-sell.
RICH_JSON = {
    "intro": "Produse care merg bine:",
    "items": [
        {"product_id": "c1", "pro_index": 0, "fit_clause": "completează rutina ochilor"},
        {"product_id": "c2", "pro_index": 0, "fit_clause": "pentru riduri"},
    ],
    "pick": {"product_id": "c1", "justification": "alegere bună"},
    "education": None,
    "suggestions": ["Vreau și cremă de zi"],
}


class _FakeLLM:
    def __init__(self, *, tool_calls=(), rich=None, final=""):
        self._tc = list(tool_calls)
        self._rich = rich
        self._final = final

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._tc:
            await execute(name, args)
        return self._final

    async def complete_schema(self, system, user, schema, *, model=None):
        return self._rich

    async def complete(self, system, user, *, model=None):
        return self._final or "Gata."


def _deps(llm):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _ctx(body="adaugă serul în coș"):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body, channel_kind="whatsapp"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    ctx.route = RouteDecision(route=Route.SALES, purchase_intent=True)
    return ctx


def _patch_common(monkeypatch, *, complementary):
    async def _by_ids(conn, business_id, ids, *, limit=1):
        return [dict(P1)] if "p1" in ids else []

    async def _complementary(conn, business_id, anchor_id, *, exclude_ids=None, limit=4):
        return complementary

    async def _cats(conn, business_id):
        return []

    async def _aliases(conn, business_id, **k):
        return []

    monkeypatch.setattr(cm, "get_products_by_ids", _by_ids)
    monkeypatch.setattr(ag, "get_complementary_products", _complementary)
    monkeypatch.setattr(ag, "list_category_names", _cats)
    monkeypatch.setattr(ag, "list_routing_aliases", _aliases)


async def test_cross_sell_after_cart_add_shows_complementary(monkeypatch):
    _patch_common(monkeypatch, complementary=[dict(C1), dict(C2)])
    llm = _FakeLLM(tool_calls=[("cart_add", {"product_id": "p1"})], rich=RICH_JSON, final="")
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None and ctx.reply.rich is not None
    rich = ctx.reply.rich
    # intro = confirmare DETERMINISTĂ a coșului (cu numele produsului adăugat), NU proza modelului
    assert rich.intro == _cart_confirm_msg(P1, "ro")
    assert "Ser hidratant Aquafil" in rich.intro
    assert rich.pick is None  # fără „Recomandarea mea" între complementare
    ids = [it.product_id for it in rich.items]
    assert ids == ["c1", "c2"] and "p1" not in ids  # complementarele, NU produsul adăugat
    ev = [e for e in ctx.events if e.type == "cross_sell"]
    assert ev and ev[0].properties["n"] == 2


async def test_cross_sell_no_complementary_falls_through(monkeypatch):
    _patch_common(monkeypatch, complementary=[])
    # final prose → fluxul normal (fără rich) confirmă coșul; cross-sell NU se aprinde.
    llm = _FakeLLM(
        tool_calls=[("cart_add", {"product_id": "p1"})], rich=RICH_JSON, final="Gata, l-am adăugat."
    )
    ctx = _ctx()
    await agent_stage(ctx, _deps(llm))

    assert ctx.reply is not None
    assert ctx.reply.rich is None  # cross-sell NU a produs un reply bogat
    ev = [e for e in ctx.events if e.type == "cross_sell"]
    assert ev and ev[0].properties["n"] == 0  # semnalat, dar fără complementare
