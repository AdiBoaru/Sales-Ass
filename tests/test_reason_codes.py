"""NX-170 — reason_codes + gate not_recommended_for (hard/soft) + embedding doc determinist."""

from src.jobs.embed_products import _embed_text
from src.tools.catalog_tools import _brief
from src.tools.reason_codes import annotate, not_recommended_gate, reason_codes


def _p(**over):
    p = {
        "id": "1",
        "name": "Velora Ser",
        "price": 50.0,
        "attributes": {
            "concerns": ["oily"],
            "suitable_for": ["oily"],
            "key_ingredients": ["niacinamidă"],
        },
    }
    p.update(over)
    return p


# --- reason_codes ------------------------------------------------------------------------------


def test_reason_codes_all_three():
    codes = reason_codes(_p(), concerns=["oily"], price_max=80, features=["niacinamidă"])
    assert codes == ["concern_match", "budget_match", "ingredient_match"]  # ordine stabilă


def test_reason_codes_budget_miss_and_no_request():
    assert "budget_match" not in reason_codes(_p(price=100.0), price_max=80)
    assert reason_codes(_p()) == []  # nicio cerere → niciun cod


# --- gate not_recommended_for (severitate) -----------------------------------------------------


def _with_nrf(**nrf):
    p = _p()
    p["attributes"]["not_recommended_for"] = [nrf]
    return p


def test_gate_hard_verified_excludes():
    p = _with_nrf(
        value="sensitive", level="hard", source="manufacturer_label", verified_at="2026-07-16"
    )
    assert not_recommended_gate(p, concerns=["sensitive"]) == (True, None)


def test_gate_soft_warns_not_excluded():
    excl, warn = not_recommended_gate(
        _with_nrf(value="sensitive", level="soft", reason="acid"), concerns=["sensitive"]
    )
    assert excl is False and warn and "sensitive" in warn


def test_gate_hard_unverified_is_soft():
    excl, warn = not_recommended_gate(
        _with_nrf(value="sensitive", level="hard"), concerns=["sensitive"]
    )
    assert excl is False and warn  # hard NEverificat → atenționare, NU excludere


def test_gate_no_requested_concern_no_effect():
    p = _with_nrf(value="sensitive", level="hard", source="x", verified_at="y")
    assert not_recommended_gate(p, concerns=None) == (False, None)  # nu excludem preventiv


# --- annotate (filtrare + adnotare + soft la coadă) --------------------------------------------


def test_annotate_filters_hard_orders_soft_last():
    hard = _with_nrf(value="sensitive", level="hard", source="x", verified_at="y")
    hard["id"] = "h"
    soft = _with_nrf(value="sensitive", level="soft")
    soft["id"] = "s"
    ok = _p(id="ok")
    out = annotate([ok, soft, hard], concerns=["sensitive"], price_max=80)
    ids = [p["id"] for p in out]
    assert "h" not in ids  # hard exclus
    assert ids[-1] == "s"  # soft coborât la coadă
    # cererea e „sensitive" (pt gate); produsele au concerns „oily" → doar budget_match (≤80)
    assert out[0]["reason_codes"] == ["budget_match"]
    assert out[-1].get("warning")


# --- _brief randează reason + warning ----------------------------------------------------------


def test_brief_renders_reason_and_warning():
    p = _p(availability="in_stock")
    p["reason_codes"] = ["concern_match", "budget_match"]
    p["warning"] = "nu e ideal pentru sensitive"
    v = _brief([p])
    assert "potrivire: pe nevoia ta, în buget" in v
    assert "⚠ nu e ideal pentru sensitive" in v


# --- embedding doc determinist -----------------------------------------------------------------


def test_embed_text_deterministic_excludes_not_recommended():
    row = {
        "name": "Velora Ser",
        "brand": "Velora",
        "category": "Seruri",
        "ai_summary": "ser hidratant",
        "attributes": {
            "concerns": ["oily"],
            "finish": "matte",
            "key_ingredients": ["niacinamidă"],
            "not_recommended_for": [{"value": "sensitive"}],
        },
    }
    t = _embed_text(row)
    assert "Velora Ser" in t and "Seruri" in t
    assert "Potrivit pentru: oily" in t and "Finish: matte" in t and "niacinamidă" in t
    assert "sensitive" not in t  # not_recommended_for NU intră în embedding-ul pozitiv
    assert _embed_text(row) == t  # determinist
