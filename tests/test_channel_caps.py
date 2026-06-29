"""NX-115 — matrice de capabilități de canal: rutare table-driven + consistență caps↔metode.

PUR (fără DB/HTTP) → rulează pe PR. (test_dispatcher.py rămâne integration pt fluxul DB real.)
Acoperă: choose_render pe fiecare ramură de degradare, consistența declarat⇔metodă reală pe
fiecare sender, și execuția ramurii în dispatch_row cu sender-e fake + conn stub."""

import pytest

from src.channels.base import CAPABILITY_METHODS, Capability
from src.channels.telegram.client import TelegramClient
from src.channels.web.sender import WebSender
from src.meta_client import MetaClient
from src.worker import dispatcher as disp
from src.worker.dispatcher import choose_render

TEXT_ONLY = frozenset({Capability.TEXT})
RICH_CAPS = frozenset(
    {Capability.TEXT, Capability.RICH, Capability.CARDS, Capability.CAROUSEL, Capability.EDIT}
)
CARDS_ONLY = frozenset({Capability.TEXT, Capability.CARDS})
TEMPLATE_CAPS = frozenset({Capability.TEXT, Capability.TEMPLATE})

REAL_SENDERS = [MetaClient, TelegramClient, WebSender]


# --- choose_render: rutare pură + degradare (P6) -----------------------------


def test_rich_branch_when_capable():
    assert choose_render({"rich": {"x": 1}, "text": "t"}, "text", RICH_CAPS) == "rich"


def test_rich_degrades_to_text_without_cap():
    assert choose_render({"rich": {"x": 1}, "text": "t"}, "text", TEXT_ONLY) == "text"


def test_carousel_branch_when_capable():
    p = {"products": [{"id": "1"}], "text": "t"}
    assert choose_render(p, "carousel", RICH_CAPS) == "carousel"


def test_carousel_degrades_to_products_with_cards_only():
    p = {"products": [{"id": "1"}], "text": "t"}
    assert choose_render(p, "carousel", CARDS_ONLY) == "products"


def test_products_degrades_to_text_without_cards():
    p = {"products": [{"id": "1"}], "text": "t"}
    assert choose_render(p, "products", TEXT_ONLY) == "text"


def test_edit_media_needs_edit_cap():
    assert choose_render({}, "edit_media", RICH_CAPS) == "edit"


def test_edit_media_unsupported_without_edit_cap():
    # edit_media NU degradează la text (e navigare UI, nu conținut nou) → dead.
    assert choose_render({}, "edit_media", TEXT_ONLY) == "edit_unsupported"


def test_plain_text_branch():
    assert choose_render({"text": "salut"}, "text", TEXT_ONLY) == "text"


def test_template_branch_when_capable():
    # PL-1: proactiv în afara ferestrei 24h pe canal cu TEMPLATE (WhatsApp).
    p = {"type": "template", "to": "u", "text": "floor", "template_name": "awb_update"}
    assert choose_render(p, "template", TEMPLATE_CAPS) == "template"


def test_template_degrades_to_text_without_cap():
    # Canal fără TEMPLATE → degradare grațioasă la text (floor = textul randat), P6.
    p = {"type": "template", "to": "u", "text": "floor", "template_name": "awb_update"}
    assert choose_render(p, "template", TEXT_ONLY) == "text"


# --- consistență caps↔metode (contract) --------------------------------------


@pytest.mark.parametrize("cls", REAL_SENDERS, ids=lambda c: c.__name__)
def test_declared_caps_have_real_methods(cls):
    for cap in cls.capabilities:
        method = CAPABILITY_METHODS.get(cap)
        if method is None:  # OFFER — randare inline, fără metodă dedicată
            continue
        assert hasattr(cls, method), f"{cls.__name__} declară {cap} dar n-are {method}()"


@pytest.mark.parametrize("cls", REAL_SENDERS, ids=lambda c: c.__name__)
def test_rich_methods_are_all_declared(cls):
    # orice metodă „bogată" prezentă pe sender TREBUIE declarată ca o capabilitate (fără surprize).
    for cap, method in CAPABILITY_METHODS.items():
        if hasattr(cls, method) and cap not in cls.capabilities:
            pytest.fail(f"{cls.__name__} are {method}() dar NU declară {cap}")


def test_every_sender_declares_text():
    for cls in REAL_SENDERS:
        assert Capability.TEXT in cls.capabilities


# --- dispatch_row: execuția ramurii alese (sender-e fake + conn stub) ---------


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


class _FakeSender:
    def __init__(self, caps):
        self.capabilities = caps
        self.max_text_len = None
        self.max_caption_len = None
        self.calls: list[str] = []

    async def send_text(self, account_id, to, text):
        self.calls.append("send_text")
        return "id-text"

    async def send_rich(self, account_id, to, payload):
        self.calls.append("send_rich")
        return "id-rich"

    async def send_products(self, account_id, to, text, products):
        self.calls.append("send_products")
        return "id-products"

    async def send_carousel_card(self, account_id, to, products, index):
        self.calls.append("send_carousel_card")
        return "id-carousel"

    async def edit_message_media(self, account_id, to, card_message_id, products, index):
        self.calls.append("edit")
        return "id-edit"

    async def send_template(self, account_id, to, name, language, params):
        self.calls.append("send_template")
        return "id-template"


def _reg(sender, kind="telegram"):
    from src.channels.base import ChannelSenderRegistry

    r = ChannelSenderRegistry()
    r.register(kind, sender)
    return r


def _row(payload, kind="telegram"):
    return {
        "id": "ob1",
        "payload": payload,
        "kind": "message",
        "channel_kind": kind,
        "channel_account_id": "acc",
        "attempts": 0,
    }


@pytest.fixture
def _stub_outbox(monkeypatch):
    """Stub-ează scrierile DB ale dispatcher-ului → dispatch_row testabil fără DB."""
    failed: dict = {}

    async def fake_mark_sent(*a, **k):
        return None

    async def fake_set_pid(*a, **k):
        return None

    async def fake_mark_failed(conn, business_id, row_id, attempts, err):
        failed["err"] = err
        return "dead"

    monkeypatch.setattr(disp, "mark_sent", fake_mark_sent)
    monkeypatch.setattr(disp, "set_message_provider_id", fake_set_pid)
    monkeypatch.setattr(disp, "mark_failed", fake_mark_failed)
    return failed


async def test_dispatch_rich_calls_send_rich(_stub_outbox):
    sender = _FakeSender(RICH_CAPS)
    payload = {"type": "text", "to": "u", "rich": {"items": []}, "text": "floor", "message_id": "m"}
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "sent" and sender.calls == ["send_rich"]


async def test_dispatch_products_degrades_to_text_on_text_only(_stub_outbox):
    sender = _FakeSender(TEXT_ONLY)
    payload = {
        "type": "products",
        "to": "u",
        "products": [{"id": "1"}],
        "text": "lead",
        "message_id": "m",
    }
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "sent" and sender.calls == ["send_text"]  # degradare grațioasă (P6)


async def test_dispatch_carousel_degrades_to_products_with_cards(_stub_outbox):
    sender = _FakeSender(CARDS_ONLY)
    payload = {
        "type": "carousel",
        "to": "u",
        "products": [{"id": "1"}],
        "text": "t",
        "message_id": "m",
    }
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "sent" and sender.calls == ["send_products"]


async def test_dispatch_template_calls_send_template(_stub_outbox):
    # PL-1: payload `type=template` pe canal cu TEMPLATE → send_template (name/language/params).
    sender = _FakeSender(TEMPLATE_CAPS)
    payload = {
        "type": "template",
        "to": "u",
        "text": "AWB 123 la FAN",
        "template_name": "awb_update",
        "language": "ro",
        "params": ["123", "FAN"],
        "message_id": "m",
    }
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "sent" and sender.calls == ["send_template"]


async def test_dispatch_template_degrades_to_text_without_cap(_stub_outbox):
    # Canal fără TEMPLATE → trimite textul randat ca floor (degradare grațioasă, P6).
    sender = _FakeSender(TEXT_ONLY)
    payload = {
        "type": "template",
        "to": "u",
        "text": "AWB 123 la FAN",
        "template_name": "awb_update",
        "language": "ro",
        "params": ["123", "FAN"],
        "message_id": "m",
    }
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "sent" and sender.calls == ["send_text"]


async def test_dispatch_edit_media_dead_without_edit_cap(_stub_outbox):
    sender = _FakeSender(TEXT_ONLY)
    payload = {
        "type": "edit_media",
        "to": "u",
        "card_message_id": "c",
        "products": [{"id": "1"}],
        "index": 1,
        "message_id": "m",
    }
    status = await disp.dispatch_row(_FakeConn(), "biz", _reg(sender), _row(payload))
    assert status == "dead" and sender.calls == []  # nu degradează la text
    assert "edit_media" in _stub_outbox["err"]
