"""`traverse_relations` — teste INTEGRATION (DB reală, businesses throwaway, cleanup la teardown).

Exclus din CI fast (`-m "not integration"`). Acoperă exact garanțiile care nu se pot dovedi fără
Postgres, fiindcă trăiesc în MOTOR, nu în Python:

  • clauza `CYCLE` chiar oprește o buclă de date (un stub de DB ar „reuși" oricum);
  • recursia rămâne tenant-scoped în AMBII pași (un bug de izolare apare doar la pasul 2, unde e
    ușor de uitat `business_id`);
  • plafonul de adâncime e respectat de query, nu doar de apelant;
  • structura nu se amestecă cu prezentarea: un pas indisponibil nu RUPE lanțul.

Notă de stare: `product_relations.kind` are încă un CHECK cu vocabular închis în migrarea 027
(`substitute | complement | accessory | routine_next`). De aceea testele folosesc tipurile
existente. Mutarea vocabularului din CHECK în date, ca un tenant de electrocasnice să-și poată
declara `requires` / `compatible_with` fără migrare, e pasul următor și are nevoie de un card
propriu — `src/domain/relation_kinds.py` e deja pregătit pentru el (nu enumeră niciun tip).
"""

from uuid import uuid4

import pytest

from src.catalog.relation_chain import walk_chain
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.catalog import (
    _MAX_RELATION_DEPTH,
    traverse_relation_chain,
    traverse_relations,
)

pytestmark = [pytest.mark.integration]


async def _make_business(conn, bid: str) -> None:
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'traversal iso', 'beauty_salon', 'active', 'ro')",
        bid,
        f"trav-{uuid4().hex[:8]}",
    )


async def _make_product(conn, bid: str, *, name: str, **cols) -> str:
    base = {"content_status": "published", "availability": "in_stock", "status": "active"}
    base.update(cols)
    keys = list(base)
    ph = ", ".join(f"${i + 5}" for i in range(len(keys)))
    return await conn.fetchval(
        f"insert into products (business_id, slug, name, price, {', '.join(keys)}) "
        f"values ($1, $2, $3, $4, {ph}) returning id::text",
        bid,
        f"p-{uuid4().hex[:8]}",
        name,
        50.0,
        *[base[k] for k in keys],
    )


async def _relate(conn, bid: str, src: str, dst: str, kind: str, position: int = 0) -> None:
    await conn.execute(
        "insert into product_relations (business_id, product_id, related_id, kind, position) "
        "values ($1, $2::uuid, $3::uuid, $4, $5)",
        bid,
        src,
        dst,
        kind,
        position,
    )


async def _cleanup(conn, *bids: str) -> None:
    for bid in bids:
        await conn.execute("delete from product_relations where business_id=$1", bid)
        await conn.execute("delete from products where business_id=$1", bid)
        await conn.execute("delete from businesses where id=$1", bid)


@pytest.fixture
async def shop():
    """Un business throwaway, cu cleanup."""
    pool = await get_pool()
    bid = str(uuid4())
    async with admin_conn(pool) as conn:
        await _make_business(conn, bid)
    try:
        yield bid
    finally:
        async with admin_conn(pool) as conn:
            await _cleanup(conn, bid)
        await close_pool()


async def _chain(conn, bid: str, length: int, kind: str = "routine_next") -> list[str]:
    """Un lanț curat p0 → p1 → ... → p{length}. Întoarce id-urile în ordine."""
    ids = [await _make_product(conn, bid, name=f"pas {i}") for i in range(length + 1)]
    for i in range(length):
        await _relate(conn, bid, ids[i], ids[i + 1], kind)
    return ids


# --- lanțul curat: adâncimea returnată e cea reală ----------------------------------------------


async def test_chain_returns_each_step_with_its_depth(shop):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ids = await _chain(conn, shop, 3)  # p0 → p1 → p2 → p3
        hops = await traverse_relations(
            conn, shop, anchor_id=ids[0], kind="routine_next", max_depth=3
        )
    assert [h["id"] for h in hops] == ids[1:]
    assert [h["depth"] for h in hops] == [1, 2, 3]


async def test_depth_one_returns_only_direct_neighbours(shop):
    """Un tip nedeclarat primește `max_depth=1` din registru ⇒ exact comportamentul de azi."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ids = await _chain(conn, shop, 3)
        hops = await traverse_relations(
            conn, shop, anchor_id=ids[0], kind="routine_next", max_depth=1
        )
    assert [h["id"] for h in hops] == [ids[1]]


async def test_query_clamps_depth_it_is_given(shop):
    """Plafonul e impus de QUERY, nu de bunăvoința apelantului: un `max_depth` absurd nu poate
    cheltui bugetul de tur (NX-241)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ids = await _chain(conn, shop, _MAX_RELATION_DEPTH + 3)
        hops = await traverse_relations(
            conn, shop, anchor_id=ids[0], kind="routine_next", max_depth=999
        )
    assert max(h["depth"] for h in hops) == _MAX_RELATION_DEPTH


# --- cicluri: motorul oprește, ancora nu se întoarce în propria listă ----------------------------


async def test_cycle_terminates_and_excludes_the_anchor(shop):
    """A → B → A. Fără clauza `CYCLE` asta ar fi o recursie nemărginită. Și fiindcă `path` conține
    doar `related_id`, ancora NU e marcată ca ciclu la pasul 2 — de aceea se exclude explicit."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b = await _make_product(conn, shop, name="B")
        await _relate(conn, shop, a, b, "complement")
        await _relate(conn, shop, b, a, "complement")
        hops = await traverse_relations(conn, shop, anchor_id=a, kind="complement", max_depth=4)
    assert [h["id"] for h in hops] == [b]


async def test_longer_cycle_does_not_repeat_nodes(shop):
    """A → B → C → A: fiecare nod apare o singură dată, la prima adâncime la care e atins."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b = await _make_product(conn, shop, name="B")
        c = await _make_product(conn, shop, name="C")
        await _relate(conn, shop, a, b, "complement")
        await _relate(conn, shop, b, c, "complement")
        await _relate(conn, shop, c, a, "complement")
        hops = await traverse_relations(conn, shop, anchor_id=a, kind="complement", max_depth=5)
    assert [(h["id"], h["depth"]) for h in hops] == [(b, 1), (c, 2)]


# --- tipul muchiei e o graniță ------------------------------------------------------------------


async def test_traversal_does_not_cross_kinds(shop):
    """A -routine_next→ B -substitute→ C. Traversarea pe `routine_next` se oprește la B: altfel
    semantica declarată per tip (adâncime, ordonare, scop) n-ar mai însemna nimic."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b = await _make_product(conn, shop, name="B")
        c = await _make_product(conn, shop, name="C")
        await _relate(conn, shop, a, b, "routine_next")
        await _relate(conn, shop, b, c, "substitute")
        hops = await traverse_relations(conn, shop, anchor_id=a, kind="routine_next", max_depth=4)
    assert [h["id"] for h in hops] == [b]


# --- structura nu se amestecă cu prezentarea ----------------------------------------------------


async def test_unavailable_step_does_not_break_the_chain(shop):
    """Pasul 2 e epuizat. Lanțul CONTINUĂ (structura e intactă) — filtrarea de cumpărabilitate e a
    apelantului, DUPĂ traversare. Altfel un stoc temporar ar șterge pașii de după el, iar clientul
    ar primi o rutină trunchiată fără să afle de ce."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b = await _make_product(conn, shop, name="B", availability="out_of_stock")
        c = await _make_product(conn, shop, name="C")
        await _relate(conn, shop, a, b, "routine_next")
        await _relate(conn, shop, b, c, "routine_next")
        hops = await traverse_relations(conn, shop, anchor_id=a, kind="routine_next", max_depth=3)
    assert [(h["id"], h["depth"]) for h in hops] == [(b, 1), (c, 2)]


# --- izolare de tenant, inclusiv la PASUL RECURSIV ----------------------------------------------


async def test_traversal_is_tenant_scoped_in_both_recursion_steps():
    """ADVERSARIAL. Două businessuri, fiecare cu lanțul lui. Interogarea pe tenantul A nu vede
    nimic din B — nici la seed, nici la pasul recursiv, unde `business_id` e cel mai ușor de uitat.
    Testul are nevoie de DOI tenanți, deci nu poate folosi fixture-ul cu unul singur."""
    pool = await get_pool()
    a_biz, b_biz = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        try:
            await _make_business(conn, a_biz)
            await _make_business(conn, b_biz)
            a_ids = await _chain(conn, a_biz, 2)
            await _chain(conn, b_biz, 2)

            hops = await traverse_relations(
                conn, a_biz, anchor_id=a_ids[0], kind="routine_next", max_depth=4
            )
            assert [h["id"] for h in hops] == a_ids[1:]

            # Ancora lui A, interogată pe tenantul B → zero. Un `business_id` uitat s-ar vedea aici.
            leaked = await traverse_relations(
                conn, b_biz, anchor_id=a_ids[0], kind="routine_next", max_depth=4
            )
            assert leaked == []
        finally:
            await _cleanup(conn, a_biz, b_biz)
    await close_pool()


# --- determinism: aceeași interogare, aceiași octeți --------------------------------------------


async def test_order_is_stable_across_calls(shop):
    """Ordinea vine din `(adâncime, poziția muchiei, id)`, nu din planul de execuție. Determinismul
    e o cerință: proiectorul NX-240 e pur, iar două citiri trebuie să dea același rezultat."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        anchor = await _make_product(conn, shop, name="ancora")
        targets = [await _make_product(conn, shop, name=f"t{i}") for i in range(4)]
        for pos, t in enumerate(targets):
            await _relate(conn, shop, anchor, t, "complement", position=len(targets) - pos)
        first = await traverse_relations(
            conn, shop, anchor_id=anchor, kind="complement", max_depth=2
        )
        second = await traverse_relations(
            conn, shop, anchor_id=anchor, kind="complement", max_depth=2
        )
    assert first == second
    assert [h["position"] for h in first] == sorted(h["position"] for h in first)


# --- NX-263: DRUMUL, nu frontiera ---------------------------------------------------------------


async def test_chain_follows_the_path_not_the_best_of_each_depth(shop):
    """Cazul care separă cele două funcții, pe SQL real.

    A → B1 (poziția 0) și A → B2 (poziția 1). B1 → D (poziția 5), B2 → C (poziția 0).
    Un „cel mai bun produs de la fiecare adâncime" ar alege B1 la pasul 1 (poziția 0) și apoi C la
    pasul 2 (poziția 0) — dar C e succesorul lui B2, nu al lui B1. Ar fi o rutină cusută din două
    ramuri: corectă pe cifre, falsă ca sfat. Lanțul real e B1 → D."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b1 = await _make_product(conn, shop, name="B1")
        b2 = await _make_product(conn, shop, name="B2")
        c = await _make_product(conn, shop, name="C")
        d = await _make_product(conn, shop, name="D")
        await _relate(conn, shop, a, b1, "routine_next", position=0)
        await _relate(conn, shop, a, b2, "routine_next", position=1)
        await _relate(conn, shop, b2, c, "routine_next", position=0)
        await _relate(conn, shop, b1, d, "routine_next", position=5)

        hops = await traverse_relation_chain(
            conn, shop, anchor_id=a, kind="routine_next", max_depth=4
        )
        chain = walk_chain(hops, a, 4)

    assert [h["id"] for h in chain] == [b1, d]
    # fiecare pas e copilul celui dinainte — drumul e VERIFICABIL, nu presupus din adâncime
    assert [h["parent"] for h in chain] == [a, b1]


async def test_chain_is_bounded_by_the_spec_depth(shop):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ids = await _chain(conn, shop, 5)
        hops = await traverse_relation_chain(
            conn, shop, anchor_id=ids[0], kind="routine_next", max_depth=2
        )
        chain = walk_chain(hops, ids[0], 2)
    assert [h["id"] for h in chain] == ids[1:3]


async def test_chain_on_a_cyclic_kind_terminates_and_stays_a_path(shop):
    """A → B → C → A pe un tip ciclic: interogarea termină, iar lanțul nu se întoarce la ancoră."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await _make_product(conn, shop, name="A")
        b = await _make_product(conn, shop, name="B")
        c = await _make_product(conn, shop, name="C")
        await _relate(conn, shop, a, b, "complement")
        await _relate(conn, shop, b, c, "complement")
        await _relate(conn, shop, c, a, "complement")
        hops = await traverse_relation_chain(
            conn, shop, anchor_id=a, kind="complement", max_depth=5
        )
        chain = walk_chain(hops, a, 5)
    assert [h["id"] for h in chain] == [b, c]


async def test_chain_is_tenant_scoped():
    pool = await get_pool()
    a_biz, b_biz = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        try:
            await _make_business(conn, a_biz)
            await _make_business(conn, b_biz)
            a_ids = await _chain(conn, a_biz, 3)
            leaked = await traverse_relation_chain(
                conn, b_biz, anchor_id=a_ids[0], kind="routine_next", max_depth=4
            )
            assert leaked == []
        finally:
            await _cleanup(conn, a_biz, b_biz)
    await close_pool()
