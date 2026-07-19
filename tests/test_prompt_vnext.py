"""NX-181 — Prompt vNext: reguli relaxate + response_shape/anti-rep + flag per business + cache.

Contract de cod (fără LLM real): OFF byte-identic (system + USER); ON injectează DOAR în USER;
response_shape din INTENȚIE; flag efectiv = global AND business opt-in; cache namespace pe versiune.
Naturalețea se măsoară cu evaluatorul (NX-180), nu aici.
"""

import inspect
from pathlib import Path

from src.agent import finalize, prompt_builder
from src.agent.finalize import _last_bot_opening
from src.agent.planner import _response_shape
from src.agent.prompt_builder import PromptInputs, prompt_vnext_effective
from src.config import get_settings
from src.db.queries import semantic_cache
from src.models import Author, Direction, Message


def _inp() -> PromptInputs:
    return PromptInputs.build("Demo", "beauty", "ro", ["creme"], [])


# --- system prompt: OFF byte-identic, ON relaxat ------------------------------


def test_rich_system_vnext_relaxes_quantity_and_adds_shape():
    off = prompt_builder.build_rich_system(_inp(), vnext=False)
    on = prompt_builder.build_rich_system(_inp(), vnext=True)
    assert off != on
    assert "PÂNĂ LA 4 produse" in off and "PÂNĂ LA 4 produse" not in on
    assert "1-3 produse" in on
    assert "MOD DE RĂSPUNS" in on and "MOD DE RĂSPUNS" not in off
    assert "id inventat" in on  # grounding păstrat


def test_rich_system_off_byte_identic_backcompat():
    assert prompt_builder.build_rich_system(_inp()) == prompt_builder.build_rich_system(
        _inp(), vnext=False
    )


# --- response_shape din INTENȚIE (nu din numărul de rezultate) ----------------


def test_response_shape_from_intent_not_count():
    # „mai ieftin" găsește 1 produs → direct_followup (NU detail din len==1)
    assert (
        _response_shape([], attr_query=False, cheaper_intent=True, rehydrated=False)
        == "direct_followup"
    )
    # „spune-mi mai multe despre primul" + get_product_details → detail
    assert (
        _response_shape(
            ["get_product_details"], attr_query=False, cheaper_intent=False, rehydrated=False
        )
        == "detail"
    )
    # superlativ redus la 1 produs de safety gate → tot direct_followup
    assert (
        _response_shape(
            ["search_products"], attr_query=True, cheaper_intent=False, rehydrated=False
        )
        == "direct_followup"
    )
    # rehidratat → direct_followup
    assert (
        _response_shape([], attr_query=False, cheaper_intent=False, rehydrated=True)
        == "direct_followup"
    )
    # căutare nouă → recommendation
    assert (
        _response_shape(
            ["search_products"], attr_query=False, cheaper_intent=False, rehydrated=False
        )
        == "recommendation"
    )


# --- flag EFECTIV per business (global AND business opt-in) --------------------


class _Biz:
    def __init__(self, settings, domain_pack=None):
        self.settings = settings
        self.domain_pack = domain_pack


def test_prompt_vnext_effective_per_business():
    s = get_settings()
    orig = s.prompt_vnext_enabled
    try:
        s.prompt_vnext_enabled = False
        assert prompt_vnext_effective(_Biz({"prompt_vnext_enabled": True})) is False  # global OFF
        s.prompt_vnext_enabled = True
        assert prompt_vnext_effective(_Biz({})) is False  # setare lipsă → OFF
        assert prompt_vnext_effective(_Biz({"prompt_vnext_enabled": False})) is False  # false → OFF
        assert (
            prompt_vnext_effective(_Biz({"prompt_vnext_enabled": "yes"})) is False
        )  # invalid → OFF
        assert prompt_vnext_effective(_Biz(None)) is False  # fără dict settings → OFF
        assert prompt_vnext_effective(_Biz({"prompt_vnext_enabled": True})) is True  # true → ON
    finally:
        s.prompt_vnext_enabled = orig


# --- USER: OFF fără shape/anti-rep, ON cu ele (system NEATINS) -----------------


class _RecUserLLM:
    async def complete_schema(self, system, user, schema, model=None):
        self.system = system
        self.user = user
        raise RuntimeError("capturat")  # → _finalize_rich prinde excepția, întoarce None


class _Ctx:
    language = "ro"
    business = _Biz({}, domain_pack=None)

    def emit(self, *a, **k):
        pass


async def _capture(response_shape: str, last_opening: str):
    s = get_settings()
    prev = s.decision_axes_enabled
    s.decision_axes_enabled = False  # izolăm USER-ul de calea de axe (compose.decision_axes)
    try:
        llm = _RecUserLLM()
        prods = [{"id": "p1", "name": "Cremă X", "price": 50.0}]
        await finalize._finalize_rich(
            llm,
            "SYSTEM-PREFIX",
            "vreau o cremă",
            prods,
            _Ctx(),
            "istoric",
            response_shape=response_shape,
            last_opening=last_opening,
        )
        return llm.system, llm.user
    finally:
        s.decision_axes_enabled = prev


async def test_off_user_has_no_shape_or_antirep():
    system, user = await _capture("", "")
    assert system == "SYSTEM-PREFIX"  # system-ul nu e atins de vNext (doar USER-ul)
    assert "Mod de răspuns" not in user
    assert "Anti-repetiție" not in user


async def test_on_injects_shape_and_antirep_in_user_only():
    system, user = await _capture("direct_followup", "Pentru ten gras")
    assert system == "SYSTEM-PREFIX"  # tot în USER, nu în system (prompt caching intact)
    assert "Mod de răspuns: direct_followup" in user
    assert "Anti-repetiție" in user and "Pentru ten gras" in user


# --- anti-repetiție cu Message.Direction.OUTBOUND real ------------------------


def test_last_bot_opening_real_outbound_message():
    class _HistCtx:
        history = [
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body="salut"),
            Message(
                direction=Direction.OUTBOUND,
                author=Author.BOT,
                body="Pentru ten gras, uite variantele. Prima e X.",
            ),
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body="si mai ieftin?"),
        ]

    assert _last_bot_opening(_HistCtx()) == "Pentru ten gras, uite variantele"

    class _Empty:
        history: list = []

    assert _last_bot_opening(_Empty()) == ""


# --- vocabular unic + cache namespace -----------------------------------------


def test_no_response_hint_vocabulary():
    root = Path(__file__).resolve().parents[1] / "src" / "agent"
    for f in ("prompt_builder.py", "finalize.py", "planner.py"):
        assert "response_hint" not in (root / f).read_text(encoding="utf-8")


def test_cache_queries_parameterized_by_prompt_version():
    # read (exact + semantic) ȘI write (upsert) folosesc aceeași dimensiune → izolare v1/vNext
    for fn in (
        semantic_cache.exact_lookup,
        semantic_cache.semantic_lookup,
        semantic_cache.upsert_entry,
    ):
        assert "prompt_version" in inspect.signature(fn).parameters
