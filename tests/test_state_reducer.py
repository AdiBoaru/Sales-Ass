"""NX-235 — reducerul: tabelul de operații, precedența și invariantele care nu se negociază.

Testele sunt scrise pe COMPORTAMENT, nu pe implementare: fiecare rând din failure matrix a
cardului are aici un test care ar pica dacă regula s-ar pierde într-un refactor. Cele mai
importante sunt cele NEGATIVE — ce NU are voie să se întâmple:

  • un fapt revocat nu revine din istoric/rezumat;
  • modelul nu promovează o inferență la `hard` și nu rescrie un `hard` existent;
  • siguranța nu cade la un topic switch;
  • aceeași cheie nu se re-întreabă la infinit.
"""

import pytest

from src.conversation.needs import NeedVocabulary, value_fingerprint
from src.conversation.state_reducer import (
    ALLOWED_OPS,
    REJECT_REASONS,
    ReducerPolicy,
    RejectedUpdate,
    StateUpdateProposal,
    reduce,
    reduce_all,
)
from src.conversation.state_v2 import ConversationStateV2, Need, adapt_v1

VOCAB = NeedVocabulary.from_pack(None)
POLICY = ReducerPolicy(vocabulary=VOCAB)


def P(op: str, **kw) -> StateUpdateProposal:
    return StateUpdateProposal(op, **kw)  # type: ignore[arg-type]


def user(op: str, **kw) -> StateUpdateProposal:
    return P(op, source="user_explicit", **kw)


def _state(*proposals: StateUpdateProposal, base: ConversationStateV2 | None = None):
    return reduce_all(base or ConversationStateV2(), list(proposals), POLICY).state


# ── Tabelul de operații ──────────────────────────────────────────────────────


def test_every_declared_op_has_a_handler():
    for op in ALLOWED_OPS:
        result = reduce(
            ConversationStateV2(), P(op, key="budget_max", value=1, category_key="x"), POLICY
        )
        assert not (isinstance(result, RejectedUpdate) and result.reason == "unknown_op")


def test_an_op_outside_the_allowlist_is_rejected_typed_not_raised():
    result = reduce(ConversationStateV2(), P("delete_everything"), POLICY)
    assert isinstance(result, RejectedUpdate) and result.reason == "unknown_op"


def test_all_reject_reasons_stay_in_the_closed_vocabulary():
    cases = [
        P("nope"),
        P("set_need", key="inventat_de_model", value="x"),
        P("set_need", key="category_key", value="x"),
        P("set_references"),
        P("set_topic"),
    ]
    for proposal in cases:
        result = reduce(ConversationStateV2(), proposal, POLICY)
        if isinstance(result, RejectedUpdate):
            assert result.reason in REJECT_REASONS


def test_an_unknown_key_never_enters_memory():
    result = reduce(
        ConversationStateV2(), user("set_need", key="culoarea_preferata_a_mamei", value="x"), POLICY
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "unknown_key"


def test_the_topic_is_not_a_need():
    result = reduce(
        ConversationStateV2(), user("set_need", key="category_key", value="seruri"), POLICY
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "topic_key"


# ── Tărie: ce poate și ce nu poate modelul (D7) ──────────────────────────────


def test_a_model_inference_never_becomes_hard():
    state = _state(
        P("set_need", key="budget_max", value=200, strength="hard", source="model_inferred")
    )
    assert state.need_for("budget_max").strength == "soft"


def test_a_model_cannot_replace_a_hard_constraint():
    base = _state(user("set_need", key="budget_max", value=100))
    result = reduce(
        base, P("set_need", key="budget_max", value=900, source="model_inferred"), POLICY
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "hard_downgrade"
    assert base.need_for("budget_max").normalized_value == 100.0


def test_the_client_can_correct_their_own_hard_constraint():
    base = _state(user("set_need", key="budget_max", value=100))
    state = _state(user("set_need", key="budget_max", value=250), base=base)
    assert state.need_for("budget_max").normalized_value == 250.0


def test_a_supersede_from_the_model_is_refused():
    base = _state(user("set_need", key="brand", value="petala"))
    result = reduce(base, P("supersede", key="brand", value="alt", source="model_inferred"), POLICY)
    assert isinstance(result, RejectedUpdate)


# ── Revocare + tombstone (bucla pe care o închide cardul) ────────────────────


def test_a_revoked_preference_does_not_come_back_from_the_summary():
    """Rândul 1 din failure matrix + pasul 1 din Codex Attack Plan."""
    base = _state(user("set_need", key="budget_max", value=100))
    revoked = _state(user("revoke", key="budget_max"), base=base)
    assert "budget_max" in revoked.revoked_keys()

    # Rezumatul/istoricul re-afirmă faptul → REFUZAT.
    result = reduce(
        revoked, P("set_need", key="budget_max", value=100, source="model_inferred"), POLICY
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "revoked_key"
    assert revoked.need_for("budget_max") is None


def test_only_the_client_can_take_back_their_own_revocation():
    base = _state(user("set_need", key="budget_max", value=100), user("revoke", key="budget_max"))
    again = _state(user("set_need", key="budget_max", value=180), base=base)
    assert again.need_for("budget_max").normalized_value == 180.0
    assert "budget_max" not in again.revoked_keys()


def test_a_correction_leaves_a_tombstone_with_a_fingerprint_not_the_value():
    base = _state(user("set_need", key="brand", value="petala"))
    state = _state(user("set_need", key="brand", value="alba"), base=base)
    tombstone = next(r for r in state.revocations if r.key == "brand")
    assert tombstone.reason_code == "superseded"
    assert tombstone.prior_value_fingerprint == value_fingerprint("petala")
    assert state.need_for("brand").normalized_value == "alba"
    # Vechea valoare rămâne în stare doar ca `superseded` — nu mai e activă nicăieri.
    assert [n.status for n in state.needs if n.key == "brand"].count("active") == 1


def test_revoking_something_never_stored_still_leaves_a_tombstone():
    """Faptul putea trăi doar în filtrele turului. Fără tombstone, ar reveni la turul următor."""
    state = _state(user("revoke", key="budget_max"))
    assert "budget_max" in state.revoked_keys()


# ── Siguranță (NX-173) ───────────────────────────────────────────────────────


def _safety_state() -> ConversationStateV2:
    return ConversationStateV2(
        needs=(
            Need(
                key="restriction",
                operator="not_contains",
                normalized_value="retinol",
                strength="hard",
                source="policy",
                sensitive_class="health",
            ),
        )
    )


def test_safety_cannot_be_revoked_by_the_model():
    result = reduce(
        _safety_state(), P("revoke", key="restriction", source="model_inferred"), POLICY
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "safety_immutable"


def test_safety_cannot_be_overwritten_by_the_model():
    result = reduce(
        _safety_state(),
        P("set_need", key="restriction", value="parfum", source="model_inferred"),
        POLICY,
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "safety_immutable"


def test_safety_survives_a_topic_switch():
    base = ConversationStateV2(
        topic=_state(user("set_topic", category_key="seruri")).topic,
        needs=_safety_state().needs,
    )
    state = _state(user("set_topic", category_key="laptopuri"), base=base)
    assert state.need_for("restriction") is not None


def test_only_an_explicit_client_revocation_can_clear_safety():
    state = _state(
        user("revoke", key="restriction", reason_code="user_explicit"), base=_safety_state()
    )
    assert state.need_for("restriction") is None


def test_a_sensitive_fact_is_not_persisted_without_consent():
    result = reduce(
        ConversationStateV2(),
        user("set_need", key="restriction", value="lactoza", sensitive_class="health"),
        POLICY,
    )
    assert isinstance(result, RejectedUpdate) and result.reason == "sensitive_no_consent"

    with_consent = ReducerPolicy(vocabulary=VOCAB, sensitive_consent=True)
    ok = reduce(
        ConversationStateV2(),
        user("set_need", key="restriction", value="lactoza", sensitive_class="health"),
        with_consent,
    )
    assert isinstance(ok, ConversationStateV2) and ok.need_for("restriction") is not None


# ── Topic switch: se resetează doar ce e legat de subiect ────────────────────


def test_a_topic_switch_resets_scoped_needs_only():
    base = _state(
        user("set_topic", category_key="skincare"),
        user("set_need", key="budget_max", value=200),
        user("set_need", key="size", value="m"),
    )
    assert base.need_for("budget_max") is not None
    state = _state(user("set_topic", category_key="laptopuri"), base=base)
    assert state.need_for("budget_max") is None  # un buget de skincare nu plafonează un laptop
    assert state.need_for("size").normalized_value == "m"  # mărimea e despre OM
    assert any(r.reason_code == "topic_reset" for r in state.revocations)


def test_the_first_topic_anchor_resets_nothing():
    base = _state(user("set_need", key="budget_max", value=200))
    state = _state(user("set_topic", category_key="skincare"), base=base)
    assert state.need_for("budget_max") is not None


def test_the_same_topic_twice_is_idempotent():
    base = _state(
        user("set_topic", category_key="skincare"), user("set_need", key="brand", value="x")
    )
    state = _state(user("set_topic", category_key="skincare"), base=base)
    assert state.need_for("brand") is not None
    assert not any(r.reason_code == "topic_reset" for r in state.revocations)


def test_a_topic_switch_clears_a_pending_question_about_the_old_subject():
    base = _state(
        user("set_topic", category_key="skincare"),
        P("set_pending_question", key="budget_max", source="policy"),
    )
    assert base.pending_clarification is not None
    state = _state(user("set_topic", category_key="laptopuri"), base=base)
    assert state.pending_clarification is None


# ── Clarificare: maximum una, fără buclă ─────────────────────────────────────


def test_only_one_question_can_be_pending():
    base = _state(P("set_pending_question", key="budget_max", source="policy"))
    result = reduce(base, P("set_pending_question", key="brand", source="policy"), POLICY)
    assert isinstance(result, RejectedUpdate) and result.reason == "already_pending"


def test_a_question_expires_after_its_window():
    base = _state(
        P("set_pending_question", key="budget_max", source="policy", expires_after_turns=1)
    )
    later = reduce_all(
        base,
        [P("set_pending_question", key="brand", source="policy")],
        POLICY,
        revision=base.revision + 5,
    )
    assert later.state.pending_clarification.target_key == "brand"


def test_the_same_key_is_not_asked_forever():
    state = ConversationStateV2()
    for _ in range(POLICY.max_clarification_attempts):
        state = _state(P("set_pending_question", key="budget_max", source="policy"), base=state)
        state = _state(user("resolve_question", key="budget_max"), base=state)
    result = reduce(state, P("set_pending_question", key="budget_max", source="policy"), POLICY)
    assert isinstance(result, RejectedUpdate) and result.reason == "already_asked"


def test_resolving_a_question_records_it_even_when_the_answer_is_useless():
    state = _state(
        P("set_pending_question", key="budget_max", source="policy"),
        user("resolve_question", key="budget_max"),
        user("set_need", key="budget_max", value="nu stiu"),
    )
    assert state.pending_clarification is None
    assert state.asked("budget_max") is not None
    assert state.need_for("budget_max") is None  # „nu știu" nu e un buget


# ── Referințe + revizie ──────────────────────────────────────────────────────


def test_a_new_displayed_list_bumps_the_revision():
    first = reduce_all(
        ConversationStateV2(),
        [P("set_references", payload={"displayed_products": [{"product_id": "p1", "name": "A"}]})],
        POLICY,
        revision=5,
    ).state
    assert first.references.displayed_revision == 5
    same = reduce_all(
        first,
        [P("set_references", payload={"displayed_products": [{"product_id": "p1", "name": "A"}]})],
        POLICY,
        revision=6,
    ).state
    assert same.references.displayed_revision == 5  # aceeași listă → aceeași revizie
    changed = reduce_all(
        same,
        [P("set_references", payload={"displayed_products": [{"product_id": "p9", "name": "Z"}]})],
        POLICY,
        revision=7,
    ).state
    assert changed.references.displayed_revision == 7


def test_the_cart_reference_carries_no_lines_or_prices():
    state = _state(
        P("set_cart_ref", payload={"ref": "c1", "version": 2, "lines": [{"price": 99.0}]})
    )
    assert state.cart_ref == {"ref": "c1", "version": 2}


# ── Proprietăți: determinism, idempotență, ordine, caps ──────────────────────


def test_the_reducer_is_deterministic():
    proposals = [
        user("set_topic", category_key="skincare"),
        user("set_need", key="budget_max", value=150),
        user("set_need", key="concerns", value="acnee"),
    ]
    a = reduce_all(ConversationStateV2(), proposals, POLICY, revision=4).state
    b = reduce_all(ConversationStateV2(), proposals, POLICY, revision=4).state
    assert a.to_jsonb() == b.to_jsonb()


def test_reapplying_the_same_batch_is_idempotent_on_the_facts():
    proposals = [
        user("set_need", key="budget_max", value=150),
        user("set_need", key="concerns", value="acnee"),
    ]
    once = reduce_all(ConversationStateV2(), proposals, POLICY, revision=4).state
    twice = reduce_all(once, proposals, POLICY, revision=5).state
    assert {(n.key, n.normalized_value) for n in twice.active_needs()} == {
        (n.key, n.normalized_value) for n in once.active_needs()
    }
    assert twice.revocations == ()  # o reafirmare identică nu e o corecție


def test_independent_operations_commute():
    a = reduce_all(
        ConversationStateV2(),
        [user("set_need", key="budget_max", value=150), user("set_need", key="size", value="m")],
        POLICY,
        revision=3,
    ).state
    b = reduce_all(
        ConversationStateV2(),
        [user("set_need", key="size", value="m"), user("set_need", key="budget_max", value=150)],
        POLICY,
        revision=3,
    ).state
    assert {(n.key, n.normalized_value) for n in a.active_needs()} == {
        (n.key, n.normalized_value) for n in b.active_needs()
    }


def test_a_batch_never_exceeds_the_caps():
    proposals = [user("set_need", key="concerns", value=f"c{i}") for i in range(60)]
    state = reduce_all(ConversationStateV2(), proposals, POLICY).state
    assert len(state.needs) <= 16


def test_a_conflict_reapply_derives_the_same_facts_on_a_fresh_state():
    """Rândul „optimistic revision conflict" din failure matrix: re-aplicăm, nu re-cerem."""
    proposals = [
        user("set_topic", category_key="skincare"),
        user("set_need", key="budget_max", value=150),
    ]
    fresh = adapt_v1({"search_constraints": {"brand": "altcineva"}}, VOCAB)
    retried = reduce_all(fresh, proposals, POLICY, revision=fresh.revision + 1).state
    assert retried.need_for("budget_max").normalized_value == 150.0
    assert retried.need_for("brand").normalized_value == "altcineva"  # scrierea concurentă rămâne


@pytest.mark.parametrize("payload", ["nu-i dict", 42, ["a"]])
def test_an_invalid_payload_is_a_typed_rejection(payload):
    result = reduce(ConversationStateV2(), P("set_references", payload=payload), POLICY)
    assert isinstance(result, RejectedUpdate) and result.reason == "invalid_payload"


def test_rejections_carry_no_values_only_keys_and_reasons():
    result = reduce(
        ConversationStateV2(),
        P("set_need", key="secret_key_0722123456", value="0722123456"),
        POLICY,
    )
    assert isinstance(result, RejectedUpdate)
    assert "0722123456" not in (result.key or "").replace("secret_key_0722123456", "")
    assert result.reason in REJECT_REASONS
