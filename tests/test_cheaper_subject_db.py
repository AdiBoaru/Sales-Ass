"""NX-314 — «mai ieftin» cu subiect, pe catalogul REAL (`sole-ro`). Read-only.

Regresia conversației `f4e1431e` (2026-09-23): după SOME BY MI Yuja Niacin (110 lei), «si ceva mai
ieftin» servea o bandă de nas de 3 lei și cinci măști sheet de 10 lei. Cu subiectul «cremă de față,
hidratare», primele carduri trebuie să fie creme de față, toate strict sub prag.

Marcate `integration` → excluse din CI. Local:
`pytest tests/test_cheaper_subject_db.py -m integration`.
"""

from __future__ import annotations

import pytest

from src.conversation.subject import ConversationSubject
from src.db.connection import close_pool, get_pool, tenant_conn
from src.db.queries.catalog import search_cheaper_than
from tests.tenants import CATALOG_BIZ

pytestmark = pytest.mark.integration

CREMA = "crema de fata"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def _anchor() -> tuple[str, float]:
    """Produsul din conversația reală, căutat după nume (id-urile nu se hardcodează)."""
    async with tenant_conn(CATALOG_BIZ) as conn:
        row = await conn.fetchrow(
            "select id::text as id, attributes->>'product_type' as t from products "
            "where business_id = $1 and status = 'active' and name ilike '%Yuja Niacin%' "
            "and attributes->>'product_type' = $2 order by id limit 1",
            CATALOG_BIZ,
            CREMA,
        )
    if row is None:
        pytest.skip("catalogul nu mai are crema din conversația reală")
    return row["id"], 110.0


async def test_the_real_conversation_now_gets_face_creams(pool):
    ref, baseline = await _anchor()
    subject = ConversationSubject(product_type=CREMA, needs=(("concerns", "hydration"),))
    async with tenant_conn(CATALOG_BIZ) as conn:
        before = await search_cheaper_than(conn, CATALOG_BIZ, [ref], baseline)
        after = await search_cheaper_than(conn, CATALOG_BIZ, [ref], baseline, subject=subject)

    # Azi: cel mai ieftin lucru din categorie, oricare ar fi el.
    assert any((p["attributes"] or {}).get("product_type") != CREMA for p in before[:3])
    # Cu subiect: primele carduri sunt creme de față, strict sub prag, niciuna afișată deja.
    assert after, "catalogul are creme de față sub 110 lei"
    assert all((p["attributes"] or {}).get("product_type") == CREMA for p in after[:3])
    assert all(p["subject_match"] for p in after[:3])
    assert all(p["price"] < baseline for p in after)
    assert ref not in {p["id"] for p in after}
    # Porțile sunt aceleași: tot în stoc, tot tenantul.
    assert all(p["availability"] in ("in_stock", "low_stock") for p in after)


async def test_a_shelf_widens_but_keeps_todays_pool(pool):
    """Raftul (ghicit de model, poate greșit) se ADAUGĂ la categoria afișată, nu o înlocuiește."""
    ref, baseline = await _anchor()
    wrong_shelf = ConversationSubject(shelf_key="machiaj", product_type=CREMA)
    no_shelf = ConversationSubject(product_type=CREMA)
    async with tenant_conn(CATALOG_BIZ) as conn:
        with_wrong = await search_cheaper_than(
            conn, CATALOG_BIZ, [ref], baseline, subject=wrong_shelf
        )
        plain = await search_cheaper_than(conn, CATALOG_BIZ, [ref], baseline, subject=no_shelf)
    assert [p["id"] for p in with_wrong[:3]] == [p["id"] for p in plain[:3]]


async def test_another_tenant_sees_nothing(pool):
    ref, baseline = await _anchor()
    other = "00000000-0000-0000-0000-000000000000"
    async with tenant_conn(other) as conn:
        got = await search_cheaper_than(
            conn, other, [ref], baseline, subject=ConversationSubject(product_type=CREMA)
        )
    assert got == []
