"""NX-159 felia 3 — response_style_profile per business (DomainPack) în system-ul de compunere.

Testează: (1) helper-ul pur `response_style_block`; (2) loader-ul citește `response_style` din
defaults + override per-tenant; (3) builderele — style ajunge în `build_agent_system`/
`build_reco_system` (PRIMAR + retry), NU în `build_rich_system` (rich neatins); (4) gating în
`_load_prompt_inputs` (flag OFF / pack absent → fără style). Fără DB real → rulează în CI.
"""

from src.agent import finalize as fz
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import (
    PromptInputs,
    build_agent_system,
    build_reco_system,
    build_rich_system,
    response_style_block,
)
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
INP_STYLED = PromptInputs.build(
    "D", "ecommerce", "ro", ["Parfumuri"], [], response_style={"ton": "cald și direct"}
)


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


# --- PromptInputs.build (hashable, determinist) -------------------------------


def test_prompt_inputs_style_hashable_and_sorted():
    a = PromptInputs.build("D", "e", "ro", [], [], response_style={"ton": "x", "salut": "y"})
    b = PromptInputs.build("D", "e", "ro", [], [], response_style={"salut": "y", "ton": "x"})
    assert a == b and hash(a) == hash(b)  # ordinea din dict nu contează → cache stabil
    assert a.response_style == (("salut", "y"), ("ton", "x"))


def test_prompt_inputs_drops_empty_style_values():
    inp = PromptInputs.build(
        "D", "e", "ro", [], [], response_style={"ton": "x", "reguli_salut": ""}
    )
    assert inp.response_style == (("ton", "x"),)


# --- builderele: PRIMAR + retry stilate, rich NU -----------------------------


def test_agent_system_carries_style():
    assert "Stil de răspuns" in build_agent_system(INP_STYLED)
    assert "Stil de răspuns" not in build_agent_system(INP)  # gol → byte-identic


def test_reco_system_carries_style():
    assert "Stil de răspuns" in build_reco_system(INP_STYLED)
    assert "Stil de răspuns" not in build_reco_system(INP)


def test_rich_system_untouched_by_style():
    # Calea rich are propriile reguli dure → NU primește ghidul de stil.
    assert "Stil de răspuns" not in build_rich_system(INP_STYLED)


# --- gating în _load_prompt_inputs --------------------------------------------


class _FakeConn:
    pass


async def _run_load(monkeypatch, *, pack, enabled):
    from types import SimpleNamespace

    from src.worker.stages import agent as ag

    async def _cats(conn, bid):
        return ["Parfumuri"]

    async def _aliases(conn, bid):
        return []

    monkeypatch.setattr(ag, "list_category_names", _cats)
    monkeypatch.setattr(ag, "list_routing_aliases", _aliases)
    monkeypatch.setattr(
        ag, "get_settings", lambda: SimpleNamespace(response_style_enabled=enabled)
    )
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", vertical="beauty"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.business.domain_pack = pack
    return await ag._load_prompt_inputs(PipelineDeps(conn=_FakeConn()), ctx)


async def test_load_prompt_inputs_injects_style_when_enabled(monkeypatch):
    pack = load_domain_pack(_biz("beauty"))
    inp = await _run_load(monkeypatch, pack=pack, enabled=True)
    assert inp.response_style and "Stil de răspuns" in build_agent_system(inp)


async def test_load_prompt_inputs_no_style_when_flag_off(monkeypatch):
    pack = load_domain_pack(_biz("beauty"))
    inp = await _run_load(monkeypatch, pack=pack, enabled=False)
    assert inp.response_style == ()


async def test_load_prompt_inputs_no_style_when_pack_absent(monkeypatch):
    inp = await _run_load(monkeypatch, pack=None, enabled=True)
    assert inp.response_style == ()


# --- integrare prin render (style ajunge în system-ul de recompunere) ---------


class _CapturingLLM:
    """Capturează SYSTEM-ul de compunere ca să verificăm că blocul de stil ajunge la model."""

    def __init__(self):
        self.last_system = None

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        self.last_system = system
        return "Ce buget ai?"  # invalid grounding → forțează calea de recompunere

    async def complete_schema(self, system, user, schema, *, model=None):
        raise RuntimeError("no structured output")


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


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", vertical="beauty"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau ceva"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


def _plan(inp, **kw):
    base = dict(products=[], final="", is_order=False, query="vreau ceva", history="", inp=inp)
    base.update(kw)
    return ResponsePlan(handled=False, **base)


async def test_render_prose_recompose_uses_styled_system():
    llm = _CapturingLLM()
    # produse + rich eșuat → proză (_finalize) recompune cu build_reco_system(inp stilat).
    await fz.render(
        _ctx(), PipelineDeps(conn=object(), llm=llm), _plan(INP_STYLED, products=[dict(PROD)])
    )
    assert llm.last_system is not None and "Stil de răspuns" in llm.last_system


async def test_render_prose_recompose_no_style_when_inp_plain():
    llm = _CapturingLLM()
    await fz.render(_ctx(), PipelineDeps(conn=object(), llm=llm), _plan(INP, products=[dict(PROD)]))
    assert llm.last_system is not None and "Stil de răspuns" not in llm.last_system
