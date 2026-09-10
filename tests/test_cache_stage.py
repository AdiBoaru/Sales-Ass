"""G5b-1 — cache_stage (lookup) + _cache_writeback (gate), fără DB/LLM real.

Query-urile de lookup și `embed` sunt monkeypatch-uite; testăm logica: hit exact,
hit semantic peste/sub prag, bypass dynamic, gate-ul de write-back.
"""

from src.config import get_settings
from src.db.provider import static_db
from src.models import BusinessConfig, Contact, InboundMessage, Reply, TurnContext
from src.worker import aftercare as ac_mod
from src.worker.aftercare import _cache_writeback
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
    async def fake_exact(conn, bid, locale, h, **k):
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
    async def no_exact(*a, **k):
        return None

    async def fake_sem(conn, bid, locale, emb, **k):
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
    async def no_exact(*a, **k):
        return None

    async def fake_sem(conn, bid, locale, emb, **k):
        return {"id": "e3", "answer": "x", "similarity": 0.80}  # sub τ_high (0.92)

    monkeypatch.setattr(cache_mod, "exact_lookup", no_exact)
    monkeypatch.setattr(cache_mod, "semantic_lookup", fake_sem)

    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.from_cache is False
    assert ctx.reply is None  # miss → pipeline continuă
    assert any(e.type == "cache_lookup" and e.properties["layer"] == "miss" for e in ctx.events)


async def test_realtime_query_bypasses(monkeypatch):
    # G5b-2: `dynamic` NU mai face bypass (trece prin cache cu price-check). DOAR
    # `realtime` (comandă/personal) e rutat pe lângă cache — răspuns specific userului.
    async def boom(*a, **k):
        raise AssertionError("nu trebuie să facă lookup pe realtime")

    monkeypatch.setattr(cache_mod, "exact_lookup", boom)
    ctx = _ctx("unde e comanda mea")
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None
    assert any(
        e.type == "cache_bypass" and e.properties["volatility"] == "realtime" for e in ctx.events
    )


async def test_contextual_cheaper_bypasses(monkeypatch):
    # „mai ieftin" e relativ la setul afișat al ACESTUI client → niciun lookup în cache-ul
    # partajat (ar servi răspunsul altui client). Bypass → turul ajunge la agent.
    async def boom(*a, **k):
        raise AssertionError("nu trebuie să facă lookup pe contextual")

    monkeypatch.setattr(cache_mod, "exact_lookup", boom)
    ctx = _ctx("ceva mai ieftin")
    await cache_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None
    assert any(
        e.type == "cache_bypass" and e.properties["volatility"] == "contextual" for e in ctx.events
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

    monkeypatch.setattr(ac_mod, "upsert_entry", fake_upsert)
    ctx = _ctx_reply(STATIC_Q, Reply(text="Retur în 14 zile."))
    await _cache_writeback(static_db(_FakeConn()), _LLM(), "biz-1", "ro", STATIC_Q, ctx)
    assert written["answer"] == "Retur în 14 zile."
    assert written["volatility_class"] == "static"


async def test_writeback_skips_from_cache(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("nu re-scrie un hit")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx_reply(STATIC_Q, Reply(text="raspuns valid"), from_cache=True)
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", STATIC_Q, ctx)  # nu aruncă


async def test_writeback_skips_products(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("produsele = dynamic, nu se scriu în v1")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx_reply(STATIC_Q, Reply(text="recomandare", products=[{"product_id": "p1"}]))
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", STATIC_Q, ctx)


async def test_writeback_skips_dynamic_query(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("query dynamic → nu se scrie")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx_reply("caut crema sub 80 lei", Reply(text="raspuns oarecare lung"))
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", "caut crema sub 80 lei", ctx)


async def test_writeback_skips_contextual_query(monkeypatch):
    # Un reply la „mai ieftin" e relativ la setul afișat → niciodată scris în cache
    # (altfel îl servim altui client cu alt baseline = cache poisoning).
    async def boom(*a, **k):
        raise AssertionError("query contextual → nu se scrie")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx_reply("ceva mai ieftin", Reply(text="Uite o variantă mai ieftină pentru tine."))
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", "ceva mai ieftin", ctx)


async def test_writeback_skips_not_cacheable(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("clarify/fallback → nu se scrie")

    monkeypatch.setattr(ac_mod, "upsert_entry", boom)
    ctx = _ctx_reply("ceva ambiguu", Reply(text="ce anume cauți?", cacheable=False))
    await _cache_writeback(static_db(None), _LLM(), "biz-1", "ro", "ceva ambiguu", ctx)


# --- NX-291: sonda de dinaintea embed-ului ----------------------------------
# `embed()` e singurul apel EXTERN al stratului gratuit și rulează pe tot traficul care ajunge
# aici. Pe o mulțime servibilă goală, L2 nu poate întoarce nimic — deci apelul e plătit pentru un
# rezultat imposibil. Sonda folosește exact filtrul lui `semantic_lookup`, deci nu ghicește.


class _ExplodingLLM:
    """Orice apel de embedding e un eșec al testului: exact costul pe care îl evităm."""

    async def embed(self, texts):
        raise AssertionError("embed() nu trebuie chemat fără candidați servibili")


async def _run_with_probe(monkeypatch, probe, llm):
    async def no_exact(*a, **k):
        return None

    monkeypatch.setattr(cache_mod, "exact_lookup", no_exact)
    monkeypatch.setattr(cache_mod, "semantic_candidates_exist", probe)
    ctx = _ctx(STATIC_Q)
    await cache_stage(ctx, PipelineDeps(conn=None, llm=llm))
    return ctx


async def test_no_candidates_skips_the_embedding_call(monkeypatch):
    async def empty(*a, **k):
        return False

    ctx = await _run_with_probe(monkeypatch, empty, _ExplodingLLM())
    assert ctx.reply is None and ctx.from_cache is False  # miss → pipeline continuă
    assert any(
        e.type == "cache_lookup"
        and e.properties["layer"] == "miss"
        and e.properties.get("reason") == "no_candidates"
        for e in ctx.events
    )


async def test_candidates_present_still_pays_for_l2(monkeypatch):
    """Simetria care contează: sonda nu are voie să devină un al doilea prag de servire."""

    async def present(*a, **k):
        return True

    async def fake_sem(conn, bid, locale, emb, **k):
        return {"id": "e9", "answer": "Livrare 2-4 zile.", "similarity": 0.99}

    async def fake_touch(*a):
        pass

    monkeypatch.setattr(cache_mod, "semantic_lookup", fake_sem)
    monkeypatch.setattr(cache_mod, "touch_hit", fake_touch)
    ctx = await _run_with_probe(monkeypatch, present, _LLM())
    assert ctx.from_cache is True and ctx.reply.text == "Livrare 2-4 zile."


async def test_probe_failure_falls_back_to_embedding(monkeypatch):
    """FAIL-OPEN. O optimizare a unei optimizări n-are voie să stingă stratul pe care îl servește:
    un cache care nu mai servește niciodată nu se vede în niciun răspuns, doar în factură."""

    async def boom(*a, **k):
        raise RuntimeError("migrare lipsă / DB jos")

    async def fake_sem(conn, bid, locale, emb, **k):
        return {"id": "e10", "answer": "Retur în 30 de zile.", "similarity": 0.99}

    async def fake_touch(*a):
        pass

    monkeypatch.setattr(cache_mod, "semantic_lookup", fake_sem)
    monkeypatch.setattr(cache_mod, "touch_hit", fake_touch)
    ctx = await _run_with_probe(monkeypatch, boom, _LLM())
    assert ctx.from_cache is True  # sonda a picat, dar L2 a rulat exact ca înainte


def test_probe_and_lookup_cannot_drift():
    """Structural: ambele interogări se construiesc din ACELAȘI fragment de filtru. Dacă cineva
    schimbă o dimensiune de cheie într-una singură, sonda ar răspunde despre altă mulțime decât
    cea căutată — iar greșeala ar fi tăcută în ambele sensuri."""
    from src.db.queries import semantic_cache as sc

    assert sc._SERVABLE_FILTER in sc._EXISTS_SQL
    assert sc._SERVABLE_FILTER in sc._SEMANTIC_SQL
    for key in ("business_id", "locale", "volatility_class", "embedding_model", "prompt_version"):
        assert key in sc._SERVABLE_FILTER
