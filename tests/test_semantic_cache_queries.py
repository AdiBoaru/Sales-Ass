"""G5b-1 — integration pe query-urile semantic_cache (Postgres real).

Validează SQL-ul prin round-trip upsert → exact, ca bot_runtime, în tranzacție rollback-uită
(zero poluare demo). Din 2026-09-24 cache-ul nu mai are vector: rândul se scrie cu
`embedding`/`embedding_model` NULL, ceea ce cere migrarea 052.
"""

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.semantic_cache import exact_lookup, upsert_entry

pytestmark = pytest.mark.integration

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_cache_roundtrip(pool):
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO)

            await upsert_entry(
                conn,
                DEMO,
                "ro",
                canonical_str="proba g5b1 cache",
                canonical_hash="g5b1-probe-hash",
                answer="Răspuns de test.",
                volatility_class="static",
                quality_score=1.0,
                ttl_days=1,
            )

            hit = await exact_lookup(conn, DEMO, "ro", "g5b1-probe-hash")
            assert hit is not None
            assert hit["answer"] == "Răspuns de test."
            assert await exact_lookup(conn, DEMO, "en", "g5b1-probe-hash") is None  # P11
            row = await conn.fetchrow(
                "select embedding is null as no_vec, embedding_model from semantic_cache "
                "where business_id = $1 and canonical_hash = $2",
                DEMO,
                "g5b1-probe-hash",
            )
            assert row["no_vec"] and row["embedding_model"] is None
        finally:
            await tr.rollback()
