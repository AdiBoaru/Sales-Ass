"""NX-173 (P0) — cache-ul NU are voie să ocolească gate-ul de siguranță.

Gaură găsită LIVE (nu de raport, nu de teste): «sunt însărcinată, ce cremă antirid pot folosi?»
era servit din `semantic_cache` cu `route=None` — stagiul 4 face early-exit ÎNAINTE de triaj/agent,
deci peste tot gate-ul de contraindicații. Un răspuns compus într-un tur anterior (posibil dinaintea
gate-ului) ajungea la toți clienții cu aceeași frază, pe termen nelimitat.

Perechea de protecții (ambele necesare):
  - CITIRE: bypass la cache pe context de siguranță (`stages/cache.py`) — ca `contextual`;
  - SCRIERE: write-back refuzat (`aftercare.py`) — un răspuns relativ la CLIENT nu intră în cache-ul
    partajat, nici pentru el, nici pentru alții.
"""

import pytest

from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Reply,
    TurnContext,
)
from src.worker.stages.cache import cache_stage


def _ctx(body: str, *, safety=None) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(safety=safety or {}),
    )


def _deps():
    from src.worker.runner import PipelineDeps

    return PipelineDeps(conn=object(), redis=None, llm=object())


def _bypass(ctx) -> str | None:
    ev = next((e for e in ctx.events if e.type == "cache_bypass"), None)
    return ev.properties.get("volatility") if ev else None


# --- CITIRE ------------------------------------------------------------------------------------


async def test_cache_read_bypassed_when_context_declared_now(monkeypatch):
    """Reproducerea EXACTĂ a găurii live: fraza care declara sarcina era servită din cache."""
    called = []
    monkeypatch.setattr(
        "src.worker.stages.cache.exact_lookup",
        lambda *a, **k: called.append(a) or None,
    )
    ctx = _ctx("sunt însărcinată, ce cremă antirid pot folosi?")
    await cache_stage(ctx, _deps())
    assert _bypass(ctx) == "safety_context"
    assert called == [], "cache-ul NU are voie nici să fie interogat pe context de siguranță"
    assert ctx.reply is None  # turul curge la triaj/agent → gate + frază garantată


async def test_cache_read_bypassed_when_context_persisted(monkeypatch):
    """Context declarat acum 10 tururi (doar în state) → tot bypass."""
    monkeypatch.setattr("src.worker.stages.cache.exact_lookup", lambda *a, **k: None)
    ctx = _ctx("ce cremă antirid recomanzi?", safety={"contexts": ["pregnancy"]})
    await cache_stage(ctx, _deps())
    assert _bypass(ctx) == "safety_context"


async def test_normal_turn_still_uses_cache(monkeypatch):
    """Fără context → cache-ul funcționează ca înainte (nu stricăm optimizarea)."""
    seen = []

    async def lookup(*a, **k):
        seen.append(a)
        return None

    monkeypatch.setattr("src.worker.stages.cache.exact_lookup", lookup)
    monkeypatch.setattr("src.worker.stages.cache.semantic_lookup", lookup)
    ctx = _ctx("ce cremă antirid recomanzi?")
    await cache_stage(ctx, _deps())
    assert _bypass(ctx) != "safety_context"
    assert seen, "turul normal TREBUIE să interogheze cache-ul"


async def test_kill_switch_off_does_not_bypass(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SAFETY_CONTRAINDICATIONS_ENABLED", "false")
    try:
        monkeypatch.setattr("src.worker.stages.cache.exact_lookup", lambda *a, **k: None)
        ctx = _ctx("sunt însărcinată, ce cremă?")
        await cache_stage(ctx, _deps())
        assert _bypass(ctx) != "safety_context"
    finally:
        get_settings.cache_clear()


# --- SCRIERE -----------------------------------------------------------------------------------


async def test_cache_write_skipped_on_safety_context(monkeypatch):
    """Un răspuns compus sub context de siguranță nu intră în cache-ul partajat."""
    from src.worker import aftercare

    wrote = []
    monkeypatch.setattr(aftercare, "upsert_entry", lambda *a, **k: wrote.append(a))
    ctx = _ctx("sunt însărcinată, ce cremă antirid?")
    ctx.reply = Reply(
        text="Țin cont că ești însărcinată…", cacheable=True
    )  # flag „greșit" dinadins
    await aftercare._cache_writeback(None, object(), "biz-1", "ro", ctx.message.body, ctx)
    assert wrote == []
    assert any(e.type == "cache_write_skipped" for e in ctx.events)


async def test_cache_write_skipped_even_if_cacheable_flag_was_left_true(monkeypatch):
    """Nu depindem de un flag setat de altcineva: verificarea e explicită la scriere."""
    from src.worker import aftercare

    wrote = []
    monkeypatch.setattr(aftercare, "upsert_entry", lambda *a, **k: wrote.append(a))
    ctx = _ctx("ce ser antirid?", safety={"contexts": ["pregnancy"]})
    ctx.reply = Reply(text="un răspuns oarecare lung cât să treacă gate-ul", cacheable=True)
    await aftercare._cache_writeback(None, object(), "biz-1", "ro", ctx.message.body, ctx)
    assert wrote == []


@pytest.mark.parametrize("cacheable", [True, False])
async def test_enforce_marks_reply_non_cacheable_end_to_end(cacheable):
    """A doua plasă: `enforce` pune `cacheable=False` → write-back-ul se oprește oricum."""
    from src.safety.compose import enforce
    from src.safety.contraindications import Block
    from src.safety.policy import Decision

    ctx = _ctx("sunt însărcinată, ce ser?")
    ctx.reply = Reply(text="Îți recomand X", cacheable=cacheable)
    ctx.safety_decision = Decision(
        kept=[],
        blocked=[Block(product_id="p1", context_id="pregnancy", rule_id="r", matched="retinol")],
        contexts=("pregnancy",),
        rule_ids=("pregnancy-retinoids",),
        must_refer=True,
    )
    enforce(ctx)
    assert ctx.reply.cacheable is False
