"""NX-336 PR D (felia D1) — bucla restrânsă: `delegate`, `faq`, `order`.

Ce dovedește suita:

- fiecare executor oferă modelului DOAR uneltele lui ∩ uneltele tenantului (`DELEGATE_TOOLS` pe
  `delegate`, `faq_lookup` pe `faq`, `check_order` pe `order`), niciodată o citire de catalog sau o
  mutație;
- o unealtă chemată din afara allowlist-ului NU ajunge la `run_tool`: refuz structurat, numărat;
- răspunsul e proza buclei, compusă de calea v1 (`build_plan(kernel=True)` + `render`);
- fără nicio unealtă permisă pe tenant, executorul rămâne `dark`;
- prin `agent_stage` REAL, journey-ul n11 (livrare, comandă, altceva) e servit de kernel.

Zero model real, zero DB (stub-urile din `tests/kernel/stage_harness.py`)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.turn_planner import DELEGATE_TOOLS, PlannedTurn
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import AmbiguityDecision, TurnPlan
from src.conversation.state_v2 import ConversationStateV2
from src.tools import base as tools_base
from src.tools.base import ToolResult
from tests.kernel import replay
from tests.kernel import stage_harness as sh

JOURNEYS = {j.journey_id: j for j in replay.load_journeys()}
N11 = "n11-electronics-acts-without-targets"


def _events(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


def _planned(executor: str) -> PlannedTurn:
    plan = TurnPlan(executor=executor, product_ids=(), search_args=None, depends_on=None)
    return PlannedTurn(plans=(plan,), primary=0)


def _outcome() -> GateOutcome:
    return GateOutcome(decision=AmbiguityDecision(verdict="act", reason="none", question=None))


class LoopLLM(sh.StageLLM):
    """Bucla scriptată care reține schemele oferite și ce a întors fiecare unealtă."""

    def __init__(self, calls=(), final="Răspunsul modelului."):
        super().__init__()
        self.fx = {"final": final, "tool_calls": list(calls)}
        self.offered: list[list[str]] = []
        self.results: list[str] = []

    async def run_tool_loop(self, system, user, tools, execute, **kw):
        self.offered.append([t["function"]["name"] for t in tools])
        for name, args in self.fx["tool_calls"]:
            self.results.append(await execute(name, args))
        return self.fx["final"]


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


@pytest.fixture
def tool_calls(monkeypatch):
    """`run_tool` înregistrat: ce unealtă a ajuns chiar să ruleze."""
    ran: list[str] = []

    async def run_tool(ctx, deps, name, args):
        ran.append(name)
        return ToolResult(ok=True, llm_view="ok")

    monkeypatch.setattr(tools_base, "run_tool", run_tool)
    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    return ran


def _ctx(cat, text="x"):
    return sh.build_ctx(cat, ConversationStateV2(), text)


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


@pytest.mark.parametrize(
    ("executor", "expected"),
    [
        ("delegate", ["faq_lookup", "check_order"]),
        ("faq", ["faq_lookup"]),
        # FAQ întâi, ca setul v1 de comandă (recenzia D1)
        ("order", ["faq_lookup", "check_order"]),
    ],
)
async def test_each_executor_offers_only_its_tools(electronics, executor, expected):
    llm = LoopLLM()
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned(executor), _outcome()) is True
    [offered] = llm.offered
    assert offered == expected, "ordinea și mulțimea uneltelor tenantului, restrânse"
    assert set(offered) <= DELEGATE_TOOLS


async def test_a_tool_outside_the_allowlist_never_runs(electronics, tool_calls):
    """`run_tool` execută orice unealtă înregistrată; allowlist-ul o oprește ÎNAINTE."""
    llm = LoopLLM(calls=[("cart_add", {"product_id": "el-01"}), ("faq_lookup", {"query": "x"})])
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome()) is True
    assert tool_calls == ["faq_lookup"]
    assert json.loads(llm.results[0]) == {"ok": False, "error": "tool_not_allowed"}
    assert {"name": "cart_add", "turn_id": "t0"} in _events(ctx, "delegate_tool_refused")


async def test_an_invented_tool_name_is_counted_without_its_text(electronics, tool_calls):
    llm = LoopLLM(calls=[("drop_tables", {})])
    ctx = _ctx(electronics)
    await kx.execute_read_plans(ctx, _deps(llm), _planned("delegate"), _outcome())
    assert tool_calls == []
    assert {"name": "unknown", "turn_id": "t0"} in _events(ctx, "delegate_tool_refused")


async def test_the_reply_is_the_loop_prose(electronics):
    # fără cifre și fără afirmații de livrare nesusținute: validatorul căii v1 le respinge, ca azi
    llm = LoopLLM(final="Salut, cu ce te pot ajuta?")
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome()) is True
    assert ctx.reply.text == "Salut, cu ce te pot ajuta?"


async def test_no_allowed_tool_on_the_tenant_stays_dark(monkeypatch, electronics):
    from src.worker.stages import agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "tool_loop_tools", lambda business, route, **kw: (["search_products"], [])
    )
    llm = LoopLLM()
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("order"), _outcome()) is None
    assert llm.offered == []


@pytest.mark.parametrize("index", [0, 1, 3])
async def test_the_n11_turns_are_served_through_the_real_stage(monkeypatch, electronics, index):
    """Livrarea, comanda și „altceva" din n11, prin `agent_stage` REAL: kernelul servește turul pe
    bucla restrânsă, iar executorul din trace e cel al planului."""
    step = next(ct for ct in sh.chain(JOURNEYS[N11], electronics) if ct.index == index)
    ctx = sh.build_ctx(
        electronics, step.state_before, step.turn.user_input, step.previous, turn_id=f"t{index}"
    )
    llm = sh.StageLLM(step.turn.expect["interpretation"])
    llm.fx["final"] = "Răspuns."
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert run.branch_result is True, _events(ctx, "kernel_turn")
    [turn] = _events(ctx, "kernel_turn")
    assert turn["executor"] in kx.DELEGATED_TOOLS and turn["served"] is True


async def test_a_failed_loop_serves_the_v1_fallback_without_a_second_loop(monkeypatch, electronics):
    """Recenzia D1: o eroare de furnizor în bucla restrânsă NU cade pe v1 (care ar rula bucla a
    doua oară, exact când furnizorul e bolnav): kernelul servește fallback-ul căii v1."""
    step = next(ct for ct in sh.chain(JOURNEYS[N11], electronics) if ct.index == 0)
    ctx = sh.build_ctx(electronics, step.state_before, step.turn.user_input, step.previous)
    llm = sh.StageLLM(step.turn.expect["interpretation"])
    loops = []

    async def broken(system, user, tools, execute, **kw):
        loops.append(1)
        raise TimeoutError("furnizor")

    monkeypatch.setattr(llm, "run_tool_loop", broken)
    run = await sh.run_turn(monkeypatch, electronics, ctx, llm)
    assert loops == [1], "o singură buclă pe tur"
    assert run.branch_result is True and ctx.reply is not None
    assert {"error": "TimeoutError", "turn_id": "t0"} in _events(ctx, "delegate_loop_failed")


async def test_an_order_view_is_answered_by_the_order_branch(monkeypatch, electronics):
    """Recenzia D1: o vedere de comandă adusă de `check_order` trece prin ramura ORDER a lui
    `render` (vederea grounded), nu prin validatorul de proză de vânzare, care respingea o sumă
    sau un AWB reale."""
    view = "Comanda 1234: livrata, 129 lei, AWB 5566778899."

    async def run_tool(ctx, deps, name, args):
        return ToolResult(ok=True, llm_view=view, prices=[129.0])

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    llm = LoopLLM(
        calls=[("check_order", {"order_number": "1234"})],
        final="Comanda ta 1234, de 129 lei, a fost livrata. AWB 5566778899.",
    )
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _planned("order"), _outcome()) is True
    assert "n-am găsit produse" not in ctx.reply.text
    assert "1234" in ctx.reply.text


async def test_the_delegate_turn_reports_its_prompt_and_validator(electronics):
    llm = LoopLLM(final="Salut, cu ce te pot ajuta?")
    ctx = _ctx(electronics)
    await kx.execute_read_plans(ctx, _deps(llm), _planned("faq"), _outcome())
    [event] = _events(ctx, "agent_prompt")
    assert event["validator_ok"] is True, "verdictul validatorului ajunge în trace (P10)"


def test_the_refusal_counter_survives_a_fallen_turn():
    assert "delegate_tool_refused" in kx.KERNEL_EXECUTOR_EVENTS
    assert "delegate_loop_failed" in kx.KERNEL_EXECUTOR_EVENTS


# --- D2: coșul și planurile multiple --------------------------------------------------------------


def _plan(executor, ids=(), depends_on=None, search_args=None):
    return TurnPlan(
        executor=executor, product_ids=list(ids), search_args=search_args, depends_on=depends_on
    )


def _gate(verdict="act", reason="none"):
    return GateOutcome(decision=AmbiguityDecision(verdict=verdict, reason=reason, question=None))


@pytest.fixture
def cart_tool(monkeypatch, electronics):
    """`run_tool` pentru `cart_add` (rândul produsului din catalogul de fixture) și căutare; `fail`
    alege id-urile pe care coșul le refuză. Reține ordinea apelurilor."""
    state = NS(calls=[], fail=set())

    async def run_tool(ctx, deps, name, args):
        state.calls.append((name, dict(args)))
        if name == "cart_add":
            pid = args["product_id"]
            if pid in state.fail:
                return ToolResult(ok=False, error="insufficient_stock", llm_view="stoc")
            [row] = sh.product_rows(electronics, [pid])
            return ToolResult(ok=True, products=[row], prices=[row["price"]], llm_view="ok")
        return ToolResult(ok=True, llm_view="ok")

    async def no_cross_sell(*a, **kw):
        state.calls.append(("cross_sell", {}))
        return False

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", no_cross_sell)
    return state


def _added(cat, pid):
    from src.catalog.render_text import display_name

    template = kx.kernel_sentence(cat.pack, "ro", "cart_added")
    return template.replace("{product}", display_name(cat.items[pid]["name"]))


async def test_cart_adds_the_plan_ids_and_says_so(electronics, cart_tool):
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert [c for c in cart_tool.calls if c[0] == "cart_add"] == [
        ("cart_add", {"product_id": "el-01"})
    ]
    assert ctx.reply.text == _added(electronics, "el-01")
    assert [p["product_id"] for p in ctx.reply.products] == ["el-01"]
    assert ctx.retrieval.catalog_read is False, "un coș nu închide sesiunea de căutare (NX-326)"


async def test_a_failed_cart_says_so(electronics, cart_tool):
    cart_tool.fail = {"el-01"}
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert ctx.reply.text == kx.kernel_sentence(electronics.pack, "ro", "cart_failed")


async def test_the_cart_sentences_are_checked_before_any_write(electronics, cart_tool):
    """Fail-closed ÎNAINTE de scriere: după o mutație turul n-ar mai avea voie să cadă pe v1."""
    import dataclasses

    bare = dataclasses.replace(
        electronics, pack=dataclasses.replace(electronics.pack, kernel_sentences={})
    )
    planned = PlannedTurn(plans=(_plan("cart", ["el-01"]),), primary=0)
    with pytest.raises(kx.NoSentence):
        await kx.execute_read_plans(_ctx(bare), _deps(LoopLLM()), planned, _outcome())
    assert cart_tool.calls == []


async def test_cross_sell_answers_when_the_add_has_complements(monkeypatch, electronics, cart_tool):
    seen = []

    async def cross_sell(ctx, deps, *, run, **kw):
        seen.append(run.added_product["id"])
        ctx.set_reply("complementare", cacheable=False)
        return True

    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", cross_sell)
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    # confirmarea din codul v1 devine fraza pachetului, prima (recenzia D2)
    assert seen == ["el-01"]
    assert ctx.reply.text == f"{_added(electronics, 'el-01')}\n\ncomplementare"


async def test_a_partial_failure_skips_cross_sell_and_says_both(
    monkeypatch, electronics, cart_tool
):
    """Recenzia D2: cu un produs picat, cross-sell-ul (care ar spune doar „adăugat") nu rulează."""
    cart_tool.fail = {"el-02"}
    ran = []

    async def cross_sell(*a, **kw):
        ran.append(1)
        return True

    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", cross_sell)
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01", "el-02"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    failed = kx.kernel_sentence(electronics.pack, "ro", "cart_failed")
    assert ran == [] and ctx.reply.text == f"{_added(electronics, 'el-01')} {failed}"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("mutation_unavailable", "mutation_unavailable"),
        ("mutation_not_exact", "mutation_not_exact"),
        # recenzia D2: orice alt refuz fără întrebare al unei mutații tot nu cade pe v1 (o buclă
        # cu `cart_add` pe exact turul oprit de poartă)
        ("no_options", "mutation_not_exact"),
        ("no_template", "mutation_not_exact"),
        ("already_asked", "mutation_not_exact"),
    ],
)
async def test_a_refused_mutation_gets_its_pack_sentence(electronics, reason, code):
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("reply_only"),), primary=0)
    outcome = _gate("must_ask", reason)
    deps = _deps(LoopLLM())
    assert await kx.execute_read_plans(ctx, deps, planned, outcome, None, True) is True
    assert ctx.reply.text == kx.kernel_sentence(electronics.pack, "ro", code)


async def test_other_reply_only_stays_dark(electronics):
    """Fără un act care scrie (`chitchat`), `reply_only` rămâne pe v1."""
    planned = PlannedTurn(plans=(_plan("reply_only"),), primary=0)
    deps = _deps(LoopLLM())
    outcome = _gate("must_ask", "no_subject")
    assert await kx.execute_read_plans(_ctx(electronics), deps, planned, outcome) is None
    assert (
        await kx.execute_read_plans(_ctx(electronics), deps, planned, _gate(), None, True) is None
    )


def _search_stub(monkeypatch, cat, ids, *, raises=None):
    from src.tools import catalog_tools

    async def search(ctx, deps, args):
        if raises is not None:
            raise raises
        return ToolResult(ok=True, products=sh.product_rows(cat, list(ids)))

    monkeypatch.setattr(catalog_tools, "run_planned_search", search)


def _args():
    from src.tools.catalog_tools import SearchArgs

    return SearchArgs(query="husa")


@pytest.mark.parametrize("depends", [None, 0])
async def test_a_failed_cart_is_said_first_and_a_dependent_plan_is_skipped(
    monkeypatch, electronics, cart_tool, depends
):
    """§C.12: coșul picat ⇒ căutarea independentă rulează, eșecul e spus ÎNTÂI; una dependentă
    («o husă pentru EL») se sare."""
    cart_tool.fail = {"el-01"}
    _search_stub(monkeypatch, electronics, ["el-02"])
    ctx = _ctx(electronics)
    planned = PlannedTurn(
        plans=(
            _plan("cart", ["el-01"]),
            _plan("search", depends_on=depends, search_args=_args()),
        ),
        primary=1,
    )
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    failed = kx.kernel_sentence(electronics.pack, "ro", "cart_failed")
    assert ctx.reply.text.startswith(failed)
    if depends == 0:
        assert ctx.reply.text == failed, "planul dependent de coșul picat nu rulează"


async def test_after_a_successful_cart_a_failed_second_plan_never_falls_back(
    monkeypatch, electronics, cart_tool
):
    """După o mutație reușită nimic nu mai cade pe v1 (ar dubla mutația)."""
    _search_stub(monkeypatch, electronics, [], raises=RuntimeError("db"))
    ctx = _ctx(electronics)
    planned = PlannedTurn(
        plans=(_plan("cart", ["el-01"]), _plan("search", search_args=_args())), primary=1
    )
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert ctx.reply.text == _added(electronics, "el-01")
    assert {"error": "RuntimeError", "turn_id": "t0"} in _events(ctx, "kernel_second_plan_failed")


async def test_a_failed_cart_lets_a_crashing_second_plan_fall_back(
    monkeypatch, electronics, cart_tool
):
    """O mutație picată n-a scris nimic, deci calea v1 poate relua turul."""
    cart_tool.fail = {"el-01"}
    _search_stub(monkeypatch, electronics, [], raises=RuntimeError("db"))
    planned = PlannedTurn(
        plans=(_plan("cart", ["el-01"]), _plan("search", search_args=_args())), primary=1
    )
    with pytest.raises(RuntimeError):
        await kx.execute_read_plans(_ctx(electronics), _deps(LoopLLM()), planned, _outcome())


async def test_two_plans_without_a_mutation_stay_dark(electronics):
    planned = PlannedTurn(plans=(_plan("detail", ["el-01"]), _plan("link", ["el-01"])), primary=1)
    assert (
        await kx.execute_read_plans(_ctx(electronics), _deps(LoopLLM()), planned, _outcome())
        is None
    )


def test_a_mutation_on_a_non_exact_target_is_blocked():
    """I10 la rulare: un id de mutație care nu vine dintr-o referință `exact` ține turul `dark`."""
    from src.agent.interpreted_turn import _mutations_exact
    from src.conversation.interpretation import ResolvedRef

    def ref(outcome, ids):
        return ResolvedRef(
            ref_id="r1",
            kind="ordinal",
            outcome=outcome,
            product_ids=ids,
            source="shown_now",
            reason=None,
        )

    def chain(resolved, ids):
        return NS(resolved=resolved, planned=PlannedTurn(plans=(_plan("cart", ids),), primary=0))

    assert _mutations_exact(chain((ref("exact", ["a"]),), ["a"]))
    assert not _mutations_exact(chain((ref("ambiguous", ["a", "b"]),), ["a"]))
    assert not _mutations_exact(chain((ref("exact", ["a"]),), ["a", "z"]))
    assert "kernel_i10_blocked" in kx.KERNEL_EXECUTOR_EVENTS


async def test_the_real_exact_cart_turn_is_served(monkeypatch, electronics, cart_tool):
    """g01 prin `agent_stage` REAL: «Adaugă-l pe primul în coș.» pe o țintă `exact`."""
    journey = JOURNEYS["g01-electronics-cart-on-an-exact-target"]
    step = next(ct for ct in sh.chain(journey, electronics) if ct.index == 1)
    ctx = sh.build_ctx(
        electronics, step.state_before, step.turn.user_input, step.previous, turn_id="t1"
    )
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(step.turn.expect["interpretation"])
    )
    assert run.branch_result is True, _events(ctx, "kernel_turn")
    adds = [args["product_id"] for name, args in cart_tool.calls if name == "cart_add"]
    assert len(adds) == 1 and ctx.reply.text == _added(electronics, adds[0])


def _stage_step(cat, journey_id, index):
    step = next(ct for ct in sh.chain(JOURNEYS[journey_id], cat) if ct.index == index)
    ctx = sh.build_ctx(cat, step.state_before, step.turn.user_input, step.previous, turn_id="t1")
    return step, ctx


async def test_the_real_sold_out_cart_turn_says_so_without_writing(monkeypatch):
    """g03 prin stagiul REAL: coș pe o rochie epuizată ⇒ poarta refuză fără întrebare
    (`mutation_unavailable`), fraza pachetului, nicio scriere."""
    cat = sh.catalog("fashion")
    sh.install(monkeypatch, cat, executors=True)
    ran = []

    async def run_tool(ctx, deps, name, args):
        ran.append(name)
        return ToolResult(ok=True, llm_view="ok")

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    step, ctx = _stage_step(cat, "g03-fashion-cart-on-a-sold-out-dress", 1)
    run = await sh.run_turn(monkeypatch, cat, ctx, sh.StageLLM(step.turn.expect["interpretation"]))
    assert run.branch_result is True and ran == []
    assert ctx.reply.text == kx.kernel_sentence(cat.pack, "ro", "mutation_unavailable")


async def test_the_real_three_act_turn_adds_first_and_says_it_first(
    monkeypatch, electronics, cart_tool
):
    """n14 prin stagiul REAL: «Adaugă-l pe primul în coș, compară-l cu al doilea și arată-mi niște
    huse.» ⇒ două planuri (coșul, apoi căutarea), al treilea act dezvăluit. Clientul citește întâi
    ce s-a întâmplat cu coșul, apoi dezvăluirea, apoi rezultatele."""
    _search_stub(monkeypatch, electronics, ["el-02"])
    step, ctx = _stage_step(electronics, "n14-electronics-three-acts-keep-two", 1)
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(step.turn.expect["interpretation"])
    )
    assert run.branch_result is True
    [pid] = [args["product_id"] for name, args in cart_tool.calls if name == "cart_add"]
    dropped = kx.kernel_sentence(electronics.pack, "ro", "dropped_act")
    text = ctx.reply.text
    assert text.startswith(_added(electronics, pid))
    assert text.index(_added(electronics, pid)) < text.index(dropped)


async def test_the_real_ambiguous_cart_turn_asks_and_writes_nothing(
    monkeypatch, electronics, cart_tool
):
    """g02 prin stagiul REAL: «Adaugă Samsung-ul în coș.» cu două Samsung pe ecran ⇒ întrebarea
    porții (I10), nicio scriere."""
    step, ctx = _stage_step(electronics, "g02-electronics-ambiguous-cart-asks-once", 1)
    run = await sh.run_turn(
        monkeypatch, electronics, ctx, sh.StageLLM(step.turn.expect["interpretation"])
    )
    assert run.branch_result is True and ctx.reply.pending_question
    assert [c for c in cart_tool.calls if c[0] == "cart_add"] == []


# --- recenzia D2 ----------------------------------------------------------------------------------


async def test_two_legacy_adds_in_one_turn_keep_both(monkeypatch, electronics):
    """P1: `cart_add` pe calea legacy pornea de la coșul de la ÎNCĂRCARE, deci al doilea apel din
    același tur îl suprascria pe primul. Unealta REALĂ, două id-uri."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "conversation_cart_enabled", False)

    async def by_ids(conn, business_id, ids, **kw):
        return sh.product_rows(electronics, list(ids))

    async def no_cross_sell(*a, **kw):
        return False

    monkeypatch.setattr("src.tools.commerce_tools.get_products_by_ids", by_ids)
    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", no_cross_sell)
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01", "el-02"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert [line["product_id"] for line in ctx.state_patch["cart"]] == ["el-01", "el-02"]


async def test_an_idempotent_replay_without_products_counts_as_added(monkeypatch, electronics):
    """P2: `CartService` întoarce `ok` FĂRĂ produse pe un replay; produsul nu rămâne cel de la
    id-ul anterior, iar reușita nu devine „nu am putut"."""
    calls = []

    async def run_tool(ctx, deps, name, args):
        calls.append(args["product_id"])
        if len(calls) == 1:
            [row] = sh.product_rows(electronics, [args["product_id"]])
            return ToolResult(ok=True, products=[row], llm_view="ok")
        return ToolResult(ok=True, products=[], llm_view="ok")

    async def no_cross_sell(*a, **kw):
        return False

    monkeypatch.setattr("src.agent.tool_executor.run_tool", run_tool, raising=False)
    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", no_cross_sell)
    ctx = _ctx(electronics)
    planned = PlannedTurn(plans=(_plan("cart", ["el-01", "el-02"]),), primary=0)
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert ctx.reply.text == f"{_added(electronics, 'el-01')} {_added(electronics, 'el-02')}"


async def test_a_second_plan_that_does_not_serve_leaves_no_session(
    monkeypatch, electronics, cart_tool
):
    """P2: al doilea plan care nu servește nu lasă în stare sesiunea pe care a scris-o."""
    from src.tools import catalog_tools

    async def search(ctx, deps, args):
        ctx.state_patch["active_search"] = {"fp": "x", "pool": ["el-09"], "cursor": 0, "page": 0}
        raise RuntimeError("compunere")

    monkeypatch.setattr(catalog_tools, "run_planned_search", search)
    ctx = _ctx(electronics)
    planned = PlannedTurn(
        plans=(_plan("cart", ["el-01"]), _plan("search", search_args=_args())), primary=1
    )
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    assert "active_search" not in ctx.state_patch
    assert ctx.reply.text == _added(electronics, "el-01")


async def test_a_disclosure_after_a_rich_cart_reply_reaches_the_widget(
    monkeypatch, electronics, cart_tool
):
    """P2: pe web un răspuns bogat se citește din `rich`, deci dezvăluirea ajunge și acolo."""
    from src.models import RichReply

    async def cross_sell(ctx, deps, **kw):
        ctx.set_reply("x", cacheable=False)
        ctx.reply.rich = RichReply(
            intro="complementare", items=[], pick=None, education=None, chips=[], disclaimer=""
        )
        return True

    monkeypatch.setattr("src.agent.planner.maybe_cross_sell", cross_sell)
    ctx = _ctx(electronics)
    planned = PlannedTurn(
        plans=(_plan("cart", ["el-01"]),), primary=0, disclosures=((0, "invalid_target"),)
    )
    assert await kx.execute_read_plans(ctx, _deps(LoopLLM()), planned, _outcome()) is True
    disclosure = kx.kernel_sentence(electronics.pack, "ro", "invalid_target")
    intro = ctx.reply.rich.intro
    assert intro.startswith(_added(electronics, "el-01")) and intro.endswith(disclosure)


def test_a_turn_with_a_cart_act_is_a_mutating_turn():
    """P2: refuzul porții fără întrebare se judecă pe actele turului, nu pe motivul porții."""
    from src.agent.interpreted_turn import _mutating_turn

    def chain(kinds, skipped=()):
        return NS(
            interpreted=NS(interpretation=NS(acts=[NS(kind=k) for k in kinds])),
            outcome=GateOutcome(
                decision=AmbiguityDecision(verdict="must_ask", reason="x", question=None),
                skipped_acts=tuple(skipped),
            ),
        )

    assert _mutating_turn(chain(["cart"]))
    assert _mutating_turn(chain(["find", "cart"]))
    assert not _mutating_turn(chain(["chitchat"]))
    assert not _mutating_turn(chain(["cart", "find"], skipped=(0,)))
