"""G5c — detecție de limbă: `detect_language` (pur) + `language_stage` (refină + persistă).

Unit, fără DB/LLM: detectorul e cod pur; `language_stage` cu `set_conversation_locale`
monkeypatch-uit. ZERO apeluri reale.
"""

from src.lang.detect import detect_language
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import language as language_mod
from src.worker.stages.language import language_stage

ALL = ["ro", "hu", "en"]


# --- detect_language (pur) ---------------------------------------------------


def test_detect_romanian():
    assert detect_language("caut o cremă de față", ALL) == "ro"
    assert detect_language("Bună, vreau un șampon fără sulfați", ALL) == "ro"


def test_detect_hungarian():
    assert detect_language("szeretnék egy arckrémet", ALL) == "hu"
    assert detect_language("Szia, mennyibe kerül a szállítás?", ALL) == "hu"


def test_detect_english():
    assert detect_language("do you have face cream", ALL) == "en"
    assert detect_language("hello, what is the price", ALL) == "en"


def test_out_of_supported_returns_none():
    # text HU, dar tenantul suportă DOAR ro → nu inventăm o limbă neacceptată
    assert detect_language("szeretnék egy arckrémet", ["ro"]) is None


def test_ambiguous_or_short_returns_none():
    assert detect_language("ok 👍", ALL) is None
    assert detect_language(":)", ALL) is None
    assert detect_language("da the", ALL) is None  # ro=1, en=1 → tie → None


def test_empty_returns_none():
    assert detect_language(None, ALL) is None
    assert detect_language("", ALL) is None


# --- language_stage ----------------------------------------------------------


def _ctx(body: str, *, language: str = "ro", supported=None) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="s", name="n", supported_locales=supported or ALL),
        contact=Contact(id="c1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv-1",
        language=language,
    )


async def test_stage_detects_and_persists(monkeypatch):
    persisted = {}

    async def fake_persist(conn, business_id, conv_id, locale):
        persisted["args"] = (business_id, conv_id, locale)

    monkeypatch.setattr(language_mod, "set_conversation_locale", fake_persist)

    ctx = _ctx("szeretnék egy arckrémet", language="ro")
    await language_stage(ctx, PipelineDeps(conn=None))

    assert ctx.language == "hu"
    assert persisted["args"] == ("biz-1", "conv-1", "hu")
    assert any(
        e.type == "language_detected"
        and e.properties["from"] == "ro"
        and e.properties["to"] == "hu"
        for e in ctx.events
    )
    assert ctx.reply is None and ctx.halt is False  # nu setează reply/halt


async def test_stage_noop_when_same_locale(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("aceeași limbă → nu persistă")

    monkeypatch.setattr(language_mod, "set_conversation_locale", boom)
    ctx = _ctx("caut o cremă de față", language="ro")  # detectat ro == curent
    await language_stage(ctx, PipelineDeps(conn=None))

    assert ctx.language == "ro"
    assert not any(e.type == "language_detected" for e in ctx.events)


async def test_stage_noop_on_monolingual_tenant(monkeypatch):
    def boom_detect(*a, **k):
        raise AssertionError("mono-lingv → detect_language nu trebuie apelat")

    async def boom_persist(*a, **k):
        raise AssertionError("mono-lingv → fără apel DB")

    monkeypatch.setattr(language_mod, "detect_language", boom_detect)
    monkeypatch.setattr(language_mod, "set_conversation_locale", boom_persist)

    ctx = _ctx("szeretnék egy arckrémet", language="ro", supported=["ro"])
    await language_stage(ctx, PipelineDeps(conn=None))
    assert ctx.language == "ro"


async def test_stage_persist_failure_is_swallowed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(language_mod, "set_conversation_locale", boom)
    ctx = _ctx("do you have face cream", language="ro")
    await language_stage(ctx, PipelineDeps(conn=None))  # nu aruncă

    assert ctx.language == "en"  # limba detectată rămâne pe ctx pentru ACEST tur
