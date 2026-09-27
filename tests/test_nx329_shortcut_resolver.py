"""NX-329 (kernel v1.0, pasul 2) — scurtăturile de link și comparație pe resolverul de referințe v2.

Limita declarată de NX-326: «trimite-mi linkul la Cerave», cu nimic Cerave pe ecran, servea
linkurile TUTUROR produselor afișate. Cu `REFERENCE_RESOLVER_V2_SHORTCUTS_ENABLED`, numele e o
referință `not_found`, iar turul pleacă la model (care poate căuta). Aceleași porți păstrează
regresiile B1/B2 ale NX-326. Faptele de catalog și vocabularul sunt stub-uri; zero model, zero
DB."""

from __future__ import annotations

import pytest

from src.agent import deterministic as det
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import get_settings
from src.conversation.references import ProductFacts, ReferenceFacts, name_key
from src.worker.stages import agent as agent_mod
from src.worker.stages.agent import agent_stage
from tests.test_nx326_named_shortcut_targets import SHOWN, _ctx, _deps, _RecordLLM

#: Un produs al catalogului care NU e pe ecran, găsit doar după numele distinctiv întreg.
OFF_SCREEN = "p9"
OFF_SCREEN_NAME = "SOME BY MI Retinol Intense Advanced Triple Action Eye Cream"


@pytest.fixture(autouse=True)
def _stub_prompt_inputs(monkeypatch):
    async def _cats(conn, business_id):
        return ["Creme"]

    async def _aliases(conn, business_id, **k):
        return []

    async def _planner_by_ids(conn, business_id, ids, **k):
        return []

    monkeypatch.setattr(agent_mod, "list_category_names", _cats)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _aliases)
    monkeypatch.setattr("src.agent.planner.get_products_by_ids", _planner_by_ids)


@pytest.fixture
def catalog(monkeypatch):
    """`get_products_by_ids` al linkului: id-urile cerute, înregistrate (ca la NX-326)."""
    calls: list[list[str]] = []
    by_id = {r.product_id: r for r in SHOWN}

    async def fake_by_ids(conn, business_id, ids, **k):
        calls.append(list(ids))
        return [
            {"id": pid, "name": by_id[pid].name, "price": by_id[pid].price, "url": f"u/{pid}"}
            for pid in ids
            if pid in by_id
        ]

    monkeypatch.setattr(det, "get_products_by_ids", fake_by_ids)
    return calls


@pytest.fixture
def compared(monkeypatch):
    seen: list[list[str]] = []

    async def fake_serve(ctx, deps, ids):
        seen.append(list(ids))
        ctx.set_reply("tabel", cacheable=False)
        return True

    monkeypatch.setattr(det, "serve_comparison", fake_serve)
    return seen


@pytest.fixture
def v2(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "named_shortcut_targets_enabled", True)
    monkeypatch.setattr(settings, "reference_resolver_v2_shortcuts_enabled", True)

    def _set(value: bool) -> None:
        monkeypatch.setattr(settings, "reference_resolver_v2_shortcuts_enabled", value)

    return _set


@pytest.fixture
def facts(monkeypatch):
    """Catalogul fals: produsele de pe ecran (minus `gone`) + un nume distinctiv în afara lui."""
    state = {"gone": set(), "fail": False, "lookups": []}
    by_id = {r.product_id: r for r in SHOWN}

    async def fake_fetch(deps, business_id, lookup):
        state["lookups"].append(lookup)
        if state["fail"]:
            raise ConnectionError("pooler plin")
        products = {
            pid: ProductFacts(pid, by_id[pid].name, by_id[pid].price, True)
            for pid in lookup.ids
            if pid in by_id and pid not in state["gone"]
        }
        named = {}
        for n in lookup.names:
            if "retinol intense" in n.lower():
                named[name_key(n)] = ((OFF_SCREEN, 50),)
                products[OFF_SCREEN] = ProductFacts(OFF_SCREEN, OFF_SCREEN_NAME, 99.0, True)
        return ReferenceFacts(products=products, named=named)

    async def fake_vocabulary(deps, business_id):
        state["vocab_loads"] = state.get("vocab_loads", 0) + 1
        return CatalogVocabulary(
            business_id=business_id,
            dimensions={"concerns": (VocabEntry("redness", "roseata", 40),)},
        )

    monkeypatch.setattr(det, "fetch_reference_facts", fake_fetch)
    monkeypatch.setattr(det, "get_vocabulary", fake_vocabulary)
    return state


def _event(ctx, name):
    return [e.properties for e in ctx.events if e.type == name]


# --- cazul lăsat descoperit de NX-326 ------------------------------------------------------------


async def test_a_name_that_is_not_on_screen_no_longer_serves_every_link(v2, facts, catalog):
    llm = _RecordLLM()
    ctx = _ctx("Trimite-mi linkul la Cerave")
    await agent_stage(ctx, _deps(llm))

    assert catalog == [], "nicio listă de linkuri servită pe o țintă negăsită"
    assert llm.loop_called
    [targets] = _event(ctx, "shortcut_targets")
    assert targets["outcome"] == "model" and targets["reason"] == "name_not_found"
    assert targets["resolver"] == "v2"


async def test_link_to_a_need_is_rejected_as_a_target(v2, facts, catalog):
    """I24: „link la roșeață": roșeața e o nevoie, nu un produs."""
    llm = _RecordLLM()
    ctx = _ctx("link la roșeață")
    await agent_stage(ctx, _deps(llm))

    assert catalog == [] and llm.loop_called
    [targets] = _event(ctx, "shortcut_targets")
    assert targets["reason"] == "invalid_reference_target"


async def test_a_full_name_outside_the_screen_is_found_in_the_catalog(v2, facts, catalog):
    ctx = _ctx(f"trimite-mi linkul la {OFF_SCREEN_NAME}")
    await agent_stage(ctx, _deps())

    assert catalog == [[OFF_SCREEN]]


# --- regresiile NX-326, pe resolver --------------------------------------------------------------


async def test_b1_link_to_a_named_product(v2, facts, catalog):
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    await agent_stage(ctx, _deps())
    assert catalog == [["p3"]]


async def test_b1_link_without_a_name_serves_every_anchor_and_reads_nothing(v2, facts, catalog):
    ctx = _ctx("Trimite-mi linkul, te rog")
    await agent_stage(ctx, _deps())
    assert catalog == [["p1", "p2", "p3", "p4", "p5"]]
    assert facts["lookups"] == [], "fără referințe nu se citește catalogul"


async def test_b1_a_word_shared_by_two_cards_serves_both(v2, facts, catalog):
    from src.models import ProductRef

    shown = [
        ProductRef("p1", "IT'S SKIN The Fresh Blueberries Toner", 50.0),
        ProductRef("p2", "IT'S SKIN The Fresh Tomato Toner", 50.0),
        ProductRef("p3", "COSRX Low pH Cleanser", 60.0),
    ]

    async def fetch(deps, business_id, lookup):
        return ReferenceFacts(
            products={
                r.product_id: ProductFacts(r.product_id, r.name, r.price, True) for r in shown
            }
        )

    det_fetch = det.fetch_reference_facts
    det.fetch_reference_facts = fetch
    try:
        ctx = _ctx("dă-mi linkul la The Fresh", shown=shown)
        await agent_stage(ctx, _deps())
    finally:
        det.fetch_reference_facts = det_fetch
    assert catalog == [["p1", "p2"]]
    [targets] = _event(ctx, "shortcut_targets")
    assert targets["source"] == "ambiguous"


async def test_b2_compare_two_named_products_in_the_order_asked(v2, facts, compared):
    ctx = _ctx("Compară Wishtrend Vitamin cu Yuja Niacin")
    await agent_stage(ctx, _deps())
    assert compared == [["p4", "p3"]]


async def test_b2_compare_ordinals(v2, facts, compared):
    ctx = _ctx("compară primul cu al treilea")
    await agent_stage(ctx, _deps())
    assert compared == [["p1", "p3"]]


async def test_compare_the_most_expensive_with_the_second(v2, facts, compared):
    """Un extrem și un ordinal în același mesaj: cel mai scump de pe ecran (p4, 120 lei) și p2."""
    ctx = _ctx("compară-l pe cel mai scump cu al doilea")
    await agent_stage(ctx, _deps())
    assert compared == [["p4", "p2"]]


async def test_compare_without_names_keeps_the_first_two(v2, facts, compared):
    ctx = _ctx("compară-le pe primele două")
    await agent_stage(ctx, _deps())
    assert compared == [["p1", "p2"]]


async def test_compare_a_single_target_goes_to_the_model(v2, facts, compared):
    llm = _RecordLLM()
    ctx = _ctx("compară Yuja Niacin cu celelalte")
    await agent_stage(ctx, _deps(llm))
    assert compared == [] and llm.loop_called


async def test_a_target_gone_from_the_catalog_goes_to_the_model(v2, facts, catalog):
    facts["gone"].add("p3")
    llm = _RecordLLM()
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    await agent_stage(ctx, _deps(llm))
    assert catalog == [] and llm.loop_called
    [ref] = _event(ctx, "reference_v2")
    assert (ref["outcome"], ref["reason"]) == ("stale", "not_in_catalog")


async def test_the_vocabulary_is_loaded_only_when_a_value_must_be_judged(v2, facts, compared):
    """Ordinalele și numele de pe ecran nu au nevoie de vocabular (~1 s cu cache rece)."""
    await agent_stage(_ctx("compară primul cu al treilea"), _deps())
    await agent_stage(_ctx("Compară Wishtrend Vitamin cu Yuja Niacin"), _deps())
    assert facts.get("vocab_loads", 0) == 0
    await agent_stage(_ctx("compară Yuja Niacin cu Cerave"), _deps(_RecordLLM()))
    assert facts["vocab_loads"] == 1


# --- degradare și flag stins ---------------------------------------------------------------------


async def test_catalog_unreadable_falls_back_to_the_nx326_path(v2, facts, catalog):
    facts["fail"] = True
    ctx = _ctx("Trimite-mi linkul la Yuja Niacin")
    await agent_stage(ctx, _deps())
    assert catalog == [["p3"]], "calea NX-326 servește în continuare produsul numit pe ecran"
    assert [e["gate"] for e in _event(ctx, "reference_v2_unavailable")] == ["link"]


async def test_flag_off_is_the_nx326_behavior(v2, facts, catalog):
    v2(False)
    ctx = _ctx("Trimite-mi linkul la Cerave")
    await agent_stage(ctx, _deps())
    assert catalog == [["p1", "p2", "p3", "p4", "p5"]]
    assert facts["lookups"] == []
    assert all("resolver" not in t for t in _event(ctx, "shortcut_targets"))


# --- politica, pură ------------------------------------------------------------------------------


def test_the_events_carry_no_names_or_user_text(v2):
    from src.conversation.interpretation import ResolvedRef

    refs = det.shortcut_references(
        "linkul la Cerave", trigger=det._LINK_RE, locale="ro", versus=False
    )
    resolved = [
        ResolvedRef(
            ref_id="r1",
            kind="name",
            outcome="not_found",
            product_ids=[],
            source="catalog",
            reason="name_not_found",
        )
    ]
    decision = det.decide_shortcut("link", refs, resolved)
    assert decision.action == "model" and "cerave" not in repr(decision).lower()


def _ambiguous(rid: str, ids: list[str], source: str = "shown_now"):
    from src.conversation.interpretation import ResolvedRef

    return ResolvedRef(
        ref_id=rid,
        kind="name",
        outcome="ambiguous",
        product_ids=ids,
        source=source,
        reason="name_shared",
    )


def _name_refs(n: int):
    from src.conversation.interpretation import Reference

    return [
        Reference(
            id=f"r{i + 1}",
            text="x",
            kind="name",
            ordinal=None,
            name="x",
            dimension=None,
            value=None,
            direction=None,
        )
        for i in range(n)
    ]


def test_two_references_on_exactly_two_identical_cards_compare_them():
    """Sonda NX-329: «Compară MUZIGAE MANSION Objet Water cu MUZIGAE MANSION…» pe două carduri
    cu nume identic. Două referințe, doi candidați, o comparație cere produse distincte: nicio
    alegere nu rămâne, deci se servește perechea (NX-326 o compara, v2 o trimitea la model)."""
    pair = [_ambiguous("r1", ["m1", "m2"]), _ambiguous("r2", ["m2", "m1"])]
    decision = det.decide_shortcut("compare", _name_refs(2), pair)
    assert (decision.action, decision.ids) == ("served", ("m1", "m2"))


@pytest.mark.parametrize(
    "resolved",
    [
        # șase carduri identice pentru două referințe: alegerea e a modelului
        [_ambiguous("r1", list("abcdef")), _ambiguous("r2", list("abcdef"))],
        # seturi diferite
        [_ambiguous("r1", ["a", "b"]), _ambiguous("r2", ["b", "c"])],
        # candidații nu sunt pe ecran
        [_ambiguous("r1", ["a", "b"], "catalog"), _ambiguous("r2", ["a", "b"], "catalog")],
    ],
    ids=["six_for_two", "different_sets", "off_screen"],
)
def test_a_pairing_that_leaves_a_choice_goes_to_the_model(resolved):
    assert det.decide_shortcut("compare", _name_refs(2), resolved).action == "model"


def test_pairing_ignores_ambiguity_that_asks_for_a_missing_product():
    """«compară-l pe al treilea cu celălalt» pe DOUĂ carduri: al treilea nu există, deci perechea de
    pe ecran nu e ce a cerut clientul (review #447)."""
    from src.conversation.interpretation import ResolvedRef

    out_of_range = ResolvedRef(
        ref_id="r1",
        kind="ordinal",
        outcome="ambiguous",
        product_ids=["a", "b"],
        source="shown_now",
        reason="ordinal_out_of_range",
    )
    no_focus = ResolvedRef(
        ref_id="r2",
        kind="the_other",
        outcome="ambiguous",
        product_ids=["a", "b"],
        source="shown_now",
        reason="no_focus",
    )
    decision = det.decide_shortcut("compare", _name_refs(2), [out_of_range, no_focus])
    assert decision.action == "model"


def test_pairing_is_only_for_comparison():
    """La link, o ambiguitate pe ecran servește deja candidații; regula nu schimbă nimic acolo."""
    pair = [_ambiguous("r1", ["m1", "m2"]), _ambiguous("r2", ["m1", "m2"])]
    assert det.decide_shortcut("link", _name_refs(2), pair).ids == ("m1", "m2")
