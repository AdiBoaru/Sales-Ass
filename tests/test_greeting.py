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
    assert "inteligență artificială" in text  # disclaimer AI
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
    assert "artificial intelligence" in ctx.reply.text


async def test_welcome_unknown_vertical_uses_generic():
    ctx = _ctx("salut")
    ctx.business.vertical = "hvac"
    await greeting.greeting_stage(ctx, _DEPS)
    assert ctx.reply is not None
    assert "Caut un produs anume" in ctx.reply.text  # generic, nu beauty
