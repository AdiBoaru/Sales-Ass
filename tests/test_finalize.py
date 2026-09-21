"""NX-144 felia 1 — `render` (faza F, `src/agent/finalize.py`). Testează DISPATCH-ul scos din
`agent_stage`: rich → proză (downgrade), proză passthrough, fallback la preț negroundat, status
comandă grounded, no-result. Grounding-ul (validator) rămâne poarta; aici verificăm regia
render→validate→recover. ZERO DB; LLM scriptat."""

from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.agent.validator import ValidationResult
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("D", "ecommerce", "ro", ["Parfumuri"], [])
PROD = {
    "id": "p1",
    "name": "Rhea Soft",
    "brand": "Rhea",
    "price": 18.99,
    "url": "https://shop/p1",
    "product_url": "https://shop/p1",
    "ai_summary": "parfum lejer",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["lejer"],
}
RICH_JSON = {
    "intro": "recomand:",
    "items": [{"product_id": "p1", "pro_index": 0, "fit_clause": "lejer"}],
    "pick": {"product_id": "p1", "justification": "cel mai bun"},
    "education": None,
    "suggestions": ["altă variantă?"],
}


class _LLM:
    def __init__(self, *, rich=None, prose="Uite o variantă bună."):
        self._rich = rich
        self._prose = prose

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return self._prose

    async def complete_schema(self, system, user, schema, *, model=None):
        if self._rich is None:
            raise RuntimeError("no structured output")
        return self._rich


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau un parfum"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


def _deps(llm):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def _plan(**kw):
    base = dict(products=[], final="", is_order=False, query="vreau un parfum", history="", inp=INP)
    base.update(kw)
    return ResponsePlan(handled=False, **base)


async def test_rich_success_sets_rich_reply():
    ctx = _ctx()
    result = await render(
        ctx, _deps(_LLM(rich=RICH_JSON)), _plan(products=[dict(PROD)], mode="rich")
    )
    assert ctx.reply is not None and ctx.reply.rich is not None
    ev = [e for e in ctx.events if e.type == "agent_recommended"]
    assert ev and ev[0].properties.get("rich") is True
    # calea BOGATĂ nu trece prin `validate_prose` (grounding-ul e la compose/membership).
    assert result is None


async def test_rich_failure_recovers_cards_from_facts():
    """NX-302: apelul rich cade, dar CARDURILE nu se pierd — se construiesc din catalog.

    Regresia pe care o pinuiește: pe turul real `57fa9fbe` («vreau o rutina de cosuri»),
    `APITimeoutError` pe `complete_schema` cobora tot răspunsul pe `_deterministic_reply` plus
    carduri mute. Măsurat pe 56 de ture cu `conversation_traces`, 14 ture cu produse ajungeau așa la
    client. Downgrade-ul rămâne SEMNALAT (`rich_downgraded`) — recuperarea nu are voie să ascundă
    faptul că modelul a căzut, altfel cifra pe care o urmărim dispare din telemetrie."""
    ctx = _ctx()
    result = await render(
        ctx, _deps(_LLM(rich=None)), _plan(products=[dict(PROD)], final="", mode="rich")
    )
    assert ctx.reply is not None and ctx.reply.rich is not None
    assert [it.product_id for it in ctx.reply.rich.items] == ["p1"]
    assert any(e.type == "rich_downgraded" for e in ctx.events)  # degradarea rămâne vizibilă
    salvage = [e for e in ctx.events if e.type == "rich_from_facts"]
    assert salvage and salvage[0].properties["reason"] == "structured-call-failed"
    # `_finalize` rulează în continuare (retry-ul de proză e o poartă de ADEVĂR, neatinsă de
    # recuperarea de FORMĂ), deci `ValidationResult`-ul lui ajunge la Turn Replay ca înainte.
    assert result == ValidationResult(ok=True, reasons=[])


async def test_rich_failure_keeps_validated_prose_as_intro():
    """Proza VALIDATĂ devine `intro` verbatim, fără să mai treacă prin `scrub_intro`.

    `validate_prose` cere ca fiecare preț din text să existe în retrieval; `scrub_intro` nu știe
    decât cifrele clientului, deci ar arunca ÎNTREG un text corect fiindcă numește un preț REAL, iar
    clientul ar rămâne cu lista de carduri și zero frază. Poarta prin care textul a trecut deja e
    strict mai tare — același argument ca la NX-239."""
    ctx = _ctx()
    prose = "Rhea Soft costă 18,99 lei și e lejer."
    await render(
        ctx, _deps(_LLM(rich=None)), _plan(products=[dict(PROD)], final=prose, mode="rich")
    )
    assert ctx.reply is not None and ctx.reply.rich is not None
    assert ctx.reply.rich.intro == prose


async def test_rich_failure_never_uses_deterministic_text_as_intro():
    """Textul de avarie NU devine încadrare — exact contradicția din turul măsurat.

    `_deterministic_reply` enumeră nume cu prețuri. Pus deasupra unor carduri care poartă ACELEAȘI
    prețuri, dă răspunsul din screenshot: patru carduri și trei nume care nu explică de ce apar
    celelalte. Două plafoane independente peste aceeași listă nu pot rămâne de acord, deci lista
    textuală nu are ce căuta acolo unde există carduri."""
    ctx = _ctx()
    # Preț inventat în proză ȘI în retry ⇒ `_finalize` întoarce textul determinist.
    result = await render(
        ctx,
        _deps(_LLM(rich=None, prose="Costă 999 lei.")),
        _plan(products=[dict(PROD)], final="Costă 777 lei.", mode="rich"),
    )
    assert result is not None and result.ok is False
    assert ctx.reply is not None and ctx.reply.rich is not None
    intro = ctx.reply.rich.intro or ""
    assert "Îți recomand:" not in intro and "999" not in intro and "777" not in intro
    assert ctx.reply.cacheable is False  # un răspuns de avarie nu otrăvește semantic_cache


async def test_rich_from_facts_kill_switch_restores_prose_path(monkeypatch):
    """`RICH_FROM_FACTS_ENABLED=false` → comportamentul de dinainte (proză + carduri simple)."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", False, raising=False)
    ctx = _ctx()
    result = await render(
        ctx, _deps(_LLM(rich=None)), _plan(products=[dict(PROD)], final="", mode="rich")
    )
    assert ctx.reply is not None and ctx.reply.rich is None
    assert any(e.type == "rich_downgraded" for e in ctx.events)
    assert result == ValidationResult(ok=True, reasons=[])


async def test_prose_passthrough_when_no_products_and_valid():
    ctx = _ctx()
    # Fără produse, text de clarificare fără preț inventat → servit verbatim.
    result = await render(ctx, _deps(_LLM()), _plan(final="Ce buget ai în minte?", mode="prose"))
    assert ctx.reply is not None and ctx.reply.text == "Ce buget ai în minte?"
    assert result == ValidationResult(ok=True, reasons=[])


async def test_invalid_price_no_products_falls_back_safe():
    ctx = _ctx()
    # Preț negroundat fără produse care să-l susțină → mesaj sigur, necacheabil (anti-poisoning).
    result = await render(ctx, _deps(_LLM()), _plan(final="Costă doar 999 lei!", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.cacheable is False
    assert "999" not in ctx.reply.text
    # NX-146 felia 2 fix: motivul respingerii ajunge în ValidationResult (→ agent_prompt).
    assert result is not None and result.ok is False and "ungrounded_price" in result.reasons


async def test_order_grounded_uses_facts():
    ctx = _ctx()
    plan = _plan(
        is_order=True, final="Comanda ta e livrată.", order_views=["status: livrata"], mode="order"
    )
    result = await render(ctx, _deps(_LLM()), plan)
    assert ctx.reply is not None and ctx.reply.text  # status servit, non-tăcere
    assert result == ValidationResult(ok=True, reasons=[])


async def test_no_products_no_final_no_result():
    ctx = _ctx()
    await render(ctx, _deps(_LLM()), _plan(final="", mode="fallback"))
    assert ctx.reply is not None and ctx.reply.cacheable is False
