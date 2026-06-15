"""G5b-1 — cache_stage (lookup) + _cache_writeback (gate), fără DB/LLM real.

Query-urile de lookup și `embed` sunt monkeypatch-uite; testăm logica: hit exact,
hit semantic peste/sub prag, bypass dynamic, gate-ul de write-back.
"""

from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, Reply, TurnContext
from src.worker import processor as proc_mod
from src.worker.processor import _cache_writeback
from src.worker.runner import PipelineDeps
from src.worker.stages import cache as cache_mod
from src.worker.stages.cache import cache_stage

STATIC_Q = "care e politica de retur"


class _LLM:
    def __init__(self, vec=None):
        self._vec = vec or [0.1, 0.2, 0.3, 0.4]

    async def embed(self, texts):
        return [self._vec for _ in texts]


class _NoopTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    """Conn minimal pentru write-back: doar `transaction()` (savepoint no-op)."""

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


# --- cache_stage -------------------------------------------------------------


async def test_exact_hit_serves_and_skips_pipeline(monkeypatch):
    async def fake_exact(conn, bid, locale, h):
        return {"id": "e1", "answer": "Retur în 14 zile."}

    async def fake_touch(conn, bid, eid):
        pass

    monkeypatch.setattr(cache_mod, "exact_lookup", fake_exact)
    monkeypatch.setattr(cache_mod, "touch_hit", fake_touch)

    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))

    assert ctx.from_cache is True
    assert ctx.reply.text == "Retur în 14 zile."
    assert any(e.type == "cache_lookup" and e.properties["layer"] == "exact" for e in ctx.events)


async def test_semantic_hit_above_threshold(monkeypatch):
    async def no_exact(*a):
        return None

    async def fake_sem(conn, bid, locale, emb):
        return {"id": "e2", "answer": "Livrare 2-4 zile.", "similarity": 0.95}

    async def fake_touch(*a):
        pass

    monkeypatch.setattr(cache_mod, "exact_lookup", no_exact)
    monkeypatch.setattr(cache_mod, "semantic_lookup", fake_sem)
    monkeypatch.setattr(cache_mod, "touch_hit", fake_touch)

    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.from_cache is True
    assert ctx.reply.text == "Livrare 2-4 zile."


async def test_semantic_miss_below_threshold(monkeypatch):
    async def no_exact(*a):
        return None

    async def fake_sem(conn, bid, locale, emb):
        return {"id": "e3", "answer": "x", "similarity": 0.80}  # sub τ_high (0.92)

    monkeypatch.setattr(cache_mod, "exact_lookup", no_exact)
    monkeypatch.setattr(cache_mod, "semantic_lookup", fake_sem)

    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.from_cache is False
    assert ctx.reply is None  # miss → pipeline continuă
    assert any(e.type == "cache_lookup" and e.properties["layer"] == "miss" for e in ctx.events)


async def test_dynamic_query_bypasses(monkeypatch):
    async def boom(*a):
        raise AssertionError("nu trebuie să facă lookup pe dynamic")

    monkeypatch.setattr(cache_mod, "exact_lookup", boom)
    ctx = _ctx("caut o cremă sub 80 lei")
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None
    assert any(
        e.type == "cache_bypass" and e.properties["volatility"] == "dynamic" for e in ctx.events
    )


async def test_disabled_noop(monkeypatch):
    monkeypatch.setattr(get_settings(), "cache_enabled", False)

    async def boom(*a):
        raise AssertionError("dezactivat → niciun lookup")

    monkeypatch.setattr(cache_mod, "exact_lookup", boom)
    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None


# --- _cache_writeback (gate) -------------------------------------------------


def _ctx_reply(body: str, reply: Reply, *, from_cache: bool = False) -> TurnContext:
    ctx = _ctx(body)
    ctx.reply = reply
    ctx.from_cache = from_cache
    return ctx


async def test_writeback_caches_static(monkeypatch):
    written = {}

    async def fake_upsert(conn, bid, locale, **kw):
        written.update(kw)

    monkeypatch.setattr(proc_mod, "upsert_entry", fake_upsert)
    ctx = _ctx_reply(STATIC_Q, Reply(text="Retur în 14 zile."))
    await _cache_writeback(_FakeConn(), _LLM(), "biz-1", "ro", STATIC_Q, ctx)
    assert written["answer"] == "Retur în 14 zile."
    assert written["volatility_class"] == "static"


async def test_writeback_skips_from_cache(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("nu re-scrie un hit")

    monkeypatch.setattr(proc_mod, "upsert_entry", boom)
    ctx = _ctx_reply(STATIC_Q, Reply(text="raspuns valid"), from_cache=True)
    await _cache_writeback(None, _LLM(), "biz-1", "ro", STATIC_Q, ctx)  # nu aruncă


async def test_writeback_skips_products(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("produsele = dynamic, nu se scriu în v1")

    monkeypatch.setattr(proc_mod, "upsert_entry", boom)
    ctx = _ctx_reply(STATIC_Q, Reply(text="recomandare", products=[{"product_id": "p1"}]))
    await _cache_writeback(None, _LLM(), "biz-1", "ro", STATIC_Q, ctx)


async def test_writeback_skips_dynamic_query(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("query dynamic → nu se scrie")

    monkeypatch.setattr(proc_mod, "upsert_entry", boom)
    ctx = _ctx_reply("caut crema sub 80 lei", Reply(text="raspuns oarecare lung"))
    await _cache_writeback(None, _LLM(), "biz-1", "ro", "caut crema sub 80 lei", ctx)


async def test_writeback_skips_not_cacheable(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("clarify/fallback → nu se scrie")

    monkeypatch.setattr(proc_mod, "upsert_entry", boom)
    ctx = _ctx_reply("ceva ambiguu", Reply(text="ce anume cauți?", cacheable=False))
    await _cache_writeback(None, _LLM(), "biz-1", "ro", "ceva ambiguu", ctx)
