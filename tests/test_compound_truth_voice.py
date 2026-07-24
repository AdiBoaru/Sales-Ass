"""Guard de voce pentru propunerile NX-202b."""

import json
import re
from pathlib import Path

DATASET = Path(__file__).parent / "golden" / "compound_truth_proposed.json"
INTERNAL_LANGUAGE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bUNKNOWN\b",
        r"fa[țt]et",
        r"suitability",
        r"\bDB\b",
        r"catalog",
        r"match exact",
        r"standardizat",
        r"\bRON\b",
    )
)


def test_all_compound_truth_cases_have_natural_voice_contract():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = [*data["compound"], *data["compare"]]

    assert len(cases) == 19
    for case in cases:
        reply = case["truth"]["exemplar_reply"]
        must_convey = case["truth"]["must_convey"]
        sentences = re.findall(r"[.!?](?:\s|$)", reply)

        assert 2 <= len(sentences) <= 3, case["id"]
        assert len(reply) <= 450, case["id"]
        assert must_convey, case["id"]
        assert not re.search(r"\d+,\d{2}\b", reply), case["id"]
        assert all(not pattern.search(reply) for pattern in INTERNAL_LANGUAGE), case["id"]
