"""NX-382 faza 4 — recomandarea pe calea kernelului, scrisă de compozitorul unic.

Contractul fazei: `render` cheamă compozitorul în locul compunerii bogate când planul vine de pe
kernel (`ResponsePlan.kernel`); faptele sunt fișa ÎNTREAGĂ a fiecărui produs (citită o dată), o
familie de variante se arată o singură dată, cardurile alese poartă motivul modelului, verificat pe
fapte. Niciun produs ales ⇒ refuzul modelului, judecat ca azi (NX-306/362). Orice eșec ⇒
compunerea bogată de azi.

Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

from src.agent import composer as cp
from src.agent import finalize
from src.agent import kernel_executors as kx
from src.agent.turn_planner import PlannedTurn
from src.config import get_settings
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import AmbiguityDecision, TurnPlan
from src.conversation.state_v2 import ConversationStateV2
from src.tools.catalog_tools import SearchArgs
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_c import _stub_search, electronics  # noqa: F401

SHADE_A = {
    "id": "a",
    "name": "KUNDAL Shampoo Cherry Blossom - sampon cu keratina 500 ml",
    "price": 99.0,
}
SHADE_B = {
    "id": "b",
    "name": "KUNDAL Shampoo Cherry Blossom - sampon pentru par uscat 500 ml",
    "price": 99.0,
}
OTHER = {"id": "c", "name": "SOME BY MI Cream", "price": 80.0}


def _reply(text="Am găsit două telefoane bune.", items=(), met=(), fit=None):
    return {
        "text": text,
        "general_advice": "",
        "items": [{"handle": h, "reason": r} for h, r in items],
        "suggestions": [],
        "obligations_met": list(met),
        "set_fit": fit or ("fits" if items else "none"),
    }


class ComposeLLM(sh.StageLLM):
    """Răspunde doar apelului compozitorului; compunerea bogată de azi primește o eroare, deci un
    test care o vede servită o vede pe rezerva ei (proza)."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply
        self.composer_calls: list[tuple[str, str, dict]] = []
        self.rich_calls = 0

    async def complete_schema(self, system, user, schema, **kw):
        if schema.get("name") == cp.SCHEMA_NAME:
            self.composer_calls.append((system, user, schema))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        self.rich_calls += 1
        raise RuntimeError("rich")


# --- familiile ------------------------------------------------------------------------------------


def test_one_representative_per_family_with_the_others_as_facts():
    reps, variants = cp.families([SHADE_A, OTHER, SHADE_B])
    assert [p["id"] for p in reps] == ["a", "c"]
    assert variants == {"a": ["KUNDAL Shampoo Cherry Blossom - sampon pentru par uscat 500 ml"]}


def test_the_variants_reach_the_facts_of_their_representative():
    inp = cp.ComposeInput(
        task="recommend",
        products=[SHADE_A, OTHER],
        variants={"a": ["KUNDAL Shampoo Cherry Blossom - sampon pentru par uscat 500 ml"]},
    )
    facts = cp.facts_block(inp, None, "ro")
    head, rest = facts.split("PRODUCT P2", 1)
    assert (
        "also available as (other shades or sizes): KUNDAL Shampoo Cherry Blossom - sampon "
        "pentru par uscat 500 ml" in head
    )
    assert "also available as" not in rest


# --- prin executorul de căutare -----------------------------------------------------------------


def _search_plan() -> PlannedTurn:
    plan = TurnPlan(
        executor="search",
        product_ids=[],
        search_args=SearchArgs(query="telefon"),
        depends_on=None,
    )
    return PlannedTurn(plans=(plan,), primary=0)


def _outcome() -> GateOutcome:
    return GateOutcome(decision=AmbiguityDecision(verdict="act", reason="none", question=None))


def _ctx(cat, text="vreau un telefon"):
    return sh.build_ctx(cat, ConversationStateV2(), text)


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


async def test_the_kernel_recommendation_is_written_by_the_composer(
    monkeypatch,
    electronics,  # noqa: F811
):
    ids = list(electronics.items)[:3]
    _stub_search(monkeypatch, electronics, ids)
    llm = ComposeLLM(
        _reply(
            "Pentru poze, uite două variante.",
            items=[("P2", "Are memoria cea mai mare dintre cele două."), ("P1", "E mai ieftin.")],
        )
    )
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome()) is True
    assert llm.rich_calls == 0, "compunerea bogată de azi nu mai rulează"
    rich = ctx.reply.rich
    assert rich is not None and rich.intro == "Pentru poze, uite două variante."
    assert [it.product_id for it in rich.items] == [ids[1], ids[0]], "ordinea modelului"
    assert rich.items[0].reason == "Are memoria cea mai mare dintre cele două."
    system, user, _ = llm.composer_calls[0]
    assert "TASK recommend:" in system and "PRODUCT P3" in user


async def test_a_refused_set_is_the_models_refusal(monkeypatch, electronics):  # noqa: F811
    ids = list(electronics.items)[:2]
    _stub_search(monkeypatch, electronics, ids)
    llm = ComposeLLM(_reply("N-am găsit ce cauți: astea sunt telefoane, nu tablete."))
    ctx = _ctx(electronics, "vreau o tableta")
    assert await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome()) is True
    assert llm.rich_calls == 0
    # ce se servește sub un refuz e decizia de azi (NX-306/362/365): fără `relevance` (setul
    # stub-ului nu vine dintr-o căutare nouă), cardurile rămân, recuperate din fapte
    assert any(
        e.type == "rich_downgraded" and e.properties["reason"] == "no-items-selected"
        for e in ctx.events
    )


async def test_a_failed_composer_falls_back_to_todays_rich_call(
    monkeypatch,
    electronics,  # noqa: F811
):
    ids = list(electronics.items)[:2]
    _stub_search(monkeypatch, electronics, ids)
    llm = ComposeLLM(_reply("Telefonul costă 9 lei.", items=[("P1", "Bun.")]))  # preț inventat
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome()) is True
    assert llm.rich_calls == 1, "rezerva: compunerea bogată de azi"


async def test_the_recommend_flag_off_is_todays_rich_call(monkeypatch, electronics):  # noqa: F811
    monkeypatch.setattr(get_settings(), "composer_recommend_enabled", False)
    ids = list(electronics.items)[:2]
    _stub_search(monkeypatch, electronics, ids)
    llm = ComposeLLM(_reply("x", items=[("P1", "Bun.")]))
    ctx = _ctx(electronics)
    await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome())
    assert llm.composer_calls == [] and llm.rich_calls == 1


async def test_the_full_sheet_is_merged_over_the_search_row(monkeypatch):
    async def by_ids(conn, business_id, ids, limit=6):
        return [
            {
                "id": "a",
                "name": "X",
                "sections": [{"kind": "usage", "body": "Seara."}],
                "lexical_step": "should-not-win",
            }
        ]

    monkeypatch.setattr("src.db.queries.catalog.get_products_by_ids", by_ids)
    ctx = NS(business=NS(id="b1"), emit=lambda *a, **k: None)
    rows = [{"id": "a", "name": "X", "lexical_step": "strict", "sections": []}]
    [merged] = await finalize._full_sheets(ctx, _deps(None), rows)
    assert merged["lexical_step"] == "strict"
    assert merged["sections"] == [{"kind": "usage", "body": "Seara."}]


async def test_a_failed_sheet_read_keeps_the_search_rows():
    events = []

    class BrokenDb:
        def __call__(self, op):
            raise RuntimeError("db")

    ctx = NS(business=NS(id="b1"), emit=lambda name, **p: events.append(p))
    rows = [{"id": "a", "name": "X"}]
    assert await finalize._full_sheets(ctx, NS(db=BrokenDb()), rows) == rows
    assert events[0]["outcome"] == "sheets_failed"


# --- verdictul pe set (AQUAPICK) ----------------------------------------------------------------


def _fit_check(items, fit):
    inp = cp.ComposeInput(task="recommend", products=[OTHER])
    composed = cp.parse(_reply("Uite ce am.", items=items, fit=fit), inp)
    return cp.check(composed, inp, facts="", units=frozenset())[0]


def test_an_empty_list_is_not_a_refusal_by_itself():
    """AQUAPICK (wide-2026-10-07): proza recomanda, `items` venea gol, iar lista goală era citită
    ca refuz: zero carduri sub un text pozitiv. Acum decide `set_fit`."""
    assert _fit_check((), "fits").reason == "items_missing"
    assert _fit_check((), "partial").reason == "items_missing"
    assert _fit_check((("P1", "Bună."),), "none").reason == "fit_contradiction"
    assert _fit_check((), "none").ok and _fit_check((("P1", "Bună."),), "partial").ok


def test_the_recommend_schema_asks_for_the_set_fit():
    body = cp.schema(cp.ComposeInput(task="recommend", products=[OTHER]))["schema"]
    assert body["properties"]["set_fit"]["enum"] == ["fits", "partial", "none"]
    assert "set_fit" in body["required"]
    assert "set_fit" not in cp.schema(cp.ComposeInput(task="cart"))["schema"]["properties"]


async def test_a_positive_text_without_cards_falls_back_to_todays_rich_call(
    monkeypatch,
    electronics,  # noqa: F811
):
    ids = list(electronics.items)[:2]
    _stub_search(monkeypatch, electronics, ids)
    llm = ComposeLLM(_reply("Uite două telefoane foarte bune.", fit="fits"))
    ctx = _ctx(electronics)
    assert await kx.execute_read_plans(ctx, _deps(llm), _search_plan(), _outcome()) is True
    assert llm.rich_calls == 1
    assert any(
        e.type == "composer" and e.properties.get("reason") == "items_missing" for e in ctx.events
    )


def test_a_routine_keeps_its_slot_order_and_step_labels(electronics):  # noqa: F811
    from src.tools.routine_tools import RoutineStepRef, RoutineView

    rows = sh.product_rows(electronics, list(electronics.items)[:3])
    ids = [r["id"] for r in rows]
    ctx = _ctx(electronics)
    ctx.routine = RoutineView(
        family="face",
        steps=tuple(
            RoutineStepRef(position=n, step=f"s{n}", label=f"Pas {n}", product_id=pid)
            for n, pid in enumerate(ids, 1)
        ),
    )
    inp = cp.ComposeInput(task="recommend", products=rows)
    # modelul a narat doar pașii 3 și 1, în ordinea lui
    composed = cp.parse(_reply("Rutina ta.", items=[("P3", "Ultimul."), ("P1", "Primul.")]), inp)
    rich = cp.rich_reply(ctx, composed, rows)
    assert [it.product_id for it in rich.items] == ids, "ordinea sloturilor, cu pasul nenarat"
    assert [it.badge for it in rich.items] == ["Pas 1", "Pas 2", "Pas 3"]
    assert [it.reason for it in rich.items] == ["Primul.", None, "Ultimul."]
