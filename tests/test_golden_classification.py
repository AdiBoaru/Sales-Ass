"""NX-202 — guard pe manifestul de clasificare al golden set-ului.

`tests/golden/classification.json` (output-ul auditului NX-202) etichetează fiecare caz cu
`bucket` (keep/rewrite) + `role` (ro-quality / safety-grounding / locale-regression). Guard-ul de
aici asigură că manifestul rămâne SINCRON cu fixture-urile: orice caz nou trebuie clasificat, iar
manifestul nu poate referi cazuri inexistente. Fără el, clasificarea devine tăcut stale (exact
clasa de bug pe care inițiativa o combate).

Nu atinge aserțiunile golden (zero risc pe gate-ul CI) — verifică doar acoperirea + valorile enum.
"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden"
VALID_BUCKETS = {"keep", "rewrite"}
VALID_ROLES = {"ro-quality", "safety-grounding", "locale-regression"}


def _ids(fixture_file: str) -> set[str]:
    data = json.loads((GOLDEN / fixture_file).read_text(encoding="utf-8"))
    return {c["id"] for c in data}


def _classification() -> dict:
    return json.loads((GOLDEN / "classification.json").read_text(encoding="utf-8"))


def test_every_case_and_conversation_is_classified():
    """Fiecare caz din cases.json + conversations.json are o intrare în manifest.
    Un caz nou neclasificat = manifest stale → pică aici, nu tăcut."""
    cls = _classification()
    case_ids = _ids("cases.json")
    conv_ids = _ids("conversations.json")

    missing_cases = case_ids - set(cls["cases"])
    missing_conv = conv_ids - set(cls["conversations"])
    assert not missing_cases, f"cazuri neclasificate: {sorted(missing_cases)}"
    assert not missing_conv, f"conversații neclasificate: {sorted(missing_conv)}"


def test_manifest_has_no_phantom_entries():
    """Manifestul nu referă cazuri care nu mai există în fixtures (curățenie la ștergere)."""
    cls = _classification()
    case_ids = _ids("cases.json")
    conv_ids = _ids("conversations.json")

    phantom_cases = set(cls["cases"]) - case_ids
    phantom_conv = set(cls["conversations"]) - conv_ids
    assert not phantom_cases, f"clasificări fantomă (caz inexistent): {sorted(phantom_cases)}"
    assert not phantom_conv, f"conversații fantomă: {sorted(phantom_conv)}"


def test_bucket_and_role_values_are_valid():
    cls = _classification()
    for scope in ("cases", "conversations"):
        for cid, meta in cls[scope].items():
            assert meta["bucket"] in VALID_BUCKETS, f"{cid}: bucket invalid {meta['bucket']!r}"
            assert meta["role"] in VALID_ROLES, f"{cid}: role invalid {meta['role']!r}"


def test_locale_regression_is_frozen_not_in_ro_quality():
    """Decizia ro-only (2026-07-23): en/hu sunt DOAR locale-regression, niciodată ro-quality.
    Guard împotriva adăugării accidentale de conținut en/hu în setul de calitate — pe AMBELE
    colecții (cases + conversations au câmp `language`). Un dialog en/hu care primea ro-quality
    trecea tăcut înainte (gap prins de Codex pe PR #242)."""
    cls = _classification()
    for fixture_file, scope in (("cases.json", "cases"), ("conversations.json", "conversations")):
        items = json.loads((GOLDEN / fixture_file).read_text(encoding="utf-8"))
        lang = {c["id"]: c.get("language", "ro") for c in items}
        for cid, meta in cls[scope].items():
            if lang.get(cid) in ("en", "hu"):
                msg = f"{cid} ({lang[cid]}, {scope}) role={meta['role']!r}; en/hu=locale-regression"
                assert meta["role"] == "locale-regression", msg
