"""NX-235 — starea v2 pe Postgres REAL: lazy upgrade, CHECK-ul de 8KB, conflict de versiune.

Exclus din CI fast (`-m "not integration"`). Ce nu se poate demonstra fără DB:

  • un rând scris în v2 trece de CHECK-ul de 8KB pe `conversations.state` (003) — caps-urile din
    cod nu sunt o convenție, sunt condiția ca scrierea să reușească;
  • migrarea lazy chiar migrează: un rând v1 devine v2 la primul commit, iar cititorul v1 continuă
    să vadă aceleași fapte prin proiecție (rollback sigur);
  • `state_version` (optimistic lock) rămâne al DB-ului: reducerul re-aplică deltele pe starea
    proaspătă și nu pierde scrierea concurentă.
"""

import json
from uuid import uuid4

import asyncpg
import pytest

from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import (
    MAX_STATE_BYTES,
    ConversationStateV2,
    adapt_v1,
    hydrate_state_v2,
    is_v2,
    project_v1,
    serialize,
)
from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.conversations import StateConflict, patch_conversation_state
from src.models import ConversationState

pytestmark = [pytest.mark.integration]

VOCAB = NeedVocabulary.from_pack(None)
POLICY = ReducerPolicy(vocabulary=VOCAB)

V1_ROW = {
    "displayed_products": [{"product_id": str(uuid4()), "name": "Ser A", "price": 89.0}],
    "search_constraints": {"budget_max": 150, "concerns": ["acnee"], "category_key": "seruri"},
    "constraints": {"recipient": "sora mea care are 34 de ani"},
    "cart": [],
    "safety": {"contexts": ["pregnancy"]},
}


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-235 state', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx235-{uuid4().hex[:8]}",
    )
    return bid


async def _make_conversation(conn, bid: str, state: dict | None = None) -> str:
    channel_id = str(uuid4())
    await conn.execute(
        "insert into channels (id, business_id, kind, provider_account_id) "
        "values ($1, $2, 'webchat', $3)",
        channel_id,
        bid,
        f"tok-{uuid4().hex[:10]}",
    )
    contact_id = await conn.fetchval(
        "insert into contacts (business_id) values ($1) returning id", bid
    )
    return await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id, state) "
        "values ($1, $2, $3, $4::jsonb) returning id::text",
        bid,
        str(contact_id),
        channel_id,
        json.dumps(state or V1_ROW),
    )


@pytest.fixture
async def shop():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        bid = await _make_business(conn)
    try:
        yield bid
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _read(conn, bid: str, conv: str) -> tuple[dict, int]:
    row = await conn.fetchrow(
        "select state, state_version from conversations where business_id = $1 and id = $2",
        bid,
        conv,
    )
    return json.loads(row["state"]) if isinstance(row["state"], str) else row["state"], row[
        "state_version"
    ]


# ── Lazy upgrade + rollback ──────────────────────────────────────────────────


async def test_a_v1_row_upgrades_on_the_first_write_and_stays_readable(shop):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)
        raw, version = await _read(conn, shop, conv)
        assert not is_v2(raw)

        state = hydrate_state_v2(raw, VOCAB)
        reduced = reduce_all(
            state,
            [StateUpdateProposal("set_need", key="brand", value="petala", source="user_explicit")],
            POLICY,
        )
        doc, size, degraded = serialize(reduced.state)
        assert not degraded and size < MAX_STATE_BYTES
        await patch_conversation_state(conn, shop, conv, doc, version)

        stored, new_version = await _read(conn, shop, conv)

    assert is_v2(stored) and new_version == version + 1
    # Cititorul v1 (cod nemigrat / după rollback) vede aceleași fapte, prin proiecție.
    legacy = ConversationState.from_jsonb(stored)
    assert legacy.search_constraints["budget_max"] == 150
    assert legacy.search_constraints["brand"] == "petala"
    assert legacy.search_constraints["category_key"] == "seruri"
    assert legacy.safety["contexts"] == ["pregnancy"]


async def test_the_upgraded_row_carries_no_raw_utterance(shop):
    """`constraints` v1 ținea răspunsul brut; după upgrade nu mai are unde să stea (P12)."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)
        raw, version = await _read(conn, shop, conv)
        doc, _, _ = serialize(hydrate_state_v2(raw, VOCAB))
        await patch_conversation_state(conn, shop, conv, doc, version)
        stored, _ = await _read(conn, shop, conv)
    assert "sora mea care are 34 de ani" not in json.dumps(stored, ensure_ascii=False)


async def test_a_revoked_need_does_not_reappear_after_a_round_trip(shop):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)
        raw, version = await _read(conn, shop, conv)
        revoked = reduce_all(
            hydrate_state_v2(raw, VOCAB),
            [StateUpdateProposal("revoke", key="budget_max", source="user_explicit")],
            POLICY,
        ).state
        doc, _, _ = serialize(revoked)
        await patch_conversation_state(conn, shop, conv, doc, version)
        stored, _ = await _read(conn, shop, conv)

    back = hydrate_state_v2(stored, VOCAB)
    assert "budget_max" in back.revoked_keys()
    assert ConversationState.from_jsonb(stored).search_constraints.get("budget_max") is None


# ── Bugetul de 8KB (CHECK-ul din 003) ────────────────────────────────────────


async def test_a_capped_state_always_fits_the_db_check(shop):
    """Caps-urile din cod sunt condiția scrierii, nu o preferință: fără ele, UPDATE-ul eșuează
    cu `CheckViolationError`, tranzacția dă rollback și am pierde RĂSPUNSUL din cauza memoriei.

    Starea uriașă se construiește ÎN MEMORIE, nu se inserează: un rând atât de mare nici n-ar
    intra în tabel — exact motivul pentru care poarta trebuie să fie înaintea scrierii."""
    pool = await get_pool()
    huge = {
        **V1_ROW,
        "search_constraints": {
            "budget_max": 150,
            "concerns": [f"concern-canonic-{i:03d}" for i in range(200)],
            "category_key": "seruri",
        },
        "displayed_products": [
            {"product_id": str(uuid4()), "name": "P" * 300, "price": 10.0} for _ in range(50)
        ],
        "cart": [{"product_id": str(uuid4()), "name": "C" * 200, "price": 1.0} for _ in range(30)],
    }
    doc, size, degraded = serialize(hydrate_state_v2(huge, VOCAB))
    assert size <= MAX_STATE_BYTES

    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)  # rând mic, ca în realitate
        _, version = await _read(conn, shop, conv)
        stored_size = await conn.fetchval(
            "select pg_column_size($1::jsonb)", json.dumps(doc, ensure_ascii=False)
        )
        assert stored_size < 8192, "bugetul de cod nu mai acoperă overhead-ul jsonb"
        await patch_conversation_state(conn, shop, conv, doc, version)
        stored, _ = await _read(conn, shop, conv)
    assert is_v2(stored) and degraded is True


async def test_the_code_budget_still_covers_the_binary_jsonb_overhead(shop):
    """Rezerva dintre bugetul de cod și CHECK-ul din DB e o MĂSURĂTOARE, nu o presupunere.

    Documentul de test e construit EXACT la limita bugetului de cod, cu forma cea mai costisitoare
    (multe obiecte mici — jsonb stochează numele cheilor în fiecare). Dacă overhead-ul crește la o
    versiune de Postgres sau la o schimbare de schemă, testul pică AICI, nu în producție."""
    pool = await get_pool()
    needs = []
    while True:
        needs.append(
            {
                "key": "concerns",
                "operator": "contains",
                "normalized_value": f"valoare-canonica-{len(needs):03d}",
                "strength": "soft",
                "status": "active",
                "source": "user_explicit",
                "source_turn_id": f"turn-{len(needs):03d}-aaaaaaaa",
                "updated_revision": len(needs),
                "scope": "seruri",
            }
        )
        doc = {"schema_version": 2, "revision": 9, "needs": needs}
        if len(json.dumps(doc, ensure_ascii=False).encode()) >= MAX_STATE_BYTES:
            break
    async with admin_conn(pool) as conn:
        stored_size = await conn.fetchval(
            "select pg_column_size($1::jsonb)", json.dumps(doc, ensure_ascii=False)
        )
    assert stored_size < 8192, (
        f"jsonb {stored_size}B pentru {MAX_STATE_BYTES}B text — rezervă prea mică"
    )


async def test_an_uncapped_state_is_refused_by_the_db(shop):
    """Dovada că poarta DB există cu adevărat — altfel testele de mai sus n-ar demonstra nimic."""
    pool = await get_pool()
    oversized = {"schema_version": 2, "needs": [], "junk": "x" * 9000}
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)
        _, version = await _read(conn, shop, conv)
        with pytest.raises(asyncpg.PostgresError):
            await patch_conversation_state(conn, shop, conv, oversized, version)


# ── Conflict de versiune: re-aplicare, nu re-cerere ──────────────────────────


async def test_a_version_conflict_reapplies_the_batch_on_the_fresh_state(shop):
    pool = await get_pool()
    proposals = [
        StateUpdateProposal("set_need", key="brand", value="petala", source="user_explicit")
    ]
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, shop)
        raw, version = await _read(conn, shop, conv)

        # Altcineva scrie între citire și patch (alt tab / job proactiv).
        concurrent = reduce_all(
            hydrate_state_v2(raw, VOCAB),
            [StateUpdateProposal("set_need", key="size", value="m", source="user_explicit")],
            POLICY,
        ).state
        await patch_conversation_state(conn, shop, conv, serialize(concurrent)[0], version)

        with pytest.raises(StateConflict):
            await patch_conversation_state(
                conn, shop, conv, serialize(adapt_v1(raw, VOCAB))[0], version
            )

        fresh_raw, fresh_version = await _read(conn, shop, conv)
        retried = reduce_all(hydrate_state_v2(fresh_raw, VOCAB), proposals, POLICY).state
        await patch_conversation_state(conn, shop, conv, serialize(retried)[0], fresh_version)
        stored, _ = await _read(conn, shop, conv)

    final = hydrate_state_v2(stored, VOCAB)
    assert final.need_for("brand").normalized_value == "petala"  # delta turului
    assert final.need_for("size").normalized_value == "m"  # scrierea concurentă NU s-a pierdut


# ── Izolare de tenant (P7) ───────────────────────────────────────────────────


async def test_state_is_never_read_across_tenants(shop):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        other = await _make_business(conn)
        try:
            conv = await _make_conversation(conn, shop)
            _, version = await _read(conn, shop, conv)
            with pytest.raises(StateConflict):
                await patch_conversation_state(
                    conn, other, conv, ConversationStateV2().to_jsonb(), version
                )
            raw, _ = await _read(conn, shop, conv)
            assert (
                project_v1(hydrate_state_v2(raw, VOCAB))["search_constraints"]["budget_max"] == 150
            )
        finally:
            await conn.execute("delete from businesses where id = $1", other)
