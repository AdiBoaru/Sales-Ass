"""NX-312 felia 5 — instrumentul de replay pe `reasoning_effort`, fără nicio rulare reală.

Ce trebuie să fie adevărat ca raportul să poată decide ceva:
1. `--dry-run` reface tot inputul și nu face NICIUN apel de model (DoD);
2. apelul se construiește din funcțiile PRODUCȚIEI, nu din copii (altfel măsori alt prompt);
3. efortul se schimbă doar pe durata apelului și revine și pe eroare (altfel un apel picat lasă
   restul rulării pe alt efort decât crezi);
4. perechile sunt OARBE și cheia le inversează exact.
ZERO OpenAI, zero DB."""

from __future__ import annotations

import pytest

from scripts.nx312_rich_effort_replay import (
    ReplayCase,
    _parse_efforts,
    blind_pairs,
    build_call,
    make_ctx,
    replay,
    summarize,
)
from src.agent.finalize import _rich_schema, rich_user_message
from src.agent.prompt_builder import build_rich_system
from src.config import get_settings
from src.models import BusinessConfig
from tests.test_plan_guidance import INP, PACK, VARIED

EFFORTS = ("high", "medium", "low")


def _business() -> BusinessConfig:
    b = BusinessConfig(id="b", slug="s", name="S")
    b.domain_pack = PACK
    return b


def _case(i: int = 1) -> ReplayCase:
    return ReplayCase(
        turn_id=f"{i:08d}-0000-0000-0000-000000000000",
        query="vreau o crema de hidratare",
        language="ro",
        history="Client: salut\nAsistent: Bună!",
        products=tuple(dict(p) for p in VARIED),
        products_source="retrieval_ids",
    )


def _prepared(n: int = 1):
    out = []
    for i in range(1, n + 1):
        case = _case(i)
        ctx = make_ctx(_business(), case)
        out.append((ctx, case, build_call(ctx, INP, case, frozenset())))
    return out


class _NoModel:
    async def complete_schema(self, *a, **k):
        raise AssertionError("dry-run nu are voie să cheme modelul")


class _FakeModel:
    """Răspunde ca modelul și ține minte efortul VĂZUT în momentul apelului."""

    def __init__(self, fail_on: str | None = None):
        self.seen: list[str] = []
        self.fail_on = fail_on

    async def complete_schema(self, system, user, schema, *, model=None):
        effort = get_settings().llm_reasoning_effort_agent
        self.seen.append(effort)
        if effort == self.fail_on:
            raise TimeoutError("simulat")
        return {
            "intro": f"Intro la efortul {effort}.",
            "items": [
                {"product_id": p["id"], "pro_index": 0, "fit_clause": "ușoară"} for p in VARIED
            ],
            "pick": None,
            "education": None,
            "suggestions": [],
        }


# --- 1. dry-run: zero apeluri ------------------------------------------------------------------


async def test_dry_run_makes_no_model_call():
    entries = await replay(_prepared(3), _NoModel(), EFFORTS, seed=1, dry_run=True)
    assert len(entries) == 3
    assert all(e["results"] == {} for e in entries)
    assert all(sorted(e["order"]) == sorted(EFFORTS) for e in entries)
    assert all(e["system_chars"] > 0 and e["user_chars"] > 0 for e in entries)


# --- 2. apelul = funcțiile producției ----------------------------------------------------------


def test_the_call_is_built_by_production_functions():
    case = _case()
    ctx = make_ctx(_business(), case)
    system, user, schema = build_call(ctx, INP, case, frozenset())
    assert system == build_rich_system(INP)
    ctx2 = make_ctx(_business(), case)
    assert user == rich_user_message(ctx2, case.query, [dict(p) for p in VARIED], case.history)
    assert schema is _rich_schema()
    assert "Conversație până acum:\nClient: salut" in user


# --- 3. efortul: pe apel, restaurat ------------------------------------------------------------


async def test_each_effort_reaches_the_call_and_the_setting_is_restored():
    before = get_settings().llm_reasoning_effort_agent
    model = _FakeModel()
    entries = await replay(_prepared(1), model, EFFORTS, seed=7, dry_run=False)
    assert model.seen == entries[0]["order"]  # fiecare apel a văzut efortul LUI, în ordinea rulată
    assert get_settings().llm_reasoning_effort_agent == before
    res = entries[0]["results"]
    assert set(res) == set(EFFORTS)
    assert all(r["ok"] and r["served"]["cards"] == 3 for r in res.values())
    assert "Intro la efortul high." in res["high"]["visible"]


async def test_a_failed_call_is_a_result_and_the_setting_still_comes_back():
    before = get_settings().llm_reasoning_effort_agent
    entries = await replay(_prepared(1), _FakeModel(fail_on="low"), EFFORTS, seed=7, dry_run=False)
    low = entries[0]["results"]["low"]
    assert low["ok"] is False and low["error"] == "TimeoutError"
    assert entries[0]["results"]["high"]["ok"] is True  # celelalte au rulat mai departe
    assert get_settings().llm_reasoning_effort_agent == before


async def test_order_is_shuffled_across_turns():
    """System-ul e identic între eforturi, deci al doilea apel prinde cache-ul primului. O ordine
    fixă ar favoriza sistematic același efort."""
    entries = await replay(_prepared(12), _NoModel(), EFFORTS, seed=3, dry_run=True)
    assert len({tuple(e["order"]) for e in entries}) > 1


# --- 4. perechi oarbe --------------------------------------------------------------------------


async def test_blind_pairs_hide_the_effort_and_the_key_maps_back():
    entries = await replay(_prepared(10), _FakeModel(), EFFORTS, seed=5, dry_run=False)
    pairs, key = blind_pairs(entries, "medium", EFFORTS, seed=5)
    assert len(pairs) == 10 * 2  # medium vs high și medium vs low, pe fiecare tur
    firsts = set()
    for p in pairs:
        assert "effort" not in p and set(p) == {"pair_id", "turn_id", "A", "B"}
        k = key[p["pair_id"]]
        assert "medium" in k.values() and k["A"] != k["B"]
        # Cheia chiar corespunde textului: intro-ul fals poartă numele efortului.
        assert f"efortul {k['A']}." in p["A"] and f"efortul {k['B']}." in p["B"]
        firsts.add(k["A"])
    assert "medium" in firsts and len(firsts) > 1  # referința nu e mereu pe aceeași parte


async def test_pairs_with_a_failed_side_are_left_out():
    entries = await replay(_prepared(2), _FakeModel(fail_on="low"), EFFORTS, seed=5, dry_run=False)
    pairs, key = blind_pairs(entries, "medium", EFFORTS, seed=5)
    assert all("low" not in k.values() for k in key.values())
    assert len(pairs) == 2


async def test_blind_pairs_are_reproducible_by_seed():
    entries = await replay(_prepared(4), _FakeModel(), EFFORTS, seed=5, dry_run=False)
    assert blind_pairs(entries, "medium", EFFORTS, seed=9) == blind_pairs(
        entries, "medium", EFFORTS, seed=9
    )


# --- raport + argumente ------------------------------------------------------------------------


async def test_summary_counts_calls_failures_and_cards():
    entries = await replay(_prepared(3), _FakeModel(fail_on="low"), EFFORTS, seed=1, dry_run=False)
    s = summarize(entries, EFFORTS)
    assert s["high"]["calls"] == 3 and s["high"]["ok"] == 3 and s["high"]["cards_mean"] == 3
    assert s["low"]["ok"] == 0 and s["low"]["ms_p50"] is None and s["low"]["cards_mean"] is None


@pytest.mark.parametrize("raw", ["high", "high,high", "high,minimal", "high,turbo"])
def test_bad_effort_lists_are_refused(raw):
    """`minimal` nu există pe GPT-6: acceptat aici, s-ar fi aflat abia la primul apel plătit."""
    with pytest.raises(SystemExit):
        _parse_efforts(raw)


def test_good_effort_list_keeps_order():
    assert _parse_efforts("low, high") == ("low", "high")
    assert _parse_efforts("none,low,medium") == ("none", "low", "medium")
