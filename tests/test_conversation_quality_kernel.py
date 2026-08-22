"""NX-220 regressions for category fidelity and iZi-style follow-ups."""

from types import SimpleNamespace

from src.agent import deterministic as det
from src.domain.pack import FacetSpec
from src.models import ProductRef, Route
from src.tools import catalog_tools as ct
from src.worker import compose
from src.worker.canonicalize import canonicalize_clarify_field


def test_verbose_clarify_field_becomes_canonical_category():
    raw = "Nu este clar ce tip de cadou/categorie vrea pentru iubita."
    assert canonicalize_clarify_field(raw) == "category"


def test_verbose_budget_field_becomes_canonical_budget():
    assert canonicalize_clarify_field("Trebuie aflat bugetul maxim al clientului") == "budget_max"


def test_internal_product_followup_slots_are_preserved():
    assert canonicalize_clarify_field("product_for_reviews") == "product_for_reviews"
    assert canonicalize_clarify_field("product_for_details") == "product_for_details"


def test_category_is_never_dropped_when_hard_enabled(monkeypatch):
    monkeypatch.setattr(
        ct,
        "get_settings",
        lambda: SimpleNamespace(search_sort_mode_enabled=True, search_category_hard_enabled=True),
    )
    steps = ct._relax_ladder(
        price_max=100,
        facet_filters={"concerns": ["cadou pentru iubita"]},
        category=["machiaj"],
        in_stock_only=False,
    )
    assert len(steps) == 2
    assert all(step["category"] == ["machiaj"] for step in steps)
    assert steps[-1]["facet_filters"] is None


def test_category_drop_remains_available_as_kill_switch(monkeypatch):
    monkeypatch.setattr(
        ct,
        "get_settings",
        lambda: SimpleNamespace(search_sort_mode_enabled=True, search_category_hard_enabled=False),
    )
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["gift"]},
        category=["makeup"],
        in_stock_only=False,
    )
    assert steps[-1]["category"] is None


def test_review_snippet_is_not_a_recommendation_reason_by_default(monkeypatch):
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: SimpleNamespace(rich_review_anchor_enabled=False),
    )
    product = {"top_pros": ["discret, exact cum voiam"]}
    assert compose._recommendation_anchor(product, 0) is None


def test_product_detail_has_consultative_sections_from_grounded_data():
    product = {
        "id": "p1",
        "name": "Ruj roșu",
        "ai_summary": "Ruj solid cu finisaj perlat și rezistență la transfer.",
        "attributes": {"finish": "perlat", "coverage": "completă"},
        "review_summary": "Culoare plăcută și aplicare ușoară",
        "top_pros": ["rezistă bine"],
        "top_cons": ["poate usca buzele"],
        "rating": 4.4,
        "review_count": 35,
    }
    pack = SimpleNamespace(
        comparison_facets=(
            FacetSpec(key="finish", labels={"ro": "Finisaj"}),
            FacetSpec(key="coverage", labels={"ro": "Acoperire"}),
        )
    )
    ctx = SimpleNamespace(language="ro", business=SimpleNamespace(domain_pack=pack))

    answer = det._detail_answer(product, ctx)

    assert "De ce ți-l recomand" in answer
    assert "Caracteristici principale" in answer
    assert "✓ Finisaj: perlat" in answer
    assert "✓ Acoperire: completă" in answer
    assert "Ce spun clienții" in answer
    assert "35 recenzii" in answer


async def test_detail_followup_routes_to_deterministic_handler(monkeypatch):
    monkeypatch.setattr(
        det,
        "get_settings",
        lambda: SimpleNamespace(
            detail_intent_enabled=True,
            review_intent_enabled=True,
            link_intent_enabled=True,
            compare_intent_enabled=True,
        ),
    )
    called = {}

    async def fake_detail(ctx, deps, query):
        called["query"] = query

    monkeypatch.setattr(det, "_handle_detail_intent", fake_detail)
    ctx = SimpleNamespace(
        route=SimpleNamespace(route=Route.SALES, filters={}),
        message=SimpleNamespace(body="vreau mai multe detalii despre primul"),
        state=SimpleNamespace(
            displayed_products=[ProductRef("p1", "Ruj A", 10), ProductRef("p2", "Ruj B", 20)],
            pending_question=None,
        ),
    )

    assert await det.try_pre_intents(ctx, object()) is True
    assert called["query"] == "vreau mai multe detalii despre primul"


def test_comparison_supports_four_products():
    products = [{"id": f"p{i}", "name": f"Produs {i}", "price": float(i * 10)} for i in range(1, 5)]
    comparison = compose.build_comparison(products, "ro")
    assert comparison is not None
    assert [column.product_id for column in comparison.columns] == ["p1", "p2", "p3", "p4"]
