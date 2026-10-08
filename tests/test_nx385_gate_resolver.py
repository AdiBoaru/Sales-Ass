"""NX-385 — ținta la resolver și la poartă: turele reale din `wide-2026-10-07`, prin `agent_stage`.

Setul de producție `wide-2026-10-07` (release `302820b`) are ture în care interpretarea era corectă,
iar resolverul sau poarta au stricat-o. Aici turele trec prin stagiul REAL (`agent_stage`, ramura
kernelului, executorii de citire de producție), cu interpretarea turului real, pe catalogul de
fixture (`tests/kernel/stage_harness.py`):

- `w2_autobronzant_incepator_manusa#4`: focusul vechi (produsul de la turul 2) bătea ecranul (două
  carduri noi), deci «cât costă una» primea fișa altui produs;
- `w5_schimba_subiect_revine#4`: pe ecranul unei comparații (ținta și partenerul, puse de turul de
  dinainte) «pe aia o vreau în coș» punea în coș ținta, fără întrebare;
- `w1_masca_noapte_utilizare#3`: aceeași întrebare „la care te referi" de două ori la rând.

Zero model real, zero DB."""

from __future__ import annotations

import pytest

from src.conversation.ambiguity_gate import target_question_key
from src.conversation.interpretation import Act, Reference, TurnInterpretation
from src.conversation.state_v2 import ConversationStateV2, DisplayedRef, References
from tests.kernel import fixture_catalog as fc
from tests.kernel import stage_harness as sh


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


def _state(cat, shown, *, focus) -> ConversationStateV2:
    items = cat.items
    return ConversationStateV2(
        revision=3,
        references=References(
            displayed_products=tuple(
                DisplayedRef(pid, items[pid]["name"], float(items[pid]["price"])) for pid in shown
            ),
            displayed_revision=3,
            selected_product=focus,
        ),
    )


def _deictic(kind: str) -> TurnInterpretation:
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind=kind, targets=["r1"], query=None)],
        changes=[],
        references=[
            Reference(
                id="r1",
                text="una",
                kind="deictic",
                ordinal=None,
                name=None,
                dimension=None,
                value=None,
                direction=None,
            )
        ],
        ambiguities=[],
        corrects_previous_turn=False,
    )


def _kernel(ctx) -> dict:
    trace = ctx.trace.get("kernel")
    assert trace is not None, ctx.trace.get("kernel_fallback")
    return trace


async def test_an_old_focus_does_not_answer_for_the_cards_on_screen(monkeypatch, electronics):
    """`w2_autobronzant_incepator_manusa#4`: «cât costă una» cu două carduri pe ecran și focusul pe
    un produs arătat cu două ture în urmă. Pe `main` planul era `detail` pe focus; acum răspunsul e
    despre cele două carduri (`act_both`)."""
    state = _state(electronics, ("el-01", "el-02"), focus="el-05")
    ctx = sh.build_ctx(electronics, state, "cat costa una", ("primul", "al doilea"), turn_id="t3")
    run = await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(_deictic("detail")))
    trace = _kernel(run.ctx)
    [resolved] = trace["resolved_refs"]
    assert (resolved["outcome"], resolved["product_ids"]) == ("ambiguous", ["el-01", "el-02"])
    assert trace["ambiguity"]["verdict"] == "act_both"
    assert [p["product_ids"] for p in trace["plans"]] == [["el-01", "el-02"]]


async def test_a_cart_on_this_one_over_a_fresh_comparison_asks_which(monkeypatch, electronics):
    """`w5_schimba_subiect_revine#4`: turul de dinainte a pus pe ecran ținta comparației (focusul)
    și partenerul. «Pe aia o vreau în coș» punea în coș ținta. Acum coșul întreabă între cele două
    și nu scrie nimic (I10)."""
    state = _state(electronics, ("el-01", "el-02"), focus="el-01")
    ctx = sh.build_ctx(electronics, state, "pe aia o vreau in cos", ("compara-le",), turn_id="t5")
    run = await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(_deictic("cart")))
    trace = _kernel(run.ctx)
    assert trace["ambiguity"]["verdict"] == "must_ask"
    assert [p["executor"] for p in trace["plans"]] == ["ask"]
    question = run.ctx.reply.text
    for pid in ("el-01", "el-02"):
        assert electronics.items[pid]["name"] in question
    assert not [e for e in run.ctx.events if e.type == "cart_add"]


def test_the_same_target_question_is_asked_once_over_two_turns():
    """`w1_masca_noapte_utilizare#2-3`: cinci produse, «se clătește dimineața?» ⇒ întrebarea; «și
    cât de des o pot pune?» ⇒ ACEEAȘI întrebare. Turul de după închide întrebarea (ca
    orchestratorul, `answer_pending`), iar a doua oară poarta răspunde despre toți (`act_both`)."""
    five = ("el-01", "el-02", "el-03", "el-04", "el-05")
    cat = fc.products("electronics")
    state = ConversationStateV2(
        revision=1,
        references=References(
            displayed_products=tuple(
                DisplayedRef(p, cat[p]["name"], float(cat[p]["price"])) for p in five
            ),
            displayed_revision=1,
        ),
    )
    first = fc.kernel_step(
        "electronics", state, _deictic("detail"), "se clateste?", turn_id="t1", answer_pending=True
    )
    assert first.outcome.decision.verdict == "must_ask"
    assert first.outcome.asked_key == target_question_key(five)
    second = fc.kernel_step(
        "electronics",
        first.state_after,
        _deictic("detail"),
        "si cat de des o pot pune?",
        earlier=("se clateste?",),
        turn_id="t2",
        answer_pending=True,
    )
    assert (second.outcome.decision.verdict, second.outcome.decision.reason) == (
        "act_both",
        "already_asked",
    )
    assert second.outcome.decision.question is None


def _act(kind: str, refs=(), query: str | None = None) -> TurnInterpretation:
    return TurnInterpretation(
        thread="continue",
        acts=[Act(kind=kind, targets=[r.id for r in refs], query=query)],
        changes=[],
        references=list(refs),
        ambiguities=[],
        corrects_previous_turn=False,
    )


def _ref(kind: str, **kw) -> Reference:
    base = {"ordinal": None, "name": None, "dimension": None, "value": None, "direction": None}
    return Reference(id="r1", text="x", kind=kind, **{**base, **kw})


def test_a_focus_from_before_a_list_never_puts_one_of_its_cards_in_the_cart():
    """Recenzia NX-385 (I10): detaliu pe un produs (focus), o căutare nouă care îl re-arată printre
    altele, un tur fără carduri («cât costă livrarea»), apoi «pe asta o vreau în coș». O primă
    variantă a regulii (revizia ecranului) lăsa focusul să decidă, iar coșul rula pe el."""
    cat = fc.products("electronics")
    start = ConversationStateV2(
        revision=1,
        references=References(
            displayed_products=tuple(
                DisplayedRef(p, cat[p]["name"], float(cat[p]["price"])) for p in ("el-05", "el-06")
            ),
            displayed_revision=1,
        ),
    )
    t1 = fc.kernel_step(
        "electronics",
        start,
        _act("detail", [_ref("ordinal", ordinal=1)]),
        "primul",
        turn_id="t1",
        shown_ids=("el-05",),
    )
    assert t1.state_after.references.selected_product == "el-05"
    t2 = fc.kernel_step(
        "electronics",
        t1.state_after,
        _act("find", query="telefoane"),
        "arata-mi telefoane",
        turn_id="t2",
        shown_ids=("el-01", "el-05", "el-02"),
    )
    t3 = fc.kernel_step(
        "electronics", t2.state_after, _act("store_info", query="livrare"), "livrare", turn_id="t3"
    )
    t4 = fc.kernel_step(
        "electronics",
        t3.state_after,
        _act("cart", [_ref("deictic")]),
        "pe asta o vreau in cos",
        turn_id="t4",
    )
    [resolved] = t4.resolved
    assert resolved.outcome == "ambiguous"
    assert t4.outcome.decision.verdict == "must_ask"
    assert all(p.executor != "cart" for p in t4.planned.plans)
