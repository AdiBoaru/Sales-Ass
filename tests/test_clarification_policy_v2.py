"""NX-235 — „merită întrebat?" ca decizie măsurată, nu ca reflex.

O clarificare costă un tur întreg din răbdarea clientului. Testele de aici verifică exact
compromisul cerut de card: întrebăm când răspunsul chiar schimbă setul sigur sau clasamentul,
tăcem (adică răspundem onest) când nu schimbă, și întrebăm ORICUM când e vorba de siguranță.
"""

import pytest

from src.conversation.clarification_policy import (
    ClarificationCandidate,
    ClarificationPolicy,
    decide_clarification,
    estimate_information_gain,
    gain_bucket,
    relaxation_candidates,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import ConversationStateV2

VOCAB = NeedVocabulary.from_pack(None)
POLICY = ClarificationPolicy(vocabulary=VOCAB)
REDUCER = ReducerPolicy(vocabulary=VOCAB)


def _state(*proposals) -> ConversationStateV2:
    return reduce_all(ConversationStateV2(), list(proposals), REDUCER).state


def _cand(key="budget_max", reason="missing_required", partition=()):
    return ClarificationCandidate(key=key, reason=reason, partition=partition)  # type: ignore[arg-type]


# ── Information gain ─────────────────────────────────────────────────────────


def test_an_even_split_is_worth_asking():
    assert estimate_information_gain(10, (5, 5)) == pytest.approx(0.5)


def test_a_question_that_separates_everything_is_the_most_valuable():
    assert estimate_information_gain(10, (1,) * 10) == pytest.approx(0.9)


def test_a_question_whose_answer_changes_nothing_scores_zero():
    assert estimate_information_gain(10, (10,)) == 0.0


def test_no_results_means_no_question():
    """Nu clarificăm în gol: fără candidați, o întrebare nu poate tăia nimic."""
    assert estimate_information_gain(0, (3, 3)) == 0.0


def test_a_single_candidate_is_already_decided():
    assert estimate_information_gain(1, (1,)) == 0.0


def test_options_that_cover_only_part_of_the_set_are_worth_less():
    partial = estimate_information_gain(10, (2, 2))
    full = estimate_information_gain(10, (5, 5))
    assert 0 < partial < full


@pytest.mark.parametrize(
    "gain,bucket", [(0.0, "none"), (0.1, "low"), (0.45, "medium"), (0.9, "high")]
)
def test_gain_buckets_stay_low_cardinality(gain, bucket):
    assert gain_bucket(gain) == bucket


# ── Decizia ──────────────────────────────────────────────────────────────────


def test_a_useful_question_is_asked():
    decision = decide_clarification(
        ConversationStateV2(), [_cand(partition=(4, 4))], total_candidates=8, policy=POLICY
    )
    assert decision.ask is True and decision.gain_bucket == "medium"


def test_a_useless_question_becomes_an_answer():
    decision = decide_clarification(
        ConversationStateV2(), [_cand(partition=(8,))], total_candidates=8, policy=POLICY
    )
    assert decision.ask is False and decision.reason == "low_gain"


def test_with_nothing_found_we_answer_honestly_instead_of_asking():
    decision = decide_clarification(
        ConversationStateV2(), [_cand(partition=(3, 3))], total_candidates=0, policy=POLICY
    )
    assert decision.ask is False and decision.reason == "low_gain"


def test_before_retrieval_the_gain_gate_is_skipped_not_assumed_zero():
    """Triajul rulează înaintea căutării: acolo nu putem estima, deci nu tăcem din prudență."""
    decision = decide_clarification(
        ConversationStateV2(), [_cand()], total_candidates=None, policy=POLICY
    )
    assert decision.ask is True and decision.gain_bucket == "unknown"


def test_we_do_not_ask_what_we_already_know():
    state = _state(
        StateUpdateProposal("set_need", key="budget_max", value=200, source="user_explicit")
    )
    decision = decide_clarification(state, [_cand()], total_candidates=None, policy=POLICY)
    assert decision.ask is False and decision.reason == "already_known"


def test_the_same_question_is_not_asked_forever():
    state = ConversationStateV2()
    for _ in range(POLICY.max_attempts_per_key):
        state = reduce_all(
            state,
            [
                StateUpdateProposal("set_pending_question", key="brand", source="policy"),
                StateUpdateProposal("resolve_question", key="brand", source="user_explicit"),
            ],
            REDUCER,
        ).state
    decision = decide_clarification(
        state, [_cand(key="brand", partition=(4, 4))], total_candidates=8, policy=POLICY
    )
    assert decision.ask is False and decision.reason == "already_asked"


def test_a_live_pending_question_blocks_a_second_one():
    state = _state(StateUpdateProposal("set_pending_question", key="budget_max", source="policy"))
    decision = decide_clarification(
        state, [_cand(key="brand", partition=(4, 4))], total_candidates=8, policy=POLICY
    )
    assert decision.ask is False and decision.reason == "already_pending"


def test_safety_asks_no_matter_what():
    """Siguranța nu concurează pe UX: trece peste gain, peste `already_asked` și peste pending."""
    state = _state(StateUpdateProposal("set_pending_question", key="budget_max", source="policy"))
    decision = decide_clarification(
        state,
        [_cand(key="restriction", reason="safety", partition=(8,))],
        total_candidates=8,
        policy=POLICY,
    )
    assert decision.ask is True and decision.reason == "safety"


def test_a_hard_conflict_always_asks():
    decision = decide_clarification(
        ConversationStateV2(),
        [_cand(reason="hard_conflict", partition=(8,))],
        total_candidates=8,
        policy=POLICY,
    )
    assert decision.ask is True


def test_safety_wins_over_a_more_informative_commercial_question():
    decision = decide_clarification(
        ConversationStateV2(),
        [_cand(key="brand", partition=(1,) * 8), _cand(key="restriction", reason="safety")],
        total_candidates=8,
        policy=POLICY,
    )
    assert decision.candidate.key == "restriction"


def test_at_most_one_question_comes_out_of_a_batch():
    decision = decide_clarification(
        ConversationStateV2(),
        [_cand(key="brand", partition=(4, 4)), _cand(key="size", partition=(4, 4))],
        total_candidates=8,
        policy=POLICY,
    )
    assert decision.ask is True and decision.candidate is not None


def test_the_choice_is_deterministic_between_equal_candidates():
    candidates = [_cand(key="size", partition=(4, 4)), _cand(key="brand", partition=(4, 4))]
    first = decide_clarification(
        ConversationStateV2(), candidates, total_candidates=8, policy=POLICY
    )
    second = decide_clarification(
        ConversationStateV2(), list(reversed(candidates)), total_candidates=8, policy=POLICY
    )
    assert first.candidate.key == second.candidate.key == "brand"


def test_no_candidate_is_not_a_decision_to_ask():
    assert (
        decide_clarification(ConversationStateV2(), [], total_candidates=5, policy=POLICY).ask
        is False
    )


# ── Relaxare onestă ──────────────────────────────────────────────────────────


def test_only_soft_needs_are_offered_for_relaxation():
    state = _state(
        StateUpdateProposal("set_need", key="budget_max", value=100, source="user_explicit"),
        StateUpdateProposal("set_need", key="brand", value="petala", source="user_explicit"),
    )
    assert relaxation_candidates(state) == ("brand",)


def test_a_conversation_with_only_hard_needs_offers_nothing_to_relax():
    state = _state(
        StateUpdateProposal("set_need", key="budget_max", value=100, source="user_explicit")
    )
    assert relaxation_candidates(state) == ()
