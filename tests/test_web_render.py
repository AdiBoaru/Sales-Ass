"""NX-127 — paritate web rich-render: `render_web` = sursă UNICĂ (sync /web/chat + async SSE).

Fără DB/Redis real: un fake Redis captează publish + pipeline; `Reply` rich/products/text trecute
prin `WebSender.send_*` și prin `render_web` direct. Dovada single-source: ruta sync și cea async
produc ACELAȘI shape pentru același `Reply`.
"""

import json
from dataclasses import asdict

import pytest

from src.channels.web.render import render_web, reply_from_outbox
from src.channels.web.sender import WebSender
from src.models import Chip, Offer, Reply, RichItem, RichReply


def _rich_reply() -> Reply:
    items = [
        RichItem(
            product_id="p1",
            name="Crema A",
            price=82.99,
            reason="hidratează",
            url="u1",
            image="i1",
            rating=4.6,
        ),
        RichItem(product_id="p2", name="Ser B", price=120.5, reason="calmează"),
    ]
    rich = RichReply(
        intro="Pentru tenul tău:",
        items=items,
        pick=("p1", "alegere bună"),
        education="contează ingredientele",
        chips=[Chip(label="Mai ieftin", payload="chip:cheaper")],
        disclaimer="Funcționez cu inteligență artificială.",
    )
    return Reply(text="floor complet", rich=rich)


class _FakePipe:
    def __init__(self, sink: dict) -> None:
        self._sink = sink
        self._ops: list = []

    def rpush(self, k, v):
        self._ops.append(("rpush", k))
        return self

    def ltrim(self, k, a, b):
        self._ops.append(("ltrim", k))
        return self

    def expire(self, k, t):
        self._ops.append(("expire", k))
        return self

    async def execute(self):
        self._sink["pipe_ops"].append([o[0] for o in self._ops])
        return [1, 1, 1]


class _FakeRedis:
    def __init__(self, *, fail_publish: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.sink: dict = {"pipe_ops": []}
        self._fail = fail_publish

    async def publish(self, channel, message):
        if self._fail:
            raise RuntimeError("publish down")
        self.published.append((channel, message))
        return 1

    def pipeline(self, transaction: bool = True):
        return _FakePipe(self.sink)


# --- single source of truth: sync == async shape ----------------------------


def test_render_web_matches_build_chat_response():
    from src.web.app import _build_chat_response
    from src.worker.processor import TurnResult

    reply = _rich_reply()
    direct = render_web(reply, "ro")
    tr = TurnResult("c", "ct", "t", None, None, reply=reply, language="ro")
    assert _build_chat_response(tr) == direct  # ruta sync delegă la ACELAȘI randor


def test_render_web_rich_shape():
    out = render_web(_rich_reply(), "ro")
    assert len(out["products"]) == 2
    assert out["suggestions"] == ["Mai ieftin"]
    # content = framing (intro + educație + disclaimer), nu enumerarea produselor. IZI-parity
    # (feedback Adi 2026-06-30): pick-ul („Recomandarea mea") e ASCUNS pe web by default.
    assert "Pentru tenul tău" in out["content"] and "contează ingredientele" in out["content"]
    assert "alegere bună" not in out["content"]  # pick ascuns pe web
    assert "inteligență artificială" in out["content"]  # disclaimer prezent
    assert out["products"][0]["product_id"] == "p1" and out["products"][0]["image_url"] == "i1"


def test_render_web_surfaces_clarify_suggestions():
    # reply NON-rich (clarify) cu chips → widget-ul le primește ca `suggestions` (idei de cadou).
    reply = Reply(text="Pentru cine e cadoul?", suggestions=["Cadou pentru ea", "Cadou pentru el"])
    out = render_web(reply, "ro")
    assert out["suggestions"] == ["Cadou pentru ea", "Cadou pentru el"]
    assert out["content"].startswith("Pentru cine") and out["products"] == []


# --- WebSender.send_rich / send_products (async, SSE) ------------------------


async def test_send_rich_publishes_rich_event_with_cards():
    r = _FakeRedis()
    s = WebSender(r)
    payload = {"type": "text", "to": "v1", "rich": asdict(_rich_reply().rich), "text": "floor"}
    mid = await s.send_rich("tok", "v1", payload)
    assert mid.startswith("web_out_")
    evt = json.loads(r.published[0][1])
    assert evt["type"] == "rich" and len(evt["products"]) == 2
    assert evt["suggestions"] == ["Mai ieftin"] and "Pentru tenul tău" in evt["content"]


async def test_send_rich_parity_with_sync():
    # același Reply → send_rich (async) publică ACELAȘI shape ca render_web (sync), minus id/type
    r = _FakeRedis()
    s = WebSender(r)
    reply = _rich_reply()
    await s.send_rich("tok", "v1", {"to": "v1", "rich": asdict(reply.rich), "text": "floor"})
    evt = json.loads(r.published[0][1])
    sync = render_web(reply, "ro")
    assert {k: evt[k] for k in ("content", "products", "suggestions")} == sync


async def test_send_rich_includes_offer_async():
    # NX-127 fix: offer serializat în payload-ul de outbox → randat NATIV și pe ruta async (buton),
    # nu doar floor-uit în text pe sync. Paritate sync↔async pentru reply cu offer.
    r = _FakeRedis()
    s = WebSender(r)
    payload = {
        "to": "v1",
        "rich": asdict(_rich_reply().rich),
        "text": "floor",
        "offer": {"kind": "open_url", "label": "Vezi oferta", "url": "https://shop.ro/x"},
    }
    await s.send_rich("tok", "v1", payload)
    evt = json.loads(r.published[0][1])
    assert evt["offer"] == {"kind": "open_url", "label": "Vezi oferta", "url": "https://shop.ro/x"}


async def test_send_products_publishes_cards_no_suggestions():
    r = _FakeRedis()
    s = WebSender(r)
    products = [{"product_id": "p1", "name": "A", "price": 10.0, "url": "u", "image": "i"}]
    await s.send_products("tok", "v1", "Recomandările mele:", products)
    evt = json.loads(r.published[0][1])
    assert evt["type"] == "rich" and len(evt["products"]) == 1 and evt["suggestions"] == []


# --- backlog atomic + fail-open (P6) ----------------------------------------


async def test_send_text_atomic_backlog():
    r = _FakeRedis()
    s = WebSender(r)
    await s.send_text("tok", "v1", "salut")
    evt = json.loads(r.published[0][1])
    assert evt["type"] == "text"
    assert r.sink["pipe_ops"] == [["rpush", "ltrim", "expire"]]  # un singur MULTI atomic


async def test_publish_fail_no_backlog_written():
    r = _FakeRedis(fail_publish=True)
    s = WebSender(r)
    payload = {"to": "v1", "rich": asdict(_rich_reply().rich), "text": "floor"}
    with pytest.raises(RuntimeError):
        await s.send_rich("tok", "v1", payload)
    assert r.sink["pipe_ops"] == []  # backlog NU se scrie după publish eșuat (P6)


# --- offer + carduri parțiale + roundtrip -----------------------------------


def test_offer_rendered_when_present_else_absent():
    out = render_web(
        Reply(text="hi", offer=Offer(kind="open_url", label="Vezi", url="https://x")), "ro"
    )
    assert out["offer"] == {"kind": "open_url", "label": "Vezi", "url": "https://x"}
    assert "offer" not in render_web(Reply(text="hi"), "ro")


def test_card_omits_missing_fields():
    reply = Reply(
        text="",
        rich=RichReply(
            intro=None,
            items=[RichItem(product_id="p", name="A", price=1.0)],
            pick=None,
            education=None,
            chips=[],
            disclaimer="d",
        ),
    )
    card = render_web(reply, "ro")["products"][0]
    assert "image_url" not in card and "url" not in card and "rating" not in card


def test_reply_from_outbox_roundtrips_asdict():
    rich = _rich_reply().rich
    rep = reply_from_outbox({"rich": asdict(rich), "text": "floor"})
    assert rep.rich is not None and len(rep.rich.items) == 2
    assert rep.rich.pick == ("p1", "alegere bună")  # tuple, nu listă
    assert rep.rich.chips[0].label == "Mai ieftin"
    assert rep.text == "floor"


def test_render_web_none_reply_empty():
    assert render_web(None, "ro") == {"content": "", "products": [], "suggestions": []}


# --- IZI carduri bogate: review_count + badge + list_price + coaching de final ---


def _rich_with(**item_kw) -> Reply:
    item = RichItem(product_id="p", name="A", price=item_kw.pop("price", 63.99), **item_kw)
    return Reply(
        text="",
        rich=RichReply(
            intro=None, items=[item], pick=None, education=None, chips=[], disclaimer="d"
        ),
    )


def test_card_surfaces_review_count_badge_list_price():
    card = render_web(
        _rich_with(rating=4.6, review_count=120, badge="Top Favorite", list_price=79.99), "ro"
    )["products"][0]
    assert card["review_count"] == 120 and card["badge"] == "Top Favorite"
    assert card["list_price"] == 79.99 and card["price"] == 63.99  # original tăiat vs curent


def test_review_count_zero_and_undiscounted_list_price_omitted():
    card = render_web(_rich_with(review_count=0, list_price=40.0, price=50.0), "ro")["products"][0]
    assert "review_count" not in card  # 0 recenzii → cheia lipsește (nu „0 recenzii")
    assert "list_price" not in card  # list (40) < price (50) → NU e reducere → fără anchor


def test_education_rendered_as_closing_coaching_on_web():
    # IZI-coaching: `education` revine ca paragraf de final pe widget (înainte cădea tăcut, NX-134).
    out = render_web(_rich_reply(), "ro")
    assert "contează ingredientele" in out["content"]


# --- render_path event (P10: degradarea rich→text devine vizibilă) ----------


async def test_render_path_emitted_on_degradation(monkeypatch):
    import src.worker.dispatcher as disp

    captured: dict = {}

    async def fake_insert(conn, business_id, events, *, conversation_id=None, contact_id=None):
        captured["events"] = events
        captured["conv"] = conversation_id

    monkeypatch.setattr(disp, "insert_events", fake_insert)
    # rich cerut, dar livrat text (canal fără RICH) → event de degradare; conv_id din rândul outbox
    await disp._emit_render_path(
        object(), "biz", "whatsapp", {"rich": {"x": 1}}, "text", "text", "conv-9"
    )
    assert captured["events"][0].type == "render_path"
    assert captured["events"][0].properties == {
        "channel_kind": "whatsapp",
        "requested": "rich",
        "delivered": "text",
    }
    assert captured["conv"] == "conv-9"  # din row['conversation_id'], NU NULL


async def test_render_path_silent_when_match(monkeypatch):
    import src.worker.dispatcher as disp

    captured: list = []

    async def fake_insert(conn, business_id, events, **k):
        captured.extend(events)

    monkeypatch.setattr(disp, "insert_events", fake_insert)
    # rich cerut ȘI livrat (webchat are RICH acum) → fără event (zero overhead pe calea fericită)
    await disp._emit_render_path(object(), "biz", "webchat", {"rich": {"x": 1}}, "text", "rich")
    # carousel cerut ȘI livrat carousel (Telegram) → NU e degradare (fidelitate completă)
    await disp._emit_render_path(
        object(), "biz", "telegram", {"products": [{"x": 1}]}, "carousel", "carousel"
    )
    assert captured == []
