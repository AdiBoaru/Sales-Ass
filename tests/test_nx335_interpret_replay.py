"""NX-335 felia 5c — proba furnizorului și raportul offline, fără model și fără DB.

Ce se fixează:

- `--dry-run` (implicit) = ZERO apeluri de model, pe ambele scripturi;
- instantaneul se citește din fișiere, nu din DB (retenția de 30 de zile nu mai contează);
- lanțul scriptului e lanțul kernelului: pe journey-urile existente dă aceeași stare ca
  `fixture_catalog.kernel_step` (nu o a doua implementare care să diveargă);
- ecranul se rotește din `recommended` (propunerea producției), iar `recent_sets` se umple;
- brațele de efort rulează în ordine amestecată per tur, determinist pe `--seed`;
- etichetele din repo nu poartă text de client."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import nx335_interpret_replay as rp
from scripts import nx335_interpret_smoke as smoke
from src.agent.llm import SchemaReply
from src.conversation.interpretation import UNIVERSAL_DIMENSIONS, TurnInterpretation
from src.conversation.interpretation_check import ACT_KINDS
from src.conversation.provenance import UserWords
from src.conversation.state_v2 import ConversationStateV2
from src.conversation.turn_interpreter import InterpretInput
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

LABELS_FILE = Path(__file__).resolve().parent / "golden" / "kernel_real" / "sole-ro.json"
LOCAL_SNAPSHOT = Path(__file__).resolve().parents[1] / "reports" / "nx335" / "sole-ro"


class CountingLLM:
    """Fake de model care numără apelurile și întoarce interpretările scriptate, în ordine."""

    def __init__(self, interpretations: list[TurnInterpretation] | None = None):
        self.calls = 0
        self._queue = list(interpretations or [])

    async def complete_schema_raw(self, system, user, schema, **kw):
        self.calls += 1
        interp = self._queue.pop(0) if self._queue else None
        content = json.dumps(interp.model_dump()) if interp is not None else "{}"
        return SchemaReply(content=content, refusal=None, finish_reason="stop")


def _journey(journey_id: str) -> replay.Journey:
    return next(j for j in replay.load_journeys() if j.journey_id == journey_id)


def _recommended(pack: str, ids) -> list[dict]:
    catalog = fc.products(pack)
    return [
        {"product_id": pid, "name": catalog[pid]["name"], "price": catalog[pid]["price"]}
        for pid in ids
    ]


def _write_snapshot(directory: Path, pack: str, turns: list[dict]) -> None:
    """Un instantaneu de FIXTURE, în forma celui real (`--snapshot`)."""
    doc = replay.load_pack(pack)
    directory.mkdir(parents=True, exist_ok=True)
    vocab = fc.vocabulary(pack)
    meta = {
        "taken_at": "2026-09-27T00:00:00+00:00",
        "business": {
            "id": f"b-{pack}",
            "slug": pack,
            "name": pack,
            "vertical": doc["vertical"],
            "default_locale": "ro",
            "settings": {"domain_pack": doc["domain_pack"]},
        },
        "category_menu": [list(m) for m in fc.category_menu(pack)],
        "vocabulary": rp.vocab_to_doc(vocab),
        "vocabulary_snapshot": "0" * 16,
    }
    (directory / rp.VOCAB_FILE).write_text(json.dumps(meta), encoding="utf-8")
    with (directory / rp.CORPUS_FILE).open("w", encoding="utf-8") as fh:
        for row in turns:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _k01_snapshot(tmp_path: Path) -> tuple[Path, replay.Journey]:
    journey = _journey("k01-electronics-park-and-resume")
    rows = [
        {
            "turn_id": f"00000000-0000-0000-0000-00000000000{i}",
            "conversation_id": "c-1",
            "seq": i + 1,
            "created_at": f"2026-09-20T10:0{i}:00+00:00",
            "language": "ro",
            "client_text": t.user_input,
            "bot_text": "Uite ce am găsit.",
            "recommended": _recommended("electronics", t.shown),
            "tool_calls": [{"name": "search_products", "args": {"category": "telefoane"}}],
            "product_search": [],
        }
        for i, t in enumerate(journey.turns)
    ]
    directory = tmp_path / "snap"
    _write_snapshot(directory, "electronics", rows)
    return directory, journey


def _facts(pack: str):
    async def facts(lookup):
        return fc.facts(pack, lookup)

    return facts


# --- zero apeluri fără --yes ---------------------------------------------------------------------


async def test_the_replay_dry_run_makes_zero_model_calls(tmp_path, monkeypatch, capsys):
    directory, _ = _k01_snapshot(tmp_path)
    fake = CountingLLM()
    monkeypatch.setattr("src.agent.llm.get_llm", lambda: fake)

    async def no_db():  # instantaneul se citește din fișiere, nu din DB
        raise AssertionError("dry-run-ul nu are voie să atingă DB-ul")

    monkeypatch.setattr("src.db.connection.get_pool", no_db)
    assert await rp.main(["--dir", str(directory)]) == 0
    assert await rp.main(["--dir", str(directory), "--dry-run", "--yes"]) == 0
    assert fake.calls == 0
    out = capsys.readouterr().out
    assert "ture: 3" in out and "apeluri: 3" in out


async def test_the_smoke_makes_zero_calls_without_yes_and_one_with_it(capsys):
    fake = CountingLLM(
        [TurnInterpretation.model_validate(replay.expand_interpretation({"acts": []}))]
    )

    async def loader(business, message):
        inp = InterpretInput(
            locale="ro",
            pack=fc.pack("gifts"),
            vocab=fc.vocabulary("gifts"),
            category_menu=fc.category_menu("gifts"),
            state=ConversationStateV2(),
            history=(),
            message=message,
        )
        return "b-gifts", inp

    assert await smoke.main(["--message", "x"], loader=loader, llm_factory=lambda: fake) == 0
    assert fake.calls == 0
    assert "turn_interpretation" in capsys.readouterr().out
    assert (
        await smoke.main(["--message", "x", "--yes"], loader=loader, llm_factory=lambda: fake) == 0
    )
    assert fake.calls == 1


def test_the_smoke_requires_a_message():
    import asyncio

    with pytest.raises(SystemExit):
        asyncio.run(smoke.main([]))


# --- lanțul: aceeași stare ca `kernel_step` ------------------------------------------------------


def _projection(state: ConversationStateV2) -> dict:
    view = fc.state_view(state)
    view["need_details"] = sorted(
        (n.key, n.operator, str(n.normalized_value), n.strength, n.status, n.source)
        for n in state.needs
    )
    return view


@pytest.mark.parametrize("journey", replay.load_journeys(), ids=lambda j: j.journey_id)
async def test_the_script_chain_gives_the_state_of_kernel_step(journey):
    """Pe fiecare journey existent, lanțul scriptului (ecranul din `recommended`) și `kernel_step`
    (ecranul din `shown`) dau aceeași stare pe ce scrie kernelul: nevoi, subiect, parcat, ecran,
    seturi de mai devreme, focus. `active_search` și memoria întrebării nu intră: acolo producția ar
    fi executat planul, pe care replay-ul nu-l execută (declarat în script). Consecința, măsurată:
    pe un journey cu anti-buclă (g02) plannerul replay-ului întreabă din nou, fiindcă v1 n-a pus
    întrebarea kernelului; starea rămâne aceeași."""
    mine = ConversationStateV2()
    theirs = ConversationStateV2()
    for i, turn in enumerate(journey.turns):
        earlier = tuple(t.user_input for t in journey.turns[:i])[::-1]
        interp = turn.expect["interpretation"]
        step = fc.kernel_step(
            journey.pack,
            theirs,
            interp,
            turn.user_input,
            earlier=earlier,
            shown_ids=turn.shown,
            turn_id=f"t{i}",
            locale=journey.locale,
        )
        theirs = step.state_after
        got = await rp.kernel_turn(
            mine,
            interp,
            words=UserWords(turn.user_input, earlier),
            pack=fc.pack(journey.pack),
            vocab=fc.vocabulary(journey.pack),
            locale=journey.locale,
            facts=_facts(journey.pack),
            recommended=_recommended(journey.pack, turn.shown),
            turn_id=f"t{i}",
        )
        mine = got.state_after
        assert _projection(mine) == _projection(theirs), f"turul {i}"
        assert got.resolved == step.resolved
        assert got.gate is not None and got.planned is not None


async def test_a_three_turn_conversation_chains_the_state_and_rotates_the_screen(tmp_path):
    directory, journey = _k01_snapshot(tmp_path)
    snap = rp.load_snapshot(directory)
    fake = CountingLLM([t.expect["interpretation"] for t in journey.turns])
    rows = await rp.run(
        snap,
        fake,
        efforts=("none",),
        facts=_facts("electronics"),
        labels={},
        seed=1,
        dry_run=False,
    )
    assert fake.calls == 3
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert all(r["outcome"] == "ok" for r in rows)
    assert rows[0]["state"]["shown"] == 2
    assert rows[1]["state"]["recent_sets"] == 1
    assert rows[1]["state"]["parked"] is True
    assert rows[2]["state"]["recent_sets"] >= 1
    assert rows[0]["versus_v1"]["compared"] is True


async def test_a_failed_interpretation_still_rotates_the_screen(tmp_path):
    directory, _ = _k01_snapshot(tmp_path)
    snap = rp.load_snapshot(directory)
    rows = await rp.run(
        snap,
        CountingLLM(),
        efforts=("none",),
        facts=_facts("electronics"),
        labels={},
        seed=1,
        dry_run=False,
    )
    assert [r["outcome"] for r in rows] == ["schema_violation"] * 3
    assert rows[1]["state"]["recent_sets"] == 1


async def test_the_arms_run_in_a_shuffled_order_per_turn(tmp_path):
    directory, _ = _k01_snapshot(tmp_path)
    snap = rp.load_snapshot(directory)

    async def orders(seed):
        rows = await rp.run(
            snap,
            None,
            efforts=("none", "low"),
            facts=_facts("electronics"),
            labels={},
            seed=seed,
            dry_run=True,
        )
        per_turn = {}
        for r in rows:
            per_turn.setdefault(r["turn_id"], []).append(r["arm"])
        return [tuple(v) for v in per_turn.values()]

    first = await orders(7)
    assert first == await orders(7), "determinist pe seed"
    assert all(sorted(o) == ["low", "none"] for o in first)
    seen = set()
    for seed in range(20):
        seen.update(await orders(seed))
    assert seen == {("none", "low"), ("low", "none")}


# --- comparatorul și metricile -------------------------------------------------------------------


def test_the_comparator_scores_fields_and_change_pairs():
    label = {
        "thread": "continue",
        "primary_act": "find",
        "targets": [],
        "changes": [["add", "concerns", "acne", "eq"], ["set", "price", "100", "lte"]],
        "ambiguous": False,
    }
    got = {
        "thread": "continue",
        "primary_act": "find",
        "targets": [],
        "changes": [["add", "concerns", "acne", "eq"], ["add", "unmapped", None, "eq"]],
        "ambiguous": False,
    }
    v = rp.compare(label, got)
    assert v["thread"] and v["primary_act"] and v["targets"] and v["ambiguous"]
    assert (v["change_hits"], v["change_labelled"], v["change_emitted"]) == (1, 2, 1)
    # `unmapped` (valoare nulă) se numără separat, în afara F1 (recenzia NX-335)
    assert (v["null_hits"], v["null_labelled"], v["null_emitted"]) == (0, 0, 1)


def test_wilson_interval():
    assert rp.wilson(0, 0) is None
    low, high = rp.wilson(114, 127)
    assert 0.83 < low < 0.85 and 0.93 < high < 0.95


# --- etichetele: fără text de client -------------------------------------------------------------

_LABEL_KEYS = {"thread", "primary_act", "targets", "changes", "ambiguous", "uncertain"}
_TARGET = re.compile(r"^(#\d+|catalog)$")
_CODE = re.compile(r"^[a-z0-9_:.\-]+$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def _labels() -> dict:
    return json.loads(LABELS_FILE.read_text(encoding="utf-8"))


def test_the_labels_file_has_the_expected_shape():
    doc = _labels()
    assert doc["schema_version"] == "kernel-real-labels.v1"
    assert doc["business"] == "sole-ro"
    turns = doc["turns"]
    assert turns
    for turn_id, label in turns.items():
        assert re.fullmatch(r"[0-9a-f-]{36}", turn_id)
        assert set(label) <= _LABEL_KEYS, turn_id
        assert label["thread"] in ("continue", "aside", "resume")
        assert label["primary_act"] in ACT_KINDS or label["primary_act"] is None


def test_the_labels_carry_no_customer_text():
    """Fiecare șir din etichete e din vocabular ÎNCHIS: fire, acte, poziții de pe ecran, op-uri,
    relații, dimensiuni ale pachetului și valori canonice (coduri declarate de pachet, chei de
    raft, numere). O valoare `unmapped` nu se scrie deloc (ar fi cuvintele clientului). Cu
    instantaneul local prezent, nicio valoare nu e un fragment din mesajul clientului care să nu fie
    și cod de vocabular."""
    pack = fc.pack("sole-ro")
    dims = {f.key for f in pack.facets} | set(UNIVERSAL_DIMENSIONS)
    declared = {f.key: set(f.values) for f in pack.facets if f.values}
    local_vocab: dict[str, set[str]] = {}
    if (LOCAL_SNAPSHOT / rp.VOCAB_FILE).exists():
        meta = json.loads((LOCAL_SNAPSHOT / rp.VOCAB_FILE).read_text(encoding="utf-8"))
        local_vocab = {
            d: {row[0] for row in rows} for d, rows in meta["vocabulary"]["dimensions"].items()
        }
        local_vocab["category"] = {k for k, _ in meta["category_menu"]}
    for turn_id, label in _labels()["turns"].items():
        for target in label.get("targets", []):
            assert _TARGET.match(target), (turn_id, target)
        for change in label.get("changes", []):
            op, dimension, value, relation = change
            assert op in ("set", "add", "remove", "replace", "clear"), turn_id
            assert relation in ("eq", "lte", "gte", "contains", "avoid"), turn_id
            assert dimension in dims, (turn_id, dimension)
            if dimension == "unmapped":
                assert value is None, (turn_id, "unmapped fără valoare: ar fi textul clientului")
                continue
            if value is None:
                continue
            if dimension == "price" or _NUMBER.match(value):
                assert _NUMBER.match(value), (turn_id, value)
            elif dimension in declared and value in declared[dimension]:
                pass
            elif dimension in local_vocab:
                assert value in local_vocab[dimension], (turn_id, dimension, value)
            else:
                assert _CODE.match(value), (turn_id, dimension, value)


def test_the_labels_cover_the_local_corpus_when_it_is_present():
    if not (LOCAL_SNAPSHOT / rp.CORPUS_FILE).exists():
        pytest.skip("instantaneul local lipsește (e doar pe mașina care l-a făcut)")
    corpus = {
        json.loads(line)["turn_id"]
        for line in (LOCAL_SNAPSHOT / rp.CORPUS_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    labelled = set(_labels()["turns"])
    assert labelled <= corpus
