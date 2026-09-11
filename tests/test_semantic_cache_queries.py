"""G5b-1 — integration pe query-urile semantic_cache (pgvector real).

Validează SQL-ul (coloane + `<=>` cosine) prin round-trip upsert → exact → semantic,
ca bot_runtime, în tranzacție rollback-uită (zero poluare demo).
"""

import pytest

from src.db.connection import close_pool, get_pool
from src.db.queries.semantic_cache import (
    exact_lookup,
    semantic_candidates_exist,
    semantic_lookup,
    upsert_entry,
)

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

            cand = await semantic_lookup(
                conn, DEMO, "ro", emb, embedding_model="text-embedding-3-small"
            )
            assert cand is not None
            assert float(cand["similarity"]) > 0.99  # vectorul identic → cosine ~1

            # NX-291: sonda de dinaintea embed-ului trebuie să răspundă despre EXACT mulțimea pe
            # care o interoghează `semantic_lookup`. Se verifică pe DB real, în ambele sensuri:
            # dacă ar diverge, L2 s-ar stinge tăcut (sau am plăti embed-ul degeaba).
            assert await semantic_candidates_exist(
                conn, DEMO, "ro", embedding_model="text-embedding-3-small"
            )
            for divergent in (
                {"locale": "en"},  # altă limbă (P11)
                {"embedding_model": "text-embedding-3-large"},  # alt spațiu vectorial
                {"volatility_class": "dynamic"},  # altă clasă de volatilitate
                {"prompt_version": "vnext"},  # alt namespace de prompt
            ):
                kwargs = {
                    "locale": "ro",
                    "embedding_model": "text-embedding-3-small",
                    **divergent,
                }
                locale = kwargs.pop("locale")
                assert not await semantic_candidates_exist(conn, DEMO, locale, **kwargs)
                assert (
                    await semantic_lookup(conn, DEMO, locale, emb, **kwargs) is None
                )  # sonda și lookup-ul cad de acord pe fiecare dimensiune de cheie
        finally:
            await tr.rollback()
