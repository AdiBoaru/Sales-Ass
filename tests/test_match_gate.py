"""NX-187 — Match Gate (pur, fără DB/LLM): tri-state + MatchSet disjunct + soft = doar scor."""

from src.agent.match_gate import (
    build_match_set,
    classify_product,
    evaluate,
)
from src.agent.query_spec import Constraint
from src.domain.facets import FacetSource, FacetType, TypedFacet

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte", "gte", "eq"),
)
_FF = TypedFacet(
    key="fragrance_free",
    value_type=FacetType.BOOL,
    source=FacetSource.ATTRIBUTE,
    source_key="fragrance_free",
    operators=("eq",),
)
_CONCERNS = TypedFacet(
    key="concerns",
    value_type=FacetType.LIST,
    source=FacetSource.ATTRIBUTE,
    source_key="concerns",
    operators=("contains",),
)
_FINISH = TypedFacet(
    key="finish",
    value_type=FacetType.ENUM,
    source=FacetSource.ATTRIBUTE,
    source_key="finish",
    operators=("eq",),
    values=("matte", "dewy"),
)
_REG = {f.key: f for f in (_PRICE, _FF, _CONCERNS, _FINISH)}


def _hard(facet, op, value):
    return Constraint(facet=facet, op=op, value=value, strength="hard", source="current_turn")


def _soft(facet, op, value):
    return Constraint(facet=facet, op=op, value=value, strength="soft", source="current_turn")


def _p(pid, price=None, **attrs):
    return {"id": pid, "price": price, "attributes": attrs}


# --- evaluate: tri-state ----------------------------------------------------


def test_evaluate_number():
    assert evaluate(_PRICE, "lte", 80, 50) == "MATCH"
    assert evaluate(_PRICE, "lte", 80, 120) == "MISMATCH"
    assert evaluate(_PRICE, "lte", 80, None) == "UNKNOWN"  # lipsă ≠ MISMATCH (D7)


def test_evaluate_bool_and_list():
    assert evaluate(_FF, "eq", True, True) == "MATCH"
    assert evaluate(_FF, "eq", True, False) == "MISMATCH"
    assert evaluate(_FF, "eq", True, None) == "UNKNOWN"
    assert evaluate(_CONCERNS, "contains", "sensitive", ["sensitive", "dry"]) == "MATCH"
    assert evaluate(_CONCERNS, "contains", "sensitive", ["oily"]) == "MISMATCH"
    assert evaluate(_CONCERNS, "contains", "sensitive", None) == "UNKNOWN"


# --- MatchSet: precedență disjunctă -----------------------------------------


def test_all_hard_match_is_exact():
    v = classify_product(
        _p("a", price=50, fragrance_free=True, concerns=["sensitive"]),
        [
            _hard("price", "lte", 80),
            _hard("fragrance_free", "eq", True),
            _hard("concerns", "contains", "sensitive"),
        ],
        _REG,
    )
    assert v.match_class == "exact"


def test_hard_unknown_without_mismatch_is_alternative():
    # fragrance_free lipsă → hard UNKNOWN, restul MATCH → alternative (nu exact, nu rejected)
    v = classify_product(
        _p("b", price=50, concerns=["sensitive"]),  # fără fragrance_free
        [_hard("price", "lte", 80), _hard("fragrance_free", "eq", True)],
        _REG,
    )
    assert v.match_class == "alternative"


def test_hard_mismatch_is_rejected_even_with_unknown():
    # buget depășit (MISMATCH) + fragrance_free lipsă (UNKNOWN) → rejected (MISMATCH are precedență)
    v = classify_product(
        _p("c", price=200),
        [_hard("price", "lte", 80), _hard("fragrance_free", "eq", True)],
        _REG,
    )
    assert v.match_class == "rejected"


def test_soft_mismatch_stays_exact_only_penalized():
    # toate hard MATCH + un soft mismatch (finish) → EXACT, nu alternative; doar soft_penalty crește
    v = classify_product(
        _p("d", price=50, fragrance_free=True, finish="dewy"),
        [
            _hard("price", "lte", 80),
            _hard("fragrance_free", "eq", True),
            _soft("finish", "eq", "matte"),
        ],
        _REG,
    )
    assert v.match_class == "exact"  # soft NU schimbă apartenența
    assert v.soft_penalty == 1  # doar scorul


def test_unknown_facet_is_unknown_not_match():
    # constrângere pe o fațetă absentă din registru → UNKNOWN (conservator), deci alternative
    v = classify_product(
        _p("e", price=50),
        [_hard("price", "lte", 80), _hard("spf", "gte", 30)],  # spf nu e în _REG
        _REG,
    )
    assert v.match_class == "alternative"
    assert {r.facet: r.status for r in v.constraint_results}["spf"] == "UNKNOWN"


def test_facet_key_alias_singular_plural():
    # QuerySpec emite „concern" (singular); registrul are „concerns" → aliasul le leagă
    v = classify_product(
        _p("f", concerns=["sensitive"]),
        [_hard("concern", "contains", "sensitive")],
        _REG,
    )
    assert v.match_class == "exact"


# --- DoD happy: setul complet A/B/C/D ---------------------------------------


def test_dod_happy_full_matchset():
    # „fără parfum, sub 80, ten sensibil"
    constraints = [
        _hard("price", "lte", 80),
        _hard("fragrance_free", "eq", True),
        _hard("concerns", "contains", "sensitive"),
    ]
    products = [
        _p("A", price=50, fragrance_free=True, concerns=["sensitive"]),  # exact
        _p("B", price=50, concerns=["sensitive"]),  # UNKNOWN parfum → alternative
        _p("C", price=200, fragrance_free=True, concerns=["sensitive"]),  # buget → rejected
        _p("D", price=50, fragrance_free=False, concerns=["sensitive"]),  # parfum → rejected
    ]
    ms = build_match_set(products, constraints, _REG)
    assert ms.exact == ("A",)
    assert ms.alternatives == ("B",)
    assert set(ms.rejected) == {"C", "D"}
    # mulțimi DISJUNCTE
    assert not (set(ms.exact) & set(ms.alternatives) & set(ms.rejected))
    assert len(ms.verdicts) == 4


def test_coverage_aggregate_reports_unknown():
    # fragrance_free UNKNOWN peste tot (niciun produs nu-l are) → distribuție 100% UNKNOWN
    products = [_p("x", price=50, concerns=["dry"]), _p("y", price=60, concerns=["oily"])]
    ms = build_match_set(
        products, [_hard("fragrance_free", "eq", True), _hard("price", "lte", 100)], _REG
    )
    cov = {r.facet: r for r in ms.coverage}
    assert (cov["fragrance_free"].match, cov["fragrance_free"].unknown) == (0, 2)
    assert (cov["price"].match, cov["price"].unknown) == (2, 0)


def test_coverage_preserves_distribution_not_collapsed():
    # Review #248: fațetă cu MATCH + UNKNOWN → distribuția PĂSTREAZĂ ambele (nu comprimă în MATCH)
    products = [_p("a", fragrance_free=True), _p("b")]  # unul are, unul nu
    ms = build_match_set(products, [_hard("fragrance_free", "eq", True)], _REG)
    ff = ms.coverage[0]
    assert ff.match == 1 and ff.unknown == 1 and ff.mismatch == 0  # UNKNOWN NU se pierde


def test_invalid_operator_is_unknown_not_match():
    # Review #248: operator neacceptat de fațetă (contains pe bool) → UNKNOWN, nu MATCH accidental
    assert evaluate(_FF, "contains", True, True) == "UNKNOWN"
    assert evaluate(_PRICE, "typo", 100, 50) == "UNKNOWN"  # op inexistent → UNKNOWN, nu eq tăcut


def test_missing_query_value_is_unknown():
    assert evaluate(_PRICE, "lte", None, 50) == "UNKNOWN"  # valoare de query lipsă → UNKNOWN


def test_non_bool_value_for_bool_facet_is_unknown():
    assert evaluate(_FF, "eq", "true", True) == "UNKNOWN"  # valoare invalidă pt bool → UNKNOWN


# --- shadow hook (kill-switch OFF by default, zero behavior change) ----------


def _ctx():
    from src.domain.loader import load_domain_pack
    from src.models import BusinessConfig, Contact, InboundMessage, TurnContext

    biz = BusinessConfig(id="b", slug="s", name="n", vertical="beauty_salon", settings={})
    biz.domain_pack = load_domain_pack(biz)
    return TurnContext(
        turn_id="t",
        business=biz,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="fond fără parfum sub 80"),
        conversation_id="conv",
        language="ro",
    )


def test_shadow_disabled_by_default(monkeypatch):
    from src.agent.planner import _match_gate_shadow
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "match_gate_shadow_enabled", False)
    ctx = _ctx()
    _match_gate_shadow(ctx, [_p("a", price=50, fragrance_free=True)], "fond fără parfum sub 80")
    assert ctx.match_set is None
    assert not [e for e in ctx.events if e.type.startswith("match_gate")]


def test_shadow_enabled_computes_matchset_no_pii(monkeypatch):
    from src.agent.planner import _match_gate_shadow
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "match_gate_shadow_enabled", True)
    ctx = _ctx()
    _match_gate_shadow(ctx, [_p("a", price=50, fragrance_free=True)], "fond fără parfum sub 80")
    assert ctx.match_set is not None  # MatchSet calculat în shadow
    evs = [e for e in ctx.events if e.type == "match_gate_shadow"]
    assert len(evs) == 1
    blob = repr(evs[0].properties)
    assert "fond" not in blob and "parfum" not in blob  # zero text brut/PII, doar numere+clase
    assert evs[0].properties["n_candidates"] == 1
