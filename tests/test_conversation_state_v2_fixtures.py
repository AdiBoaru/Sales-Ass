"""NX-235 — conversații MULTI-TUR, ca fixture-uri.

Regulile care contează în cardul ăsta nu se văd într-un singur apel: o revocare care nu revine,
un `hard` care nu se relaxează, un topic switch care nu atinge siguranța — toate se manifestă
peste tururi. Fixture-urile din `tests/fixtures/conversation_state_v2/` descriu conversații
întregi (RO/EN/HU), iar testul de aici le rulează prin reducerul REAL.

Un caz nou = un fișier JSON, nu cod. Vezi `README.md` din folderul de fixture-uri pentru schemă.
"""

import json
from pathlib import Path

import pytest

from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import hydrate_state_v2

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "conversation_state_v2").glob("*.json"))
VOCAB = NeedVocabulary.from_pack(None)
POLICY = ReducerPolicy(vocabulary=VOCAB)


def _proposal(raw: dict) -> StateUpdateProposal:
    return StateUpdateProposal(**raw)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_multi_turn_conversation(path: Path):
    case = json.loads(path.read_text(encoding="utf-8"))
    state = hydrate_state_v2(case.get("initial_state"), VOCAB)

    for index, turn in enumerate(case["turns"], start=1):
        reduced = reduce_all(
            state,
            [_proposal(p) for p in turn["proposals"]],
            POLICY,
            revision=state.revision + 1,
        )
        state = reduced.state
        expect = turn.get("expect") or {}
        where = f"{case['name']} · turul {index} ({turn.get('utterance', '')[:40]})"

        for key, value in (expect.get("active") or {}).items():
            need = state.need_for(key)
            assert need is not None, f"{where}: nevoia `{key}` lipsește"
            assert need.normalized_value == value, f"{where}: `{key}`"

        for key in expect.get("absent") or []:
            assert state.need_for(key) is None, f"{where}: `{key}` n-ar trebui să fie activă"

        if "revoked" in expect:
            assert sorted(state.revoked_keys()) == sorted(expect["revoked"]), f"{where}: revocări"

        for key, strength in (expect.get("strength") or {}).items():
            assert state.need_for(key).strength == strength, f"{where}: tăria lui `{key}`"

        for key, operator in (expect.get("operator") or {}).items():
            assert state.need_for(key).operator == operator, f"{where}: operatorul lui `{key}`"

        for key in expect.get("superseded") or []:
            statuses = [n.status for n in state.needs if n.key == key]
            assert "superseded" in statuses, f"{where}: `{key}` ar trebui superseded"

        if "topic" in expect:
            assert state.topic.category_key == expect["topic"], f"{where}: subiect"

        if "pending" in expect:
            pending = state.pending_clarification
            actual = pending.target_key if pending else None
            assert actual == expect["pending"], f"{where}: întrebare în așteptare"

        for key in expect.get("asked") or []:
            assert state.asked(key) is not None, f"{where}: `{key}` ar trebui marcată ca întrebată"

        if "rejected" in expect:
            reasons = sorted(r.reason for r in reduced.rejected)
            assert reasons == sorted(expect["rejected"]), f"{where}: respingeri"


def test_the_fixture_corpus_covers_the_failure_matrix():
    """Plasă: dacă un fixture dispare la un refactor, testul de mai sus ar trece cu 0 cazuri."""
    names = {p.stem for p in FIXTURES}
    assert len(FIXTURES) >= 6
    assert {"ro", "en", "hu"} <= {n.split("_")[0] for n in names}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_no_fixture_utterance_ever_lands_in_the_state(path: Path):
    """Textul din fixture e DOAR pentru cititor. Dacă ajunge în JSONB, poarta de normalizare are
    o gaură — și odată cu ea, orice PII dintr-o propoziție reală."""
    case = json.loads(path.read_text(encoding="utf-8"))
    state = hydrate_state_v2(case.get("initial_state"), VOCAB)
    utterances = []
    for turn in case["turns"]:
        utterances.append(turn.get("utterance", ""))
        state = reduce_all(
            state, [_proposal(p) for p in turn["proposals"]], POLICY, revision=state.revision + 1
        ).state
    blob = json.dumps(state.to_jsonb(), ensure_ascii=False)
    for utterance in utterances:
        if len(utterance) > 20:
            assert utterance not in blob
