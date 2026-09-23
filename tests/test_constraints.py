"""NX-133 — stiva de constrângeri multi-tur. `merge_constraints` pur (fără DB/LLM) + un test de
pipeline cu ScriptedLLM care verifică că hint-ul cară constrângerile dintr-un tur anterior."""

import pytest

from src.agent import planner as planner_mod
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Route,
    RouteDecision,
    TurnContext,
)
from src.worker.runner import PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import agent_stage, merge_constraints

# --- funcția pură -------------------------------------------------------------


def test_refine_keeps_prior_constraints():
    # cazul rundei 2: „ser cu vitamina C sub 150" apoi „am tenul mixt" (category_key None) →
    # vitamina C + bugetul se PĂSTREAZĂ, ten mixt se adaugă.
    stored = {"budget_max": 150, "concerns": ["vitamina c"], "category_key": "seruri"}
    merged, reset = merge_constraints(stored, {"suitable_for": "ten mixt"}, None)
    assert reset is False
    assert merged["budget_max"] == 150
    assert merged["concerns"] == ["vitamina c"]
    assert merged["suitable_for"] == "ten mixt"
    assert merged["category_key"] == "seruri"  # păstrat (follow-up neancorat)


def test_new_scalar_overwrites():
    merged, _ = merge_constraints({"budget_max": 150}, {"budget_max": 80}, None)
    assert merged["budget_max"] == 80  # bugetul recent bate vechiul


def test_category_change_resets_stack():
    stored = {"budget_max": 150, "concerns": ["vitamina c"], "category_key": "seruri"}
    merged, reset = merge_constraints(stored, {"concerns": ["matreata"]}, "sampon")
    assert reset is True
    assert "budget_max" not in merged  # reset total pe subiect nou
    assert merged["concerns"] == ["matreata"]
    assert merged["category_key"] == "sampon"


def test_concerns_union_dedupe_recent_first():
    stored = {"concerns": ["vitamina c"]}
    merged, _ = merge_constraints(stored, {"concerns": ["ten mixt", "vitamina c"]}, None)
    # recent (turul curent) întâi, dedupe case-insensitive
    assert merged["concerns"] == ["ten mixt", "vitamina c"]


def test_concerns_capped_at_five():
    stored = {"concerns": ["a", "b", "c", "d", "e"]}
    merged, _ = merge_constraints(stored, {"concerns": ["nou"]}, None)
    assert merged["concerns"] == ["nou", "a", "b", "c", "d"]  # 5 total, recent întâi


def test_empty_in_empty_out():
    merged, reset = merge_constraints({}, {}, None)
    assert merged == {} and reset is False


def test_corrupt_stored_treated_as_empty():
    # state vechi/corupt (string în loc de dict) → tratat ca {}, fără crash (back-compat)
    merged, reset = merge_constraints("nu-i dict", {"budget_max": 50}, None)
    assert merged == {"budget_max": 50} and reset is False


def test_absent_slot_keeps_stored():
    # triaj fără sloturi (nano vechi / follow-up pur) → stiva se păstrează neatinsă
    stored = {"budget_max": 150, "concerns": ["x"], "category_key": "seruri"}
    merged, reset = merge_constraints(stored, {}, None)
    assert merged == stored and reset is False


def test_category_null_keeps_prev_even_with_new_slot():
    merged, reset = merge_constraints({"category_key": "seruri"}, {"brand": "CeraVe"}, None)
    assert reset is False and merged["category_key"] == "seruri" and merged["brand"] == "CeraVe"


# --- pipeline (ScriptedLLM, zero OpenAI/DB real) ------------------------------


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Seruri", "Sampoane"]

    async def _aliases(conn, business_id, **k):
        return []

    async def _no_complementary(conn, business_id, anchor_id, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)
    monkeypatch.setattr(planner_mod, "get_complementary_products", _no_complementary)


class _CaptureLLM:
    """Capturează mesajul USER trimis la run_tool_loop (conține filters_hint)."""

    def __init__(self):
        self.user = None

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None, **kw):
        self.user = user
        return ""  # fără text → nu intră pe calea rich; testăm doar hint-ul


def _ctx(*, state=None, filters=None, category_key=None, body="am tenul mixt"):
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    if state is not None:
        ctx.state = state
    ctx.route = RouteDecision(route=Route.SALES, filters=filters or {}, category_key=category_key)
    return ctx


async def test_hint_carries_prior_constraint_on_refine():
    # turul 2: stivă din turul 1 (vit C + buget) + rafinare „ten mixt" (fără category_key)
    state = ConversationState(
        search_constraints={"budget_max": 150, "concerns": ["vitamina c"], "category_key": "seruri"}
    )
    ctx = _ctx(state=state, filters={"suitable_for": "ten mixt"}, category_key=None)
    llm = _CaptureLLM()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=llm))

    assert llm.user is not None
    # hint-ul (în mesajul USER) cară constrângerile turului 1 + nevoia nouă
    assert "150" in llm.user and "vitamina c" in llm.user and "ten mixt" in llm.user
    # stiva persistată pe ctx.state conține tot
    sc = ctx.state.search_constraints
    assert sc["budget_max"] == 150 and "vitamina c" in sc["concerns"]
    assert sc["suitable_for"] == "ten mixt"
    # event emis cu carried > 0 (au fost cărate chei din stivă)
    ev = [e for e in ctx.events if e.type == "constraints_merged"]
    assert ev and ev[0].properties["carried"] >= 1 and ev[0].properties["reset"] is False


async def test_order_route_does_not_touch_stack():
    state = ConversationState(search_constraints={"budget_max": 150, "category_key": "seruri"})
    ctx = _ctx(state=state, filters={}, category_key=None, body="unde e comanda mea?")
    ctx.route = RouteDecision(route=Route.ORDER)
    llm = _CaptureLLM()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=llm))
    # ruta ORDER nu atinge stiva (rămâne cum era) și nu emite constraints_merged
    assert ctx.state.search_constraints == {"budget_max": 150, "category_key": "seruri"}
    assert not any(e.type == "constraints_merged" for e in ctx.events)


# --- al doilea declanșator de reset: schimbarea de RAFT (incident 2026-09-16) --


def test_topic_switch_resets_stack_without_any_category_key():
    """Incidentul, în forma lui pură: stiva stă pe machiaj (un ruj), clientul cere produse de păr,
    iar triajul NU dă nicio categorie (turul vine după o clarificare, deci triajul nici nu rulează).
    Fără al doilea declanșator, `brand` pleca mai departe în promptul de căutare."""
    stored = {"brand": "MUZIGAE", "concerns": ["ruj mat"], "category_key": "machiaj-buze"}
    merged, reset = merge_constraints(stored, {}, None, switched_topic=True)
    assert reset is True
    assert merged == {}  # stiva cade INTEGRAL, inclusiv ancora de categorie


def test_topic_switch_keeps_what_the_current_turn_declares():
    """Resetul șterge ISTORIA, nu turul curent: ce spune clientul ACUM rămâne."""
    stored = {"brand": "MUZIGAE", "category_key": "machiaj-buze"}
    merged, reset = merge_constraints(stored, {"budget_max": 100}, None, switched_topic=True)
    assert reset is True and merged == {"budget_max": 100}


def test_no_topic_switch_is_byte_identical_to_before():
    """Kill-switch-ul stins (sau raft nenumit) ⇒ exact comportamentul de dinainte."""
    stored = {"brand": "MUZIGAE", "category_key": "machiaj-buze"}
    assert merge_constraints(stored, {}, None, switched_topic=False) == merge_constraints(
        stored, {}, None
    )


async def test_brand_from_another_shelf_no_longer_reaches_the_prompt(monkeypatch):
    """Capătul lanțului, pe pipeline: cu stiva pe machiaj și mesajul „vreau sa vad produse de par",
    hint-ul nu mai poate ordona modelului `brand: MUZIGAE`.

    Asta e fraza care a produs `search_products(brand="MUZIGAE", category="ingrijirea parului")`
    și, două tururi mai târziu, patru carduri de fard de obraz la o cerere de păr."""
    from src.catalog.vocabulary import CatalogVocabulary, VocabEntry

    async def _vocab(deps, business_id):
        return CatalogVocabulary(
            business_id=business_id,
            dimensions={
                "category": (
                    VocabEntry(key="par", label="Par", count=234, path="par"),
                    VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj"),
                    VocabEntry(key="machiaj-buze", label="Buze", count=298, path="machiaj/buze"),
                )
            },
        )

    monkeypatch.setattr(agent_mod, "get_vocabulary", _vocab)
    state = ConversationState(
        search_constraints={
            "brand": "MUZIGAE",
            "concerns": ["ruj mat"],
            "category_key": "machiaj-buze",
        }
    )
    ctx = _ctx(state=state, filters={}, category_key=None, body="vreau sa vad produse de par")
    llm = _CaptureLLM()
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=llm))

    assert llm.user is not None
    assert "MUZIGAE" not in llm.user and "ruj mat" not in llm.user
    assert ctx.state.search_constraints == {}
    ev = [e for e in ctx.events if e.type == "topic_switch_reset"]
    assert ev and ev[0].properties["from_root"] == "machiaj"
    assert ev[0].properties["to_roots"] == ["par"]
    assert set(ev[0].properties["dropped"]) == {"brand", "concerns"}
