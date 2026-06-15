"""G5b-1 — integration pe query-urile semantic_cache (pgvector real).

Validează SQL-ul (coloane + `<=>` cosine) prin round-trip upsert → exact → semantic,
ca bot_runtime, în tranzacție rollback-uită (zero poluare demo).
"""

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.semantic_cache import exact_lookup, semantic_lookup, upsert_entry

pytestmark = pytest.mark.integration

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


async def test_cache_roundtrip(pool):
    emb = [0.013] * 1536
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
                embedding=emb,
                answer="Răspuns de test.",
                volatility_class="static",
                embedding_model="text-embedding-3-small",
                quality_score=1.0,
                ttl_days=1,
            )

            hit = await exact_lookup(conn, DEMO, "ro", "g5b1-probe-hash")
            assert hit is not None
            assert hit["answer"] == "Răspuns de test."

            cand = await semantic_lookup(conn, DEMO, "ro", emb)
            assert cand is not None
            assert float(cand["similarity"]) > 0.99  # vectorul identic → cosine ~1
        finally:
            await tr.rollback()
