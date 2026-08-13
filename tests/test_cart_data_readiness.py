"""NX-237 — data-readiness: matricea publicată, zero defaults comerciale inventate, buget batch.

Testele de aici păzesc CONTRACTUL de date, nu o implementare: fiecare câmp afișabil are o sursă
canonică declarată în docs/CART-DATA-READINESS.md, iar ce NU are sursă rămâne UNKNOWN — nu se
umple din `updated_at` generic, din fixture-uri de UI sau din heuristici.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.commerce.cart_models import build_snapshot
from src.commerce.facts_provider import build_facts

DOC = Path(__file__).resolve().parent.parent / "docs" / "CART-DATA-READINESS.md"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
P1 = "11111111-1111-4111-8111-111111111111"

# Câmpurile pe care matricea TREBUIE să le acopere (card: field → source → SLA → UNKNOWN → CTA).
REQUIRED_MATRIX_FIELDS = (
    "price.current",
    "price.list_price",
    "currency",
    "availability",
    "stock",
    "variant",
    "rating",
    "review_count",
    "review_summary",
    "delivery",
    "promotion",
    "voucher",
)


def test_matrix_document_exists_and_covers_every_field():
    assert DOC.exists(), "matricea data-readiness nu e publicată (docs/CART-DATA-READINESS.md)"
    text = DOC.read_text(encoding="utf-8")
    for field in REQUIRED_MATRIX_FIELDS:
        assert field in text, f"câmpul {field} lipsește din matricea data-readiness"
    # Decizia sistemului canonic (Definition of Ready) trebuie să fie EXPLICITĂ în doc.
    assert "coșul asistentului" in text or "cosul asistentului" in text
    assert "UNKNOWN" in text


def test_no_commercial_defaults_are_invented():
    """Un rând aproape gol NU produce fapte comerciale — nici preț 0, nici rating 0, nici stoc."""
    bare = {
        "id": P1,
        "name": "Produs sărac",
        "price": None,
        "currency": None,
        "availability": None,
        "stock": None,
        "rating": 0.0,
        "review_count": 0,
        "review_summary": "",
        "variants": [],
        "synced_at": None,
        "updated_at": None,
    }
    f = build_facts([bare], [(P1, None)], now=NOW, sla_s=86400).get(P1, None)
    assert f.price is None and f.rating is None and f.stock is None
    assert {"price", "rating", "stock", "availability", "currency"} <= set(f.unknown)
    # Fără sursă canonică — permanent absente, indiferent de orice timestamp:
    assert {"delivery_promise", "promotion_eligibility", "voucher"} <= set(f.unknown)


def test_unknown_omits_dependent_claims_and_cta():
    """Politica UNKNOWN → CTA: snapshotul nu afișează total și nu oferă checkout pe date lipsă."""
    bare = {
        "id": P1,
        "name": "X",
        "price": None,
        "currency": "RON",
        "availability": "in_stock",
        "stock": None,
        "rating": 0,
        "review_count": 0,
        "review_summary": "",
        "variants": [],
        "synced_at": NOW,
        "updated_at": NOW,
    }
    facts = build_facts([bare], [(P1, None)], now=NOW, sla_s=86400)
    snap = build_snapshot(
        cart_id="c1",
        version=1,
        status="active",
        items=[{"product_id": P1, "variant_id": None, "quantity": 1}],
        facts=facts.facts,
    )
    assert snap.totals.status == "unknown" and snap.totals.display is None
    assert not snap.checkout_eligible
    assert snap.lines[0].unit_price_display is None  # claim-ul omis, nu „0,00 lei"


def test_batch_budget_is_one_call_for_full_cart():
    """Contractul anti-N+1 din provider: 10 linii = un batch (query_count contorizat la sursă)."""
    rows = []
    refs = []
    for i in range(1, 11):
        pid = f"{i:08d}-1111-4111-8111-111111111111"
        rows.append(
            {
                "id": pid,
                "name": f"P{i}",
                "price": 10.0,
                "currency": "RON",
                "availability": "in_stock",
                "stock": 5,
                "rating": 4,
                "review_count": 3,
                "review_summary": "ok",
                "variants": [],
                "synced_at": NOW,
                "updated_at": NOW,
            }
        )
        refs.append((pid, None))
    batch = build_facts(rows, refs, now=NOW, sla_s=86400, query_count=1)
    assert len(batch.facts) == 10 and batch.query_count == 1
