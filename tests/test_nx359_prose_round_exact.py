"""NX-359 — runda de proză se sare și pe rafinările unei recomandări (profilul `exact`).

Conversația `1848eeba` (`sole-ro`, 2026-09-30): «am tenul uscat» și «ceva sub 100 lei» ies cu
obligația `answer` ⇒ profilul `exact`, iar NX-312 sărea runda doar pe `recommend`. Fiecare tur a
plătit ~2,2 s pentru un text pe care compunerea bogată nu-l citește. Pe 30 de zile, 0 din 40 de ture
`exact` care au început cu o căutare reușită au chemat altă unealtă; runda costa p50 3,2 s.
ZERO OpenAI, zero DB."""

import pytest

from src.agent import turn_profile
from src.config import get_settings
from src.worker.stages.agent import (
    PROSE_ROUND_REASONS,
    _prose_round_redundant,
    agent_stage,
)
from tests.test_agent import _deps, _patch_search
from tests.test_skip_prose_round import _ONE_TYPE, _SEARCH, _ctx_with_pack, _events, _LoopLLM

_SEARCH_OK = dict(is_order=False, called=["search_products"], has_products=True)


def test_the_profiles_a_search_ends_are_declared_data() -> None:
    assert turn_profile.search_ends_turn_names() == frozenset({"exact", "recommend"})


@pytest.mark.parametrize("profile", ["exact", "recommend"])
def test_a_successful_search_ends_exact_and_recommend(profile) -> None:
    ends = turn_profile.search_ends_turn_names()
    assert _prose_round_redundant(profile=profile, search_ends=ends, **_SEARCH_OK) == (
        True,
        "search_only",
    )


@pytest.mark.parametrize("profile", ["compare", "howto", "routine", "mutation"])
def test_profiles_that_expect_a_second_tool_keep_the_round(profile) -> None:
    ends = turn_profile.search_ends_turn_names()
    got = _prose_round_redundant(profile=profile, search_ends=ends, **_SEARCH_OK)
    assert got == (False, "profile_needs_round") and got[1] in PROSE_ROUND_REASONS


def test_exact_still_keeps_the_round_on_another_tool_or_no_products() -> None:
    ends = turn_profile.search_ends_turn_names()
    assert _prose_round_redundant(
        is_order=False,
        profile="exact",
        called=["search_products", "get_product_details"],
        has_products=True,
        search_ends=ends,
    ) == (False, "other_tool")
    assert _prose_round_redundant(
        is_order=False, profile="exact", called=["search_products"], has_products=False,
        search_ends=ends,
    ) == (False, "no_products")  # fmt: skip


def test_default_is_the_nx312_rule() -> None:
    assert _prose_round_redundant(profile="exact", **_SEARCH_OK) == (False, "profile_needs_round")


@pytest.mark.parametrize("body", ["am tenul uscat", "ceva sub 100 lei"])
async def test_refinement_turn_skips_the_round(monkeypatch, body) -> None:
    """Cap-coadă pe stagiul agent: mesajele reale ale conversației ies `exact`."""
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_exact_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "rich_from_facts_enabled", True, raising=False)
    _patch_search(monkeypatch, _ONE_TYPE)
    ctx = _ctx_with_pack(body)
    assert turn_profile.name_for_turn(ctx) == "exact"
    llm = _LoopLLM(tool_calls=_SEARCH)

    await agent_stage(ctx, _deps(llm))

    assert _events(ctx, "prose_round") == [{"skipped": True, "reason": "search_only"}]
    assert ctx.reply is not None and ctx.reply.rich is not None and ctx.reply.rich.items
    assert llm.complete_calls == 0  # rich picat ⇒ rezerva din catalog, fără recompunere


async def test_flag_off_keeps_the_round_on_exact(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "tool_loop_skip_prose_exact_enabled", False, raising=False)
    _patch_search(monkeypatch, _ONE_TYPE)
    ctx = _ctx_with_pack("am tenul uscat")
    llm = _LoopLLM(tool_calls=_SEARCH)

    await agent_stage(ctx, _deps(llm))

    assert _events(ctx, "prose_round") == [{"skipped": False, "reason": "profile_needs_round"}]
