"""NX-235 — contractul `ConversationStateV2`: schemă, caps, adapter v1→v2 și proiecție înapoi.

Ce apără testele de aici nu e forma dataclass-urilor, ci trei promisiuni:

  1. **Adapterul e conservator.** Un rând vechi nu se transformă în fapte hard. Ce nu se
     normalizează curat devine `unknown` — fiindcă `UNKNOWN != MISMATCH`, iar a inventa un fapt
     dintr-o propoziție e mai rău decât a nu ști.
  2. **Round-trip-ul e sigur în ambele sensuri.** Un rând scris v2 e citit corect de codul v1
     (proiecție), altfel rollback-ul ar însemna pierdere de memorie.
  3. **Starea nu poartă text brut.** Un răspuns liber („pentru sora mea, are tenul mixt") nu
     ajunge valoare de stare, oricâte drumuri ar încerca să-l ducă acolo.
"""

import json

import pytest

from src.conversation.needs import NeedVocabulary
from src.conversation.state_v2 import (
    MAX_NEEDS,
    MAX_STATE_BYTES,
    STATE_SCHEMA_VERSION,
    ConversationStateV2,
    DisplayedRef,
    Need,
    Revocation,
    adapt_v1,
    enforce_caps,
    hydrate_state_v2,
    is_v2,
    project_v1,
    serialize,
    size_bucket,
    state_diff,
)
from src.models import ConversationState

VOCAB = NeedVocabulary.from_pack(None)

FREE_TEXT = "pentru sora mea care are tenul mixt si vrea ceva ieftin"


def _v1(**overrides) -> dict:
    base = {
        "displayed_products": [{"product_id": "p1", "name": "Ser A", "price": 89.0}],
        "search_constraints": {
            "budget_max": 150,
            "concerns": ["acnee", "pori"],
            "brand": "petala",
            "category_key": "seruri",
        },
        "constraints": {"recipient": "sora"},
        "asked_intents": ["budget_max"],
        "pending_question": {"field": "budget", "resume_route": "sales", "attempts": 1},
        "cart": [{"product_id": "p1", "name": "Ser A", "price": 89.0, "quantity": 1}],
        "safety": {"contexts": ["pregnancy"], "source": "user"},
    }
    base.update(overrides)
    return base


# ── Adapter v1 → v2 ──────────────────────────────────────────────────────────


def test_a_legacy_row_becomes_typed_needs_without_inventing_anything():
    state = adapt_v1(_v1(), VOCAB)
    by_key = {n.key: n for n in state.needs}
    assert by_key["budget_max"].normalized_value == 150.0
    assert by_key["budget_max"].strength == "hard"  # o limită e inviolabilă (D7)
    assert by_key["brand"].strength == "soft"  # o preferință rămâne negociabilă
    assert state.topic.category_key == "seruri"
    # Nicio revocare inventată retroactiv: un rând vechi nu conține dovada unei retrageri.
    assert state.revocations == ()


def test_a_list_key_becomes_one_need_per_canonical_value():
    state = adapt_v1(_v1(), VOCAB)
    concerns = [n.normalized_value for n in state.needs if n.key == "concerns"]
    assert concerns == ["acnee", "pori"]
    assert all(n.operator == "contains" for n in state.needs if n.key == "concerns")


def test_free_text_never_becomes_a_fact():
    """Cazul care justifică poarta: `clarify` scria răspunsul BRUT ca valoare de stare."""
    state = adapt_v1(_v1(constraints={"budget": FREE_TEXT}, search_constraints={}), VOCAB)
    budget = next(n for n in state.needs if n.key == "budget_max")
    assert budget.normalized_value is None
    assert budget.status == "unknown" and budget.strength == "soft"
    assert FREE_TEXT not in json.dumps(state.to_jsonb(), ensure_ascii=False)


def test_unknown_is_not_a_mismatch():
    state = adapt_v1(_v1(constraints={"brand": FREE_TEXT}, search_constraints={}), VOCAB)
    brand = next(n for n in state.needs if n.key == "brand")
    assert brand.status == "unknown"
    assert brand not in state.active_needs()  # nu filtrează nimic
    assert state.need_for("brand") is None


def test_the_legacy_pending_question_keeps_a_deterministic_id():
    a = adapt_v1(_v1(), VOCAB).pending_clarification
    b = adapt_v1(_v1(), VOCAB).pending_clarification
    assert a is not None and a.target_key == "budget_max"
    assert a.question_id == b.question_id  # fără ceas, fără random → replay stabil


def test_keys_owned_by_other_cards_are_carried_untouched():
    """`cart` e al NX-237, `safety` al NX-173. v2 le cară, nu le rescrie (P3)."""
    state = adapt_v1(_v1(), VOCAB)
    doc = state.to_jsonb()
    assert doc["cart"] == _v1()["cart"]
    assert doc["safety"] == _v1()["safety"]


# ── Proiecția v2 → v1 (back-compat + rollback) ───────────────────────────────


def test_a_v2_row_is_read_correctly_by_v1_code():
    doc = adapt_v1(_v1(), VOCAB).to_jsonb()
    assert is_v2(doc)
    hydrated = ConversationState.from_jsonb(doc)
    assert [p.product_id for p in hydrated.displayed_products] == ["p1"]
    assert hydrated.search_constraints["budget_max"] == 150.0
    assert hydrated.search_constraints["category_key"] == "seruri"
    assert hydrated.pending_question["field"] == "budget_max"
    assert hydrated.cart[0]["product_id"] == "p1"
    assert hydrated.safety["contexts"] == ["pregnancy"]


def test_a_revoked_need_simply_does_not_appear_in_the_v1_projection():
    state = adapt_v1(_v1(), VOCAB)
    revoked = ConversationStateV2(
        needs=tuple(
            n if n.key != "budget_max" else Need(**{**n.to_jsonb(), "status": "revoked"})
            for n in state.needs
        ),
        revocations=(Revocation(key="budget_max"),),
        topic=state.topic,
    )
    projected = project_v1(revoked)
    assert "budget_max" not in projected["search_constraints"]
    assert (
        ConversationState.from_jsonb(revoked.to_jsonb()).search_constraints.get("budget_max")
        is None
    )


def test_an_unknown_key_in_a_v2_document_is_carried_not_dropped():
    """O cheie scrisă de o cale adăugată DUPĂ cardul ăsta e aproape sigur date reale ale altui
    card. Un format care le pierde tăcut transformă aprinderea unui flag într-o pierdere de date
    pe care nimeni n-o observă."""
    doc = {**adapt_v1(_v1(), VOCAB).to_jsonb(), "experiment_x": {"seen": 3}}
    back = ConversationStateV2.from_jsonb(doc)
    assert back.passthrough["experiment_x"] == {"seen": 3}
    assert back.to_jsonb()["experiment_x"] == {"seen": 3}
    assert project_v1(back)["experiment_x"] == {"seen": 3}


def test_v1_rows_are_untouched_by_the_v2_reader():
    raw = _v1()
    assert not is_v2(raw)
    assert ConversationState.from_jsonb(raw).search_constraints["budget_max"] == 150


# ── Hidratare defensivă ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        [],
        "junk",
        {"schema_version": 2, "needs": "nope"},
        {"schema_version": 2, "needs": [1, 2]},
    ],
)
def test_a_corrupt_row_degrades_memory_not_the_turn(raw):
    state = hydrate_state_v2(raw, VOCAB)
    assert isinstance(state, ConversationStateV2)
    assert state.active_needs() == ()


def test_a_need_with_a_container_value_is_dropped_not_stored():
    doc = {"schema_version": 2, "needs": [{"key": "brand", "normalized_value": {"a": [1] * 500}}]}
    state = hydrate_state_v2(doc, VOCAB)
    assert state.needs[0].normalized_value is None


def test_hydration_uses_the_document_revision_as_a_floor():
    doc = {"schema_version": 2, "revision": 7}
    assert hydrate_state_v2(doc, VOCAB, revision=3).revision == 7
    assert hydrate_state_v2({}, VOCAB, revision=3).revision == 3


# ── Caps + buget de serializare ──────────────────────────────────────────────


def test_caps_sacrifice_soft_before_hard():
    needs = tuple(
        Need(key="concerns", operator="contains", normalized_value=f"c{i}", updated_revision=i)
        for i in range(MAX_NEEDS + 6)
    ) + (Need(key="budget_max", operator="lte", normalized_value=10.0, strength="hard"),)
    capped = enforce_caps(ConversationStateV2(needs=needs))
    assert len(capped.needs) <= MAX_NEEDS
    assert any(n.strength == "hard" for n in capped.needs)


def test_a_sensitive_need_survives_the_cap():
    needs = tuple(
        Need(key="concerns", operator="contains", normalized_value=f"c{i}", updated_revision=i)
        for i in range(MAX_NEEDS + 4)
    ) + (
        Need(
            key="restriction",
            operator="not_contains",
            normalized_value="x",
            sensitive_class="health",
        ),
    )
    capped = enforce_caps(ConversationStateV2(needs=needs))
    assert any(n.sensitive_class == "health" for n in capped.needs)


def test_an_oversized_state_degrades_instead_of_failing_the_write():
    """Un `cart` corupt/importat nu are voie să facă UPDATE-ul să pice: am pierde RĂSPUNSUL din
    cauza memoriei. Coșul (NX-237) e ultimul sacrificat, dar E sacrificat — vizibil."""
    huge = ConversationStateV2(
        passthrough={
            "cart": [{"note": "x" * 400} for _ in range(40)],
            "safety": {"contexts": ["pregnancy"]},
        },
        needs=(Need(key="budget_max", operator="lte", normalized_value=1.0, strength="hard"),),
    )
    doc, size, degraded = serialize(huge)
    assert degraded is True and size <= MAX_STATE_BYTES
    assert doc["schema_version"] == STATE_SCHEMA_VERSION
    assert "cart" not in doc
    assert doc["safety"] == {"contexts": ["pregnancy"]}  # gate P0 — pleacă ultimul


def test_the_hard_needs_outlive_everything_except_the_budget_itself():
    huge = ConversationStateV2(
        needs=(
            Need(key="budget_max", operator="lte", normalized_value=1.0, strength="hard"),
            *(
                Need(key="concerns", operator="contains", normalized_value="c" * 40)
                for _ in range(MAX_NEEDS)
            ),
        ),
        passthrough={"cart": [{"note": "x" * 400} for _ in range(40)]},
    )
    doc, size, degraded = serialize(huge)
    assert degraded is True and size <= MAX_STATE_BYTES
    assert any(n["key"] == "budget_max" and n["strength"] == "hard" for n in doc["needs"])


def test_the_code_budget_leaves_headroom_for_the_binary_jsonb():
    """Lungimea textului JSON e un proxy OPTIMIST pentru `pg_column_size` (overhead per intrare).
    Rezerva e ce împiedică un `CheckViolationError` la scriere.

    Aici verificăm doar că rezerva EXISTĂ și acoperă de două ori overhead-ul măsurat (max +12%);
    că e suficientă pe Postgres real o demonstrează
    `test_conversation_state_v2_db.test_the_code_budget_still_covers_the_binary_jsonb_overhead`."""
    from src.conversation.state_v2 import DB_STATE_LIMIT_BYTES

    assert MAX_STATE_BYTES * 1.25 < DB_STATE_LIMIT_BYTES


def test_a_normal_state_is_neither_degraded_nor_oversized():
    doc, size, degraded = serialize(adapt_v1(_v1(), VOCAB))
    assert degraded is False and size < MAX_STATE_BYTES
    assert size_bucket(size) in {"0-1k", "1-2k", "2-4k", "4-6k"}


@pytest.mark.parametrize(
    "size,bucket",
    [(0, "0-1k"), (1024, "0-1k"), (1500, "1-2k"), (3000, "2-4k"), (9000, "over-budget")],
)
def test_size_buckets_are_low_cardinality(size, bucket):
    assert size_bucket(size) == bucket


# ── Privacy (P12) ────────────────────────────────────────────────────────────


def test_the_serialized_document_carries_no_raw_utterance():
    raw = _v1(constraints={"recipient": FREE_TEXT, "budget": "am 200 lei pentru Ana Popescu"})
    doc, _, _ = serialize(adapt_v1(raw, VOCAB))
    blob = json.dumps(doc, ensure_ascii=False)
    assert FREE_TEXT not in blob and "Ana Popescu" not in blob


def test_a_tombstone_stores_a_fingerprint_not_the_value():
    from src.conversation.needs import value_fingerprint

    fingerprint = value_fingerprint(150.0)
    assert "150" not in fingerprint and len(fingerprint) == 16
    assert value_fingerprint(150.0) == fingerprint  # determinist (replay)


# ── Diff-ul de shadow ────────────────────────────────────────────────────────


def test_the_shadow_diff_reports_keys_never_values():
    projected = project_v1(adapt_v1(_v1(), VOCAB))
    diff = state_diff(_v1(), projected)
    assert all(isinstance(v, list) for v in diff.values())
    assert "150" not in json.dumps(diff)


def test_a_clean_adaptation_shows_the_expected_shape_difference():
    """`constraints` v1 ținea slotul brut; proiecția îl dă canonic. Diferența e AȘTEPTATĂ și
    vizibilă — de asta shadow-ul compară chei, nu text."""
    diff = state_diff(_v1(), project_v1(adapt_v1(_v1(), VOCAB)))
    assert "search_constraints_missing" not in diff


def test_displayed_refs_survive_the_round_trip():
    state = ConversationStateV2()
    state = ConversationStateV2(
        references=state.references.__class__(
            displayed_products=(DisplayedRef("p1", "A", 10.0), DisplayedRef("p2", "B", None))
        )
    )
    back = ConversationStateV2.from_jsonb(state.to_jsonb())
    assert [d.product_id for d in back.references.displayed_products] == ["p1", "p2"]
    assert back.references.displayed_products[1].price is None
