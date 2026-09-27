"""NX-329 felia 2b — faptele resolverului de referințe: un checkout, fiecare statement pe tenant.

Conexiune de înregistrare, zero DB: se verifică FORMA citirii (P7 pe fiecare statement, un singur
checkout, id-urile care nu sunt UUID nu ajung în `$2::uuid[]`) și maparea rândului în
`ProductFacts`. Pe catalogul real: `tests/test_reference_facts_db.py` (`-m integration`)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from src.catalog.reference_facts import facts_from_row, fetch_reference_facts
from src.conversation.references import CatalogLookup, name_key

BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
P1 = "11111111-1111-1111-1111-111111111111"
P2 = "22222222-2222-2222-2222-222222222222"
P9 = "99999999-9999-9999-9999-999999999999"


class _Conn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "name_len" in sql:
            return [{"id": P9, "name_len": 21}]
        return [
            {
                "id": P1,
                "name": "Samsung Phone 1",
                "price": 1500.0,
                "availability": "in_stock",
                "rating": 4.5,
                "brand": "Samsung",
                "attributes": '{"color": "negru"}',
                "variant_labels": ["alb", "negru"],
            }
        ]


class _Deps:
    def __init__(self):
        self.conn = _Conn()
        self.checkouts: list[str] = []

    def db(self, op):
        @asynccontextmanager
        async def _cm():
            self.checkouts.append(op)
            yield self.conn

        return _cm()


async def test_one_checkout_and_every_statement_is_tenant_scoped():
    deps = _Deps()
    lookup = CatalogLookup(
        ids=(P1, P2, "not-a-uuid"), names=("Xiaomi Phone 9 512 GB",), attributes=("color",)
    )
    facts = await fetch_reference_facts(deps, BIZ, lookup)

    assert deps.checkouts == ["reference_facts"]
    assert len(deps.conn.calls) == 2
    for sql, args in deps.conn.calls:
        assert "p.business_id = $1" in sql
        assert args[0] == BIZ
    _, facts_args = deps.conn.calls[-1]
    assert facts_args[1] == [P1, P2, P9], "id-urile numelui găsit se revalidează în același lot"
    assert "not-a-uuid" not in facts_args[1]
    assert facts_args[2] == ["color"]
    assert facts.named == {name_key("Xiaomi Phone 9 512 GB"): ((P9, 21),)}
    assert set(facts.products) == {P1}


async def test_no_names_no_name_statement():
    deps = _Deps()
    await fetch_reference_facts(deps, BIZ, CatalogLookup(ids=(P1,)))
    assert len(deps.conn.calls) == 1


async def test_the_join_on_brands_and_variants_is_tenant_scoped_too():
    deps = _Deps()
    await fetch_reference_facts(deps, BIZ, CatalogLookup(ids=(P1,)))
    sql, _ = deps.conn.calls[0]
    assert "b.business_id = p.business_id" in sql
    assert "v.business_id = p.business_id" in sql


def test_row_mapping_keeps_unknown_apart_from_false():
    in_stock = facts_from_row({"id": P1, "availability": "low_stock", "price": 10})
    gone = facts_from_row({"id": P1, "availability": "discontinued"})
    unknown = facts_from_row({"id": P1, "availability": None, "price": None})
    assert (in_stock.available, gone.available, unknown.available) == (True, False, None)
    assert unknown.price is None and in_stock.price == 10.0
