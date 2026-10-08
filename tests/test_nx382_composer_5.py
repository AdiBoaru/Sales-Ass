"""NX-382 faza 5 — frazele fixe ale kernelului devin obligații ale compozitorului.

Contractul fazei: dezvăluirile planului (`PlannedTurn.disclosures`) ajung la compozitor ca
obligații, cu sensul lor; fraza pachetului (`kernel_sentences`) se pune doar pentru ce modelul n-a
acoperit SAU când textul lui n-a fost servit. „Nu e potrivirea exactă" nu se pune peste zero
carduri. Un refuz explicit (`set_fit=none`) se respectă și pe o pagină. Memoria NX-374 acceptă
dezvăluirea scrisă de model.

Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

from src.agent import composer as cp
from src.agent import kernel_executors as kx
from src.agent import prompt_builder
from src.agent.turn_planner import PlannedTurn
from src.conversation.interpretation import TurnPlan
from src.models import Reply, RichReply
from tests.test_interpreted_turn_c import _ctx, _outcome, electronics  # noqa: F401

PRODUCT = {
    "id": "p1",
    "name": "COSRX The Retinol 0.1 Cream",
    "price": 142.0,
    "availability": "in_stock",
    "attributes": {},
    "sections": [{"kind": "summary", "body": "Crema de noapte cu retinol."}],
}


def _reply(text, met=()):
    return {
        "text": text,
        "general_advice": "",
        "items": [],
        "suggestions": [],
        "obligations_met": list(met),
    }


class ComposeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, str, dict]] = []

    async def complete_schema(self, system, user, schema, **kw):
        self.calls.append((system, user, schema))
        return self.reply


def _deps(llm):
    from tests.kernel import stage_harness as sh

    return NS(db=sh.RecordingDb(), llm=llm)


# --- obligațiile ---------------------------------------------------------------------------------


def test_the_plan_disclosures_become_obligations_with_their_meaning():
    planned = PlannedTurn(
        plans=(TurnPlan(executor="detail", product_ids=["p1"], search_args=None, depends_on=None),),
        primary=0,
        disclosures=((0, "not_exact_match"), (0, "not_exact_match"), (0, "need_unverifiable")),
        disclosed_needs=("fragrance_free",),
    )
    told = kx._disclosures_of(planned)
    assert told == (("not_exact_match", {}), ("need_unverifiable", {"needs": ["fragrance_free"]}))
    ctx = NS(kernel_disclosures=told)
    obligations = cp.disclosure_obligations(ctx, cp.ComposeInput(task="detail"))
    assert [o.code for o in obligations] == ["not_exact_match", "need_unverifiable"]
    assert obligations[1].facts["needs"] == ["fragrance_free"]
    assert "not taken into account" in obligations[1].facts["meaning"]
    # pe „n-am găsit" și pe un salut nu se spune nicio dezvăluire
    assert cp.disclosure_obligations(ctx, cp.ComposeInput(task="no_results")) == ()
    assert cp.disclosure_obligations(ctx, cp.ComposeInput(task="chitchat")) == ()


def test_an_unknown_code_stays_on_the_pack_sentence():
    ctx = NS(kernel_disclosures=(("mutation_unavailable", {}),))
    assert cp.disclosure_obligations(ctx, cp.ComposeInput(task="detail")) == ()


def _served(text, composed, told=frozenset({"not_exact_match"}), products=None):
    return NS(
        reply=Reply(text=text, products=products),
        composed_reply=composed,
        disclosures_composed=told,
    )


def test_a_disclosure_counts_as_told_only_when_the_composed_text_was_served():
    assert cp.composed_disclosures(_served("Uite ce am.", "Uite ce am.")) == {"not_exact_match"}
    # textul compozitorului a fost înlocuit (refuz → mesajul de no-result, o rezervă)
    assert cp.composed_disclosures(_served("N-am găsit produse.", "Uite ce am.")) == frozenset()
    assert cp.composed_disclosures(_served("x", None)) == frozenset()


async def test_the_composer_says_the_disclosure_and_the_pack_sentence_is_not_added(
    monkeypatch,
    electronics,  # noqa: F811
):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "composer_detail_enabled", True)

    async def by_ids(conn, business_id, ids, limit=6):
        return [dict(PRODUCT)]

    monkeypatch.setattr(kx, "get_products_by_ids", by_ids)
    text = "N-am găsit exact crema pe care ai numit-o, dar asta e cea mai apropiată."
    llm = ComposeLLM(_reply(text, met=["not_exact_match"]))
    ctx = _ctx(electronics, "crema cosrx retinol")
    planned = PlannedTurn(
        plans=(TurnPlan(executor="detail", product_ids=["p1"], search_args=None, depends_on=None),),
        primary=0,
        disclosures=((0, "not_exact_match"),),
    )
    assert await kx.execute_read_plans(ctx, _deps(llm), planned, _outcome()) is True
    pack_sentence = kx.kernel_sentence(electronics.pack, "ro", "not_exact_match")
    assert ctx.reply.text.startswith(text) and pack_sentence not in ctx.reply.text
    assert '- not_exact_match {"meaning":' in llm.calls[0][1]


async def test_a_composer_that_skips_the_disclosure_falls_back(
    monkeypatch,
    electronics,  # noqa: F811
):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "composer_detail_enabled", True)

    async def by_ids(conn, business_id, ids, limit=6):
        return [dict(PRODUCT)]

    sheet = []

    async def details(ctx, deps, pid, *, lead=None, gated=None):
        sheet.append(pid)
        ctx.set_reply("FISA", products=[{"product_id": pid, "name": "X", "price": 1.0}])

    monkeypatch.setattr(kx, "get_products_by_ids", by_ids)
    monkeypatch.setattr(kx.det, "serve_details", details)
    llm = ComposeLLM(_reply("Asta e crema."))  # obligația nedeclarată ⇒ respins
    ctx = _ctx(electronics, "crema cosrx retinol")
    planned = PlannedTurn(
        plans=(TurnPlan(executor="detail", product_ids=["p1"], search_args=None, depends_on=None),),
        primary=0,
        disclosures=((0, "not_exact_match"),),
    )
    assert await kx.execute_read_plans(ctx, _deps(llm), planned, _outcome()) is True
    pack_sentence = kx.kernel_sentence(electronics.pack, "ro", "not_exact_match")
    assert sheet == ["p1"] and ctx.reply.text.startswith(pack_sentence)


def test_not_exact_match_is_not_said_over_zero_cards(electronics):  # noqa: F811
    ctx = _ctx(electronics)
    ctx.set_reply("N-am găsit nimic potrivit.", cacheable=False)
    planned = PlannedTurn(plans=(), primary=0, disclosures=((0, "not_exact_match"),))
    assert kx._disclosure_text(ctx, planned) == ""
    ctx.reply.rich = RichReply(
        intro="x", items=[NS()], pick=None, education=None, chips=[], disclaimer=None
    )
    assert kx._disclosure_text(ctx, planned) != ""


def test_the_need_memory_accepts_the_composed_disclosure():
    from src.agent import interpreted_turn as it

    planned = NS(disclosed_needs=("fragrance_free",))
    ctx = NS(
        business=NS(domain_pack=None),
        language="ro",
        turn_id="t1",
        reply=Reply(text="Nu pot verifica cerința fără parfum pe produsele astea."),
        composed_reply="Nu pot verifica cerința fără parfum pe produsele astea.",
        disclosures_composed=frozenset({"need_unverifiable"}),
    )
    assert it._disclosure_memory(ctx, planned)


# --- regulile bogate (v1) -------------------------------------------------------------------------


def test_the_refine_rule_no_longer_asks_for_a_without_claim():
    """wide-2026-10-07: REFINE cerea „Am găsit șampoane fără parfum", regula de ingrediente o
    interzicea; compunerea refuza seturi bune (~17)."""
    assert "fără parfum" not in prompt_builder._RICH_RULES.split("REFINE", 1)[1].split("\n- ")[0]
    assert "nu că produsele rămase sunt" in prompt_builder._RICH_RULES


async def test_an_explicit_refusal_withholds_the_cards_even_without_relevance(
    monkeypatch,
    electronics,  # noqa: F811
):
    """Punctul 2 din analiza wide: o rafinare fără argumente noi devine pagina 2, setul n-are
    `relevance`, iar cardurile refuzate ajungeau pe ecran (sub „Ți-am ales X."). Cu `set_fit=none`,
    refuzul modelului e răspunsul."""
    from tests.test_interpreted_turn_c import _stub_search
    from tests.test_nx382_composer_4 import ComposeLLM as RecommendLLM
    from tests.test_nx382_composer_4 import _ctx as reco_ctx
    from tests.test_nx382_composer_4 import _reply as reco_reply
    from tests.test_nx382_composer_4 import _search_plan

    _stub_search(monkeypatch, electronics, list(electronics.items)[:2])
    refusal = "Astea sunt telefoane, nu tablete, deci nu ți le recomand."
    llm = RecommendLLM(reco_reply(refusal, fit="none"))
    ctx = reco_ctx(electronics, "vreau o tableta")
    assert await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome()) is True
    rich = ctx.reply.rich
    assert not ctx.reply.products and (rich is None or not rich.items)
    assert any(e.type == "refused_set_withheld" for e in ctx.events)
