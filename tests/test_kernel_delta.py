"""NX-330 (kernel v1.0, pasul 3a) — traducerea interpretării validate în propuneri pentru reducer.

Tabelul de traducere (op × dimensiune → propunere), regula parantezei, prețul relativ recitit, plus
proprietățile pasului 3a pe cele 5 pachete: I7 (doar `explicit` poate fi dur), I23 (`inferred` nu
produce propunere), I20 (determinism) și I6 pe lanțul `check_changes → to_delta → reduce_all`, prin
reducerul de azi, neatins. Zero model, zero DB."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.delta import RankingSignal, to_delta
from src.conversation.interpretation import ResolvedRef
from src.conversation.needs import NeedVocabulary
from src.conversation.provenance import (
    UserWords,
    check_changes,
    hard_capable,
    need_handles,
    tenant_dimensions,
)
from src.conversation.references import ProductFacts, ReferenceFacts
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_provenance import SOLE, SOLE_VOCAB, ch, interp, ref


def delta_for(*changes, said: str, refs=(), resolved=(), facts=None, handles=(), thread="continue"):
    i = interp(*changes, refs=refs, thread=thread)
    checked = check_changes(
        i, words=UserWords(said), handles=handles, vocab=SOLE_VOCAB, pack=SOLE, locale="ro"
    )
    return to_delta(
        i,
        checked,
        resolved,
        facts,
        handles=handles,
        needs=NeedVocabulary.from_pack(SOLE),
        turn_id="t1",
    )


def only(delta) -> StateUpdateProposal:
    [p] = delta.proposals
    return p


# --- tabelul de traducere ------------------------------------------------------------------------


def test_an_explicit_value_becomes_a_need_from_the_user():
    p = only(delta_for(ch(dimension="concerns", value="redness", quote="roșeața"), said="roșeața"))
    assert (p.op, p.key, p.value, p.source, p.strength) == (
        "set_need",
        "concerns",
        "redness",
        "user_explicit",
        "soft",
    )


def test_an_implicit_value_is_a_soft_user_implicit_need():
    p = only(
        delta_for(ch(dimension="skin_type", value="dry", quote="se usucă"), said="se usucă pielea")
    )
    assert (p.source, p.strength) == ("user_implicit", "soft")


def test_i23_an_inferred_change_is_only_a_ranking_signal():
    d = delta_for(ch(dimension="concerns", value="redness", quote="roșeață"), said="ce recomanzi?")
    assert d.proposals == ()
    assert d.ranking == (RankingSignal("concerns", "redness", "eq"),)


@pytest.mark.parametrize(
    ("relation", "keys"),
    [("lte", ["budget_max"]), ("gte", ["budget_min"]), ("eq", ["budget_max", "budget_min"])],
)
def test_a_price_bound_lands_on_the_budget_keys(relation, keys):
    quote = {"lte": "sub 100 lei", "gte": "minim 100 lei", "eq": "exact 100 lei"}[relation]
    d = delta_for(
        ch(dimension="price", relation=relation, number=100, unit="lei", quote=quote), said=quote
    )
    assert [p.key for p in d.proposals] == keys
    assert all(p.value == 100.0 and p.strength == "hard" for p in d.proposals)


def test_a_category_is_a_topic_proposal_and_goes_first():
    """Raftul e subiectul, deci se aplică ÎNAINTEA celorlalte schimbări (ordinea din contract),
    chiar dacă modelul l-a scris al doilea."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            **SOLE_VOCAB.dimensions,
            "category": (
                VocabEntry("ten-ingrijirea-tenului", "Ingrijirea tenului", 900, "ten/ingrijire"),
            ),
        },
    )
    i = interp(
        ch(dimension="concerns", value="redness", quote="roșeață"),
        ch(dimension="category", value="ten-ingrijirea-tenului", quote="îngrijirea tenului"),
    )
    said = "ceva de îngrijirea tenului pentru roșeață"
    checked = check_changes(i, words=UserWords(said), vocab=vocab, pack=SOLE, locale="ro")
    d = to_delta(i, checked, needs=NeedVocabulary.from_pack(SOLE), turn_id="t")
    assert [p.op for p in d.proposals] == ["set_topic", "set_need"]
    assert d.proposals[0].category_key == "ten-ingrijirea-tenului"
    assert d.proposals[0].source == "user_explicit"


def test_avoid_on_a_facet_without_its_own_exclusion_is_a_restriction():
    p = only(
        delta_for(
            ch(dimension="texture", relation="avoid", value="greasy", quote="să nu fie gras"),
            said="să nu fie gras",
        )
    )
    assert (p.op, p.key, p.value) == ("set_need", "restriction", "greasy")


def test_remove_and_replace_act_on_the_handle():
    handles = need_handles(
        ConversationStateV2().needs
        + reduce_all(
            ConversationStateV2(),
            [
                StateUpdateProposal(
                    "set_need", key="concerns", value="redness", source="user_explicit"
                )
            ],
            ReducerPolicy(vocabulary=NeedVocabulary.from_pack(SOLE)),
        ).state.needs
    )
    removed = only(
        delta_for(
            ch(op="remove", target="c1", quote="nu mai contează roșeața"),
            said="nu mai contează roșeața",
            handles=handles,
        )
    )
    assert (removed.op, removed.key, removed.value) == ("revoke", "concerns", "redness")
    replaced = only(
        delta_for(
            ch(op="replace", target="c1", value="hydration", quote="hidratare"),
            said="de fapt vreau hidratare",
            handles=handles,
        )
    )
    assert (replaced.op, replaced.key, replaced.value) == ("supersede", "concerns", "hydration")


def test_clear_topic_and_clear_all_are_their_own_proposals():
    d = delta_for(
        ch(op="clear", target="topic", quote="hai să o luăm de la capăt"),
        ch(op="clear", target="all", quote="uită tot"),
        said="hai să o luăm de la capăt, uită tot",
    )
    assert [p.op for p in d.proposals] == ["clear_topic", "clear_all"]


def test_an_unmapped_signal_is_never_hard():
    i = interp(ch(dimension="unmapped", value="gaming", quote="gaming"))
    checked = check_changes(i, words=UserWords("gaming"), pack=fc.pack("electronics"), locale="ro")
    p = only(to_delta(i, checked, turn_id="t"))
    assert (p.key, p.strength) == ("unmapped", "soft")


def test_a_relative_bound_is_computed_from_the_reread_price():
    change = ch(dimension="price", relation="lte", relative_to="r1", quote="mai ieftin decât ăsta")
    exact = ResolvedRef(
        ref_id="r1",
        kind="deictic",
        outcome="exact",
        product_ids=["p1"],
        source="shown_now",
        reason="page_deictic",
    )
    facts = ReferenceFacts(products={"p1": ProductFacts("p1", "Crema", 89.9, True)})
    p = only(
        delta_for(
            change,
            said="ai ceva mai ieftin decât ăsta?",
            refs=[ref("r1")],
            resolved=[exact],
            facts=facts,
        )
    )
    assert (p.key, p.value, p.strength) == ("budget_max", 89.9, "hard")


def test_a_relative_bound_on_an_ambiguous_target_is_rejected_not_guessed():
    change = ch(dimension="price", relation="lte", relative_to="r1", quote="mai ieftin decât ăsta")
    ambiguous = ResolvedRef(
        ref_id="r1",
        kind="deictic",
        outcome="ambiguous",
        product_ids=["p1", "p2"],
        source="shown_now",
        reason="no_anchor",
    )
    d = delta_for(change, said="mai ieftin decât ăsta", refs=[ref("r1")], resolved=[ambiguous])
    assert d.proposals == ()
    assert [c.rejected for c in d.rejected] == ["unknown_reference"]


# --- thread și contoare --------------------------------------------------------------------------


def test_an_aside_with_changes_is_treated_as_continue_and_counted():
    d = delta_for(
        ch(dimension="concerns", value="redness", quote="roșeața"), said="roșeața", thread="aside"
    )
    assert d.thread == "continue" and d.counters["aside_with_changes"] == 1
    assert len(d.proposals) == 1


def test_a_pure_aside_proposes_nothing():
    d = delta_for(said="cât durează livrarea?", thread="aside")
    assert (d.thread, d.proposals, d.ranking) == ("aside", (), ())


def test_truncation_is_counted():
    changes = [ch(dimension="concerns", value="redness", quote="roșeața") for _ in range(12)]
    d = delta_for(*changes, said="roșeața")
    assert d.counters["interpretation_truncated"] == 2
    assert [c.rejected for c in d.rejected] == ["truncated", "truncated"]


# --- I6 pe lanț, prin reducerul de azi -----------------------------------------------------------


def _revoked_redness() -> ConversationStateV2:
    policy = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(SOLE))
    s = reduce_all(
        ConversationStateV2(),
        [StateUpdateProposal("set_need", key="concerns", value="redness", source="user_explicit")],
        policy,
    ).state
    return reduce_all(
        s,
        [StateUpdateProposal("revoke", key="concerns", source="user_explicit")],
        policy,
    ).state


@pytest.mark.parametrize(
    ("said", "quote", "revived"),
    [
        ("am din nou roșeață", "roșeață", True),  # explicit ⇒ o poate învia
        ("mi s-a înroșit iar fața", "mi s-a înroșit", False),  # implicit ⇒ nu
    ],
)
def test_i6_only_explicit_evidence_revives_a_revoked_need(said, quote, revived):
    state = _revoked_redness()
    d = delta_for(ch(dimension="concerns", value="redness", quote=quote), said=said)
    out = reduce_all(state, d.proposals, ReducerPolicy(vocabulary=NeedVocabulary.from_pack(SOLE)))
    active = [n for n in out.state.needs if n.is_active and n.key == "concerns"]
    assert bool(active) is revived
    if not revived:
        assert [r.reason for r in out.rejected] == ["revoked_key"]


# --- proprietăți pe cele 5 pachete ---------------------------------------------------------------

PACKS = ("electronics", "fashion", "furniture", "gifts", "sole-ro")
_FILLER = ("vreau", "ceva", "pentru", "nu", "sub", "minim", "mai", "ieftin", "decat", "asta", "bun")


def _vocab(name: str):
    return SOLE_VOCAB if name == "sole-ro" else fc.vocabulary(name)


def _values(name: str) -> list[str]:
    vocab = _vocab(name)
    return [e.key for dim in vocab.facet_names for e in vocab.entries(dim)] or ["x"]


@st.composite
def turns(draw):
    name = draw(st.sampled_from(PACKS))
    pack = fc.pack(name)
    dims = sorted(tenant_dimensions(pack) - {"category"})
    words = [*_values(name), *_FILLER, "100", "256"]
    said = draw(st.lists(st.sampled_from(words), min_size=1, max_size=8))
    changes = []
    for _ in range(draw(st.integers(0, 4))):
        numeric = draw(st.booleans())
        start = draw(st.integers(0, len(said) - 1))
        end = draw(st.integers(start + 1, len(said)))
        quote = " ".join(said[start:end]) if draw(st.booleans()) else "spus doar de bot"
        changes.append(
            ch(
                op=draw(st.sampled_from(["set", "add"])),
                dimension=draw(st.sampled_from(dims)),
                relation=draw(st.sampled_from([None, "eq", "contains", "avoid", "lte", "gte"])),
                value=None if numeric else draw(st.sampled_from(words)),
                number=draw(st.sampled_from([100, 256, 3])) if numeric else None,
                unit=draw(st.sampled_from([None, "lei", "gb", "locuri", "ml"]))
                if numeric
                else None,
                quote=quote,
            )
        )
    return (
        name,
        pack,
        " ".join(said),
        interp(*changes, thread=draw(st.sampled_from(["continue", "aside"]))),
    )


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(turns())
def test_properties_i7_i23_i20_on_every_pack(turn):
    name, pack, said, i = turn
    words = UserWords(said)
    kwargs = {"words": words, "vocab": _vocab(name), "pack": pack, "locale": "ro"}
    checked = check_changes(i, **kwargs)
    needs = NeedVocabulary.from_pack(pack)

    for c in checked:
        # I7 + I8: doar `explicit` pe o dimensiune hard-capable poate fi dur.
        if c.strength == "hard":
            assert c.provenance == "explicit" and hard_capable(c.dimension, pack), c
        if c.provenance == "inferred" and c.rejected is None:
            assert c.strength == "ranking", c

    delta = to_delta(i, checked, needs=needs, turn_id="t")
    # I23: nicio propunere nu vine dintr-o inferență.
    assert all(p.source in ("user_explicit", "user_implicit") for p in delta.proposals)
    # I7 pe propuneri: dur doar de la client, explicit.
    assert all(p.source == "user_explicit" for p in delta.proposals if p.strength == "hard")
    assert all(p.key != "unmapped" or p.strength != "hard" for p in delta.proposals)
    # I20 (partea 3a): determinism, byte cu byte.
    assert check_changes(i, **kwargs) == checked
    assert to_delta(i, checked, needs=needs, turn_id="t") == delta
