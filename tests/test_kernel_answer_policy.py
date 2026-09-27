"""NX-332 felia 4a-3 — politica de răspuns (I12): niciun verdict între produse pe un atribut decisiv
necunoscut pe vreunul dintre ele.

Dimensiunea decisivă vine dintr-o referință `extreme` a actului sau din ETICHETA DE RÂND a unei
fațete scrisă de client în cerere (date de pachet). Fără etichetă în pachet, nicio dimensiune
ghicită. `UNKNOWN ≠ MISMATCH`: politica spune doar ce lipsește, niciodată că un produs „nu are".
Zero model, zero DB."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.conversation.answer_policy import answer_policy, read_query
from src.conversation.interpretation import (
    Act,
    AmbiguityDecision,
    AnswerPolicy,
    Reference,
    ResolvedRef,
)
from src.conversation.references import ProductFacts
from src.domain.pack import FacetSpec
from tests.kernel import fixture_catalog as fc


def act(kind: str, *targets: str, query: str | None = None) -> Act:
    return Act(kind=kind, targets=list(targets), query=query)


def exact(rid: str, pid: str) -> ResolvedRef:
    return ResolvedRef(
        ref_id=rid,
        kind="ordinal",
        outcome="exact",
        product_ids=[pid],
        source="shown_now",
        reason=None,
    )


def ambiguous(rid: str, *pids: str) -> ResolvedRef:
    return ResolvedRef(
        ref_id=rid,
        kind="name",
        outcome="ambiguous",
        product_ids=list(pids),
        source="shown_now",
        reason="name_shared",
    )


def extreme(rid: str, dimension: str | None, direction: str = "min") -> Reference:
    return Reference(
        id=rid,
        text=rid,
        kind="extreme",
        ordinal=None,
        name=None,
        dimension=dimension,
        value=None,
        direction=direction,  # type: ignore[arg-type]
    )


def facts(name: str, *ids: str) -> dict[str, ProductFacts]:
    """Faptele „după unelte": ale pachetului, doar pentru produsele pe care executorul le-a
    întors."""
    catalog = fc.facts(name).products
    return {pid: catalog[pid] for pid in ids}


def run(a: Act, resolved, known, *, pack="electronics", **kw) -> AnswerPolicy | None:
    return answer_policy(
        a,
        resolved,
        known,
        vocab=fc.vocabulary(pack),
        pack=kw.pop("loaded", None) or fc.pack(pack),
        locale=kw.pop("locale", "ro"),
        **kw,
    )


# --- happy path ----------------------------------------------------------------------------------


def test_cheaper_of_two_with_both_prices_known_allows_a_verdict():
    """«e mai ieftin X sau Y?»: dimensiunea vine din referința `extreme` (prețul)."""
    resolved = [exact("r1", "el-01"), exact("r2", "el-02"), exact("r3", "el-01")]
    policy = run(
        act("compare", "r1", "r2", "r3", query="e mai ieftin primul sau al doilea"),
        resolved,
        facts("electronics", "el-01", "el-02"),
        references=[extreme("r3", "price")],
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])


def test_better_screen_with_the_screen_unknown_on_one_forbids_a_winner():
    """«care e mai bun la ecran, primul sau al doilea?» fără referință `extreme`: eticheta de rând
    „Ecran" a fațetei `screen` numește dimensiunea; ecranul lipsește pe al doilea."""
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(
        known["el-02"],
        attributes={k: v for k, v in known["el-02"].attributes.items() if k != "screen"},
    )
    policy = run(
        act("compare", "r1", "r2", query="care e mai bun la ecran, primul sau al doilea?"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        known,
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["screen"])


def test_the_same_question_with_the_screen_known_on_both_allows_a_verdict():
    policy = run(
        act("compare", "r1", "r2", query="care e mai bun la ecran, primul sau al doilea?"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        facts("electronics", "el-01", "el-02"),
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])


def test_without_the_row_label_in_the_pack_no_dimension_is_guessed():
    loaded = fc.pack("electronics")
    stripped = replace(
        loaded,
        facets=tuple(replace(f, labels={}) if f.key == "screen" else f for f in loaded.facets),
    )
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(known["el-02"], attributes={})
    policy = run(
        act("compare", "r1", "r2", query="care e mai bun la ecran, primul sau al doilea?"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        known,
        loaded=stripped,
    )
    assert policy is None


def test_an_inflected_row_label_still_names_the_dimension():
    evidence = read_query(
        act("compare", query="care are ecranul mai mare"), pack=fc.pack("electronics"), locale="ro"
    )
    assert evidence.dimension == "screen" and evidence.has_words


def test_a_comparison_row_label_of_the_pack_counts_too():
    """Eticheta de rând din `comparison_facets` (FacetSpec) e aceeași dată ca a fațetei tipizate."""
    loaded = replace(
        fc.pack("furniture"),
        facet_labels=(FacetSpec(key="material", labels={"ro": "Material"}),),
    )
    known = facts("furniture", "fu-01", "fu-02")
    known["fu-02"] = replace(known["fu-02"], attributes={"seats": 3})
    policy = run(
        act("compare", "r1", "r2", query="din ce material e fiecare"),
        [exact("r1", "fu-01"), exact("r2", "fu-02")],
        known,
        pack="furniture",
        loaded=loaded,
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["material"])


# --- când nu se aplică ---------------------------------------------------------------------------


def test_a_detail_on_one_product_is_not_a_judgement():
    assert (
        run(
            act("detail", "r1", query="cat de mare e ecranul"),
            [exact("r1", "el-01")],
            facts("electronics", "el-01"),
        )
        is None
    )


def test_a_detail_with_two_candidates_is_judged():
    known = facts("electronics", "el-01", "el-18")
    known["el-18"] = replace(known["el-18"], attributes={})
    policy = run(
        act("detail", "r1", query="care are ecran mai mare"),
        [ambiguous("r1", "el-01", "el-18")],
        known,
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["screen"])


def test_an_act_both_verdict_is_judged_on_any_read():
    both = AmbiguityDecision(verdict="act_both", reason="ambiguous_read", question=None)
    policy = run(
        act("link", "r1", query="linkul la cel cu ecran mai mare"),
        [ambiguous("r1", "el-01", "el-07")],
        facts("electronics", "el-01", "el-07"),
        ambiguity=both,
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])
    assert (
        run(
            act("link", "r1", query="linkul la cel cu ecran mai mare"),
            [ambiguous("r1", "el-01", "el-07")],
            facts("electronics", "el-01", "el-07"),
        )
        is None
    )


def test_a_find_is_never_judged():
    assert run(act("find", query="ecran mare"), [], facts("electronics", "el-01", "el-02")) is None


def test_a_comparison_with_one_target_is_judged_against_the_partner_from_the_executor():
    """Calea `COMPARE_WITH_SIMILAR`: o țintă, partenerul îl NUMEȘTE executorul (`partners`)."""
    known = facts("electronics", "el-01", "el-04")
    policy = run(
        act("compare", "r1", query="e mai bun la ecran"),
        [exact("r1", "el-01")],
        known,
        partners=["el-04"],
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])


def test_the_facts_of_another_act_are_not_candidates():
    """Recenzia NX-332: într-un tur cu mai multe acte, faptele conțin și produsele actului vecin.
    Adăugate ca parteneri, un produs fără `screen` interzicea un verdict corect. Fără parteneri
    numiți, o comparație cu o singură țintă nu are pe cine judeca."""
    known = facts("electronics", "el-01", "el-04")
    policy = run(act("compare", "r1", query="e mai bun la ecran"), [exact("r1", "el-01")], known)
    assert policy is None


def test_a_regional_locale_reads_the_language_labels():
    """Recenzia NX-332: `ro-RO` nu cădea pe etichetele `ro`, deci I12 nu mai rula."""
    policy = run(
        act("compare", "r1", "r2", query="care e mai bun la ecran"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        facts("electronics", "el-01", "el-02"),
        locale="ro-RO",
    )
    assert policy is not None


def test_no_deciding_dimension_means_no_policy():
    assert (
        run(
            act("compare", "r1", "r2", query="care e mai bun"),
            [exact("r1", "el-01"), exact("r2", "el-02")],
            facts("electronics", "el-01", "el-02"),
        )
        is None
    )


# --- dimensiunea din referința `extreme` ---------------------------------------------------------


def test_an_extreme_on_rating_with_an_unknown_rating_forbids_a_winner():
    """Ratingul lipsește pe tot pachetul de fixture: necunoscut, nu zero."""
    policy = run(
        act("compare", "r1", "r2", "r3"),
        [exact("r1", "el-01"), exact("r2", "el-02"), exact("r3", "el-01")],
        facts("electronics", "el-01", "el-02"),
        references=[extreme("r3", "rating", "max")],
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["rating"])


def test_an_extreme_on_a_dimension_the_vocabulary_does_not_know_is_ignored():
    policy = run(
        act("compare", "r1", "r2", "r3"),
        [exact("r1", "el-01"), exact("r2", "el-02"), exact("r3", "el-01")],
        facts("electronics", "el-01", "el-02"),
        references=[extreme("r3", "gaming_score", "max")],
    )
    assert policy is None


def test_the_extreme_wins_over_a_label_in_the_query():
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(known["el-02"], attributes={})
    policy = run(
        act("compare", "r1", "r2", "r3", query="cel mai ieftin, ca ecranul nu conteaza"),
        [exact("r1", "el-01"), exact("r2", "el-02"), exact("r3", "el-01")],
        known,
        references=[extreme("r3", "price")],
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])


def test_a_price_missing_on_one_forbids_a_winner():
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(known["el-02"], price=None)
    policy = run(
        act("compare", "r1", "r2", "r3"),
        [exact("r1", "el-01"), exact("r2", "el-02"), exact("r3", "el-01")],
        known,
        references=[extreme("r3", "price")],
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["price"])


# --- UNKNOWN ≠ MISMATCH --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, "0"])
def test_a_zero_is_a_known_value_not_a_missing_one(value):
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(known["el-02"], attributes={"screen": value})
    policy = run(
        act("compare", "r1", "r2", query="la ecran"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        known,
    )
    assert policy == AnswerPolicy(verdict_allowed=True, missing=[])


def test_a_candidate_the_executor_did_not_return_is_unknown():
    policy = run(
        act("compare", "r1", "r2", query="la ecran"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        facts("electronics", "el-01"),
    )
    assert policy == AnswerPolicy(verdict_allowed=False, missing=["screen"])


def test_the_policy_only_names_what_is_missing():
    """Politica nu poartă nicio afirmație despre produse: doar numele dimensiunii."""
    known = facts("electronics", "el-01", "el-02")
    known["el-02"] = replace(known["el-02"], attributes={})
    policy = run(
        act("compare", "r1", "r2", query="la ecran"),
        [exact("r1", "el-01"), exact("r2", "el-02")],
        known,
    )
    assert set(AnswerPolicy.model_fields) == {"verdict_allowed", "missing"}
    assert policy.missing == ["screen"]


# --- dovada cererii -------------------------------------------------------------------------------


def test_a_query_of_only_function_words_has_no_words():
    evidence = read_query(act("find", query="ceva"), pack=fc.pack("gifts"), locale="ro")
    assert not evidence.has_words and evidence.dimension is None
    assert not read_query(act("find"), pack=fc.pack("gifts"), locale="ro").has_words


def test_without_a_locale_the_labels_of_every_locale_count():
    evidence = read_query(
        act("compare", query="which one has the bigger screen"),
        pack=fc.pack("electronics"),
        locale=None,
    )
    assert evidence.dimension == "screen"


def test_the_category_row_label_is_not_a_deciding_dimension():
    evidence = read_query(
        act("compare", query="din ce categorie e fiecare"), pack=fc.pack("electronics"), locale="ro"
    )
    assert evidence.dimension is None
