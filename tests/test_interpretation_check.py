"""NX-335 (kernel pasul 5) — validarea interpretării, în modulul PUR `interpretation_check`.

Contractul pune validarea în kernelul pur („Everything after it (validation, resolver, …) is the
pure kernel"), iar porțile rolului `adapter` sunt mai slabe, deci validarea, amprenta
vocabularului și evenimentul `turn_interpretation` stau aici, rol `pure`. Ce se fixează:

- `validate` NU taie interpretarea (tăierea de la coadă ar pierde actul principal); plafoanele le
  aplică cine le consumă (`check_changes`, poarta, plannerul), iar `overflow` doar numără;
- I22 pe LANȚUL REAL: o țintă fantomă e numărată de adaptor și scoasă de poarta NX-332, un
  `relative_to` fantomă e respins de `check_changes`; mulțimea adaptorului == cea a porții;
- I18: fiecare eveniment poartă versiunea contractului, pe toate cele șase `outcome`, fără text de
  client (P12)."""

from __future__ import annotations

import json
import re

import pytest

from src.conversation import interpretation_check as ic
from src.conversation.interpretation import KERNEL_CONTRACT_VERSION, TurnInterpretation
from src.conversation.kernel_trace import _VERSION_RE
from src.conversation.needs import NeedVocabulary
from src.conversation.provenance import MAX_CHANGES, UserWords, check_changes, need_handles
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.kernel import gates, replay
from tests.test_kernel_contract import _loaded_model_clients

PACK = "electronics"


def _interp(**fields) -> TurnInterpretation:
    base = {
        "thread": "continue",
        "acts": [],
        "changes": [],
        "references": [],
        "ambiguities": [],
        "corrects_previous_turn": False,
    }
    base.update(fields)
    return TurnInterpretation.model_validate(replay.expand_interpretation(base))


def _screen() -> ConversationStateV2:
    """Starea după primul tur din k01 (două telefoane pe ecran, două nevoi)."""
    journey = next(
        j for j in replay.load_journeys() if j.journey_id == "k01-electronics-park-and-resume"
    )
    turn = journey.turns[0]
    step = fc.kernel_step(
        PACK,
        ConversationStateV2(),
        turn.expect["interpretation"],
        turn.user_input,
        shown_ids=turn.shown,
        turn_id="t0",
    )
    return step.state_after


def _ordinals(n: int) -> list[dict]:
    return [
        {"id": f"r{i}", "text": f"al {i}-lea", "kind": "ordinal", "ordinal": i}
        for i in range(1, n + 1)
    ]


def _validate(interp: TurnInterpretation, state: ConversationStateV2, message: str = "mesaj"):
    pack = fc.pack(PACK)
    return ic.validate(
        interp,
        words=UserWords(message),
        handles=need_handles(state.needs, NeedVocabulary.from_pack(pack)),
        vocab=fc.vocabulary(PACK),
        pack=pack,
        locale="ro",
    )


# --- validate nu taie nimic ----------------------------------------------------------------------


def test_four_acts_and_twelve_changes_are_kept_counted_and_the_primary_act_survives():
    state = _screen()
    changes = [
        {"op": "add", "dimension": "unmapped", "value": f"v{i}", "quote": "mesaj"}
        for i in range(12)
    ]
    interp = _interp(
        acts=[
            {"kind": "chitchat"},
            {"kind": "store_info", "query": "livrare"},
            {"kind": "find", "query": "telefon"},
            {"kind": "compare", "targets": ["r1", "r2"]},
        ],
        changes=changes,
        references=_ordinals(2),
    )
    validated = _validate(interp, state)
    assert validated.interpretation == interp, "validate nu schimbă interpretarea"
    assert len(validated.interpretation.acts) == 4
    assert dict(validated.overflow) == {"acts": 1, "changes": 2, "references": 0}
    assert len(validated.checked) == 12
    assert [c.rejected for c in validated.checked[MAX_CHANGES:]] == ["truncated", "truncated"]
    assert all(c.rejected != "truncated" for c in validated.checked[:MAX_CHANGES])
    step = fc.kernel_step(PACK, state, interp, "mesaj", turn_id="t1")
    primary = step.planned.plans[step.planned.primary]
    assert primary.executor == "compare"


def test_validate_checks_changes_like_check_changes():
    state = _screen()
    interp = _interp(
        changes=[
            {
                "op": "set",
                "dimension": "price",
                "relation": "lte",
                "number": 2000,
                "quote": "sub 2000",
            }
        ]
    )
    validated = _validate(interp, state, "ceva sub 2000")
    pack = fc.pack(PACK)
    expected = check_changes(
        interp,
        words=UserWords("ceva sub 2000"),
        handles=need_handles(state.needs, NeedVocabulary.from_pack(pack)),
        vocab=fc.vocabulary(PACK),
        pack=pack,
        locale="ro",
    )
    assert validated.checked == expected


# --- I22 pe lanțul real --------------------------------------------------------------------------


def test_a_phantom_act_target_is_counted_and_the_gate_skips_the_act():
    state = _screen()
    interp = _interp(acts=[{"kind": "detail", "targets": ["r9"]}], references=_ordinals(1))
    validated = _validate(interp, state)
    assert validated.unknown_refs == ["r9"]
    event = ic.interpretation_event("ok", effort="none", snapshot="0" * 16, validated=validated)
    assert event["unknown_reference"] == 1
    step = fc.kernel_step(PACK, state, interp, "mesaj", turn_id="t1")
    assert step.outcome.skipped_acts == (0,)


def test_a_phantom_relative_to_is_rejected_by_check_changes():
    state = _screen()
    interp = _interp(
        changes=[
            {
                "op": "set",
                "dimension": "price",
                "relation": "lte",
                "relative_to": "r4",
                "quote": "mai ieftin",
            }
        ],
        references=_ordinals(1),
    )
    validated = _validate(interp, state, "ceva mai ieftin")
    assert validated.checked[0].rejected == "unknown_reference"
    assert validated.unknown_refs == ["r4"]


def test_r7_is_declared_for_an_act_but_rejected_as_relative_to_the_known_mismatch():
    """Nepotrivirea care EXISTĂ în `main` (Architecture Review, punctul 3), fixată ca să nu se
    schimbe tăcut: `check_changes` declară doar primele 6 referințe, resolverul și poarta pe toate.
    `r7` ca țintă de act NU e `unknown_reference`; ca `relative_to` e respins."""
    state = _screen()
    interp = _interp(
        acts=[{"kind": "detail", "targets": ["r7"]}],
        changes=[
            {
                "op": "set",
                "dimension": "price",
                "relation": "lte",
                "relative_to": "r7",
                "quote": "mai ieftin",
            }
        ],
        references=_ordinals(7),
    )
    validated = _validate(interp, state, "ceva mai ieftin")
    assert validated.unknown_refs == []
    assert validated.checked[0].rejected == "unknown_reference"
    assert dict(validated.overflow)["references"] == 1
    step = fc.kernel_step(PACK, state, interp, "ceva mai ieftin", turn_id="t1")
    assert step.outcome.skipped_acts == ()
    assert [r.ref_id for r in step.resolved][-1] == "r7"


def test_the_adapter_and_the_gate_agree_on_every_act_including_the_fourth():
    state = _screen()
    interp = _interp(
        acts=[
            {"kind": "compare", "targets": ["r1", "r2"]},
            {"kind": "detail", "targets": ["r7"]},
            {"kind": "link", "targets": ["r8"]},
            {"kind": "detail", "targets": ["r9"]},
        ],
        references=_ordinals(7),
    )
    validated = _validate(interp, state)
    assert validated.unknown_refs == ["r8", "r9"]
    step = fc.kernel_step(PACK, state, interp, "mesaj", turn_id="t1")
    unknown = set(validated.unknown_refs)
    from_adapter = {i for i, a in enumerate(interp.acts) if set(a.targets) & unknown}
    assert set(step.outcome.skipped_acts) == from_adapter == {2, 3}


# --- I18 și evenimentul --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ic.OUTCOMES)
def test_every_event_carries_the_contract_version_and_only_listed_keys(outcome):
    validated = None
    if outcome == "ok":
        validated = _validate(_interp(acts=[{"kind": "find", "query": "x"}]), ConversationStateV2())
    event = ic.interpretation_event(outcome, effort="none", snapshot="ab" * 8, validated=validated)
    assert event["contract_version"] == KERNEL_CONTRACT_VERSION
    assert _VERSION_RE.fullmatch(event["contract_version"])
    assert set(event) == ic.EVENT_KEYS
    assert event["outcome"] == outcome
    assert event["prompt_version"] == ic.INTERPRET_PROMPT_VERSION


def test_the_event_counts_and_carries_no_customer_text():
    state = _screen()
    secret = "textul secret al clientului"
    interp = _interp(
        acts=[{"kind": "find", "query": secret}, {"kind": "detail", "targets": ["r1"]}],
        changes=[
            {"op": "add", "dimension": "unmapped", "value": secret, "quote": secret},
            {
                "op": "set",
                "dimension": "price",
                "relation": "lte",
                "number": 100,
                "unit": "GB",
                "quote": secret,
            },
        ],
        references=[{"id": "r1", "text": secret, "kind": "name", "name": secret}],
        ambiguities=[{"about": "value", "readings": [secret, secret]}],
    )
    validated = _validate(interp, state, secret)
    event = ic.interpretation_event("ok", effort="none", snapshot="c" * 16, validated=validated)
    assert secret not in json.dumps(event, ensure_ascii=False)
    assert event["acts"] == ["find", "detail"]
    assert event["changes"] == 2
    assert event["references"] == 1
    assert event["ambiguities"] == 1
    assert sum(event["provenance"].values()) == 1  # doar schimbările nerespinse
    assert event["rejected"].get("unit_mismatch") == 1
    assert set(event["rejected"]) <= set(ic.CHANGE_REJECTS)
    assert event["thread"] == "continue"
    assert event["corrects_previous_turn"] is False


def test_a_non_ok_event_is_neutral():
    event = ic.interpretation_event("refused", effort="none", snapshot="d" * 16)
    assert event["acts"] == [] and event["changes"] == 0 and event["thread"] is None
    assert event["overflow"] == {"acts": 0, "changes": 0, "references": 0}


def test_an_unknown_outcome_is_refused():
    with pytest.raises(ValueError):
        ic.interpretation_event("maybe", effort="none", snapshot="e" * 16)


# --- parse_reply: ordinea tabelului din §3 -------------------------------------------------------

GOOD = json.dumps(
    replay.expand_interpretation(
        {
            "thread": "continue",
            "acts": [],
            "changes": [],
            "references": [],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )
)


@pytest.mark.parametrize(
    ("content", "refusal", "finish", "outcome"),
    [
        (GOOD, None, "stop", "ok"),
        (None, "nu", "stop", "refused"),
        (GOOD, None, "content_filter", "refused"),
        ("{", "nu", "length", "refused"),
        ("{", None, "length", "truncated"),
        (GOOD, None, "length", "truncated"),
        (None, None, "stop", "invalid_json"),
        ("   ", None, "stop", "invalid_json"),
        ("{x", None, "stop", "invalid_json"),
        ("[]", None, "stop", "schema_violation"),
        (json.dumps({"thread": "continue"}), None, "stop", "schema_violation"),
        (GOOD[:-1] + ', "extra": 1}', None, "stop", "schema_violation"),
    ],
)
def test_parse_reply_follows_the_table_order(content, refusal, finish, outcome):
    got, interp = ic.parse_reply(content, refusal=refusal, finish_reason=finish)
    assert got == outcome
    assert (interp is not None) == (outcome == "ok")


# --- amprenta vocabularului ----------------------------------------------------------------------


def test_the_snapshot_id_is_16_hex_and_follows_the_prompt_version(monkeypatch):
    pack, vocab = fc.pack(PACK), fc.vocabulary(PACK)
    first = ic.snapshot_id(pack, vocab)
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert ic.snapshot_id(pack, vocab) == first
    monkeypatch.setattr(ic, "INTERPRET_PROMPT_VERSION", "interpret.v999")
    assert ic.snapshot_id(pack, vocab) != first


def test_the_snapshot_id_changes_with_a_facet_alias():
    import dataclasses

    pack, vocab = fc.pack("fashion"), fc.vocabulary("fashion")
    facet = next(f for f in pack.facets if f.aliases)
    changed = dataclasses.replace(facet, aliases={**facet.aliases, "alias nou": facet.values[0]})
    other = dataclasses.replace(
        pack, facets=tuple(changed if f is facet else f for f in pack.facets)
    )
    assert ic.snapshot_id(pack, vocab) != ic.snapshot_id(other, vocab)
    assert ic.snapshot_id(None, None) == ic.snapshot_id(None, None)


# --- registrul și porțile ------------------------------------------------------------------------


def test_the_step_5_modules_are_registered_and_no_longer_planned():
    registry = gates.load_modules()
    roles = gates.modules_by_role()
    assert "src/conversation/interpretation_check.py" in roles["pure"]
    assert roles["adapter"] == ["src/conversation/turn_interpreter.py"]
    assert "src/conversation/turn_interpreter.py" not in registry["planned"]
    assert not registry["planned"], "niciun modul de kernel planificat rămas nescris"


def test_the_pure_check_module_does_not_load_a_model_client_even_transitively():
    assert _loaded_model_clients(["src.conversation.interpretation_check"]) == ""


def test_the_prompt_version_lives_in_the_pure_module():
    from src.conversation import turn_interpreter

    assert turn_interpreter.INTERPRET_PROMPT_VERSION is ic.INTERPRET_PROMPT_VERSION
