"""Teste pentru contractul central TurnContext + dataclass-urile."""

from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    Offer,
    ProductRef,
    Reply,
    Route,
    RouteDecision,
    TurnContext,
)


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="wamid.1", body="salut"),
        conversation_id="conv1",
    )


def test_minimal_construction_defaults():
    ctx = _ctx()
    assert ctx.language == "ro"
    assert ctx.route is None
    assert ctx.reply is None
    assert ctx.history == []
    assert ctx.events == []
    assert isinstance(ctx.state, ConversationState)
    assert ctx.state.state_version == 0


def test_emit_accumulates_events():
    ctx = _ctx()
    ctx.emit("cache_hit", cache_type="semantic")
    ctx.emit("route", route="sales")
    assert [e.type for e in ctx.events] == ["cache_hit", "route"]
    # NX-122: emit() injectează turn_id-ul turului în orice event (P10).
    assert ctx.events[0].properties == {"cache_type": "semantic", "turn_id": "t1"}


def test_set_reply_signals_early_exit():
    ctx = _ctx()
    assert ctx.reply is None
    ctx.set_reply("răspuns", kind="message")
    assert isinstance(ctx.reply, Reply)
    assert ctx.reply.text == "răspuns"
    assert ctx.reply.kind == "message"


# --- NX-114: Reply.offer (seam neutru de canal) + floor de aplatizare -------


def test_set_offer_floors_url_into_text():
    ctx = _ctx()
    ctx.set_reply("Iată produsul potrivit.")
    ctx.set_offer(Offer(kind="open_url", label="Vezi produsul", url="https://shop.ro/p/1"))
    assert ctx.reply.offer.kind == "open_url"
    assert "https://shop.ro/p/1" in ctx.reply.text  # floor: url append-uit pt canale text


def test_set_offer_without_url_keeps_text():
    ctx = _ctx()
    ctx.set_reply("Răspuns simplu.")
    ctx.set_offer(Offer(kind="quick_reply", label="Reia comanda", payload="offer:reorder"))
    assert ctx.reply.text == "Răspuns simplu."  # quick_reply fără url → text neschimbat
    assert ctx.reply.offer.payload == "offer:reorder"


def test_set_offer_url_not_duplicated():
    ctx = _ctx()
    ctx.set_reply("Vezi: https://shop.ro/p/1")
    ctx.set_offer(Offer(kind="open_url", label="Vezi", url="https://shop.ro/p/1"))
    assert ctx.reply.text.count("https://shop.ro/p/1") == 1  # nu dublăm url-ul deja prezent


def test_reply_offer_defaults_none():
    assert Reply(text="x").offer is None  # un Sender vechi nu vede offer (floor opțional)


def test_route_decision_uses_enum():
    rd = RouteDecision(route=Route.SALES, category_key="machiaj")
    assert rd.route == "sales"
    assert rd.route is Route.SALES
    assert rd.filters == {}


def test_state_holds_product_refs_not_objects():
    state = ConversationState(
        displayed_products=[ProductRef(product_id="p1", name="Ruj", price=49.9)]
    )
    ref = state.displayed_products[0]
    assert ref.product_id == "p1"
    # ref-ul are DOAR id+name+price, nu obiectul complet
    assert set(vars(ref).keys()) == {"product_id", "name", "price"}


def test_message_history_enums():
    m = Message(direction=Direction.INBOUND, author=Author.CONTACT, body="hi")
    assert m.direction is Direction.INBOUND
    assert m.author is Author.CONTACT
    assert m.content_type == "text"
