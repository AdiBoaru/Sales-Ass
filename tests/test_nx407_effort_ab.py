"""NX-407 — sonda A/B a efortului: dry-run fără apeluri, perechi oarbe reproductibile, regula GO
pre-înregistrată (egalitățile nu intră în numitor, o poartă nemăsurată nu dă GO)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sim import agent_effort_ab as ab

SETS = Path(__file__).parent / "golden" / "prod_sets"


def _turn(msg, secs, *, served="agent", reasoning=0, reason=None, content="text"):
    return {
        "message": msg,
        "seconds": secs,
        "served_by": served,
        "reasoning_tokens": reasoning,
        "reason": reason,
        "content": content,
        "products": [{"name": "Crema", "price": 50, "reason": "lejeră"}],
        "suggestions": ["Ai ceva mai ieftin?"],
    }


RUN = {
    "set": "t",
    "efforts": {
        "low": {
            "c1": {
                "turns": [_turn("salut", 4.0, content="low-1"), _turn("cum?", 6.0, content="low-2")]
            },
            "c2": {"turns": [_turn("spf", 8.0, served="fallback", reason="model_error")]},
        },
        "medium": {
            "c1": {
                "turns": [
                    _turn("salut", 7.0, reasoning=120, content="med-1"),
                    _turn("cum?", 9.0, reasoning=300, content="med-2"),
                ]
            },
            "c2": {"turns": [_turn("spf", 25.0, served="fallback", reason="round_timeout")]},
        },
    },
}


def test_the_dry_run_counts_and_spends_nothing(capsys):
    assert ab.main(["--set", str(SETS / "izi-2026-10-10.json")]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "1 conversații" in out and "10 ture de agent" in out


def test_the_heldout_set_needs_final_and_efforts_are_a_closed_vocabulary():
    with pytest.raises(SystemExit):
        ab.main(["--set", str(SETS / "heldout-2026-10-01.json")])
    with pytest.raises(SystemExit):
        ab.main(["--set", str(SETS / "izi-2026-10-10.json"), "--efforts", "low,high"])
    with pytest.raises(SystemExit):
        ab.main(["--set", str(SETS / "izi-2026-10-10.json"), "--efforts", "low,low"])


def test_percentile_and_summary():
    assert ab.percentile([], 0.5) is None
    assert ab.percentile([1.0, 3.0], 0.5) == 2.0
    s = ab.summary(RUN)
    assert s["low"]["p50_s"] == 6.0 and s["low"]["fallbacks"] == 1
    assert s["medium"]["round_timeouts"] == 1 and s["medium"]["turns_with_reasoning"] == 2
    assert s["low"]["turns_with_reasoning"] == 0


def test_blind_pairs_hide_the_effort_and_are_reproducible():
    md, key = ab.blind_pairs(RUN, "low", "medium", seed=7)
    assert set(key) == {"c1#1", "c1#2", "c2#1"}
    assert all(sorted(v.values()) == ["low", "medium"] for v in key.values())
    assert "low" not in md.replace("low-", "") and "medium" not in md.replace("med-", "")
    assert "seconds" not in md and "reasoning" not in md
    assert ab.blind_pairs(RUN, "low", "medium", seed=7) == (md, key)
    a_is = key["c1#1"]["A"]
    block = md.split("### c1#1")[1].split("### ")[0]
    assert (
        block.index("**A**")
        < block.index("low-1" if a_is == "low" else "med-1")
        < block.index("**B**")
    )


def _key():
    return {
        "c1#1": {"A": "low", "B": "medium"},
        "c1#2": {"A": "medium", "B": "low"},
        "c2#1": {"A": "low", "B": "medium"},
    }


_OK = {"conversations": 12, "unmeasured": 0}
STATS = {
    "low": {**_OK, "p50_s": 6.0, "p90_s": 9.0, "fallbacks": 1},
    "medium": {**_OK, "p50_s": 8.0, "p90_s": 15.0, "fallbacks": 1},
}


def test_go_needs_preference_latency_and_fallbacks():
    votes = {"c1#1": "B", "c1#2": "A", "c2#1": "="}
    v = ab.verdict(votes, _key(), STATS, base="low", candidate="medium")
    assert v["verdict"] == "GO" and v["preferred"] == 2 and v["against"] == 0
    slow = {**STATS, "medium": {**_OK, "p50_s": 9.5, "p90_s": 15.0, "fallbacks": 1}}
    assert ab.verdict(votes, _key(), slow, base="low", candidate="medium")["verdict"] == "NO-GO"
    late = {**STATS, "medium": {**_OK, "p50_s": 8.0, "p90_s": 21.0, "fallbacks": 1}}
    assert ab.verdict(votes, _key(), late, base="low", candidate="medium")["verdict"] == "NO-GO"
    falls = {**STATS, "medium": {**_OK, "p50_s": 8.0, "p90_s": 15.0, "fallbacks": 3}}
    assert ab.verdict(votes, _key(), falls, base="low", candidate="medium")["verdict"] == "NO-GO"


def test_ties_do_not_count_and_no_votes_is_insufficient():
    split = {"c1#1": "B", "c1#2": "B", "c2#1": "="}
    v = ab.verdict(split, _key(), STATS, base="low", candidate="medium")
    assert v["preference"] == 0.5 and v["verdict"] == "NO-GO"
    none = ab.verdict({"c1#1": "=", "c1#2": ""}, _key(), STATS, base="low", candidate="medium")
    assert none["verdict"] == "INSUFFICIENT" and none["preference"] is None
    missing = ab.verdict({"c1#1": "B"}, _key(), {"low": {}}, base="low", candidate="medium")
    assert missing["verdict"] == "INSUFFICIENT"


def test_an_unmeasured_turn_or_a_small_set_is_never_go():
    """Recenzia adversarială: o conversație nealiniată pierdea `served_by`, deci căderile ieșeau 0
    și verdictul GO; iar `--only` putea da GO pe sub 10 conversații."""
    votes = {"c1#1": "B", "c1#2": "A"}
    blind = {**STATS, "medium": {**STATS["medium"], "unmeasured": 1}}
    assert ab.verdict(votes, _key(), blind, base="low", candidate="medium")["verdict"] == (
        "INSUFFICIENT"
    )
    small = {k: {**v, "conversations": 3} for k, v in STATS.items()}
    assert ab.verdict(votes, _key(), small, base="low", candidate="medium")["verdict"] == (
        "INSUFFICIENT"
    )
    lost = {
        "efforts": {
            "medium": {
                "c1": {"aligned": False, "turns": [{"message": "x", "wall_s": 3.0}]},
                "c2": {"turns": [{"message": "y", "error": "OSError"}]},
            }
        }
    }
    s = ab.summary(lost)["medium"]
    assert s["unmeasured"] == 2 and s["fallbacks"] == 0 and s["p50_s"] is None


def test_efforts_alternate_per_conversation():
    assert ab.effort_order(["low", "medium"], 0) == ["low", "medium"]
    assert ab.effort_order(["low", "medium"], 1) == ["medium", "low"]
