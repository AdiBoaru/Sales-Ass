"""Welcome (stagiul 4, free layer): is_greeting + greeting_stage.

Unit, fără DB/LLM — stagiul nu folosește `deps`. Verifică detecția de pur salut,
mesajul branded (nume bot + magazin + disclaimer + sugestii), override per business,
limba, și că NU se declanșează pe întrebări de produs / când e dezactivat.
"""

from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.runner import PipelineDeps
from src.worker.stages import greeting
from src.worker.stages.greeting import is_greeting

_DEPS = PipelineDeps(conn=None)


# --- is_greeting (pur) -------------------------------------------------------


def test_is_greeting_positive():
    assert is_greeting("salut")
    assert is_greeting("Bună ziua!")  # diacritice + punctuație
    assert is_greeting("buna 😊")  # emoji ignorat
    assert is_greeting("  HEY  ")
    assert is_greeting("szia")  # HU
    assert is_greeting("hello")


def test_is_greeting_negative():
    assert not is_greeting("salut, caut o cremă")  # salut + intenție de produs
    assert not is_greeting("caut un telefon")
    assert not is_greeting("")
    assert not is_greeting(None)
    assert not is_greeting("vreau sa vorbesc cu un om")


# --- NX-126: setul de saluturi e ASCII pur (fără homoglife) ------------------


def test_all_greetings_are_self_normalized_ascii():
    """Guard anti-homoglif: fiecare salut e deja propria-i formă normalizată (ASCII). Prinde
    orice „о" chirilic / diacritic introdus viitor în set (regresia „hellо")."""
    bad = [g for g in greeting._GREETINGS if greeting._norm(g) != g]
    assert not bad, f"saluturi ne-normalizate (homoglife/diacritice): {bad}"
    non_ascii = [g for g in greeting._GREETINGS if not g.isascii()]
    assert not non_ascii, f"saluturi non-ASCII: {non_ascii}"


def test_hungarian_greetings_match():
    assert is_greeting("helló")  # HU: normalizează la „hello" (acoperit de intrarea ASCII)
    assert is_greeting("szia")
    assert is_greeting("Jó napot!")


# --- greeting_stage ----------------------------------------------------------


def _ctx(body: str = "salut", *, language: str = "ro", settings: dict | None = None) -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(
            id="biz-1", slug="sole", name="Sole Demo", vertical="beauty", settings=settings or {}
        ),
        contact=Contact(id="c1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv-1",
        language=language,
    )


async def test_welcome_on_pure_greeting():
    ctx = _ctx("salut")
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    text = ctx.reply.text
    assert "Native" in text  # numele implicit al botului
    assert "Sole Demo" in text  # numele magazinului
    assert "Cu ce te ajut azi?" in text
    assert "Spune-mi ce cauți" not in text
    assert "inteligență artificială" not in text  # disclaimer OFF default (#2)
    assert "Caut o cremă pentru ten uscat" in text  # sugestie pe vertical beauty
    assert ctx.reply.cacheable is False
    assert any(e.type == "welcome_sent" for e in ctx.events)


async def test_no_welcome_on_product_query():
    ctx = _ctx("caut o cremă pentru ten uscat")
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is None


async def test_welcome_disabled_global():
    ctx = _ctx("salut", settings={"welcome": {"enabled": False}})
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is None


async def test_welcome_business_override():
    ctx = _ctx(
        "salut",
        settings={"welcome": {"bot_name": "Ana", "suggestions": ["Vreau o programare"]}},
    )
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    text = ctx.reply.text
    assert "Ana" in text
    assert "Native" not in text
    assert "Vreau o programare" in text
    assert "Caut o cremă" not in text  # override înlocuiește default-ul pe vertical


async def test_welcome_language_en():
    ctx = _ctx("hi", language="en")
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    assert "I'm Native" in ctx.reply.text
    assert "How can I help today?" in ctx.reply.text  # ask EN natural (nu „Tell me what you need")
    assert "artificial intelligence" not in ctx.reply.text  # disclaimer OFF default (#2)


async def test_welcome_includes_disclaimer_when_enabled(monkeypatch):
    """AI_DISCLAIMER_ENABLED=true repune dezvăluirea în welcome (reversibil)."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_disclaimer_enabled", True)
    ctx = _ctx("salut")
    await greeting.greeting_stage(ctx, _DEPS)
    assert "inteligență artificială" in ctx.reply.text


async def test_welcome_unknown_vertical_uses_generic():
    ctx = _ctx("salut")
    ctx.business.vertical = "hvac"
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    assert "Caut un produs anume" in ctx.reply.text  # generic, nu beauty


async def test_welcome_ask_override_string():
    """settings["welcome"]["ask"] (string) înlocuiește textul de întâmpinare implicit."""
    ctx = _ctx("salut", settings={"welcome": {"ask": "Zi-mi direct ce vrei și ți-l găsesc."}})
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    assert "Zi-mi direct ce vrei și ți-l găsesc." in ctx.reply.text
    assert "Cu ce te ajut azi?" not in ctx.reply.text  # override, nu default-ul RO


async def test_welcome_ask_override_per_language():
    """Override-ul poate fi un dict pe limbă; se alege intrarea limbii curente."""
    ctx = _ctx("hi", language="en", settings={"welcome": {"ask": {"en": "What are you after?"}}})
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    assert "What are you after?" in ctx.reply.text
    assert "How can I help today?" not in ctx.reply.text  # override înlocuiește default-ul EN
