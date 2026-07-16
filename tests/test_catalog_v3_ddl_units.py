"""NX-171b/c — teste UNIT (CI-fast, fără DB) pentru părțile pure ale feliilor de catalog v3:
`derive_relations` (171b, backfill relații) + `classify_content_status` (171c, backfill status).

Perechile integration (FK cross-tenant, filtru read-path, anti-duplicate embeddings) sunt în
`tests/test_catalog_v3_ddl_integration.py`.
"""

import json
from pathlib import Path

from scripts.seed_catalog_v2 import derive_relations
from src.jobs.backfill_content_status import classify_content_status

_JSON = Path(__file__).resolve().parents[1] / "db" / "seed" / "catalog_v2.json"
_VALID_KINDS = {"substitute", "complement", "accessory", "routine_next"}


def _catalog() -> dict:
    return json.loads(_JSON.read_text(encoding="utf-8"))


# --- 171b: derive_relations -------------------------------------------------------------------


def test_routine_next_chains_same_brand_first():
    """Un produs la pasul `cleanse` se leagă (routine_next) de produsele de la următorul pas ocupat
    (`tone`), cu același brand ÎNAINTEA altui brand (rutina din aceeași gamă = pasul 0)."""
    products = [
        {
            "slug": "a",
            "brandSlug": "x",
            "primaryCategorySlug": "cleanser",
            "rating": 4.5,
            "price": 50,
            "attributes": {"routine_step": "cleanse", "concerns": ["dry"]},
        },
        {
            "slug": "b",
            "brandSlug": "x",
            "primaryCategorySlug": "toner",
            "rating": 4.6,
            "price": 40,
            "attributes": {"routine_step": "tone", "concerns": ["dry"]},
        },
        {
            "slug": "c",
            "brandSlug": "y",
            "primaryCategorySlug": "toner",
            "rating": 4.9,
            "price": 45,
            "attributes": {"routine_step": "tone"},
        },
    ]
    rn = [
        r
        for r in derive_relations(products)
        if r["kind"] == "routine_next" and r["product_slug"] == "a"
    ]
    assert [r["related_slug"] for r in rn] == ["b", "c"]  # same-brand (b) înaintea lui c
    assert [r["position"] for r in rn] == [0, 1]


def test_derive_relations_deterministic_no_self_no_dup():
    """Idempotent (aceeași ieșire la re-rulare), fără self-relation, fără duplicat (product,related,
    kind), kind mereu în enum-ul canonic — pe catalogul REAL (150 produse)."""
    products = _catalog()["products"]
    first = derive_relations(products)
    assert first == derive_relations(products)  # determinist
    seen = set()
    for r in first:
        assert r["product_slug"] != r["related_slug"], "self-relation"
        assert r["kind"] in _VALID_KINDS
        assert r["position"] >= 0
        key = (r["product_slug"], r["related_slug"], r["kind"])
        assert key not in seen, f"duplicat {key}"
        seen.add(key)
    kinds = {r["kind"] for r in first}
    assert {"routine_next", "complement", "substitute"} <= kinds  # toate 3 derivate


def test_substitute_is_cheaper_or_equal_same_category():
    """`substitute` = alternativă în aceeași categorie primară, la preț ≤ ancoră (nu scumpește)."""
    products = _catalog()["products"]
    by_slug = {p["slug"]: p for p in products}
    for r in derive_relations(products):
        if r["kind"] == "substitute":
            anchor = by_slug[r["product_slug"]]
            alt = by_slug[r["related_slug"]]
            assert anchor["primaryCategorySlug"] == alt["primaryCategorySlug"]
            assert float(alt.get("price") or 0) <= float(anchor.get("price") or 0)


# --- 171c: classify_content_status ------------------------------------------------------------


def test_clean_catalog_all_published():
    """Catalogul demo trece auditul v3 (gate curat) → toate produsele 'published'."""
    mapping = classify_content_status(_catalog())
    assert mapping
    assert set(mapping.values()) == {"published"}


def test_violation_product_becomes_draft():
    """Un produs corupt (fără `best_for`, atributul-motiv de recomandare v3) apare în violation →
    'draft'; restul rămân 'published'. Dovedește citirea machine-readable a `product_slugs`."""
    data = _catalog()
    victim = data["products"][0]["slug"]
    # rupem un atribut cerut de contractul v3 DOAR pe primul produs
    data["products"][0].setdefault("attributes", {}).pop("best_for", None)
    data["products"][0]["attributes"]["best_for"] = ""  # gol → missing_best_for
    mapping = classify_content_status(data)
    assert mapping[victim] == "draft"
    others = [s for sl, s in mapping.items() if sl != victim]
    assert others and set(others) == {"published"}
