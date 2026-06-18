"""NX-134 — disclaimer AI (art. 50 AI Act) garantat pe TOATE rutele, aplicat la Sender.

`ensure_disclaimer` (pur, idempotent) + calea Sender din `handle_turn`: orice reply
(simple/clarify/prose/fallback) iese cu disclaimer-ul; welcome/rich nu-l dublează; cache-ul
stochează textul PUR (re-aplicat la hit). ZERO OpenAI/DB real (stub conn, pattern G8-1)."""

from src.models import BusinessConfig, Contact
from src.worker import compose
from src.worker import processor as proc
from src.worker.compose import ensure_disclaimer
from src.worker.processor import handle_turn

RO = compose._DISCLAIMER["ro"]
EN = compose._DISCLAIMER["en"]
HU = compose._DISCLAIMER["hu"]


# --- ensure_disclaimer: pur, idempotent --------------------------------------


def test_appends_disclaimer_ro():
    out = ensure_disclaimer("Avem 3 creme bune.", "ro")
    assert out == f"Avem 3 creme bune.\n\n{RO}"
    assert out.endswith(RO)


def test_idempotent_double_apply():
    once = ensure_disclaimer("Text scurt.", "ro")
    assert ensure_disclaimer(once, "ro") == once  # a doua aplicare = identic


def test_not_doubled_when_already_present():
    # forma welcome/rich (flatten) conține deja disclaimer-ul → nu se dublează
    text = f"Bun venit!\n\n{RO}"
    out = ensure_disclaimer(text, "ro")
    assert out.count(RO) == 1


def test_empty_and_none_return_just_disclaimer():
    assert ensure_disclaimer("", "ro") == RO
    assert ensure_disclaimer(None, "ro") == RO  # defensiv: nu aruncă


def test_locale_selection_and_fallback():
    assert ensure_disclaimer("Hi", "en").endswith(EN)
    assert ensure_disclaimer("Szia", "hu").endswith(HU)
    assert ensure_disclaimer("Hallo", "de").endswith(RO)  # locale necunoscut → fallback ro
    assert ensure_disclaimer("X", None).endswith(RO)


def test_existing_ro_not_doubled_under_other_language():
    # text scris în `ro` (are forma ro), dar acum ctx.language="en" → set-membership pe TOATE
    # locale-urile prinde forma ro existentă → NU adaugă și varianta en.
    text = f"Răspuns vechi.\n\n{RO}"
    out = ensure_disclaimer(text, "en")
    assert out == text and EN not in out


# --- calea Sender (handle_turn stubbed): aplicat o dată, cache pur ------------


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


async def _run(monkeypatch, stage):
    """handle_turn cu DB stubbed; întoarce payload outbox, body-ul outbound și textul pt cache."""
    cap: dict = {}

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": {},
            "state_version": 0,
            "locale": "ro",
            "bot_active": True,
            "handoff_until": None,
        }

    async def fake_insert_msg(conn, business_id, conv_id, contact_id, direction, author, **k):
        if k.get("status") == "queued":  # mesajul OUTBOUND (Sender)
            cap["body"] = k.get("body")
        return "msg-id"

    async def fake_outbox(conn, business_id, conv_id, key, payload, **k):
        cap["payload"] = payload
        return "outbox-1"

    async def fake_cache(conn, llm, business_id, locale, body, ctx):
        cap["cache_text"] = ctx.reply.text  # textul PUR pe care cache-ul l-ar stoca

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="c", business_id="biz-1")

    async def fake_claim(*a, **k):
        return True

    async def fake_budget(*a, **k):
        return None

    monkeypatch.setattr(proc, "claim_inbound", fake_claim)
    monkeypatch.setattr(proc, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(proc, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(proc, "insert_message", fake_insert_msg)
    monkeypatch.setattr(proc, "touch_last_inbound", anoop)
    monkeypatch.setattr(proc, "get_recent_messages", anoop)
    monkeypatch.setattr(proc, "get_summary_for_context", anoop)
    monkeypatch.setattr(proc, "enqueue_outbox", fake_outbox)
    monkeypatch.setattr(proc, "patch_conversation_state", anoop)
    monkeypatch.setattr(proc, "_persist_events", anoop)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", fake_budget)
    monkeypatch.setattr(proc, "_cache_writeback", fake_cache)
    monkeypatch.setattr(proc, "_summarize_if_needed", anoop)

    business = BusinessConfig(id="biz-1", slug="s", name="n")
    event = {
        "channel_kind": "telegram",
        "sender_external_id": "u1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": "caut o cremă",
    }
    await handle_turn(_FakeConn(), business, "chan-1", event, stages=[stage])
    return cap


async def test_sender_applies_disclaimer_simple_route(monkeypatch):
    async def simple_stage(ctx, deps):
        ctx.set_reply("Salut! Cu ce te ajut?")

    cap = await _run(monkeypatch, simple_stage)
    assert cap["payload"]["text"].endswith(RO)  # disclaimer aplicat
    assert cap["body"] == cap["payload"]["text"]  # body == payload (identice)
    assert RO not in cap["cache_text"]  # cache-ul stochează textul PUR


async def test_sender_does_not_double_when_reply_has_disclaimer(monkeypatch):
    async def rich_like_stage(ctx, deps):
        # simulează flatten(rich): textul aplatizat conține deja disclaimer-ul
        ctx.set_reply(f"Recomand crema X.\n\n{RO}")

    cap = await _run(monkeypatch, rich_like_stage)
    assert cap["payload"]["text"].count(RO) == 1  # o singură apariție
