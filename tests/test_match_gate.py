"""NX-187 — Match Gate (shadow): MatchSet DISJUNCT + verdict MATCH/MISMATCH/UNKNOWN (pur)."""

from src.agent.match_gate import MATCH, MISMATCH, UNKNOWN, evaluate_constraint, match_set
from src.agent.query_spec import Constraint, QuerySpec


def test_evaluate_constraint_verdicts():
    p = {"price": 72, "fragrance_free": True, "attributes": {"suitable_for": ["sensitive"]}}
    assert evaluate_constraint(p, Constraint("price", "lte", 80), None) == MATCH
    assert evaluate_constraint(p, Constraint("price", "lte", 50), None) == MISMATCH
    assert evaluate_constraint(p, Constraint("fragrance_free", "eq", True), None) == MATCH
    assert (
        evaluate_constraint(p, Constraint("suitable_for", "contains", "sensitive"), None) == MATCH
    )
    # valoare lipsă → UNKNOWN (NU MISMATCH)
    assert (
        evaluate_constraint({"id": "x"}, Constraint("fragrance_free", "eq", True), None) == UNKNOWN
    )


def test_match_set_disjoint_codex_example():
    spec = QuerySpec(
        constraints=(
            Constraint("price", "lte", 80, "hard"),
            Constraint("fragrance_free", "eq", True, "hard"),
            Constraint("suitable_for", "contains", "sensitive", "hard"),
        )
    )
    a = {
        "id": "A",
        "price": 72,
        "fragrance_free": True,
        "attributes": {"suitable_for": ["sensitive"]},
    }
    b = {
        "id": "B",
        "price": 65,
        "attributes": {"suitable_for": ["sensitive"]},
    }  # fragrance lipsă → UNKNOWN
    c = {
        "id": "C",
        "price": 95,
        "fragrance_free": True,
        "attributes": {"suitable_for": ["sensitive"]},
    }
    d = {
        "id": "D",
        "price": 50,
        "fragrance_free": False,
        "attributes": {"suitable_for": ["sensitive"]},
    }
    ms = match_set([a, b, c, d], spec)
    assert ms["exact"] == ["A"]
    assert ms["alternatives"] == ["B"]  # hard UNKNOWN → alternative (nu exact)
    assert set(ms["rejected"]) == {"C", "D"}  # buget / fragrance MISMATCH


def test_soft_constraint_ignored_for_membership():
    # all-hard-MATCH + soft mismatch → EXACT (soft = doar ranking, nu apartenență)
    spec = QuerySpec(
        constraints=(
            Constraint("price", "lte", 80, "hard"),
            Constraint("rating", "gte", 5, "soft"),
        )
    )
    p = {"id": "A", "price": 50, "rating": 4}  # soft mismatch, dar ignorat
    assert match_set([p], spec)["exact"] == ["A"]


def test_no_hard_constraints_all_exact():
    assert match_set([{"id": "x"}], QuerySpec())["exact"] == ["x"]
