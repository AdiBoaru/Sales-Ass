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


def test_multiple_constraints_same_facet_not_collapsed():
    # Codex: două constrângeri pe ACEEAȘI fațetă (concerns=oily ȘI concerns=sensitive). Produsul
    # respectă doar `oily` → MISMATCH pe `sensitive` NU trebuie șters de MATCH pe `oily`.
    spec = QuerySpec(
        constraints=(
            Constraint("concerns", "contains", "oily", "hard"),
            Constraint("concerns", "contains", "sensitive", "hard"),
        )
    )
    p = {"id": "P", "attributes": {"concerns": ["oily"]}}  # are oily, NU sensitive
    ms = match_set([p], spec)
    assert ms["rejected"] == ["P"]  # nu „exact" — a doua constrângere e MISMATCH
    assert ms["exact"] == []


def test_bool_string_coercion_not_truthy():
    # Codex: `bool('false')` e True. Valoare string „false" vs constrângere bool True → MISMATCH,
    # nu MATCH accidental.
    p = {"id": "x", "fragrance_free": "false"}
    assert evaluate_constraint(p, Constraint("fragrance_free", "eq", True), None) == MISMATCH
    p2 = {"id": "y", "fragrance_free": "da"}
    assert evaluate_constraint(p2, Constraint("fragrance_free", "eq", True), None) == MATCH
    # token necunoscut → UNKNOWN (nu ghicim)
    p3 = {"id": "z", "fragrance_free": "poate"}
    assert evaluate_constraint(p3, Constraint("fragrance_free", "eq", True), None) == UNKNOWN
    # numeric non-0/1 → UNKNOWN (Codex: bool(2) e True → fals-pozitiv)
    p4 = {"id": "w", "fragrance_free": 2}
    assert evaluate_constraint(p4, Constraint("fragrance_free", "eq", True), None) == UNKNOWN
    # 1/0 sunt valide DOAR pe un facet bool DECLARAT (Codex R9: fără spec, int vs bool = UNKNOWN,
    # nu coerce). În realitate facet-ul bool ARE spec în registru.
    from src.domain.facets import FacetSpec

    b = FacetSpec("ff", "bool", ("eq",))
    assert evaluate_constraint({"ff": 1}, Constraint("ff", "eq", True), b) == MATCH
    assert evaluate_constraint({"ff": 0}, Constraint("ff", "eq", True), b) == MISMATCH
    assert (
        evaluate_constraint({"ff": 1}, Constraint("ff", "eq", True), None) == UNKNOWN
    )  # fără spec


def test_number_nan_and_text_are_unknown():
    # Codex R6: NaN/text nu se compară numeric → UNKNOWN (aliniat cu coverage, nu MISMATCH tăcut)
    c = Constraint("spf", "gte", 30)
    assert evaluate_constraint({"spf": float("nan")}, c, None) == UNKNOWN
    assert evaluate_constraint({"spf": "n/a"}, c, None) == UNKNOWN
    assert evaluate_constraint({"spf": 50}, c, None) == MATCH  # număr valid rămâne


def test_eq_uses_typed_helpers_bool_number_nan():
    from src.domain.facets import FacetSpec

    spec = FacetSpec("fragrance_free", "bool", ("eq",))
    # Codex R7: bool facet cu valori STRING → parse_bool pe AMBELE părți (nu _norm brut)
    assert (
        evaluate_constraint({"fragrance_free": "true"}, Constraint("ff", "eq", "da"), spec) == MATCH
    )
    assert (
        evaluate_constraint({"fragrance_free": "false"}, Constraint("ff", "eq", "da"), spec)
        == MISMATCH
    )
    # fără spec, ambii tokeni bool → tot parse_bool („da" == „true")
    assert evaluate_constraint({"x": "da"}, Constraint("x", "eq", "true"), None) == MATCH
    # NaN == NaN NU e MATCH (era prin _norm 'nan'=='nan')
    assert (
        evaluate_constraint({"spf": float("nan")}, Constraint("spf", "eq", float("nan")), None)
        == UNKNOWN
    )
    # numeric eq: 5 == 5.0 → MATCH (nu _norm „5" vs „5.0")
    assert evaluate_constraint({"spf": 5}, Constraint("spf", "eq", 5.0), None) == MATCH


def test_eq_number_spec_rejects_infinity_and_bool():
    from src.domain.facets import FacetSpec

    num = FacetSpec("spf", "number", ("eq",))
    # Codex R9: number + eq validează cu is_valid_number ÎNAINTE de conversie
    assert (
        evaluate_constraint({"spf": "Infinity"}, Constraint("spf", "eq", "Infinity"), num)
        == UNKNOWN
    )
    assert evaluate_constraint({"spf": float("inf")}, Constraint("spf", "eq", 5), num) == UNKNOWN
    # 1 == True pe un facet NUMBER → UNKNOWN (tip declarat are prioritate, nu coerce bool)
    assert evaluate_constraint({"spf": 1}, Constraint("spf", "eq", True), num) == UNKNOWN
    assert evaluate_constraint({"spf": 30}, Constraint("spf", "eq", 30.0), num) == MATCH
    assert evaluate_constraint({"spf": 30}, Constraint("spf", "eq", 31), num) == MISMATCH


def test_eq_spec_priority_over_runtime_type():
    from src.domain.facets import FacetSpec

    # bool facet: 1 == True → MATCH (1 e „true" pentru bool)
    b = FacetSpec("ff", "bool", ("eq",))
    assert evaluate_constraint({"ff": 1}, Constraint("ff", "eq", True), b) == MATCH
    assert evaluate_constraint({"ff": 2}, Constraint("ff", "eq", True), b) == UNKNOWN
    # fără spec: 1(int) vs True(bool) = tip incompatibil → UNKNOWN (nu coerce)
    assert evaluate_constraint({"x": 1}, Constraint("x", "eq", True), None) == UNKNOWN
    # text spec: egalitate de string
    t = FacetSpec("finish", "text", ("eq",))
    assert evaluate_constraint({"finish": "Matte"}, Constraint("finish", "eq", "matte"), t) == MATCH


def test_typed_bool_coverage_matches_match_gate():
    # Codex R8 §4: coverage și Match Gate DAU ACEEAȘI semantică pe bool. Valid în coverage ⟺ verdict
    # cunoscut (MATCH/MISMATCH) în Match Gate; invalid ⟺ UNKNOWN.
    from src.domain.facets import FacetSpec, facet_coverage

    spec = FacetSpec("ff", "bool", ("eq",))
    cases = [
        (True, True),
        (False, True),
        ("true", True),
        ("false", True),
        ("da", True),
        ("nu", True),
        (1, True),
        (0, True),
        (2, False),
        ("poate", False),
        ("", False),
    ]
    for val, valid in cases:
        cov = facet_coverage([{"ff": val}], spec)
        assert (cov["valid"] == 1) is valid, (val, cov)
        verdict = evaluate_constraint({"ff": val}, Constraint("ff", "eq", True), spec)
        assert (verdict in (MATCH, MISMATCH)) is valid, (val, verdict)


def test_typed_number_coverage_matches_match_gate():
    # Codex R8 §4: aceeași semantică pe number. bool/NaN/inf/text → invalid ⟺ UNKNOWN.
    from src.domain.facets import FacetSpec, facet_coverage

    spec = FacetSpec("spf", "number", ("gte",))
    cases = [
        (30, True),
        (30.5, True),
        ("30", True),
        (True, False),
        (float("nan"), False),
        (float("inf"), False),
        ("n/a", False),
    ]
    for val, valid in cases:
        cov = facet_coverage([{"spf": val}], spec)
        assert (cov["valid"] == 1) is valid, (val, cov)
        verdict = evaluate_constraint({"spf": val}, Constraint("spf", "gte", 10), spec)
        assert (verdict in (MATCH, MISMATCH)) is valid, (val, verdict)


def test_no_hard_constraints_all_exact():
    assert match_set([{"id": "x"}], QuerySpec())["exact"] == ["x"]
