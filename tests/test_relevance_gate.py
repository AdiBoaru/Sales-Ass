"""NX-257 — poarta de POTRIVIRE: un produs ale cărui date CONTRAZIC cererea iese.

Fațetele de aici sunt SINTETICE și dintr-un vertical care nu există în repo (electronice:
`voltage` partiționantă, `use_case` aditivă). Deliberat: mecanismul trebuie să fie general, iar
un test scris pe „ten gras" ar trece și dacă am fi hardcodat beauty. Dacă poarta funcționează pe
voltaj, funcționează pe orice fațetă pe care un tenant o declară partiționantă.
"""

from src.agent.match_gate import build_match_set
from src.agent.query_spec import Constraint
from src.agent.relevance_gate import apply_mask
from src.domain.facets import FacetConfigError, build_facets

# `voltage`: cumpărătorul are exact una (priza lui e 230V sau nu e) → contradicție posibilă.
# `use_case`: obiectiv, se acumulează → un produs care nu-l poartă nu contrazice pe nimeni.
_FACETS_RAW = [
    {
        "key": "voltage",
        "value_type": "enum",
        "source": "attribute",
        "source_key": "voltage",
        "operators": ["eq"],
        "values": ["110v", "230v"],
        "binding": "partitioning",
        "min_coverage": 0.5,
    },
    {
        "key": "use_case",
        "value_type": "list",
        "source": "attribute",
        "source_key": "use_case",
        "operators": ["contains"],
        "values": ["travel", "studio", "gaming"],
        "binding": "additive",
    },
]
FACETS = {f.key: f for f in build_facets(_FACETS_RAW)}


def _p(pid: str, **attrs):
    return {"id": pid, "name": pid, "price": 10.0, "attributes": attrs}


def _mask(products, constraints):
    ms = build_match_set(products, tuple(constraints), FACETS)
    return apply_mask(products, ms, FACETS)


# --- fațetă partiționantă: contradicția exclude, necunoscutul trece ------------------------


def test_partitioning_mismatch_is_excluded_unknown_survives():
    products = [
        _p("ok", voltage="230v"),
        _p("contra", voltage="110v"),
        _p("neetichetat"),  # fără valoare → UNKNOWN
    ]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="hard")])
    assert [p["id"] for p in out.kept] == ["ok", "neetichetat"]  # D7: UNKNOWN trece MEREU
    assert out.excluded_ids == ("contra",)
    assert out.enforced_facets == ("voltage",)


def test_additive_facet_never_excludes():
    """`use_case` e obiectiv: un produs care nu-l poartă nu contrazice nimic. Chiar declarat hard,
    nu are voie să excludă — binding-ul fațetei bate tăria constrângerii."""
    products = [_p("are", use_case=["travel"]), _p("nu_are", use_case=["studio"])]
    out = _mask(
        products, [Constraint(facet="use_case", op="contains", value="travel", strength="hard")]
    )
    assert [p["id"] for p in out.kept] == ["are", "nu_are"]
    assert out.excluded_ids == ()
    assert out.enforced_facets == ()


def test_soft_constraint_on_partitioning_facet_does_not_exclude():
    """Tăria contează și ea: pe fațetă partiționantă, dar `soft` (nerostit de client), rankingul
    o simte, apartenența nu."""
    products = [_p("ok", voltage="230v"), _p("contra", voltage="110v")]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="soft")])
    assert out.excluded_ids == ()


# --- pragul de acoperire: sub el, fațeta nu capătă drept de excludere ----------------------


def test_low_coverage_disables_enforcement_and_is_reported():
    """Un singur produs din patru are voltajul declarat (25% < 50%): nu putem distinge
    „contrazice" de „neetichetat", deci fațeta nu taie — și o spune."""
    products = [_p("contra", voltage="110v"), _p("a"), _p("b"), _p("c")]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="hard")])
    assert out.excluded_ids == ()
    assert out.skipped_low_coverage == ("voltage",)
    assert out.enforced_facets == ()


def test_coverage_at_threshold_enforces():
    products = [_p("contra", voltage="110v"), _p("ok", voltage="230v"), _p("x"), _p("y")]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="hard")])
    assert out.excluded_ids == ("contra",)  # 50% == pragul declarat → aplică


# --- degradări și granițe -------------------------------------------------------------------


def test_no_match_set_is_a_noop():
    products = [_p("a", voltage="110v")]
    out = apply_mask(products, None, FACETS)
    assert [p["id"] for p in out.kept] == ["a"]
    assert not out.changed


def test_mask_may_empty_the_set_and_says_so():
    """Dacă TOT ce am găsit contrazice cererea, setul gol e răspunsul onest. Poarta nu repopulează
    (un răspuns plin și greșit e mai rău decât unul gol și corect)."""
    products = [_p("c1", voltage="110v"), _p("c2", voltage="110v")]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="hard")])
    assert out.kept == ()
    assert set(out.excluded_ids) == {"c1", "c2"}


def test_order_is_preserved():
    products = [_p("a", voltage="230v"), _p("bad", voltage="110v"), _p("b", voltage="230v")]
    out = _mask(products, [Constraint(facet="voltage", op="eq", value="230v", strength="hard")])
    assert [p["id"] for p in out.kept] == ["a", "b"]  # rankingul nu se rescrie aici


def test_unknown_facet_in_registry_cannot_exclude():
    """Constrângere pe o fațetă pe care registrul n-o cunoaște: nu are binding, deci nu taie."""
    products = [_p("a", voltage="110v")]
    ms = build_match_set(
        products, (Constraint(facet="inventata", op="eq", value="x", strength="hard"),), FACETS
    )
    out = apply_mask(products, ms, FACETS)
    assert out.excluded_ids == ()


# --- registrul: binding declarat, validat, fail-closed --------------------------------------


def test_binding_defaults_to_additive():
    (facet,) = build_facets(
        [{"key": "k", "value_type": "text", "source": "attribute", "source_key": "k"}]
    )
    assert facet.binding == "additive"  # nicio fațetă nu capătă putere de excludere prin tăcere


def test_invalid_binding_is_rejected_fail_closed():
    """Vocabular ÎNCHIS: un binding inventat respinge fațeta (nu o încarcă cu un default tăcut)."""
    assert (
        build_facets(
            [
                {
                    "key": "k",
                    "value_type": "text",
                    "source": "attribute",
                    "source_key": "k",
                    "binding": "cumva",
                }
            ]
        )
        == ()
    )
    try:
        from src.domain.facets import _build_one

        _build_one(
            {
                "key": "k",
                "value_type": "text",
                "source": "attribute",
                "source_key": "k",
                "binding": "cumva",
            }
        )
    except FacetConfigError as e:
        assert "binding" in str(e)
    else:  # pragma: no cover
        raise AssertionError("binding invalid ar fi trebuit respins")
