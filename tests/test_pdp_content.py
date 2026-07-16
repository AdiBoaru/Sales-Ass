"""NX-168e-2 — teste pt conținutul PDP derivat determinist (sections/ingredients/badges/reviews)."""

from scripts.pdp_content import badges, ingredient_list, reviews, sections, slugify


def _p(**over):
    p = {
        "slug": "auralis-hydra-ser",
        "name": "Auralis Hydra Ser",
        "rating": 4.8,
        "attributes": {
            "usage": {"time": ["morning", "evening"]},
            "key_benefit": "Hidratare intensă, reduce senzația de tensiune",
            "key_ingredients": ["acid hialuronic", "niacinamidă"],
            "fragrance_free": True,
            "best_for": "ten uscat",
        },
        "reviewSummary": {
            "topPros": ["se absoarbe rapid", "hidratează bine"],
            "topCons": ["preț mai mare"],
        },
    }
    p.update(over)
    return p


def test_slugify():
    assert slugify("Acid Hialuronic") == "acid-hialuronic"
    assert slugify("niacinamidă") == "niacinamida"


def test_sections_kinds():
    kinds = [s["kind"] for s in sections(_p())]
    assert kinds == ["usage", "benefits", "ingredients"]
    body = next(s["body"] for s in sections(_p()) if s["kind"] == "usage")
    assert "dimineața" in body and "seara" in body


def test_sections_warnings_from_contraindication():
    p = _p()
    p["attributes"]["not_recommended_for"] = [
        {"value": "sensitive", "level": "soft", "reason": "x"}
    ]
    assert any(s["kind"] == "warnings" for s in sections(p))


def test_ingredient_list():
    assert ingredient_list(_p()) == ["acid hialuronic", "niacinamidă"]


def test_badges_derived_from_real_attrs():
    assert "Fără parfum" in badges(_p())  # fragrance_free
    assert "Best-seller" in badges(_p())  # rating 4.8 >= 4.7
    assert "Cu SPF 30" in badges(_p(attributes={**_p()["attributes"], "spf": 30}))
    assert badges(_p(rating=4.2, attributes={"key_benefit": "x"})) == []  # niciun semnal


def test_reviews_deterministic_and_valid():
    r1 = reviews(_p())
    r2 = reviews(_p())
    assert r1 == r2  # determinist (fără random)
    assert len(r1) == 3  # 2 pozitive (pros) + 1 mixt (con)
    assert [x["external_id"] for x in r1] == [
        "auralis-hydra-ser-r1",
        "auralis-hydra-ser-r2",
        "auralis-hydra-ser-r3",
    ]
    assert all(1 <= x["rating"] <= 5 for x in r1)
    assert "preț mai mare" in r1[-1]["body"]  # recenzia mixtă folosește con-ul real


def test_reviews_no_summary_empty():
    assert reviews({"slug": "x", "rating": 4.5, "reviewSummary": {}}) == []
