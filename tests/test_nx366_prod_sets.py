"""NX-366 felia C — seturile de conversații de test sunt înghețate: amprenta nu se schimbă tăcut.

Setul nevăzut (`heldout-2026-10-01.json`) a fost scris de un agent fără acces la analiza setului
din 2026-10-01 și înghețat înaintea oricărei reparații. Testul nu-i citește conținutul dincolo de
formă: verdictul final al reparațiilor se dă pe el, o singură dată, deci nu are voie să fie
„ajustat" pe drum.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.sim import prod_set_run

SETS = Path(__file__).parent / "golden" / "prod_sets"
MANIFEST = json.loads((SETS / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_set_matches_its_frozen_fingerprint(name):
    # Pe bytes normalizați CRLF→LF (ca NX-247): un checkout pe Windows nu e o editare.
    raw = (SETS / name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == MANIFEST[name]["sha256"], (
        f"{name} s-a schimbat după înghețare: un set nou primește alt fișier, nu o editare"
    )


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_set_shape_is_runnable(name):
    doc = prod_set_run.load_set(SETS / name)
    convs = doc["conversations"]
    assert len(convs) == MANIFEST[name]["conversations"]
    assert sum(len(c["turns"]) for c in convs) == MANIFEST[name]["messages"]
    assert len({c["id"] for c in convs}) == len(convs)
    assert all(
        isinstance(t["message"], str) and t["message"].strip() for c in convs for t in c["turns"]
    )


def test_heldout_is_declared_frozen():
    assert "frozen" in MANIFEST["heldout-2026-10-01.json"]
    assert MANIFEST["heldout-2026-10-01.json"]["sha256"] == (
        "cc72b384447a6995ccd8e4ae236e645a87aae09bfac931c452d804021041fdd3"
    )


def test_runner_is_dry_by_default(capsys):
    assert prod_set_run.main(["--set", str(SETS / "prod-2026-10-01.json")]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "10 conversații" in out and "36 ture" in out
