"""NX-335 — corecturile după recenzia adversarială, fiecare cu testul care PICA înainte.

Numerotarea urmează recenzia (1-11). Fiecare test fixează un defect găsit pe codul livrat, nu o
proprietate nouă: fără el, defectul ar putea reveni tăcut."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from scripts import kernel_prompt_snapshot as kps
from scripts import nx335_interpret_replay as rp
from src.agent.llm import SchemaReply
from src.config import Settings
from src.conversation import interpretation_check as ic
from src.conversation import turn_interpreter as ti
from src.conversation.interpretation import KERNEL_CONTRACT_VERSION, TurnInterpretation
from src.conversation.state_v2 import ConversationStateV2
from src.domain.constraints import EMPTY_UNITS
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

ROOT = Path(__file__).resolve().parents[1]
LABELS_FILE = ROOT / "tests" / "golden" / "kernel_real" / "sole-ro.json"
LOCAL_SNAPSHOT = ROOT / "reports" / "nx335" / "sole-ro"


def _input(name: str, state: ConversationStateV2 | None = None, **kw) -> ti.InterpretInput:
    base = dict(
        locale="ro",
        pack=fc.pack(name),
        vocab=fc.vocabulary(name),
        category_menu=fc.category_menu(name),
        state=state or ConversationStateV2(),
        history=(),
        message="Caut ceva.",
    )
    base.update(kw)
    return ti.InterpretInput(**base)


class _Fake:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    async def complete_schema_raw(self, system, user, schema, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.reply


_EMPTY = json.dumps(
    {
        "thread": "continue",
        "acts": [],
        "changes": [],
        "references": [],
        "ambiguities": [],
        "corrects_previous_turn": False,
    }
)


# --- 1. snapshot-urile promptului sunt verificate -------------------------------------------------


def test_the_prompt_snapshots_match():
    assert kps.differences() == []


def test_a_changed_instruction_fails_the_prompt_snapshot(monkeypatch):
    monkeypatch.setattr(ti, "_INSTRUCTIONS", ti._INSTRUCTIONS + "\nMUTATED")
    assert set(kps.differences()) == set(replay.FIXTURE_PACKS)


# --- 2. comparatorul ține seama de op și de relație; valorile nule ies din F1 ---------------------


def test_an_avoid_or_a_remove_is_not_a_hit_against_an_add():
    label = {"changes": [["add", "concerns", "acne", "eq"]]}
    for got in (
        [["add", "concerns", "acne", "avoid"]],
        [["remove", "concerns", "acne", "eq"]],
        [["clear", "concerns", "acne", "eq"]],
    ):
        assert rp.compare(label, {"changes": got})["change_hits"] == 0, got
    # clasele: set/add/replace = afirmă; eq/contains/None = pozitiv
    assert (
        rp.compare(label, {"changes": [["set", "concerns", "acne", "contains"]]})["change_hits"]
        == 1
    )


def test_null_valued_pairs_are_counted_apart_from_f1():
    label = {
        "changes": [
            ["set", "price", None, "lte"],
            ["add", "unmapped", None, "eq"],
            ["add", "concerns", "acne", "eq"],
        ]
    }
    got = {
        "changes": [
            ["set", "price", None, "lte"],
            ["add", "unmapped", None, "avoid"],
            ["add", "concerns", "acne", "eq"],
        ]
    }
    v = rp.compare(label, got)
    assert (v["change_hits"], v["change_labelled"], v["change_emitted"]) == (1, 1, 1)
    assert (v["null_hits"], v["null_labelled"], v["null_emitted"]) == (1, 2, 2)


def test_the_summary_reports_null_valued_pairs_separately():
    rows = [
        {
            "arm": "none",
            "outcome": "ok",
            "event": ic.interpretation_event("ok", effort="none", snapshot="0" * 16),
            "observed": {"changes": []},
            "verdict": rp.compare(
                {"changes": [["set", "price", None, "lte"]]},
                {"changes": [["set", "price", None, "lte"]]},
            )
            | {"thread": True, "primary_act": True, "targets": True, "ambiguous": True},
            "first_divergence": None,
            "conversation_id": "c",
            "ms": 1.0,
            "cost_usd": 0.0,
            "versus_v1": {"compared": False},
        }
    ]
    arm = rp.summarize(rows, ("none",))["arms"]["none"]
    assert arm["changes"]["labelled"] == 0
    assert arm["null_valued_changes"] == {"hits": 1, "labelled": 1, "emitted": 1}


# --- 3. amprenta vede tot ce schimbă ieșirea validării --------------------------------------------


@pytest.mark.parametrize(
    "field_update",
    [
        {"enforce_ready": True},
        {"scope": "conversation"},
        {"source_key": "other_key"},
        {"operators": ("eq",)},
        {"labels": {"ro": "Alta eticheta"}},  # NX-349: frazele unei fațete da/nu
    ],
)
def test_the_snapshot_id_sees_every_field_validation_reads(field_update):
    pack, vocab = fc.pack("fashion"), fc.vocabulary("fashion")
    flipped = dataclasses.replace(
        pack, facets=tuple(dataclasses.replace(f, **field_update) for f in pack.facets)
    )
    assert ic.snapshot_id(pack, vocab) != ic.snapshot_id(flipped, vocab)


def test_the_snapshot_id_sees_the_value_type_and_the_units():
    pack, vocab = fc.pack("electronics"), fc.vocabulary("electronics")
    facet = next(f for f in pack.facets if f.key == "brand")
    other_type = next(f.value_type for f in pack.facets if f.value_type != facet.value_type)
    changed = dataclasses.replace(
        pack,
        facets=tuple(
            dataclasses.replace(f, value_type=other_type) if f is facet else f for f in pack.facets
        ),
    )
    assert ic.snapshot_id(pack, vocab) != ic.snapshot_id(changed, vocab)
    assert ic.snapshot_id(pack, vocab) != ic.snapshot_id(
        dataclasses.replace(pack, units=EMPTY_UNITS), vocab
    )


def test_enforce_ready_changes_the_strength_and_therefore_the_id():
    """Premisa testului de mai sus, măsurată: `enforce_ready` schimbă tăria din `validate`."""
    pack, vocab = fc.pack("fashion"), fc.vocabulary("fashion")
    hard = dataclasses.replace(
        pack, facets=tuple(dataclasses.replace(f, enforce_ready=True) for f in pack.facets)
    )
    interp = TurnInterpretation.model_validate(
        replay.expand_interpretation(
            {
                "changes": [
                    {
                        "op": "set",
                        "dimension": "color",
                        "relation": "eq",
                        "value": "negru",
                        "quote": "negru",
                    }
                ]
            }
        )
    )
    words = ic.UserWords("ceva negru")
    soft = ic.validate(interp, words=words, vocab=vocab, pack=pack, locale="ro").checked[0]
    strong = ic.validate(interp, words=words, vocab=vocab, pack=hard, locale="ro").checked[0]
    assert (soft.strength, strong.strength) == ("soft", "hard")


def test_loading_a_snapshot_recomputes_and_records_the_id(tmp_path):
    directory = tmp_path / "snap"
    doc = replay.load_pack("gifts")
    directory.mkdir()
    (directory / rp.VOCAB_FILE).write_text(
        json.dumps(
            {
                "business": {
                    "id": "b-gifts",
                    "slug": "gifts",
                    "name": "gifts",
                    "vertical": doc["vertical"],
                    "default_locale": "ro",
                    "settings": {"domain_pack": doc["domain_pack"]},
                },
                "category_menu": [list(m) for m in fc.category_menu("gifts")],
                "vocabulary": rp.vocab_to_doc(fc.vocabulary("gifts")),
                "vocabulary_snapshot": "0" * 16,
            }
        ),
        encoding="utf-8",
    )
    (directory / rp.CORPUS_FILE).write_text("", encoding="utf-8")
    snap = rp.load_snapshot(directory)
    assert snap.vocabulary_snapshot == ic.snapshot_id(snap.pack, snap.vocab)
    meta = json.loads((directory / rp.VOCAB_FILE).read_text(encoding="utf-8"))
    assert meta["vocabulary_snapshot"] == snap.vocabulary_snapshot
    assert meta["vocabulary_snapshot_at_capture"] == "0" * 16


# --- 5. nicio frază de client în fișierul de etichete, nici în metadate ---------------------------


def _strings(node, path=()):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield path + (k,), k
            yield from _strings(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, path + (i,))


def _words(text: str) -> list[str]:
    from src.catalog.query_terms import tokens

    return list(tokens(text))


def test_the_labels_metadata_quotes_nothing():
    """Textul liber al fișierului (metadatele) nu citează nimic: niciun semn de citare."""
    doc = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    for path, text in _strings({k: v for k, v in doc.items() if k != "turns"}):
        assert not re.search(r"[`\"„”«»]", text), (path, text)


def test_no_string_of_the_labels_file_carries_a_customer_phrase():
    """Cu instantaneul local prezent: nicio pereche de cuvinte consecutive dintr-un mesaj de client
    nu apare în textul liber al fișierului, iar niciun mesaj întreg nu apare în nicio valoare."""
    if not (LOCAL_SNAPSHOT / rp.CORPUS_FILE).exists():
        pytest.skip("instantaneul local lipsește (e doar pe mașina care l-a făcut)")
    messages = [
        json.loads(line)["client_text"]
        for line in (LOCAL_SNAPSHOT / rp.CORPUS_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    doc = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    meta = {k: v for k, v in doc.items() if k != "turns"}
    free = " ".join(" ".join(_words(t)) for _, t in _strings(meta))
    for message in messages:
        words = [w for w in _words(message)]
        for a, b in zip(words, words[1:], strict=False):
            if len(a) >= 3 and len(b) >= 3:
                assert f" {a} {b} " not in f" {free} ", (a, b)
    everything = [" ".join(_words(t)) for _, t in _strings(doc)]
    for message in messages:
        whole = " ".join(_words(message))
        if len(whole) >= 8:
            assert all(whole not in s for s in everything), whole


# --- 6. numerele și booleenii se randează exact -------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (12345.67, "12345.67"),
        (1000000.0, "1000000"),
        (100.0, "100"),
        (0.5, "0.5"),
        (99.99, "99.99"),
        (True, "true"),
        (False, "false"),
        (3, "3"),
    ],
)
def test_values_render_exactly(value, shown):
    assert ic.format_value(value) == shown
    assert ti._value(None, "price", value, "ro") == shown


def test_observed_uses_the_same_rendering_as_the_labels():
    from src.conversation.interpretation import CheckedChange, StateChange

    change = StateChange(
        op="set",
        target=None,
        dimension="price",
        relation="lte",
        value=None,
        number=1000000.0,
        unit=None,
        relative_to=None,
        quote="x",
    )
    checked = CheckedChange(
        change=change,
        dimension="price",
        canonical_value=1000000.0,
        provenance="explicit",
        strength="hard",
        rejected=None,
    )
    kernel = rp.KernelTurn(
        validated=ic.Validated(
            interpretation=TurnInterpretation.model_validate(json.loads(_EMPTY)),
            checked=[checked],
            unknown_refs=[],
            overflow={},
        ),
        resolved=(),
        delta=None,  # type: ignore[arg-type]
        gate=None,
        planned=None,
        state_after=ConversationStateV2(),
    )
    got = rp.observed(
        TurnInterpretation.model_validate(json.loads(_EMPTY)), kernel, ConversationStateV2()
    )
    assert got["changes"] == [["set", "price", "1000000", "lte"]]


# --- 7. efortul e un vocabular închis, iar evenimentul poartă valoarea de pe sârmă ---------------


@pytest.mark.parametrize("raw", ["", "  ", "fast", "NONE"])
def test_an_empty_or_unknown_interpret_effort_fails_at_boot(monkeypatch, raw):
    monkeypatch.setenv("LLM_REASONING_EFFORT_INTERPRET", raw)
    with pytest.raises(Exception):
        Settings()


async def test_the_event_logs_the_stripped_effort_sent_on_the_wire():
    fake = _Fake(SchemaReply(_EMPTY, None, "stop"))
    out = await ti.interpret_turn(fake, _input("gifts"), business_id="b", effort=" low ")
    assert fake.calls[0]["reasoning_effort"] == "low"
    assert out.event["effort"] == "low"


async def test_an_unknown_arm_effort_is_an_internal_error_without_a_call():
    fake = _Fake(SchemaReply(_EMPTY, None, "stop"))
    out = await ti.interpret_turn(fake, _input("gifts"), business_id="b", effort="fast")
    assert out.outcome == "internal_error"
    assert fake.calls == []


# --- 8. P6 pe tot corpul adaptorului --------------------------------------------------------------


class _BrokenPack:
    @property
    def facets(self):
        raise RuntimeError("pachet stricat")


async def test_a_malformed_input_is_an_internal_error_before_any_call():
    fake = _Fake(SchemaReply(_EMPTY, None, "stop"))
    out = await ti.interpret_turn(fake, _input("gifts", pack=_BrokenPack()), business_id="b")
    assert out.outcome == "internal_error"
    assert fake.calls == []
    assert out.event["contract_version"] == KERNEL_CONTRACT_VERSION
    assert set(out.event) == ic.EVENT_KEYS


async def test_a_failure_after_the_call_is_an_internal_error_with_one_call(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("validare stricată")

    monkeypatch.setattr(ti, "validate", boom)
    fake = _Fake(SchemaReply(_EMPTY, None, "stop"))
    out = await ti.interpret_turn(fake, _input("gifts"), business_id="b")
    assert out.outcome == "internal_error"
    assert len(fake.calls) == 1
    assert out.interpretation is None
    assert set(out.event) == ic.EVENT_KEYS


def test_internal_error_is_in_the_closed_outcome_vocabulary():
    assert "internal_error" in ic.OUTCOMES
    event = ic.interpretation_event("internal_error", effort="none", snapshot="")
    assert event["contract_version"] == KERNEL_CONTRACT_VERSION


# --- 9. riscul numerelor din replicile botului se măsoară; setul parcat e în vedere -------------


def test_a_number_where_a_relative_bound_was_expected_is_flagged():
    label = {"changes": [["set", "price", None, "lte"]]}
    assert rp.compare(label, {"changes": [["set", "price", "99", "lte"]]})["number_for_relative"]
    assert not rp.compare(label, {"changes": [["set", "price", None, "lte"]]})[
        "number_for_relative"
    ]


def test_the_parked_items_are_in_the_view():
    journey = next(
        j for j in replay.load_journeys() if j.journey_id == "k01-electronics-park-and-resume"
    )
    state = ConversationStateV2()
    for i, turn in enumerate(journey.turns[:2]):
        state = fc.kernel_step(
            journey.pack,
            state,
            turn.expect["interpretation"],
            turn.user_input,
            shown_ids=turn.shown,
            turn_id=f"t{i}",
        ).state_after
    assert state.parked is not None and state.parked.shown
    view = ti.render_view(_input("electronics", state))
    parked = view.split("PARKED:", 1)[1].split("ON SCREEN", 1)[0]
    for ref in state.parked.shown:
        assert ti.display(ref.name) in parked
        assert ref.product_id not in view


# --- 10. ecranul replay-ului e construit de funcția producției ------------------------------------


def test_the_screen_proposal_is_productions_refs():
    from src.worker.processor import _displayed_product_refs

    recommended = [
        {"product_id": "p1", "name": "A", "price": "12.5"},
        {"product_id": "p2", "name": None, "price": 3},
        {"product_id": "p3", "name": "C", "price": None},
        {"id": "p4", "name": "D", "price": 7},
    ]
    proposal = rp.screen_proposal(recommended, "t1")
    assert proposal.payload == {"displayed_products": _displayed_product_refs(recommended)}
    assert [d["product_id"] for d in proposal.payload["displayed_products"]] == ["p1", "p4"]
    assert proposal.source == "catalog"


# --- 11. ScriptedLLM întoarce o interpretare validă -----------------------------------------------


async def test_scripted_llm_raw_reply_is_a_valid_interpretation():
    from src.evals.scripted_llm import ScriptedLLM

    reply = await ScriptedLLM({"final": "text", "tool_calls": []}).complete_schema_raw("S", "U", {})
    TurnInterpretation.model_validate_json(reply.content)
    explicit = json.loads(_EMPTY) | {"thread": "aside"}
    reply = await ScriptedLLM({"interpretation": explicit}).complete_schema_raw("S", "U", {})
    assert TurnInterpretation.model_validate_json(reply.content).thread == "aside"
