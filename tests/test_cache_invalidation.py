"""G5b-2 — invalidare cache dynamic: price-check self-healing + data_version + purjă.

Unit (monkeypatch, fără DB/LLM): cache_stage pe tier `dynamic` (hit cu preț valid,
evict pe data_version / preț schimbat / produs dispărut / semnătură coruptă), write-back
dynamic. Integration (DB real): `current_prices`, provenance pe exact_lookup, price-check
și `purge_by_product`/bump pe pgvector, în tranzacție rollback-uită.
"""

import pytest

from src.db.provider import static_db
from src.models import BusinessConfig, Contact, InboundMessage, Reply, TurnContext
from src.worker import aftercare as ac_mod
from src.worker.aftercare import _cache_writeback
from src.worker.runner import PipelineDeps
from src.worker.stages import cache as cache_mod
from src.worker.stages.cache import cache_stage

DYNAMIC_Q = "caut o crema sub 80 lei"  # classify_volatility → dynamic
STATIC_Q = "care e politica de retur"  # classify_volatility → static


class _LLM:
    async def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _NoopTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _NoopTx()


def _ctx(body: str) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language="ro",
    )


def _patch(monkeypatch, **fns):
    """Monkeypatch funcțiile importate în cache_stage (cache_mod)."""
    for name, fn in fns.items():
        monkeypatch.setattr(cache_mod, name, fn)


async def _noop(*a, **k):
    return None


# --- cache_stage dynamic: hit valid ------------------------------------------


async def test_dynamic_hit_price_match_serves(monkeypatch):
    entry = {
        "id": "e1",
        "answer": "Îți recomand crema X la 49.90 lei.",
        "retrieval_signature": [{"product_id": "p1", "price": 49.9}],
        "data_version": 3,
    }

    async def fake_exact(
        conn, bid, locale, h, *, volatility_class, embedding_model=None, prompt_version="v1"
    ):
        assert volatility_class == "dynamic"
        return entry

    async def deny_delete(*a, **k):
        raise AssertionError("preț valid → NU se evacuează")

    _patch(
        monkeypatch,
        exact_lookup=fake_exact,
        touch_hit=_noop,
        delete_entry=deny_delete,
        get_data_version=lambda conn, bid: _coro(3),
        current_prices=lambda conn, bid, pids: _coro({"p1": 49.9}),
    )

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.from_cache is True
    assert ctx.reply.text == entry["answer"]
    assert any(
        e.type == "cache_lookup"
        and e.properties["layer"] == "exact"
        and e.properties["volatility"] == "dynamic"
        for e in ctx.events
    )


# --- cache_stage dynamic: invalidare (evict → miss) --------------------------


def _coro(value):
    async def _c(*a, **k):
        return value

    return _c()


async def _evict_setup(monkeypatch, entry, *, data_version=3, prices=None):
    """Exact lookup întoarce `entry`; L2 semantic = None (→ miss final). Înregistrează
    id-urile evacuate. Întoarce lista `deleted`."""
    deleted = []

    async def fake_exact(
        conn, bid, locale, h, *, volatility_class, embedding_model=None, prompt_version="v1"
    ):
        return entry

    async def fake_sem(
        conn, bid, locale, emb, *, volatility_class, embedding_model=None, prompt_version="v1"
    ):
        return None

    async def fake_delete(conn, bid, eid):
        deleted.append(eid)

    _patch(
        monkeypatch,
        exact_lookup=fake_exact,
        semantic_lookup=fake_sem,
        touch_hit=_noop,
        delete_entry=fake_delete,
        get_data_version=lambda conn, bid: _coro(data_version),
        current_prices=lambda conn, bid, pids: _coro(prices if prices is not None else {}),
    )
    return deleted


async def test_dynamic_data_version_mismatch_evicts(monkeypatch):
    entry = {
        "id": "e1",
        "answer": "x",
        "retrieval_signature": [{"product_id": "p1", "price": 49.9}],
        "data_version": 2,
    }
    deleted = await _evict_setup(monkeypatch, entry, data_version=3, prices={"p1": 49.9})

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.reply is None  # evict → miss → pipeline continuă
    assert deleted == ["e1"]
    assert any(
        e.type == "cache_lookup" and e.properties["layer"] == "stale_evict" for e in ctx.events
    )


async def test_dynamic_price_changed_evicts(monkeypatch):
    entry = {
        "id": "e2",
        "answer": "x",
        "retrieval_signature": [{"product_id": "p1", "price": 49.9}],
        "data_version": 3,
    }
    deleted = await _evict_setup(monkeypatch, entry, data_version=3, prices={"p1": 59.9})

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.reply is None
    assert deleted == ["e2"]


async def test_dynamic_product_disappeared_evicts(monkeypatch):
    entry = {
        "id": "e3",
        "answer": "x",
        "retrieval_signature": [{"product_id": "p1", "price": 49.9}],
        "data_version": 3,
    }
    deleted = await _evict_setup(monkeypatch, entry, data_version=3, prices={})  # p1 lipsește

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.reply is None
    assert deleted == ["e3"]


async def test_dynamic_empty_signature_evicts(monkeypatch):
    entry = {"id": "e4", "answer": "x", "retrieval_signature": None, "data_version": 3}
    deleted = await _evict_setup(monkeypatch, entry, data_version=3, prices={"p1": 49.9})

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.reply is None
    assert deleted == ["e4"]


async def test_dynamic_pricecheck_raises_is_miss(monkeypatch):
    entry = {
        "id": "e5",
        "answer": "x",
        "retrieval_signature": [{"product_id": "p1", "price": 49.9}],
        "data_version": 3,
    }

    async def fake_exact(
        conn, bid, locale, h, *, volatility_class, embedding_model=None, prompt_version="v1"
    ):
        return entry

    async def boom(*a, **k):
        raise RuntimeError("DB down")

    _patch(
        monkeypatch,
        exact_lookup=fake_exact,
        touch_hit=_noop,
        delete_entry=_noop,
        get_data_version=lambda conn, bid: _coro(3),
        current_prices=boom,  # aruncă în price-check
    )

    ctx = _ctx(DYNAMIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))  # fail-closed, nu aruncă
    assert ctx.reply is None  # nu servește nevalidat


# --- static NU e afectat de data_version (price-check) ------------------------


async def test_static_ignores_data_version(monkeypatch):
    entry = {
        "id": "s1",
        "answer": "Retur în 14 zile.",
        "retrieval_signature": None,
        "data_version": None,
    }

    async def fake_exact(
        conn, bid, locale, h, *, volatility_class, embedding_model=None, prompt_version="v1"
    ):
        assert volatility_class == "static"
        return entry

    async def boom(*a, **k):
        raise AssertionError("static NU consultă data_version / price-check")

    _patch(
        monkeypatch,
        exact_lookup=fake_exact,
        touch_hit=_noop,
        get_data_version=boom,
        current_prices=boom,
    )

    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.from_cache is True
    assert ctx.reply.text == "Retur în 14 zile."


# --- write-back dynamic ------------------------------------------------------


async def test_writeback_caches_dynamic(monkeypatch):
    written = {}

    async def fake_upsert(conn, bid, locale, **kw):
        written.update(kw)

    async def fake_dv(conn, bid):
        return 5

    monkeypatch.setattr(ac_mod, "upsert_entry", fake_upsert)
    monkeypatch.setattr(ac_mod, "get_data_version", fake_dv)

    reply = Reply(
        text="Îți recomand crema X la 49.90 lei.",
        products=[{"product_id": "p1", "price": 49.9, "name": "Crema X"}],
    )
    ctx = _ctx(DYNAMIC_Q)
    ctx.reply = reply
    await _cache_writeback(static_db(_FakeConn()), _LLM(), "biz-1", "ro", DYNAMIC_Q, ctx)

    assert written["volatility_class"] == "dynamic"
    assert written["retrieval_signature"] == [{"product_id": "p1", "price": 49.9}]
    assert written["data_version"] == 5
    assert written["ttl_minutes"] == 30
    assert any(
        e.type == "cache_write" and e.properties["volatility"] == "dynamic" for e in ctx.events
    )


async def test_writeback_dynamic_without_products_skips(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("dynamic fără produse → nu se scrie")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx(DYNAMIC_Q)
    ctx.reply = Reply(text="raspuns dynamic fara produse")  # products None
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", DYNAMIC_Q, ctx)  # nu aruncă


# --- Integration (DB real) ---------------------------------------------------

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"


@pytest.fixture
async def pool():
    from src.db.connection import close_pool, get_pool

    p = await get_pool()
    yield p
    await close_pool()


@pytest.mark.integration
async def test_current_prices_and_pricecheck_real_db(pool):
    from src.db.queries.businesses import get_data_version
    from src.db.queries.semantic_cache import (
        current_prices,
        exact_lookup,
        purge_by_product,
        upsert_entry,
    )

    emb = [0.011] * 1536
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO)

            pid = await conn.fetchval(
                "select id::text from products "
                "where business_id = $1 and status = 'active' limit 1",
                DEMO,
            )
            assert pid is not None
            prices = await current_prices(conn, DEMO, [pid])
            assert pid in prices  # produs activ → preț curent
            price = prices[pid]
            dv = await get_data_version(conn, DEMO)

            await upsert_entry(
                conn,
                DEMO,
                "ro",
                canonical_str="proba g5b2 dynamic",
                canonical_hash="g5b2-probe-hash",
                embedding=emb,
                answer="Recomandare de test.",
                volatility_class="dynamic",
                embedding_model="text-embedding-3-small",
                quality_score=1.0,
                ttl_minutes=30,
                retrieval_signature=[{"product_id": pid, "price": price}],
                data_version=dv,
            )

            hit = await exact_lookup(
                conn, DEMO, "ro", "g5b2-probe-hash", volatility_class="dynamic"
            )
            assert hit is not None
            assert hit["retrieval_signature"] == [{"product_id": pid, "price": price}]
            assert hit["data_version"] == dv
            # preț neschimbat → price-check ar servi
            cur = await current_prices(conn, DEMO, [pid])
            assert abs(cur[pid] - price) < 0.005

            # purge_by_product țintește entry-ul care conține pid
            n = await purge_by_product(conn, DEMO, pid)
            assert n >= 1
            assert (
                await exact_lookup(conn, DEMO, "ro", "g5b2-probe-hash", volatility_class="dynamic")
                is None
            )
        finally:
            await tr.rollback()


@pytest.mark.integration
async def test_bump_data_version_real_db(pool):
    # bump = operație ADMIN (sync de catalog / utilitar) — rulează pe rolul privilegiat
    # (scriptul folosește DSN-ul admin), NU bot_runtime. Tranzacție rollback-uită.
    from src.db.queries.businesses import bump_data_version, get_data_version

    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            before = await get_data_version(conn, DEMO)
            after = await bump_data_version(conn, DEMO)
            assert after == before + 1
        finally:
            await tr.rollback()
