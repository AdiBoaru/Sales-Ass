"""NX-159 felia 3 — response_style_profile per business (DomainPack) injectat pe căile NON-rich.

Testează: (1) helper-ul pur `response_style_block`; (2) loader-ul citește `response_style` din
defaults + override per-tenant; (3) `render` injectează blocul în proza LLM, gated + byte-identic
când e absent/OFF. Fără DB real → rulează în CI.
"""

from src.agent import finalize as fz
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs, response_style_block
from src.domain.loader import load_domain_pack
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("D", "ecommerce", "ro", ["Parfumuri"], [])


# --- response_style_block (pur) -----------------------------------------------


def test_style_block_empty_when_none():
    assert response_style_block(None) == ""
    assert response_style_block({}) == ""


def test_style_block_renders_ordered_labels():
    block = response_style_block(
        {"ton": "cald", "reguli_upsell": "soft", "nivel_detaliu": "standard"}
    )
    assert block.startswith("Stil de răspuns")
    # ordine stabilă: ton → nivel_detaliu → upsell (salut/disclaimere absente sărite)
    assert block.index("ton: cald") < block.index("nivel de detaliu: standard")
    assert block.index("nivel de detaliu: standard") < block.index("upsell: soft")
    assert block.endswith("\n")


def test_style_block_skips_unknown_and_empty_keys():
    block = response_style_block({"ton": "cald", "necunoscut": "x", "reguli_salut": ""})
    assert "cald" in block and "necunoscut" not in block and "salut" not in block


# --- loader (defaults + override) ---------------------------------------------


def _biz(vertical="beauty", settings=None):
    return BusinessConfig(id="b", slug="d", name="D", vertical=vertical, settings=settings or {})


def test_loader_reads_default_style():
    pack = load_domain_pack(_biz("beauty"))
    assert pack is not None
    assert pack.response_style.get("ton")  # din beauty_salon.json


def test_loader_tenant_override_wins():
    pack = load_domain_pack(
        _biz("beauty", {"domain_pack": {"response_style": {"ton": "sec și direct"}}})
    )
    assert pack.response_style["ton"] == "sec și direct"
    # celelalte chei rămân din default (deep-merge)
    assert pack.response_style.get("reguli_upsell")


# --- integrare prin render (injecție gated) -----------------------------------


class _CapturingLLM:
    """Capturează user-ul de compunere ca să verificăm injecția blocului de stil."""

    def __init__(self):
        self.last_user = None

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.last_user = user
        return "Ce buget ai?"  # forțează fallback/clarify, dar user-ul e capturat

    async def complete_schema(self, system, user, schema, *, model=None):
        raise RuntimeError("no structured output")


def _ctx(pack=None):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", vertical="beauty"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau ceva"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.business.domain_pack = pack
    return ctx


def _plan(**kw):
    base = dict(products=[], final="", is_order=False, query="vreau ceva", history="", inp=INP)
    base.update(kw)
    return ResponsePlan(handled=False, **base)


PROD = {
    "id": "p1",
    "name": "Rhea",
    "brand": "Rhea",
    "price": 18.99,
    "url": "https://shop/p1",
    "ai_summary": "lejer",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["lejer"],
}


async def test_render_injects_style_block_into_prose():
    pack = load_domain_pack(_biz("beauty"))
    llm = _CapturingLLM()
    ctx = _ctx(pack)
    # produse + rich eșuat → cade pe proză (_finalize) → user conține blocul de stil.
    await fz.render(ctx, PipelineDeps(conn=object(), llm=llm), _plan(products=[dict(PROD)]))
    assert llm.last_user is not None and "Stil de răspuns" in llm.last_user


async def test_render_no_style_when_disabled(monkeypatch):
    from src.config import get_settings

    # Override DOAR flag-ul pe settings-ul real (restul căilor din render au nevoie de celelalte).
    monkeypatch.setattr(get_settings(), "response_style_enabled", False)
    pack = load_domain_pack(_biz("beauty"))
    llm = _CapturingLLM()
    ctx = _ctx(pack)
    await fz.render(ctx, PipelineDeps(conn=object(), llm=llm), _plan(products=[dict(PROD)]))
    assert llm.last_user is not None and "Stil de răspuns" not in llm.last_user


async def test_render_no_style_when_pack_absent():
    llm = _CapturingLLM()
    ctx = _ctx(pack=None)  # fără DomainPack → byte-identic (fără bloc)
    await fz.render(ctx, PipelineDeps(conn=object(), llm=llm), _plan(products=[dict(PROD)]))
    assert llm.last_user is not None and "Stil de răspuns" not in llm.last_user
