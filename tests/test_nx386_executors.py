"""NX-386 (`kernel.v7.1`) — executorii: rutina fără subiect, limita de jos a prețului, promptul fără
promisiunea unui coleg.

Turele reale (setul wide-2026-10-07): `w1_rutina_seara_pete_fara_retinol#1` (rutină cerută fără
subiect ⇒ fără familie ⇒ căutare simplă pe v1); `w1_antirid_premium_50plus#1` («peste 200 de lei» ⇒
golul `price_min`, o cremă de ochi de 60 de lei servită); `w4_curier_termen_azi#3` (promptul cerea
„zi că verifici cu un coleg", iar coleg nu există). Zero model, zero DB."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from scripts.derive_family_by_need import family_by_need
from src.agent.turn_planner import routine_family
from src.conversation.state_v2 import ConversationStateV2, Topic
from src.domain.routine_steps import RoutineStepConfigError
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import SearchArgs, run_planned_search
from src.worker.runner import PipelineDeps
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import _need

# --- familia rutinei din nevoi ------------------------------------------------------------------


def _pack_with(by_need: dict[str, str]):
    pack = fc.pack("sole-ro")
    steps = dataclasses.replace(pack.routine_steps, family_by_need=by_need)
    return dataclasses.replace(pack, routine_steps=steps)


def _state(needs=(), shelf=None):
    return ConversationStateV2(revision=1, topic=Topic(category_key=shelf), needs=tuple(needs))


def test_a_routine_without_subject_takes_the_family_of_its_needs():
    pack = _pack_with({"concerns:hyperpigmentation": "fata", "concerns:hair_dryness": "par"})
    state = _state([_need("concerns", "hyperpigmentation")])
    assert routine_family(state, pack=pack, vocab=None) == "fata"


def test_needs_of_two_families_decide_nothing():
    pack = _pack_with({"concerns:hyperpigmentation": "fata", "concerns:hair_dryness": "par"})
    state = _state([_need("concerns", "hyperpigmentation"), _need("concerns", "hair_dryness")])
    assert routine_family(state, pack=pack, vocab=None) is None


def test_a_named_shelf_without_a_family_is_not_overridden_by_needs():
    pack = _pack_with({"concerns:hyperpigmentation": "fata"})
    state = _state([_need("concerns", "hyperpigmentation")], shelf="accesorii")
    assert routine_family(state, pack=pack, vocab=None) is None


def test_without_the_data_nothing_changes():
    assert (
        routine_family(_state([_need("concerns", "acne")]), pack=_pack_with({}), vocab=None) is None
    )


def test_the_family_map_is_a_clear_majority_of_the_catalog():
    rows = [("concerns", "acne", "fata")] * 18 + [("concerns", "acne", "corp")] * 2
    rows += [("concerns", "hydration", "fata")] * 10 + [("concerns", "hydration", "corp")] * 10
    rows += [("concerns", "dandruff", "par")] * 5  # prea puține produse
    out = family_by_need(rows, ["fata", "par", "corp"])
    assert out == {"concerns:acne": "fata"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [({"acne": "fata"}, "invalidă"), ({"concerns:acne": "nu-exista"}, "nu e o familie")],
)
def test_the_writer_rejects_a_bad_need_map(raw, message):
    """La SCRIERE (`build_spec`) o hartă stricată e refuzată; la citire costă doar harta."""
    import json
    import pathlib

    from src.domain.routine_steps import build_spec, load_routine_steps

    seed = pathlib.Path("db/seed/domain_pack_sole_ro.json").read_text(encoding="utf-8")
    spec = {**json.loads(seed)["routine_steps"], "family_by_need": raw}
    with pytest.raises(RoutineStepConfigError, match=message):
        build_spec(spec)
    loaded = load_routine_steps(spec)
    assert "fata" in loaded.families and loaded.family_by_need == {}


# --- price_min până în SQL ----------------------------------------------------------------------


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


@pytest.fixture
def lexical(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        calls.append(kwargs)
        return [{"id": "p1", "name": "Crema antirid", "price": 250.0, "lexical_step": "strict"}]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        from src.catalog.vocabulary import CatalogVocabulary

        return CatalogVocabulary(business_id=business_id)

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    return calls


def _ctx(pack) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D", domain_pack=pack),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="ceva antirid premium"),
        conversation_id="conv",
        state=ConversationState(),
    )


async def test_price_min_reaches_the_query_as_a_price_floor(lexical):
    ctx = _ctx(fc.pack("sole-ro"))
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    await run_planned_search(ctx, deps, SearchArgs(query="crema antirid", price_min=200.0))
    bounds = lexical[0].get("constraints") or ()
    assert [(b.constraint.op, float(b.constraint.value)) for b in bounds] == [("gte", 200.0)]


async def test_without_price_min_the_planned_query_has_no_constraint(lexical):
    ctx = _ctx(fc.pack("sole-ro"))
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    await run_planned_search(ctx, deps, SearchArgs(query="crema antirid"))
    assert not lexical[0].get("constraints")


def test_the_model_cannot_send_price_min():
    assert "price_min" in ct.PLANNER_ONLY_FIELDS


# --- promptul nu promite un coleg ---------------------------------------------------------------


def test_the_agent_prompt_does_not_promise_a_colleague():
    from src.agent import prompt_builder

    text = prompt_builder._BASE_RULES if hasattr(prompt_builder, "_BASE_RULES") else ""
    source = (
        text or open(prompt_builder.__file__, encoding="utf-8").read()  # noqa: SIM115
    )
    assert "verifici cu un coleg" not in source


def test_a_bundle_without_subject_is_planned_with_the_family_of_its_needs():
    """`w1_rutina_seara_pete_fara_retinol#1`: «fă-mi o rutină de seară pentru pete» fără subiect ⇒
    pe `main` planul `bundle` fără familie, deci calea v1."""
    from src.agent.turn_planner import plan_turn
    from tests.test_kernel_planner import _gate, _interp

    pack = _pack_with({"concerns:hyperpigmentation": "fata"})
    planned = plan_turn(
        _interp(acts=[{"kind": "bundle"}]),
        _state([_need("concerns", "hyperpigmentation")]),
        (),
        (),
        _gate(),
        changed=True,
        pack=pack,
        vocab=fc.vocabulary("sole-ro"),
        locale="ro",
    )
    plan = planned.plans[planned.primary]
    assert (plan.executor, plan.family) == ("bundle", "fata")
