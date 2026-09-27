"""NX-329 (kernel v1.0, pasul 2) — resolverul de referințe v2: șapte feluri, surse, patru verdicte.

Contractul: niciun id de produs nu vine de la model (I1), o mutație cere țintă `exact` (I10), iar o
țintă care numește o proprietate e respinsă (I24). Suita e tabelară pe pachetele de fixture (alte
verticale decât SOLE, ca un literal de skincare scurs în resolver să pice aici) plus SOLE. Zero
model, zero DB: faptele se construiesc offline (`tests/kernel/fixture_catalog.py`)."""

from __future__ import annotations

import random

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.interpretation import Act, Reference
from src.conversation.references import (
    REASONS,
    ActionRef,
    ProductFacts,
    ReferenceFacts,
    ReferenceSources,
    ShownItem,
    gate_act_targets,
    plan_lookup,
    resolve_references,
)
from src.domain.loader import load_domain_pack
from src.domain.pack import DEFAULT_REFERENCE_DIMENSIONS
from src.models import BusinessConfig
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay


def ref(rid: str, kind: str, **fields) -> Reference:
    base = {"ordinal": None, "name": None, "dimension": None, "value": None, "direction": None}
    return Reference(id=rid, text=fields.pop("text", rid), kind=kind, **{**base, **fields})


def run(pack_name: str, refs, sources: ReferenceSources, *, vocab=True, **facts_kw):
    pack = fc.pack(pack_name)
    lookup = plan_lookup(refs, sources, pack=pack, locale="ro")
    facts = fc.facts(pack_name, lookup, **facts_kw)
    return resolve_references(
        refs,
        sources,
        facts,
        vocab=fc.vocabulary(pack_name) if vocab else None,
        pack=pack,
        locale="ro",
    )


def one(pack_name: str, reference: Reference, sources: ReferenceSources, **kw):
    [resolved] = run(pack_name, [reference], sources, **kw)
    return resolved


def screen(pack_name: str, *ids: str, **kw) -> ReferenceSources:
    return ReferenceSources(shown_now=fc.shown(pack_name, *ids), **kw)


# --- ordinal -------------------------------------------------------------------------------------


def test_ordinal_picks_the_position_on_screen():
    r = one(
        "electronics",
        ref("r1", "ordinal", ordinal=2),
        screen("electronics", "el-01", "el-02", "el-03"),
    )
    assert (r.outcome, r.product_ids, r.source, r.reason) == (
        "exact",
        ["el-02"],
        "shown_now",
        "ordinal_in_set",
    )


def test_ordinal_beyond_the_set_is_ambiguous_as_the_contract_says():
    r = one(
        "electronics",
        ref("r1", "ordinal", ordinal=4),
        screen("electronics", "el-01", "el-02", "el-03"),
    )
    assert r.outcome == "ambiguous" and r.reason == "ordinal_out_of_range"
    assert r.product_ids == ["el-01", "el-02", "el-03"]


@pytest.mark.parametrize(
    ("ordinal", "ids", "reason"),
    [
        (None, ("el-01",), "invalid_reference"),
        (0, ("el-01",), "invalid_reference"),
        (1, (), "no_set"),
    ],
)
def test_ordinal_not_found(ordinal, ids, reason):
    r = one("electronics", ref("r1", "ordinal", ordinal=ordinal), screen("electronics", *ids))
    assert (r.outcome, r.reason, r.product_ids) == ("not_found", reason, [])


# --- deictic -------------------------------------------------------------------------------------


def test_deictic_prefers_a_valid_action_anchor():
    sources = screen(
        "fashion", "fa-01", "fa-02", action=ActionRef("fa-02", revision=3), displayed_revision=3
    )
    r = one("fashion", ref("r1", "deictic"), sources)
    assert (r.outcome, r.product_ids, r.source) == ("exact", ["fa-02"], "action")


@pytest.mark.parametrize(
    ("anchor", "reason"),
    [
        (ActionRef("fa-02", valid=False), "anchor_invalid"),
        (ActionRef("fa-02", revision=2), "anchor_stale"),
    ],
)
def test_a_rejected_action_anchor_stops_and_never_falls_back(anchor, reason):
    sources = screen("fashion", "fa-01", action=anchor, displayed_revision=3)
    r = one("fashion", ref("r1", "deictic"), sources)
    assert (r.outcome, r.reason, r.product_ids) == ("stale", reason, [])


def test_deictic_on_a_product_page_is_the_page_even_with_cards_on_screen():
    sources = ReferenceSources(
        shown_now=fc.shown("furniture", "fu-01", "fu-02"), page=fc.shown("furniture", "fu-05")[0]
    )
    r = one("furniture", ref("r1", "deictic"), sources)
    assert (r.outcome, r.product_ids, r.source, r.reason) == (
        "exact",
        ["fu-05"],
        "page",
        "page_deictic",
    )


def test_deictic_uses_the_focus_then_a_single_card_then_asks():
    focused = screen("gifts", "gi-01", "gi-02", focus="gi-02")
    assert one("gifts", ref("r1", "deictic"), focused).product_ids == ["gi-02"]
    single = one("gifts", ref("r1", "deictic"), screen("gifts", "gi-03"))
    assert (single.outcome, single.reason) == ("exact", "single_in_set")
    many = one("gifts", ref("r1", "deictic"), screen("gifts", "gi-01", "gi-02"))
    assert (many.outcome, many.reason, many.product_ids) == (
        "ambiguous",
        "no_anchor",
        ["gi-01", "gi-02"],
    )
    none = one("gifts", ref("r1", "deictic"), ReferenceSources())
    assert (none.outcome, none.reason) == ("not_found", "no_set")


# --- name ----------------------------------------------------------------------------------------


def test_name_on_screen_is_exact():
    r = one(
        "electronics",
        ref("r1", "name", name="Apple Phone"),
        screen("electronics", "el-01", "el-02", "el-03"),
    )
    assert (r.outcome, r.product_ids, r.source, r.reason) == (
        "exact",
        ["el-02"],
        "shown_now",
        "named",
    )


def test_a_name_shared_by_two_cards_is_ambiguous_not_a_pick():
    r = one(
        "electronics",
        ref("r1", "name", name="Samsung-ul"),
        screen("electronics", "el-01", "el-07", "el-02"),
    )
    assert (r.outcome, r.reason, r.product_ids) == ("ambiguous", "name_shared", ["el-01", "el-07"])


def test_the_screen_wins_over_an_earlier_set():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-02"),
        shown_earlier=(fc.shown("electronics", "el-08"),),
    )
    r = one("electronics", ref("r1", "name", name="Apple"), sources)
    assert (r.product_ids, r.source) == (["el-02"], "shown_now")


def test_a_name_shown_earlier_only_resolves_there():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-01"),
        shown_earlier=(fc.shown("electronics", "el-03"),),
    )
    r = one("electronics", ref("r1", "name", name="Xiaomi"), sources)
    assert (r.outcome, r.product_ids, r.source) == ("exact", ["el-03"], "shown_earlier")


def test_a_full_distinctive_name_is_found_in_the_catalog():
    r = one(
        "electronics",
        ref("r1", "name", name="Xiaomi Phone 9 512 GB"),
        screen("electronics", "el-01"),
    )
    assert (r.outcome, r.product_ids, r.source) == ("exact", ["el-09"], "catalog")


def test_two_catalog_names_of_equal_length_are_a_tie():
    r = one("gifts", ref("r1", "name", name="set ceai 1 si set ceai 9"), screen("gifts", "gi-02"))
    assert (r.outcome, r.reason, r.source) == ("ambiguous", "catalog_tie", "catalog")
    assert sorted(r.product_ids) == ["gi-01", "gi-09"]


def test_a_name_that_is_nowhere_is_not_found():
    """«linkul la Cerave»: nimic pe ecran, niciun nume distinctiv întreg în catalog."""
    r = one(
        "electronics", ref("r1", "name", name="Huawei"), screen("electronics", "el-01", "el-02")
    )
    assert (r.outcome, r.reason, r.product_ids) == ("not_found", "name_not_found", [])


def test_a_name_that_is_a_reference_value_becomes_an_attribute():
    """«cea neagră»: nu e în niciun nume, dar e o culoare (dimensiune de referință la modă)."""
    r = one("fashion", ref("r1", "name", name="neagra"), screen("fashion", "fa-02", "fa-03"))
    assert (r.kind, r.outcome, r.product_ids, r.reason) == (
        "attribute",
        "exact",
        ["fa-02"],
        "attribute_match",
    )


def test_a_variant_label_counts_as_the_products_value():
    r = one("fashion", ref("r1", "name", name="neagra"), screen("fashion", "fa-01", "fa-03"))
    assert (r.outcome, r.product_ids) == ("exact", ["fa-01"])


# --- I24: o proprietate nu e o țintă -------------------------------------------------------------


@pytest.mark.parametrize(
    ("pack_name", "word", "ids"),
    [("fashion", "bumbac", ("fa-01", "fa-02")), ("gifts", "craciun", ("gi-03", "gi-04"))],
)
def test_a_need_or_use_value_denotes_a_property(pack_name, word, ids):
    r = one(pack_name, ref("r1", "name", name=word), screen(pack_name, *ids))
    assert (r.kind, r.outcome, r.reason, r.product_ids) == (
        "attribute",
        "not_found",
        "denotes_property",
        [],
    )


def test_link_to_redness_is_rejected_on_sole():
    """„link la roșeață": pe SOLE «roșeața» e un alias al nevoii `redness`, nu un produs."""
    doc = replay.load_pack("sole-ro")
    pack = load_domain_pack(
        BusinessConfig(
            id="b",
            slug="sole-ro",
            name="SOLE",
            vertical="ecommerce",
            settings={"domain_pack": doc["domain_pack"]},
        )
    )
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            "concerns": (VocabEntry("redness", "redness", 40), VocabEntry("acne", "acne", 30))
        },
    )
    shown = (
        ShownItem("s1", "SOME BY MI Yuja Niacin Serum", 110.0),
        ShownItem("s2", "COSRX Snail Cream", 89.0),
    )
    facts = ReferenceFacts(
        products={s.product_id: ProductFacts(s.product_id, s.name, s.price, True) for s in shown}
    )
    refs = [ref("r1", "name", name="roseata")]
    [r] = resolve_references(
        refs, ReferenceSources(shown_now=shown), facts, vocab=vocab, pack=pack, locale="ro"
    )
    assert r.reason == "denotes_property"
    [check] = gate_act_targets([Act(kind="link", targets=["r1"], query=None)], [r])
    assert check.verdict == "invalid_reference_target"


def test_a_description_that_names_a_product_class_is_not_a_property():
    """«linkul la serul cu vitamina C»: vocabularul potrivește pe submulțime („vitamina c"), dar
    fraza are un cap nominal („serul"), deci descrie un produs. Găsit pe catalogul real SOLE: era
    respinsă ca proprietate (I24), ceea ce pe calea interpretată ar fi blocat căutarea după nume."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={"key_ingredients": (VocabEntry("vitamina c", "vitamina c", 30),)},
    )
    shown = (ShownItem("s1", "SOME BY MI Yuja Niacin Gel Cream", 85.0),)
    facts = ReferenceFacts(products={"s1": ProductFacts("s1", shown[0].name, 85.0, True)})
    sources = ReferenceSources(shown_now=shown)
    described, bare = resolve_references(
        [ref("r1", "name", name="serul cu vitamina c"), ref("r2", "name", name="vitamina c")],
        sources,
        facts,
        vocab=vocab,
        locale="ro",
    )
    assert (described.outcome, described.reason) == ("not_found", "name_not_found")
    assert bare.reason == "denotes_property"


def test_a_model_proposed_attribute_on_a_need_dimension_is_a_property():
    r = one(
        "fashion",
        ref("r1", "attribute", dimension="material", value="bumbac"),
        screen("fashion", "fa-01"),
    )
    assert r.reason == "denotes_property"


# --- attribute -----------------------------------------------------------------------------------


def test_attribute_brand_picks_the_one_card():
    r = one(
        "electronics",
        ref("r1", "attribute", dimension="brand", value="Apple"),
        screen("electronics", "el-01", "el-02", "el-03"),
    )
    assert (r.outcome, r.product_ids, r.reason) == ("exact", ["el-02"], "attribute_match")


def test_attribute_through_a_pack_alias():
    r = one(
        "electronics",
        ref("r1", "attribute", dimension="color", value="neagra"),
        screen("electronics", "el-02", "el-05"),
    )
    assert (r.outcome, r.product_ids) == ("exact", ["el-05"])


def test_attribute_matching_nothing_on_the_sets():
    r = one(
        "electronics",
        ref("r1", "attribute", dimension="color", value="gri"),
        screen("electronics", "el-01", "el-02"),
    )
    assert (r.outcome, r.reason) == ("not_found", "attribute_not_in_sets")


def test_attribute_without_a_vocabulary_cannot_be_judged():
    r = one(
        "electronics",
        ref("r1", "attribute", dimension="color", value="neagra"),
        screen("electronics", "el-02", "el-05"),
        vocab=False,
    )
    assert (r.outcome, r.reason) == ("not_found", "vocabulary_unavailable")


# --- extreme -------------------------------------------------------------------------------------


def test_extreme_price_uses_the_price_read_again_from_the_catalog():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-03", "el-01", "el-02", prices={"el-01": 1400.0})
    )
    r = one("electronics", ref("r1", "extreme", direction="min"), sources)
    assert (r.outcome, r.product_ids, r.reason) == ("exact", ["el-01"], "price_changed")


def test_extreme_catalog_price_decides_even_against_the_shown_price():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-01", "el-02", prices={"el-02": 100.0})
    )
    r = one("electronics", ref("r1", "extreme", direction="min"), sources)
    assert r.product_ids == ["el-01"]


def test_extreme_tie_is_never_exact():
    r = one(
        "electronics",
        ref("r1", "extreme", dimension="storage", direction="max"),
        screen("electronics", "el-01", "el-04"),
    )
    assert (r.outcome, r.reason) == ("ambiguous", "extreme_tie")


def test_extreme_with_an_unknown_value_is_ambiguous():
    override = {"el-02": {"attributes": {"brand": "Apple", "color": "alb"}}}
    r = one(
        "electronics",
        ref("r1", "extreme", dimension="storage", direction="max"),
        screen("electronics", "el-01", "el-02"),
        override=override,
    )
    assert (r.outcome, r.reason, r.product_ids) == (
        "ambiguous",
        "extreme_unknown_value",
        ["el-01", "el-02"],
    )


def test_extreme_when_a_shown_product_left_the_catalog_does_not_guess():
    """Pe ecran [el-01 1500, el-02 1750, el-03 2000], el-01 a fost scos din catalog. „Cel mai
    ieftin" era el-01; a răspunde el-02 cu `exact` ar fi o ghicire cu aer de siguranță."""
    r = one(
        "electronics",
        ref("r1", "extreme", direction="min"),
        screen("electronics", "el-01", "el-02", "el-03"),
        drop=["el-01"],
    )
    assert (r.outcome, r.reason, r.product_ids) == (
        "ambiguous",
        "extreme_unknown_value",
        ["el-02", "el-03"],
    )


def test_extreme_on_a_non_numeric_dimension_is_not_orderable():
    r = one(
        "electronics",
        ref("r1", "extreme", dimension="color", direction="max"),
        screen("electronics", "el-01", "el-02"),
    )
    assert (r.outcome, r.reason) == ("not_found", "not_orderable")


def test_extreme_needs_a_direction():
    r = one("electronics", ref("r1", "extreme", dimension="price"), screen("electronics", "el-01"))
    assert r.reason == "invalid_reference"


# --- the_other -----------------------------------------------------------------------------------


def test_the_other_is_the_complement_of_the_previous_target():
    refs = [ref("r1", "name", name="Mara"), ref("r2", "the_other")]
    r1, r2 = run("fashion", refs, screen("fashion", "fa-01", "fa-02"))
    assert r1.product_ids == ["fa-01"]
    assert (r2.outcome, r2.product_ids, r2.reason) == ("exact", ["fa-02"], "the_other")


def test_the_other_uses_the_focus_when_nothing_was_named():
    r = one("fashion", ref("r1", "the_other"), screen("fashion", "fa-01", "fa-02", focus="fa-02"))
    assert r.product_ids == ["fa-01"]


def test_the_other_needs_a_pair_and_a_focus():
    three = one("fashion", ref("r1", "the_other"), screen("fashion", "fa-01", "fa-02", "fa-03"))
    assert (three.outcome, three.reason) == ("ambiguous", "not_a_pair")
    pair = one("fashion", ref("r1", "the_other"), screen("fashion", "fa-01", "fa-02"))
    assert (pair.outcome, pair.reason) == ("ambiguous", "no_focus")


# --- earlier -------------------------------------------------------------------------------------


def test_earlier_resolves_on_the_sets_shown_before():
    sources = ReferenceSources(
        shown_now=fc.shown("gifts", "gi-01"), shown_earlier=(fc.shown("gifts", "gi-05"),)
    )
    r = one("gifts", ref("r1", "earlier"), sources)
    assert (r.outcome, r.product_ids, r.source, r.reason) == (
        "exact",
        ["gi-05"],
        "shown_earlier",
        "earlier_single",
    )


def test_earlier_with_a_name_searches_older_sets_too():
    sources = ReferenceSources(
        shown_earlier=(fc.shown("gifts", "gi-02", "gi-03"), fc.shown("gifts", "gi-04", "gi-08")),
    )
    r = one("gifts", ref("r1", "earlier", name="Puzzle"), sources)
    assert (r.outcome, r.product_ids) == ("exact", ["gi-04"])


def test_earlier_without_a_discriminator_on_a_bigger_set_asks():
    sources = ReferenceSources(shown_earlier=(fc.shown("gifts", "gi-02", "gi-03"),))
    r = one("gifts", ref("r1", "earlier"), sources)
    assert (r.outcome, r.reason) == ("ambiguous", "earlier_unspecified")


def test_earlier_reaches_the_parked_topic():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-01"), parked=fc.shown("electronics", "el-06")
    )
    r = one("electronics", ref("r1", "earlier"), sources)
    assert (r.product_ids, r.source) == (["el-06"], "parked")


def test_earlier_with_nothing_before():
    r = one("gifts", ref("r1", "earlier"), screen("gifts", "gi-01"))
    assert (r.outcome, r.reason) == ("not_found", "no_earlier")


# --- resume: setul în focus e cel parcat ---------------------------------------------------------


def test_resume_moves_the_focus_to_the_parked_set():
    """Designul §C: „înapoi la telefoane, cel mai ieftin" pe setul PARCAT, fără câmp `within`."""
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-06"),
        parked=fc.shown("electronics", "el-03", "el-01", "el-02"),
        thread="resume",
    )
    r = one("electronics", ref("r1", "extreme", direction="min"), sources)
    assert (r.outcome, r.product_ids, r.source) == ("exact", ["el-01"], "parked")


# --- revalidarea (I1) ----------------------------------------------------------------------------


def test_a_target_gone_from_the_catalog_is_stale():
    r = one(
        "gifts", ref("r1", "ordinal", ordinal=1), screen("gifts", "gi-01", "gi-02"), drop=["gi-01"]
    )
    assert (r.outcome, r.reason, r.product_ids) == ("stale", "not_in_catalog", [])


def test_ambiguous_candidates_gone_from_the_catalog_are_stale():
    r = one(
        "gifts", ref("r1", "deictic"), screen("gifts", "gi-01", "gi-02"), drop=["gi-01", "gi-02"]
    )
    assert (r.outcome, r.reason) == ("stale", "not_in_catalog")


def test_an_out_of_stock_target_stays_exact_and_says_so():
    r = one("electronics", ref("r1", "ordinal", ordinal=1), screen("electronics", "el-07"))
    assert (r.outcome, r.product_ids, r.reason) == ("exact", ["el-07"], "unavailable")


def test_i1_no_id_leaves_the_resolver_unless_the_catalog_returned_it_this_turn():
    """Proprietatea I1: surse aleatoare, inclusiv id-uri de alt tenant sau inventate (absente din
    fapte), 7 feluri. Niciun id din ieșire nu lipsește din faptele turului; `exact` are exact un
    produs; motivul e mereu din vocabularul închis."""
    rng = random.Random(329)
    catalog = sorted(fc.products("electronics"))
    foreign = ["other-tenant-1", "other-tenant-2", "made-up"]
    kinds = ["ordinal", "deictic", "name", "attribute", "extreme", "the_other", "earlier"]
    for _ in range(400):
        pool = catalog + foreign

        def pick(n):
            return tuple(
                ShownItem(pid, fc.products("electronics").get(pid, {}).get("name", pid), 1.0)
                for pid in rng.sample(pool, n)
            )

        sources = ReferenceSources(
            shown_now=pick(rng.randint(0, 4)),
            shown_earlier=tuple(pick(rng.randint(1, 3)) for _ in range(rng.randint(0, 2))),
            parked=pick(rng.randint(0, 3)),
            page=pick(1)[0] if rng.random() < 0.3 else None,
            focus=rng.choice(pool) if rng.random() < 0.3 else None,
            thread=rng.choice(["continue", "resume"]),
        )
        refs = [
            ref(
                f"r{i}",
                rng.choice(kinds),
                ordinal=rng.choice([None, 1, 2, 5]),
                name=rng.choice([None, "Samsung", "Apple Phone", "made-up"]),
                dimension=rng.choice([None, "brand", "color", "storage", "price"]),
                value=rng.choice([None, "negru", "Apple"]),
                direction=rng.choice([None, "min", "max"]),
            )
            for i in range(1, rng.randint(2, 4))
        ]
        pack = fc.pack("electronics")
        facts = fc.facts("electronics", plan_lookup(refs, sources, pack=pack, locale="ro"))
        for r in resolve_references(
            refs, sources, facts, vocab=fc.vocabulary("electronics"), pack=pack, locale="ro"
        ):
            assert set(r.product_ids) <= set(facts.products), r
            assert not set(r.product_ids) & set(foreign), r
            assert r.reason in REASONS, r
            if r.outcome == "exact":
                assert len(r.product_ids) == 1, r


# --- cinci pachete: aceleași reguli, alte verticale ----------------------------------------------


@pytest.mark.parametrize("pack_name", replay.FIXTURE_PACKS)
def test_the_same_rules_hold_on_every_fixture_vertical(pack_name):
    ids = sorted(fc.products(pack_name))[:3]
    catalog = fc.products(pack_name)
    cheapest = min(ids, key=lambda pid: catalog[pid]["price"])
    refs = [
        ref("r1", "ordinal", ordinal=2),
        ref("r2", "extreme", direction="min"),
        ref("r3", "name", name=catalog[ids[2]]["name"]),
    ]
    r1, r2, r3 = run(pack_name, refs, screen(pack_name, *ids))
    assert r1.product_ids == [ids[1]]
    assert r2.product_ids == [cheapest]
    assert (r3.outcome, r3.product_ids) == ("exact", [ids[2]])


def test_the_rules_hold_on_sole_without_a_catalog_vocabulary():
    doc = replay.load_pack("sole-ro")
    pack = load_domain_pack(
        BusinessConfig(
            id="b",
            slug="sole-ro",
            name="SOLE",
            vertical="ecommerce",
            settings={"domain_pack": doc["domain_pack"]},
        )
    )
    shown = (
        ShownItem("p1", "COSRX Advanced Snail 92 All In One Cream", 89.0),
        ShownItem("p3", "SOME BY MI Yuja Niacin Brightening Moisture Gel Cream", 110.0),
        ShownItem("p4", "By Wishtrend Pure Vitamin C 21.5 Advanced Serum", 120.0),
    )
    facts = ReferenceFacts(
        products={s.product_id: ProductFacts(s.product_id, s.name, s.price, True) for s in shown}
    )
    refs = [
        ref("r1", "name", name="Wishtrend Vitamin"),
        ref("r2", "name", name="Yuja Niacin"),
        ref("r3", "extreme", direction="max"),
    ]
    got = resolve_references(refs, ReferenceSources(shown_now=shown), facts, pack=pack, locale="ro")
    assert [r.product_ids for r in got] == [["p4"], ["p3"], ["p4"]]


# --- poarta țintelor (I10, I24) ------------------------------------------------------------------


def _resolved(rid, outcome, reason="named", ids=("x",)):
    from src.conversation.interpretation import ResolvedRef

    return ResolvedRef(
        ref_id=rid,
        kind="name",
        outcome=outcome,
        product_ids=list(ids) if outcome in ("exact", "ambiguous") else [],
        source="shown_now",
        reason=reason,
    )


def test_gate_cart_needs_an_exact_target():
    acts = [Act(kind="cart", targets=["r1"], query=None)]
    assert gate_act_targets(acts, [_resolved("r1", "exact", "extreme")])[0].verdict == "ok"
    assert (
        gate_act_targets(acts, [_resolved("r1", "ambiguous", "name_shared")])[0].verdict
        == "not_exact"
    )
    assert (
        gate_act_targets(acts, [_resolved("r1", "not_found", "name_not_found")])[0].verdict
        == "not_exact"
    )


def test_gate_read_only_acts_accept_ambiguity_but_never_a_property():
    link = [Act(kind="link", targets=["r1"], query=None)]
    assert gate_act_targets(link, [_resolved("r1", "ambiguous", "name_shared")])[0].verdict == "ok"
    assert (
        gate_act_targets(link, [_resolved("r1", "not_found", "denotes_property")])[0].verdict
        == "invalid_reference_target"
    )


def test_gate_an_undeclared_target_is_unknown():
    acts = [Act(kind="compare", targets=["r1", "r9"], query=None)]
    verdicts = [c.verdict for c in gate_act_targets(acts, [_resolved("r1", "exact")])]
    assert verdicts == ["ok", "unknown_reference"]


# --- plan_lookup ---------------------------------------------------------------------------------


def test_plan_lookup_revalidates_every_source_id_and_searches_only_missing_names():
    sources = ReferenceSources(
        shown_now=fc.shown("electronics", "el-01", "el-02"),
        shown_earlier=(fc.shown("electronics", "el-03"),),
        parked=fc.shown("electronics", "el-04"),
        page=fc.shown("electronics", "el-05")[0],
        focus="el-06",
    )
    refs = [
        ref("r1", "name", name="Apple"),
        ref("r2", "name", name="Xiaomi Phone 9 512 GB"),
        ref("r3", "extreme", dimension="storage", direction="max"),
    ]
    lookup = plan_lookup(refs, sources, pack=fc.pack("electronics"), locale="ro")
    assert set(lookup.ids) == {"el-01", "el-02", "el-03", "el-04", "el-05", "el-06"}
    assert lookup.names == ("Xiaomi Phone 9 512 GB",)
    assert lookup.attributes == ("brand", "color", "storage")


# --- pachetul: dimensiunile de referință ---------------------------------------------------------


def _pack_with(reference_dimensions):
    doc = replay.load_pack("gifts")
    domain_pack = {**doc["domain_pack"], "reference_dimensions": reference_dimensions}
    return load_domain_pack(
        BusinessConfig(
            id="b", slug="g", name="g", vertical="ecommerce", settings={"domain_pack": domain_pack}
        )
    )


def test_the_loader_refuses_an_additive_facet_as_a_reference_dimension():
    assert _pack_with(["brand", "occasion", "recipient"]).reference_dimensions == ("brand",)


def test_the_loader_default_and_the_declared_fixture_dimensions():
    assert fc.pack("gifts").reference_dimensions == DEFAULT_REFERENCE_DIMENSIONS
    assert fc.pack("electronics").reference_dimensions == ("brand", "color")
