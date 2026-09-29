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


# --- NX-339: interpretarea brută în raport --------------------------------------------------------


async def test_the_rows_keep_the_raw_interpretation_and_the_validator_verdicts(tmp_path):
    """Fără citat și fără verdictul pe schimbare, o respingere `semantic_mismatch` nu se poate
    explica (NX-339, clasa E). Raportul e local și ignorat de git; evenimentul rămâne fără text."""
    directory, journey = _k01_snapshot(tmp_path)
    snap = rp.load_snapshot(directory)
    expected = [t.expect["interpretation"] for t in journey.turns]
    rows = await rp.run(
        snap,
        CountingLLM(expected),
        efforts=("none",),
        facts=_facts("electronics"),
        labels={},
        seed=1,
        dry_run=False,
    )
    first = rows[0]
    assert first["interpretation"] == expected[0].model_dump(mode="json")
    assert len(first["checked"]) == len(expected[0].changes)
    assert all({"change", "rejected", "provenance"} <= set(c) for c in first["checked"])
    assert first["checked"][0]["change"]["quote"] == expected[0].changes[0].quote
    assert "quote" not in json.dumps(first["event"])


async def test_a_missing_local_snapshot_exits_clearly_with_zero_calls(
    tmp_path, monkeypatch, capsys
):
    fake = CountingLLM()
    monkeypatch.setattr("src.agent.llm.get_llm", lambda: fake)
    assert await rp.main(["--dir", str(tmp_path / "absent"), "--yes"]) == 2
    assert fake.calls == 0
    assert "--snapshot" in capsys.readouterr().err


def test_the_raw_interpretation_is_gitignored():
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "reports/nx335/" in ignore.splitlines()


# --- NX-339: câmpurile parțiale ale comparatorului ------------------------------------------------


def test_an_absent_field_is_not_evaluated_and_an_empty_one_is():
    got = {
        "thread": "aside",
        "primary_act": "find",
        "targets": [],
        "changes": [["set", "price", "9", "lte"]],
    }
    absent = rp.compare({"primary_act": "find", "targets": []}, got)
    assert absent["thread"] is None and absent["ambiguous"] is None
    assert absent["changes_evaluated"] is False
    assert (absent["change_emitted"], absent["change_labelled"]) == (0, 0)
    empty = rp.compare({"primary_act": "find", "targets": [], "changes": []}, got)
    assert empty["changes_evaluated"] is True
    assert (empty["change_hits"], empty["change_emitted"]) == (0, 1)  # un fals pozitiv


def test_the_summary_rates_only_the_evaluated_turns():
    def row(verdict):
        return {
            "arm": "none",
            "outcome": "ok",
            "event": {"provenance": {}, "rejected": {}, "unknown_reference": 0},
            "observed": {"changes": []},
            "verdict": verdict,
            "first_divergence": None,
            "ms": 1.0,
            "cost_usd": 0.0,
        }

    got = {"primary_act": "find", "targets": [], "changes": []}
    with_thread = rp.compare(
        {"thread": "continue", "primary_act": "find", "targets": []}, {**got, "thread": "aside"}
    )
    without = rp.compare({"primary_act": "find", "targets": []}, got)
    arm = rp.summarize([row(with_thread), row(without)], ("none",))["arms"]["none"]
    assert (arm["thread"]["k"], arm["thread"]["n"]) == (0, 1)
    assert (arm["primary_act"]["k"], arm["primary_act"]["n"]) == (2, 2)
    assert arm["changes_turns"] == 0


# --- NX-339: journey-urile ca set nevăzut ---------------------------------------------------------


@pytest.fixture(scope="module")
def cases():
    return rp.journey_cases()


def test_the_journeys_split_into_the_unseen_set_and_the_tuned_tenant(cases):
    assert {c.set_name for c in cases} == {"B", "A-bis"}
    assert all(c.set_name == ("A-bis" if c.pack in rp.TUNED_PACKS else "B") for c in cases)
    fields = rp.journey_fields()
    # regula câmpurilor parțiale: eticheta poartă doar ce fixează journey-ul
    for c in cases:
        keys = fields[(c.journey_id, c.index)]
        assert ("changes" in c.label) == ("changes" in keys)
        assert ("thread" in c.label) == ("thread" in keys)
        assert ("targets" in c.label) == ("references" in keys)
        assert ("ambiguous" in c.label) == ("ambiguities" in keys)
        assert ("corrects" in c.label) == ("corrects_previous_turn" in keys)


def test_journeys_with_an_injected_state_are_excluded_and_counted(cases):
    """Recenzia NX-339, constatarea 1: `chain` pornește din stare goală, deci un journey scris pe
    `sources`/`state_before` (resolverul, secțiunea C) ar fi pus în fața modelului pe un ecran gol.
    Se exclude întreg, iar turele sărite se numără, ca să fie raportate."""
    journeys = {j.journey_id: j for j in replay.load_journeys()}
    kept = {c.journey_id for c in cases}
    injected = {jid for jid, j in journeys.items() if rp.injected_state(j)}
    assert injected and not (kept & injected)
    assert all(not j.turns[0].sources for jid, j in journeys.items() if jid in kept)
    excluded = rp.Counter()
    again = rp.journey_cases(excluded=excluded)
    fields = rp.journey_fields()
    assert len(again) + sum(excluded.values()) == len(fields)
    assert excluded["B"] > 0


def test_the_state_of_a_later_turn_carries_what_the_earlier_turn_showed(cases):
    later = next(c for c in cases if c.index > 0 and c.state_before.references.displayed_products)
    assert later.inp.history and later.inp.history[0][0] == "user"
    assert len(later.earlier) == later.index  # toate turele de dinainte, ca în `chain`


async def test_journeys_dry_run_makes_zero_calls(monkeypatch, capsys):
    fake = CountingLLM()
    monkeypatch.setattr("src.agent.llm.get_llm", lambda: fake)
    assert await rp.main(["--journeys"]) == 0
    assert fake.calls == 0
    out = capsys.readouterr().out
    assert "'A-bis': " in out and "'B': " in out
    assert "excluse (stare injectată" in out


def _expected_of(case) -> TurnInterpretation:
    journey = next(j for j in replay.load_journeys() if j.journey_id == case.journey_id)
    return journey.turns[case.index].expect["interpretation"]


async def test_a_model_that_answers_the_label_scores_one_on_every_metric(cases):
    """Paritatea căii de comparare: eticheta și modelul trec prin același pas pur, deci un model
    care răspunde EXACT interpretarea așteptată iese 1,0 peste tot. Orice altă cifră ar fi o
    asimetrie a unealtei, adică ratări puse pe seama modelului."""
    fake = CountingLLM([_expected_of(c) for c in cases])
    rows = await rp.run_journeys(cases, fake, efforts=("none",), seed=1, dry_run=False)
    assert fake.calls == len(cases)
    for name, arms in rp.summarize_sets(rows, ("none",))["sets"].items():
        arm = arms["none"]
        assert arm["outcomes"] == {"ok": sum(1 for c in cases if c.set_name == name)}
        for field in ("primary_act", "thread", "targets", "ambiguity"):
            assert arm[field]["rate"] in (1.0, None), (name, field, arm[field])
        assert arm["changes"]["f1"] in (1.0, None), (name, arm["changes"])
        assert arm["unknown_reference"]["k"] == 0


async def test_a_wrong_act_is_counted_on_that_turn_only(cases):
    wrong = next(i for i, c in enumerate(cases) if c.set_name == "B")
    scripted = [_expected_of(c) for c in cases]
    acts = [a.model_copy(update={"kind": "other", "targets": []}) for a in scripted[wrong].acts]
    scripted[wrong] = scripted[wrong].model_copy(update={"acts": acts})
    rows = await rp.run_journeys(
        cases, CountingLLM(scripted), efforts=("none",), seed=1, dry_run=False
    )
    arm = rp.summarize_sets(rows, ("none",))["sets"]["B"]["none"]
    assert arm["primary_act"]["n"] - arm["primary_act"]["k"] == 1


# --- NX-339: regresiile între două rapoarte -------------------------------------------------------


def _verdict(**changed) -> dict:
    base = {
        "thread": True,
        "primary_act": True,
        "targets": True,
        "ambiguous": True,
        "changes_evaluated": True,
        "change_hits": 1,
        "change_labelled": 1,
        "change_emitted": 1,
        "null_hits": 0,
        "null_labelled": 0,
        "null_emitted": 0,
    }
    return {**base, **changed}


def test_regressions_list_the_turns_that_got_worse_on_the_intersection():
    def row(tid, **changed):
        return {"turn_id": tid, "arm": "none", "verdict": _verdict(**changed)}

    before = [row("a"), row("b", primary_act=False), row("c"), row("only-before")]
    after = [row("a", thread=False, change_emitted=2), row("b"), row("c"), row("only-after")]
    rep = rp.regressions(before, after)
    assert rep["compared"] == 3 and rep["only_before"] == 1 and rep["only_after"] == 1
    assert rep["regressed"] == 1 and rep["improved"] == 1
    assert rep["turns"] == [{"turn_id": "a", "arm": "none", "fields": ["thread", "changes"]}]


async def test_the_regressions_cli_rescores_both_reports_on_the_current_labels(
    tmp_path, capsys, cases
):
    """Recenzia NX-339, constatarea 4: verdictele salvate sunt pe etichetele din ziua rulării.
    CLI-ul le recalculează din `observed`, deci un verdict vechi (aici fals „corect") nu
    contează."""
    case = next(c for c in cases if c.set_name == "B")
    right = {**case.label, "primary_act": case.label["primary_act"]}
    wrong = {**case.label, "primary_act": "chitchat"}

    def row(observed, stale):
        return {
            "turn_id": case.turn_id,
            "arm": "none",
            "set": "B",
            "outcome": "ok",
            "observed": observed,
            "verdict": _verdict(primary_act=stale),
        }

    # verdictele salvate spun opusul adevărului; recalculul trebuie să le ignore
    (tmp_path / "a.json").write_text(json.dumps({"rows": [row(right, False)]}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"rows": [row(wrong, True)]}), encoding="utf-8")
    assert await rp.main(["--regressions", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["regressed"] == 1 and report["turns"][0]["fields"] == ["primary_act"]


# --- NX-339: constatările recenziei, fiecare cu testul ei -----------------------------------------


async def test_a_different_off_screen_product_is_not_the_same_target(cases):
    """Recenzia NX-339, constatarea 2: în afara ecranului, două produse DIFERITE nu mai devin
    amândouă „catalog". Pe k01#2 («cel mai ieftin» din setul parcat) un extrem întors (`max`)
    alege alt telefon, iar ținta trebuie să iasă greșită."""
    case = next(
        c for c in cases if c.journey_id == "k01-electronics-park-and-resume" and c.index == 2
    )
    assert any(t.startswith("id:") for t in case.label["targets"])
    expected = _expected_of(case)
    refs = [
        r.model_copy(update={"direction": "max"}) if r.kind == "extreme" else r
        for r in expected.references
    ]
    flipped = expected.model_copy(update={"references": refs})
    rows = await rp.run_journeys(
        [case], CountingLLM([flipped]), efforts=("none",), seed=1, dry_run=False
    )
    assert rows[0]["verdict"]["targets"] is False


async def test_a_failed_call_scores_wrong_and_never_passes_the_reference_gate(cases):
    """Recenzia NX-339, constatarea 3: un apel eșuat nu e „acord" pe o etichetă goală, iar
    `unknown_reference` se numără doar pe turele reușite (`not_ok` e poarta lor)."""
    rows = await rp.run_journeys(cases, CountingLLM(), efforts=("none",), seed=1, dry_run=False)
    arm = rp.summarize_sets(rows, ("none",))["sets"]["B"]["none"]
    assert arm["not_ok"]["rate"] == 1.0
    for field in ("primary_act", "targets", "ambiguity"):
        assert arm[field]["k"] == 0, (field, arm[field])
    assert arm["unknown_reference"]["n"] == 0 and arm["unknown_reference"]["rate"] is None
    assert all(rp._exact_changes(r["verdict"]) in (False, None) for r in rows)


async def test_the_model_side_validates_with_the_same_earlier_words_as_the_label(
    cases, monkeypatch
):
    """Recenzia NX-339, constatarea 7: niciun journey nu citează un tur anterior, deci paritatea
    cuvintelor de dinainte se fixează direct, pe argumentul pasat validatorului."""
    from tests.kernel import fixture_catalog

    seen: list[tuple[str, ...]] = []
    real = fixture_catalog.kernel_step

    def spy(*a, **kw):
        seen.append(kw["earlier"])
        return real(*a, **kw)

    monkeypatch.setattr(fixture_catalog, "kernel_step", spy)
    later = [c for c in cases if c.index > 0][:3]
    await rp.run_journeys(
        later,
        CountingLLM([_expected_of(c) for c in later]),
        efforts=("none",),
        seed=1,
        dry_run=False,
    )
    assert seen == [c.earlier for c in later] and all(seen)


# --- NX-339c: setul C, scris separat pentru verdictul lui interpret.v3 ----------------------------


@pytest.fixture(scope="module")
def cases_c():
    return rp.journey_cases(rp.HOLDOUT_C_DIR, set_name="C")


def test_set_c_is_frozen_before_any_run():
    """Setul C se îngheață ÎNAINTEA primei rulări: o etichetă schimbată după ce s-au văzut
    rezultatele ar face din el un set de reglaj. Amprenta e pe bytes normalizați CRLF→LF."""
    assert rp.holdout_c_digest() == rp.HOLDOUT_C_SHA256


def test_set_c_labels_are_well_formed_and_nothing_is_excluded(cases_c):
    journeys = replay.load_journeys(rp.HOLDOUT_C_DIR)
    assert [p for j in journeys for p in replay.label_problems(j)] == []
    assert all(j.journey_id.startswith("c") for j in journeys)
    assert not any(rp.injected_state(j) for j in journeys)
    assert {c.set_name for c in cases_c} == {"C"}
    assert len(cases_c) == 89


def test_set_c_does_not_overlap_the_seen_journeys():
    seen = {j.journey_id for j in replay.load_journeys()}
    seen_texts = {t.user_input for j in replay.load_journeys() for t in j.turns}
    c = replay.load_journeys(rp.HOLDOUT_C_DIR)
    assert not ({j.journey_id for j in c} & seen)
    assert not ({t.user_input for j in c for t in j.turns} & seen_texts)


async def test_a_model_that_answers_the_set_c_label_scores_one(cases_c):
    """Paritatea unealtei pe setul C: o etichetă pe care comparatorul n-o poate reproduce ar
    număra o ratare a unealtei ca ratare a modelului."""
    journeys = {j.journey_id: j for j in replay.load_journeys(rp.HOLDOUT_C_DIR)}
    fake = CountingLLM(
        [journeys[c.journey_id].turns[c.index].expect["interpretation"] for c in cases_c]
    )
    rows = await rp.run_journeys(cases_c, fake, efforts=("none",), seed=1, dry_run=False)
    arm = rp.summarize_sets(rows, ("none",))["sets"]["C"]["none"]
    assert arm["outcomes"] == {"ok": len(cases_c)}
    for field in ("primary_act", "thread", "targets", "ambiguity"):
        assert arm[field]["rate"] in (1.0, None), (field, arm[field])


async def test_set_c_dry_run_makes_zero_calls(monkeypatch, capsys):
    fake = CountingLLM()
    monkeypatch.setattr("src.agent.llm.get_llm", lambda: fake)
    assert await rp.main(["--journeys", "--journeys-dir", str(rp.HOLDOUT_C_DIR)]) == 0
    assert fake.calls == 0
    assert "'C': 89" in capsys.readouterr().out


# --- NX-347: setul D, scris separat pentru verdictul lui interpret.v4 ----------------------------


@pytest.fixture(scope="module")
def cases_d():
    return rp.journey_cases(rp.HOLDOUT_D_DIR, set_name="D")


def test_set_d_is_frozen_before_any_run():
    """Amprenta raportată de agentul care l-a scris; orice etichetă schimbată după aceea pică."""
    assert rp.holdout_c_digest(rp.HOLDOUT_D_DIR) == rp.HOLDOUT_D_SHA256


def test_set_d_labels_are_well_formed_and_nothing_is_excluded(cases_d):
    journeys = replay.load_journeys(rp.HOLDOUT_D_DIR)
    assert [p for j in journeys for p in replay.label_problems(j)] == []
    assert all(j.journey_id.startswith("d") for j in journeys)
    assert not any(rp.injected_state(j) for j in journeys)
    assert {c.set_name for c in cases_d} == {"D"}
    assert len(cases_d) == 88


def test_set_d_does_not_overlap_the_seen_journeys_or_set_c():
    """Id-urile sunt disjuncte. Textele pot coincide doar pe formule scurte și generice (două, de 7
    și 19 caractere, măsurate la îngheț): o replică lungă copiată dintr-un set văzut ar pica."""
    seen = replay.load_journeys() + replay.load_journeys(rp.HOLDOUT_C_DIR)
    d = replay.load_journeys(rp.HOLDOUT_D_DIR)
    assert not ({j.journey_id for j in d} & {j.journey_id for j in seen})
    seen_texts = {t.user_input.strip().lower() for j in seen for t in j.turns}
    common = [
        t.user_input.strip().lower()
        for j in d
        for t in j.turns
        if t.user_input.strip().lower() in seen_texts
    ]
    assert len(common) <= 2 and all(len(x) <= 20 for x in common)


async def test_a_model_that_answers_the_set_d_label_scores_one(cases_d):
    """Paritatea unealtei pe setul D, inclusiv pe schimbări (regula NX-345)."""
    journeys = {j.journey_id: j for j in replay.load_journeys(rp.HOLDOUT_D_DIR)}
    fake = CountingLLM(
        [journeys[c.journey_id].turns[c.index].expect["interpretation"] for c in cases_d]
    )
    rows = await rp.run_journeys(cases_d, fake, efforts=("none",), seed=1, dry_run=False)
    arm = rp.summarize_sets(rows, ("none",))["sets"]["D"]["none"]
    assert arm["outcomes"] == {"ok": len(cases_d)}
    for field in ("primary_act", "thread", "targets", "ambiguity"):
        assert arm[field]["rate"] in (1.0, None), (field, arm[field])
    assert arm["changes"]["f1"] in (1.0, None), arm["changes"]


async def test_a_changed_frozen_set_refuses_to_run(monkeypatch, capsys):
    """Recenzia v4: amprenta se verifică și în runner, înaintea oricărui apel, nu doar în test."""
    fake = CountingLLM()
    monkeypatch.setattr("src.agent.llm.get_llm", lambda: fake)
    monkeypatch.setattr(rp, "HOLDOUT_D_SHA256", "0" * 64)
    code = await rp.main(
        ["--journeys", "--journeys-dir", str(rp.HOLDOUT_D_DIR), "--set-name", "D", "--yes"]
    )
    assert code == 2 and fake.calls == 0
    assert "refuz" in capsys.readouterr().out
