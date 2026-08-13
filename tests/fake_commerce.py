"""NX-237 — store-ul in-memory al coșului canonic: contractul EXACT al query-urilor din
`db/queries/carts.py`, fără Postgres. Folosit de testele unit (`test_cart_service.py`,
`test_cart_pipeline.py`) și de proba reproductibilă `scripts/sim/cart_receipt_recovery.py` —
o singură implementare, ca fake-ul să nu divergă între consumatori.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def product_row(pid: str, **over: Any) -> dict[str, Any]:
    """Un rând de catalog în forma întoarsă de `load_cart_facts_rows` (post `_row_to_product`)."""
    row = {
        "id": pid,
        "name": f"Produs {pid[:4]}",
        "brand": "Brand",
        "price": 100.0,
        "list_price": None,
        "currency": "RON",
        "availability": "in_stock",
        "stock": 20,
        "rating": 4.5,
        "review_count": 12,
        "review_summary": "bun",
        "attributes": {},
        "url": f"https://shop.example/{pid[:4]}",
        "delivery_class": None,
        "restock_date": None,
        "synced_at": NOW,
        "updated_at": NOW,
        "ingredients_db": [],
        "variants": [],
    }
    row.update(over)
    return row


class Store:
    """Persistența in-memory, cu semantica reală (variant NULL = aceeași linie, unique pe
    idempotency_key, finalize doar din pending/unknown)."""

    def __init__(self) -> None:
        self.carts: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.products: dict[str, dict[str, Any]] = {}
        self.checkout_links: list[dict[str, Any]] = []
        self.hydration_calls = 0
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def active_cart(self, business_id: str, conversation_id: str) -> dict[str, Any] | None:
        for c in self.carts:
            if (
                c["business_id"] == business_id
                and c["conversation_id"] == conversation_id
                and c["status"] == "active"
            ):
                return c
        return None


def install(store: Store, patch) -> None:
    """Leagă store-ul de modulele reale. `patch(obj, name, fn)` — `monkeypatch.setattr` în teste,
    un setattr simplu în scripturi (procesul e one-off, nu are nevoie de undo)."""
    import src.commerce.cart_service as cs
    import src.commerce.facts_provider as fp

    st = store

    async def create_cart_if_absent(conn, business_id, conversation_id):
        if st.active_cart(business_id, conversation_id) is None:
            st.carts.append(
                {
                    "id": st._next("cart"),
                    "business_id": business_id,
                    "conversation_id": conversation_id,
                    "version": 0,
                    "status": "active",
                    "currency": None,
                }
            )

    async def lock_active_cart(conn, business_id, conversation_id):
        c = st.active_cart(business_id, conversation_id)
        return dict(c) if c else None

    async def get_active_cart(conn, business_id, conversation_id):
        return await lock_active_cart(conn, business_id, conversation_id)

    async def bump_cart_version(conn, business_id, cart_id):
        for c in st.carts:
            if c["business_id"] == business_id and c["id"] == cart_id:
                c["version"] += 1
                return c["version"]
        raise AssertionError("bump pe cart inexistent")

    async def set_cart_status(conn, business_id, cart_id, status):
        for c in st.carts:
            if c["business_id"] == business_id and c["id"] == cart_id:
                c["status"] = status

    async def get_cart_items(conn, business_id, cart_id):
        return [
            dict(it)
            for it in st.items
            if it["business_id"] == business_id and it["cart_id"] == cart_id
        ]

    async def insert_cart_item(
        conn, business_id, cart_id, product_id, variant_id, quantity, turn_id
    ):
        st.items.append(
            {
                "id": st._next("item"),
                "business_id": business_id,
                "cart_id": cart_id,
                "product_id": product_id,
                "variant_id": variant_id,
                "quantity": quantity,
            }
        )

    async def set_cart_item_quantity(
        conn, business_id, cart_id, product_id, variant_id, quantity, turn_id
    ):
        for it in st.items:
            if (
                it["business_id"] == business_id
                and it["cart_id"] == cart_id
                and it["product_id"] == product_id
                and (it["variant_id"] or None) == (variant_id or None)
            ):
                it["quantity"] = quantity
                return True
        return False

    async def delete_cart_item(conn, business_id, cart_id, product_id, variant_id):
        before = len(st.items)
        st.items = [
            it
            for it in st.items
            if not (
                it["business_id"] == business_id
                and it["cart_id"] == cart_id
                and it["product_id"] == product_id
                and (it["variant_id"] or None) == (variant_id or None)
            )
        ]
        return len(st.items) < before

    async def clear_cart_items(conn, business_id, cart_id):
        before = len(st.items)
        st.items = [
            it
            for it in st.items
            if not (it["business_id"] == business_id and it["cart_id"] == cart_id)
        ]
        return before - len(st.items)

    async def get_receipt_by_key(conn, business_id, idempotency_key):
        for r in st.receipts:
            if r["business_id"] == business_id and r["idempotency_key"] == idempotency_key:
                return dict(r)
        return None

    async def insert_receipt(conn, business_id, **kw):
        if await get_receipt_by_key(conn, business_id, kw["idempotency_key"]) is not None:
            return None  # unique (business_id, idempotency_key) → ON CONFLICT DO NOTHING
        row = {"id": st._next("rcpt"), "business_id": business_id, **kw}
        st.receipts.append(row)
        return dict(row)

    async def finalize_receipt(conn, business_id, receipt_id, *, status, **kw):
        for r in st.receipts:
            if (
                r["business_id"] == business_id
                and r["id"] == receipt_id
                and r["status"] in ("pending", "unknown_reconcile")
            ):
                r["status"] = status
                for key in ("result_code", "after_version", "external_ref", "url"):
                    if kw.get(key) is not None:
                        r[key] = kw[key]

    async def load_cart_facts_rows(conn, business_id, product_ids, *, limit=12):
        st.hydration_calls += 1
        return [dict(st.products[pid]) for pid in product_ids if pid in st.products]

    async def create_checkout_link(
        conn, business_id, conversation_id, contact_id, ref_code, cart, url, expires_at
    ):
        st.checkout_links.append(
            {"business_id": business_id, "ref_code": ref_code, "cart": cart, "url": url}
        )
        return {"id": st._next("link"), "ref_code": ref_code, "url": url}

    for name, fn in {
        "create_cart_if_absent": create_cart_if_absent,
        "lock_active_cart": lock_active_cart,
        "get_active_cart": get_active_cart,
        "bump_cart_version": bump_cart_version,
        "set_cart_status": set_cart_status,
        "get_cart_items": get_cart_items,
        "insert_cart_item": insert_cart_item,
        "set_cart_item_quantity": set_cart_item_quantity,
        "delete_cart_item": delete_cart_item,
        "clear_cart_items": clear_cart_items,
        "get_receipt_by_key": get_receipt_by_key,
        "insert_receipt": insert_receipt,
        "finalize_receipt": finalize_receipt,
    }.items():
        patch(cs.q, name, fn)
    patch(fp, "load_cart_facts_rows", load_cart_facts_rows)
    patch(cs, "create_checkout_link", create_checkout_link)
