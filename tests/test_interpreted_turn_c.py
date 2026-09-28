"""NX-336 PR C (felia C1) — executorii de CITIRE ai turului interpretat.

Ce dovedește suita:

- `build_plan(kernel=True)` nu mai re-deduce intenția (checkout, cross-sell, chip, superlativ,
  „mai ieftin", R3), iar `kernel=False` e calea de azi;
- `search` servește prin `ToolRun.execute_planned` + compunerea v1; zero rezultate ⇒ fraza
  `no_results` a pachetului, niciodată ecranul vechi; fără frază ⇒ fallback `no_sentence`;
- `page`, `detail`, `compare`, `link` (niciodată `ids=None`), `ask`, `reply_only`, fiecare pe
  executorul de azi, cu id-urile DOAR din plan (I1);
- ce nu e legat (mutații, `bundle`, `delegate`, `faq`, `order`, multi-act) rămâne `dark`;
- dezvăluirile plannerului se pun o singură dată ÎNAINTEA răspunsului, din pachet (fail-open);
- `kernel_sentences`: vocabular închis, fără marcatori, prin loader.

Zero model, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace as NS

import pytest

from src.agent import interpreted_turn as it
from src.agent import kernel_executors as kx
from src.agent.turn_planner import PlannedTurn
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import AmbiguityDecision, TurnPlan
from src.conversation.state_v2 import ConversationStateV2
from src.domain.loader import _norm_kernel_sentences
from src.domain.pack import KERNEL_SENTENCE_CODES
from src.tools.base import ToolResult
from tests.kernel import replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}
K01 = ("k01-electronics-park-and-resume", 0)


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _plan(executor: str, product_ids=(), search_args=None) -> TurnPlan:
    return TurnPlan(
        executor=executor, product_ids=tuple(product_ids), search_args=search_args, depends_on=None
    )


def _planned(*plans: TurnPlan, disclosures=()) -> PlannedTurn:
    return PlannedTurn(plans=tuple(plans), primary=0, disclosures=tuple(disclosures))


def _outcome(question: str | None = None, verdict: str = "act") -> GateOutcome:
    return GateOutcome(
        decision=AmbiguityDecision(verdict=verdict, reason="none", question=question)
    )


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


def _stub_search(monkeypatch, cat, ids):
    """`run_planned_search` pe catalogul de fixture: întoarce `ids` (poate fi gol)."""
    from src.tools import catalog_tools

    calls = []

    async def search(ctx, deps, args):
        calls.append(args)
        async with deps.db("search_products"):
            pass
        rows = sh.product_rows(cat, list(ids))
        return ToolResult(
            ok=True,
            products=rows,
            state_patch=(
                {"active_search": {"fp": "fx", "pool": list(ids), "cursor": 0, "page": 0}}
                if rows
                else {}
            ),
        )

    monkeypatch.setattr(catalog_tools, "run_planned_search", search)
    return calls


def _with_sentences(cat, sentences):
    """Același catalog, cu alt `kernel_sentences` (pachetul e înghețat)."""
    return dataclasses.replace(cat, pack=dataclasses.replace(cat.pack, kernel_sentences=sentences))


async def _turn(monkeypatch, cat, journey=K01, *, state=None, previous=()):
    turn = JOURNEYS[journey[0]].turns[journey[1]]
    ctx = sh.build_ctx(cat, state or ConversationStateV2(), turn.user_input, previous)
    return await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(turn.expect["interpretation"]))


# --- 1. search ------------------------------------------------------------------------------------


async def test_search_is_served_by_the_planned_search_and_the_v1_composition(
    monkeypatch, electronics
):
    shown = JOURNEYS[K01[0]].turns[K01[1]].shown
    calls = _stub_search(monkeypatch, electronics, shown)
    run = await _turn(monkeypatch, electronics)
    ctx = run.ctx
    assert run.branch_result is True, _events(ctx, "kernel_turn")
    assert len(calls) == 1, "o singură căutare, cu argumentele PLANULUI"
    assert run.llm.loops == [], "bucla de unelte a căii v1 nu rulează"
    assert ctx.reply is not None and ctx.reply.text
    served = [p.get("id") or p.get("product_id") for p in (ctx.reply.products or [])]
    if ctx.reply.rich is not None:
        served = [i.product_id for i in ctx.reply.rich.items]
    assert served and set(served) <= set(shown)
    assert ctx.retrieval is not None and ctx.retrieval.catalog_read
    [turn] = _events(ctx, "kernel_turn")
    assert turn["served"] is True and turn["executor"] == "search"
    assert ctx.kernel_turn is not None


async def test_zero_results_answer_with_the_pack_sentence_not_the_old_screen(
    monkeypatch, electronics
):
    _stub_search(monkeypatch, electronics, ())
    run = await _turn(monkeypatch, electronics)
    ctx = run.ctx
    assert run.branch_result is True
    sentence = kx.kernel_sentence(electronics.pack, "ro", "no_results")
    assert sentence and ctx.reply.text == sentence
    assert not ctx.reply.products and ctx.reply.rich is None
    assert ctx.reply.cacheable is False
    assert ctx.retrieval.products == [] and ctx.retrieval.catalog_read


async def test_zero_results_without_the_sentence_fall_back_to_v1(monkeypatch, electronics):
    _stub_search(monkeypatch, electronics, ())
    bare = _with_sentences(electronics, {})
    run = await _turn(monkeypatch, bare)
    ctx = run.ctx
    assert run.branch_result is False and ctx.kernel_turn is None
    assert _events(ctx, "kernel_turn")[-1]["fallback_reason"] == "no_sentence"
    assert {"code": "no_results", "turn_id": "t0"} in _events(ctx, "kernel_sentence_missing")
    assert run.after_branch == run.before_branch, "contextul predat căii v1 e cel de dinainte"
    # recenzia, constatarea 6: evenimentele căutării kernelului nu rămân lângă ale căii v1 (bucla
    # scriptată a căii v1 nu cheamă nicio unealtă, deci niciun `tool_call` nu trebuie să existe)
    assert _events(ctx, "tool_call") == []


async def test_zero_results_after_a_name_not_found_say_one_thing(monkeypatch, electronics):
    """Recenzia, constatarea 5: pe `no_results`, dezvăluirea `not_exact_match` („îți arăt ce am
    cel mai aproape") ar contrazice fraza. Rămâne doar `no_results`."""
    ctx = _ctx(electronics, "x")
    _stub_search(monkeypatch, electronics, ())
    planned = _planned(
        _plan(executor="search", search_args=_args()), disclosures=((0, "not_exact_match"),)
    )
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert ctx.reply.text == kx.kernel_sentence(electronics.pack, "ro", "no_results")


async def test_an_exhausted_same_search_refuses_instead_of_not_in_catalog(monkeypatch, electronics):
    """Recenzia, constatarea 5: aceeași amprentă ca sesiunea activă, pool epuizat ⇒ nu e „nu am
    găsit în catalog"; executorul refuză, iar răspunsul „nu mai am" e al căii v1."""
    from src.tools import catalog_tools

    async def exhausted(ctx, deps, args):
        return ToolResult(ok=True, products=[], llm_view=catalog_tools._NO_MORE_VIEW)

    monkeypatch.setattr(catalog_tools, "run_planned_search", exhausted)
    planned = _planned(_plan(executor="search", search_args=_args()))
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is False


# --- 2. executorii, direct ------------------------------------------------------------------------


def _ctx(cat, text="x", state=None):
    return sh.build_ctx(cat, state or ConversationStateV2(), text)


def _deps():
    return NS(db=sh.RecordingDb(), llm=sh.StageLLM())


def _args():
    from src.tools.catalog_tools import SearchArgs

    return SearchArgs(query="x")


async def test_link_is_never_called_with_no_ids(monkeypatch, electronics):
    seen = []

    async def link(ctx, deps, ids=None):
        seen.append(ids)
        ctx.set_reply("linkuri", cacheable=False)

    monkeypatch.setattr(kx.det, "_handle_link_intent", link)
    ctx = _ctx(electronics)
    empty = _planned(_plan(executor="link", product_ids=()))
    assert await kx.execute_read_plans(ctx, _deps(), empty, _outcome()) is False
    one = _planned(_plan(executor="link", product_ids=("p1",)))
    assert await kx.execute_read_plans(ctx, _deps(), one, _outcome()) is True
    assert seen == [["p1"]]


@pytest.mark.parametrize(
    ("executor", "ids", "called"),
    [
        ("detail", ("p1",), "details"),
        ("detail", ("p1", "p2"), "comparison"),
        ("compare", ("p1", "p2"), "comparison"),
    ],
)
async def test_detail_and_compare_route_to_the_existing_handlers(
    monkeypatch, electronics, executor, ids, called
):
    seen = []

    async def details(ctx, deps, pid, *, lead=None):
        seen.append(("details", [pid]))
        ctx.set_reply("detalii", cacheable=False)

    async def comparison(ctx, deps, pids):
        seen.append(("comparison", list(pids)))
        ctx.set_reply("comparatie", cacheable=False)
        return True

    monkeypatch.setattr(kx.det, "serve_details", details)
    monkeypatch.setattr(kx.det, "serve_comparison", comparison)
    ctx = _ctx(electronics)
    planned = _planned(_plan(executor=executor, product_ids=ids))
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert seen == [(called, list(ids))]


@pytest.mark.parametrize("executor", ["compare", "detail"])
async def test_a_comparison_without_a_verdict_right_stays_dark_until_c2(
    monkeypatch, electronics, executor
):
    """Recenzia, constatarea 3 (I12): politica interzice verdictul, iar compunerea de azi l-ar
    scrie. Turul rămâne pe v1 fără ca vreun handler să ruleze; cu verdictul permis, servește."""
    from src.conversation.interpretation import AnswerPolicy

    ran = []

    async def comparison(ctx, deps, pids):
        ran.append(list(pids))
        ctx.set_reply("comparatie", cacheable=False)
        return True

    monkeypatch.setattr(kx.det, "serve_comparison", comparison)
    planned = _planned(_plan(executor=executor, product_ids=("p1", "p2")))
    blocked = AnswerPolicy(verdict_allowed=False, missing=["screen"])
    allowed = AnswerPolicy(verdict_allowed=True, missing=[])
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome(), blocked) is None
    assert ran == [] and ctx.reply is None
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome(), allowed) is True
    assert ran == [["p1", "p2"]]


async def test_compare_on_a_single_target_stays_dark_until_c2(electronics):
    planned = _planned(_plan(executor="compare", product_ids=("p1",)))
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is None


@pytest.mark.parametrize("executor", ["cart", "bundle", "delegate", "faq", "order", "reply_only"])
async def test_unbound_executors_stay_dark(electronics, executor):
    """`reply_only` inclus (recenzia, constatarea 1): vine din `chitchat` sau dintr-o MUTAȚIE
    refuzată de poartă fără întrebare (coș pe un produs epuizat), unde o frază generică ar înlocui
    răspunsul mutației. Rămâne pe calea v1 până la PR D."""
    planned = _planned(_plan(executor=executor, product_ids=()))
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is None


async def test_multi_act_stays_dark(electronics):
    planned = _planned(
        _plan(executor="detail", product_ids=("p1",)),
        _plan(executor="link", product_ids=("p1",)),
    )
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is None


async def test_ask_puts_the_gate_question_with_the_candidates(electronics):
    ctx = _ctx(electronics)
    ids = tuple(list(electronics.items)[:2])
    planned = _planned(_plan(executor="ask", product_ids=ids))
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome("Care?")) is True
    assert ctx.reply.text == "Care?" and ctx.reply.pending_question
    # recenzia, constatarea 2: carduri, nu rânduri de catalog (identitate + nume scurt, NX-301)
    from src.catalog.render_text import display_name

    assert [p["product_id"] for p in ctx.reply.products] == list(ids)
    assert [p["name"] for p in ctx.reply.products] == [
        display_name(electronics.items[i]["name"]) for i in ids
    ]
    assert all("id" not in p and "attributes" not in p for p in ctx.reply.products)


async def test_ask_without_a_question_refuses(electronics):
    planned = _planned(_plan(executor="ask", product_ids=()))
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is False


async def test_no_results_fails_closed_without_the_sentence(monkeypatch, electronics):
    _stub_search(monkeypatch, electronics, ())
    planned = _planned(_plan(executor="search", search_args=_args()))
    with pytest.raises(kx.NoSentence):
        await kx.execute_read_plans(
            _ctx(_with_sentences(electronics, {})), _deps(), planned, _outcome()
        )


async def test_page_serves_the_next_page_of_the_session(monkeypatch, electronics):
    from src.tools import catalog_tools

    ids = list(electronics.items)[:3]
    seen = []

    async def next_page(ctx, deps, sess, limit):
        seen.append((dict(sess), limit))
        return ToolResult(ok=True, products=sh.product_rows(electronics, ids))

    monkeypatch.setattr(catalog_tools, "continue_search_session", next_page)
    ctx = _ctx(electronics, "mai arata-mi")
    ctx.state.active_search = {"fp": "fx", "pool": ids, "cursor": 0, "page": 0}
    planned = _planned(_plan(executor="page"))
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert seen == [({"fp": "fx", "pool": ids, "cursor": 0, "page": 0}, 6)]
    served = (
        [i.product_id for i in ctx.reply.rich.items]
        if ctx.reply.rich is not None
        else [p.get("id") or p.get("product_id") for p in ctx.reply.products or []]
    )
    assert served and set(served) <= set(ids)


async def test_page_on_an_exhausted_pool_refuses(monkeypatch, electronics):
    ctx = _ctx(electronics, "mai arata-mi")
    ctx.state.active_search = {"fp": "fx", "pool": [], "cursor": 0, "page": 1}
    planned = _planned(_plan(executor="page"))
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is False


async def test_page_without_a_session_refuses(electronics):
    planned = _planned(_plan(executor="page", product_ids=()))
    assert await kx.execute_read_plans(_ctx(electronics), _deps(), planned, _outcome()) is False


# --- 3. dezvăluirile ------------------------------------------------------------------------------


async def test_disclosures_prefix_the_reply_once_and_missing_ones_are_counted(
    monkeypatch, electronics
):
    async def details(ctx, deps, pid, *, lead=None):
        ctx.set_reply("detalii", cacheable=True)

    monkeypatch.setattr(kx.det, "serve_details", details)
    lead = kx.kernel_sentence(electronics.pack, "ro", "not_exact_match")
    cat = _with_sentences(electronics, {"ro": {"not_exact_match": lead}})
    ctx = _ctx(cat)
    planned = _planned(
        _plan(executor="detail", product_ids=("p1",)),
        disclosures=((0, "not_exact_match"), (0, "not_exact_match"), (0, "dropped_act")),
    )
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    assert ctx.reply.text == f"{lead}\n\ndetalii"
    assert ctx.reply.text.count(lead) == 1
    assert ctx.reply.cacheable is False
    assert {"code": "dropped_act", "turn_id": "t0"} in _events(ctx, "kernel_sentence_missing")


async def test_the_disclosure_reaches_the_comparison_the_widget_shows(monkeypatch, electronics):
    """Recenzia, constatarea 4: pe o comparație widgetul citește `comparison.intro`, nu
    `reply.text`, deci fraza trebuie pusă și acolo."""
    from src.models import Comparison

    async def comparison(ctx, deps, pids):
        ctx.set_reply("comparatie", cacheable=False)
        ctx.reply.comparison = Comparison(columns=[], rows=[], intro="Lead.")
        return True

    monkeypatch.setattr(kx.det, "serve_comparison", comparison)
    ctx = _ctx(electronics)
    planned = _planned(
        _plan(executor="compare", product_ids=("p1", "p2")), disclosures=((0, "invalid_target"),)
    )
    assert await kx.execute_read_plans(ctx, _deps(), planned, _outcome()) is True
    lead = kx.kernel_sentence(electronics.pack, "ro", "invalid_target")
    assert ctx.reply.comparison.intro == f"{lead}\n\nLead."
    assert ctx.reply.text.startswith(lead)


# --- 4. frazele pachetului ------------------------------------------------------------------------


def test_kernel_sentences_cover_every_planner_disclosure():
    from src.agent.turn_planner import DISCLOSURES

    assert set(DISCLOSURES) <= KERNEL_SENTENCE_CODES
    for name in (*replay.FIXTURE_PACKS, "sole-ro"):
        pack = sh.catalog(name).pack
        for code in KERNEL_SENTENCE_CODES:
            assert kx.kernel_sentence(pack, "ro", code), (name, code)


def test_the_loader_rejects_unknown_codes_and_markers():
    out = _norm_kernel_sentences(
        {
            "ro": {
                "no_results": "  Nimic.  ",
                "made_up": "x",
                "no_target": "Caut {query}",
                "dropped_act": 3,
            },
            "en": "nope",
        }
    )
    assert out == {"ro": {"no_results": "Nimic."}}


def test_kernel_sentences_follow_the_voice_rules():
    from src.agent.voice import naturalize

    for name in (*replay.FIXTURE_PACKS, "sole-ro"):
        pack = sh.catalog(name).pack
        for per_code in pack.kernel_sentences.values():
            for phrase in per_code.values():
                assert naturalize(phrase) == phrase, phrase
                assert ";" not in phrase


# --- 5. build_plan(kernel=True) -------------------------------------------------------------------


@pytest.mark.parametrize("cheaper", [True, False])
async def test_build_plan_in_kernel_mode_does_not_re_derive_the_intent(
    monkeypatch, electronics, cheaper
):
    """Pe un tur fără produse, cu ecran și cu «mai ieftin» (sau fără), calea de azi re-deduce
    intenția (cross-sell, chip de set, «mai ieftin», R3); `kernel=True` nu rulează niciuna, iar
    `ctx.retrieval` rămâne setul executorului (gol)."""
    from src.agent import planner
    from src.agent.tool_executor import ToolRun
    from src.models import ProductRef, RouteDecision

    calls: list[str] = []

    async def cross_sell(*a, **kw):
        calls.append("cross_sell")
        return False

    async def chip_set(*a, **kw):
        calls.append("chip_set")
        return []

    async def cheaper_followup(*a, **kw):
        calls.append("cheaper")
        return NS(handled=False, products=[])

    async def rehydrate(*a, **kw):
        calls.append("rehydrate")
        return []

    monkeypatch.setattr(planner, "maybe_cross_sell", cross_sell)
    monkeypatch.setattr(planner, "resolve_chip_set", chip_set)
    monkeypatch.setattr(planner, "resolve_cheaper_followup", cheaper_followup)
    monkeypatch.setattr(planner, "rehydrate_displayed", rehydrate)
    monkeypatch.setattr(planner, "cheaper_followup_detected", lambda ctx, q: cheaper)

    async def plan(kernel: bool) -> list[str]:
        calls.clear()
        ctx = _ctx(electronics, "ceva mai ieftin")
        ctx.route = RouteDecision(route="sales")
        ctx.state.displayed_products = [
            ProductRef(product_id=p, name=electronics.items[p]["name"], price=1.0)
            for p in list(electronics.items)[:2]
        ]
        deps = _deps()
        await planner.build_plan(
            ctx,
            deps,
            ToolRun(ctx, deps),
            NS(),
            final="",
            retrieved=[],
            is_order=False,
            show_more=False,
            query="ceva mai ieftin",
            history="",
            tool_names=[],
            kernel=kernel,
        )
        assert ctx.retrieval is not None
        if kernel:
            assert ctx.retrieval.products == []
        return list(calls)

    today = await plan(False)
    assert "cross_sell" in today and "chip_set" in today
    assert ("cheaper" if cheaper else "rehydrate") in today
    assert await plan(True) == []


async def test_the_production_seam_serves_reads_and_keeps_the_rest_dark(electronics):
    planned = _planned(_plan(executor="cart", product_ids=("p1",)))
    assert await it.execute_plans(_ctx(electronics), _deps(), planned, _outcome()) is None
