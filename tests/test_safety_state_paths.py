"""NX-173 (P0) — căile care REHIDRATEAZĂ din `displayed_products` / aduc produse în afara buclei de
tool-uri: link intent, compare intent (`deterministic.py`) + cross-sell, superlativ, „mai ieftin",
rehidratare de grounding (`planner.py`).

Toate au ocolit gate-ul în prima versiune (review Codex): ies ÎNAINTE de tool loop, deci
backstop-ul din `ToolRun.execute` nu le vede. Scenariul: turul 1 afișează un retinoid (legitim,
fără context), turul 2 declară sarcina și cere „dă-mi linkul" / „compară primele două" / „care e
cea mai bună" — produsul VECHI din state nu are voie să reapară.
"""

import pytest

from src.agent import deterministic as det
from src.agent import planner as pl
from src.agent.deterministic import try_pre_intents
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Message,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_stage_mod

UNSAFE = {
    "id": "unsafe-retinal",
    "name": "LumaDerm Renew Ser",
    "brand": "LumaDerm",
    "price": 149.0,
    "url": "https://shop.x/p/2",
    "ai_summary": "ser cu retinal",
    "availability": "in_stock",
    "rating": 4.9,
    "attributes": {"key_ingredients": ["retinal"], "best_for": "riduri"},
}
SAFE = {
    "id": "safe-bakuchiol",
    "name": "Ser Bakuchiol Gentle",
    "brand": "Auralis",
    "price": 84.0,
    "url": "https://shop.x/p/1",
    "ai_summary": "bakuchiol",
    "availability": "in_stock",
    "rating": 4.6,
    "attributes": {"key_ingredients": ["bakuchiol"], "best_for": "riduri"},
}
SAFE2 = {**SAFE, "id": "safe-vitc", "name": "Nova Vitamina C", "price": 99.0, "rating": 4.4}
BY_ID = {p["id"]: p for p in (UNSAFE, SAFE, SAFE2)}
PREG = "sunt însărcinată"


def _ctx(body: str, displayed: list[str], *, filters=None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[Message(direction="inbound", author="contact", body=PREG)],
        state=ConversationState(
            displayed_products=[
                ProductRef(product_id=i, name=BY_ID[i]["name"], price=BY_ID[i]["price"])
                for i in displayed
            ]
        ),
    )
    ctx.route = RouteDecision(route=Route.SALES, filters=filters or {})
    return ctx


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def by_ids(conn, business_id, ids, **k):
        return [dict(BY_ID[i]) for i in ids if i in BY_ID]

    for mod in (det, pl, agent_stage_mod):
        monkeypatch.setattr(mod, "get_products_by_ids", by_ids, raising=False)

    async def roots(conn, business_id, ids, **k):
        return {i: "skincare" for i in ids}

    monkeypatch.setattr(det, "product_category_roots", roots, raising=False)


def _rids(ctx) -> set[str]:
    return {str(p.get("id")) for p in (ctx.retrieval.products if ctx.retrieval else [])}


# --- [leak] link intent -------------------------------------------------------------------------


async def test_link_intent_does_not_serve_blocked_product_from_state():
    """T1 a afișat un retinoid; T2 „sunt însărcinată, dă-mi linkul" → NU-l retrimite."""
    ctx = _ctx("dă-mi linkul", ["unsafe-retinal"])
    handled = await try_pre_intents(ctx, _deps())
    assert handled is True  # P6: tratat, nu tăcere
    assert _rids(ctx) == set()
    assert "LumaDerm" not in (ctx.reply.text or "")
    assert not (ctx.reply.products or [])
    assert ctx.reply.offer is None, "niciun buton open_url spre produsul blocat"


async def test_link_intent_still_serves_safe_product():
    ctx = _ctx("dă-mi linkul", ["safe-bakuchiol"])
    assert await try_pre_intents(ctx, _deps()) is True
    assert _rids(ctx) == {"safe-bakuchiol"}
    assert ctx.reply.offer is not None and "shop.x/p/1" in ctx.reply.offer.url


async def test_link_intent_serves_only_safe_of_mixed_set():
    ctx = _ctx("dă-mi linkul", ["unsafe-retinal", "safe-bakuchiol"])
    assert await try_pre_intents(ctx, _deps()) is True
    assert _rids(ctx) == {"safe-bakuchiol"}
    assert "LumaDerm" not in (ctx.reply.text or "")


# --- [leak] compare intent ----------------------------------------------------------------------


async def test_compare_intent_falls_back_when_one_is_blocked():
    """<2 sigure → False → cade pe bucla LLM (care caută fresh, deja gate-uit). NU compară tăcut."""
    ctx = _ctx("compară primele două", ["unsafe-retinal", "safe-bakuchiol"])
    assert await try_pre_intents(ctx, _deps()) is False
    assert ctx.reply is None


async def test_compare_intent_works_on_two_safe():
    ctx = _ctx("compară primele două", ["safe-bakuchiol", "safe-vitc"])
    assert await try_pre_intents(ctx, _deps()) is True
    assert _rids(ctx) == {"safe-bakuchiol", "safe-vitc"}


async def test_compare_intent_blocked_even_with_inherited_filters():
    """Filtre moștenite de triaj (NX-174) nu schimbă nimic pt siguranță: gate-ul e independent."""
    ctx = _ctx(
        "compară primele două", ["unsafe-retinal", "safe-bakuchiol"], filters={"category": "seruri"}
    )
    # cu filtre, `try_pre_intents` nici nu intră pe compare — dar dacă intră, gate-ul blochează
    assert await try_pre_intents(ctx, _deps()) is False


# --- [leak] planner: superlativ / cheaper / rehidratare -----------------------------------------


async def test_attr_query_rehydrate_excludes_blocked(monkeypatch):
    """[leak] „care e cea mai bună?" pe setul afișat → nu reintroduce retinoidul din T1."""
    from src.safety.policy import SafetyPolicy

    ctx = _ctx("care dintre ele e cea mai bună?", ["unsafe-retinal", "safe-bakuchiol"])
    policy = SafetyPolicy.for_turn(ctx)
    kept, d = policy.gate(ctx, [dict(UNSAFE), dict(SAFE)], purpose="attr_query")
    assert {p["id"] for p in kept} == {"safe-bakuchiol"}
    assert d.blocked_ids == ("unsafe-retinal",)


async def test_cheaper_search_excludes_blocked():
    """[leak] „ceva mai ieftin" = căutare NOUĂ în DB, în afara ToolRun."""
    from src.safety.policy import SafetyPolicy

    ctx = _ctx("ceva mai ieftin", ["safe-vitc"])
    kept, _ = SafetyPolicy.for_turn(ctx).gate(ctx, [dict(UNSAFE), dict(SAFE)], purpose="cheaper")
    assert {p["id"] for p in kept} == {"safe-bakuchiol"}


async def test_cross_sell_excludes_blocked():
    """[leak] un cart_add SIGUR putea trage un complement contraindicat."""
    from src.safety.policy import SafetyPolicy

    ctx = _ctx("adaugă-l în coș", ["safe-bakuchiol"])
    kept, _ = SafetyPolicy.for_turn(ctx).gate(ctx, [dict(UNSAFE)], purpose="cross_sell")
    assert kept == []


# --- decizia acumulată pe tur -------------------------------------------------------------------


async def test_decision_accumulates_across_paths():
    """Mai multe căi într-un tur → o singură decizie (union), ca fraza să fie garantată o dată."""
    from src.safety.policy import SafetyPolicy

    ctx = _ctx("dă-mi linkul", ["unsafe-retinal"])
    policy = SafetyPolicy.for_turn(ctx)
    policy.gate(ctx, [dict(UNSAFE)], purpose="link")
    policy.gate(ctx, [dict(SAFE)], purpose="search")
    d = ctx.safety_decision
    assert d.must_refer is True
    assert d.blocked_ids == ("unsafe-retinal",)
    assert d.contexts == ("pregnancy",)


# --- curățarea state-ului la declararea contextului ---------------------------------------------


async def test_prune_displayed_removes_now_blocked_products():
    """T1 a afișat un retinoid (legitim); T2 declară sarcina → iese din `displayed_products`.
    Hidratează din catalog: „LumaDerm Renew Ser" nu-și trădează retinalul în NUME, deci un prune
    pe ref-urile din state (id/nume/preț) ar rata exact cazul real."""
    from src.worker.stages.agent import _persist_safety_context

    ctx = _ctx("sunt însărcinată, ce ai?", ["unsafe-retinal", "safe-bakuchiol"])
    await _persist_safety_context(ctx, _deps())
    assert [p.product_id for p in ctx.state.displayed_products] == ["safe-bakuchiol"]
    assert [p["product_id"] for p in ctx.state_patch["displayed_products"]] == ["safe-bakuchiol"]
    assert any(e.type == "safety_state_pruned" for e in ctx.events)


async def test_prune_persists_context_with_source_and_timestamp():
    from src.worker.stages.agent import _persist_safety_context

    ctx = _ctx("sunt însărcinată", ["safe-bakuchiol"])
    await _persist_safety_context(ctx, _deps())
    patch = ctx.state_patch["safety"]
    assert patch["contexts"] == ["pregnancy"]
    assert patch["source"] == "declared_by_contact" and patch["updated_at"]
    assert ctx.state.safety == patch  # vizibil pt policy-ul chemat mai jos în ACELAȘI tur


async def test_no_prune_without_context():
    from src.worker.stages.agent import _persist_safety_context

    ctx = _ctx("ce seruri ai?", ["unsafe-retinal", "safe-bakuchiol"])
    ctx.history = []
    await _persist_safety_context(ctx, _deps())
    assert len(ctx.state.displayed_products) == 2
    assert "safety" not in ctx.state_patch


async def test_persist_is_idempotent_when_context_already_stored():
    """Context deja persistat → nicio scriere inutilă de state (P4: bugetul de 8KB)."""
    from src.worker.stages.agent import _persist_safety_context

    ctx = _ctx("sunt însărcinată", ["safe-bakuchiol"])
    ctx.state.safety = {"contexts": ["pregnancy"], "source": "declared_by_contact"}
    await _persist_safety_context(ctx, _deps())
    assert "safety" not in ctx.state_patch


async def test_prune_survives_db_failure(monkeypatch):
    """Query eșuat → state neatins, turul continuă (P6). Garanția reală rămâne la rehidratare."""
    from src.worker.stages import agent as agent_stage_mod
    from src.worker.stages.agent import _persist_safety_context

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_stage_mod, "get_products_by_ids", boom)
    ctx = _ctx("sunt însărcinată", ["unsafe-retinal"])
    await _persist_safety_context(ctx, _deps())
    assert len(ctx.state.displayed_products) == 1  # neatins
    assert ctx.state_patch["safety"]["contexts"] == ["pregnancy"]  # contextul TOT se persistă
