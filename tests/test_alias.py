"""NX-73 — strat gratuit alias (alias_stage) + query-uri lookup_alias / get_faq_answer.

Query-urile sunt monkeypatch-uite; ZERO OpenAI/DB real (ca test_faq / test_cache_stage).
Acoperă: hit FAQ → reply + early-exit (cache/triaj neatins), hit route → ctx.route + triaj sărit,
miss → continuă, FAQ lipsă în limbă → miss, route invalid → miss, eroare DB → miss, normalizare
refolosită, ordinea în DEFAULT_STAGES, izolarea pe locale a get_faq_answer.
"""

from src.config import get_settings
from src.db.queries import aliases as aliases_q
from src.models import BusinessConfig, Contact, InboundMessage, Route, RouteDecision, TurnContext
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, run_pipeline
from src.worker.stages import alias as alias_mod
from src.worker.stages import cache as cache_mod
from src.worker.stages import faq as faq_mod
from src.worker.stages.alias import alias_stage
from src.worker.stages.cache import cache_stage
from src.worker.stages.faq import faq_stage


class _LLM:
    """Adaptor fals cu `embed` — ca să probăm că guard-ul de rută preempt-ează cache/FAQ
    ÎNAINTE de orice embed (nu doar pentru că llm e None)."""

    async def embed(self, texts, *, model=None):
        return [[0.1, 0.2] for _ in texts]


def _ctx(body: str, *, locale: str = "ro") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language=locale,
    )


def _patch_lookup(monkeypatch, alias):
    async def fake_lookup(conn, bid, phrase_norm):
        return alias

    monkeypatch.setattr(alias_mod, "lookup_alias", fake_lookup)


def _alias(kind, *, target_id=None, target_value=None):
    return {"id": "a1", "target_kind": kind, "target_id": target_id, "target_value": target_value}


# --- alias_stage: hit FAQ ----------------------------------------------------


async def test_faq_hit_serves_and_early_exits(monkeypatch):
    _patch_lookup(monkeypatch, _alias("faq", target_id="f1"))

    async def fake_faq(conn, bid, faq_id, locale):
        assert faq_id == "f1" and locale == "ro"
        return "Program L-V 9-18."

    monkeypatch.setattr(alias_mod, "get_faq_answer", fake_faq)

    async def boom_next(ctx, deps):
        raise AssertionError("cache/triaj NU trebuie atins după un hit FAQ alias")

    ctx = _ctx("program?")
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=None), [alias_stage, boom_next])

    assert ctx.reply is not None and ctx.reply.text == "Program L-V 9-18."
    assert ctx.reply.cacheable is True  # static reutilizabil → G5b îl prinde la paraphrase
    assert any(
        e.type == "alias_lookup"
        and e.properties.get("hit")
        and e.properties.get("target_kind") == "faq"
        for e in ctx.events
    )


# --- alias_stage: hit route / category ---------------------------------------


async def test_route_hit_sets_route_no_reply(monkeypatch):
    _patch_lookup(monkeypatch, _alias("route", target_value="order"))
    ctx = _ctx("unde e comanda")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.route is not None and ctx.route.route == Route.ORDER
    assert ctx.reply is None  # rutare, nu răspuns → triaj sărit, agent/order răspunde
    assert any(
        e.type == "alias_lookup" and e.properties.get("route") == "order" for e in ctx.events
    )


async def test_route_hit_makes_triage_a_noop(monkeypatch):
    # dovedește „triaj sărit": cu ctx.route deja setat, triajul nu cheamă LLM-ul (DoD)
    from src.worker.stages.triage import triage_stage

    _patch_lookup(monkeypatch, _alias("route", target_value="order"))
    ctx = _ctx("unde e comanda")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))

    class _LLMBoom:
        model_triage = "nano"

        async def classify_json(self, *a, **k):
            raise AssertionError("triajul NU trebuie să cheme LLM când ruta e deja setată")

    await triage_stage(ctx, PipelineDeps(conn=None, llm=_LLMBoom()))
    assert ctx.route.route == Route.ORDER  # neschimbat de triaj


async def test_category_hit_routes_sales_with_slug(monkeypatch):
    _patch_lookup(monkeypatch, _alias("category", target_value="creme-hidratante"))
    ctx = _ctx("creme hidratante")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.route.route == Route.SALES and ctx.route.category_key == "creme-hidratante"
    assert ctx.reply is None


# --- miss / edge / failure ---------------------------------------------------


async def test_miss_continues(monkeypatch):
    _patch_lookup(monkeypatch, None)
    ctx = _ctx("ceva ce nu e alias")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.reply is None and ctx.route is None
    assert any(e.type == "alias_lookup" and e.properties.get("hit") is False for e in ctx.events)


async def test_faq_locale_miss_is_graceful(monkeypatch):
    _patch_lookup(monkeypatch, _alias("faq", target_id="f1"))

    async def none_faq(conn, bid, faq_id, locale):
        return None  # FAQ lipsă în limba curentă

    monkeypatch.setattr(alias_mod, "get_faq_answer", none_faq)
    ctx = _ctx("retur", locale="hu")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.reply is None and ctx.route is None  # miss → pipeline continuă
    assert any(e.properties.get("reason") == "faq_locale_miss" for e in ctx.events)


async def test_bad_route_value_is_miss(monkeypatch):
    _patch_lookup(monkeypatch, _alias("route", target_value="buy"))  # nu e o Route validă
    ctx = _ctx("x")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.route is None and ctx.reply is None
    assert any(e.properties.get("reason") == "bad_route" for e in ctx.events)


async def test_normalization_reused_for_diacritics(monkeypatch):
    seen = {}

    async def fake_lookup(conn, bid, phrase_norm):
        seen["phrase_norm"] = phrase_norm
        return None

    monkeypatch.setattr(alias_mod, "lookup_alias", fake_lookup)
    ctx = _ctx("Progrăm?")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    # canonicalize: lower + fără diacritice + punctuație→spațiu → match exact pe „program" stocat
    assert seen["phrase_norm"] == "program"


async def test_disabled_noop(monkeypatch):
    # contor, NU `raise` în fake: alias_stage prinde Exception → un AssertionError ar fi înghițit
    # (false confidence). Contorul probează că guard-ul SARE lookup-ul, nu că eroarea e prinsă.
    monkeypatch.setattr(get_settings(), "alias_enabled", False)
    calls = []

    async def track(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(alias_mod, "lookup_alias", track)
    ctx = _ctx("program")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert calls == []  # dezactivat → lookup-ul NU e atins
    assert ctx.reply is None and not ctx.events


async def test_empty_body_noop(monkeypatch):
    calls = []

    async def track(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(alias_mod, "lookup_alias", track)
    ctx = _ctx("   ")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert calls == []  # body gol → lookup-ul NU e atins
    assert ctx.reply is None and ctx.route is None


async def test_lookup_error_is_graceful_miss(monkeypatch):
    async def boom(conn, bid, phrase_norm):
        raise RuntimeError("DB down / migrare neaplicată")

    monkeypatch.setattr(alias_mod, "lookup_alias", boom)
    ctx = _ctx("program")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))  # nu propagă excepția
    assert ctx.reply is None and ctx.route is None


async def test_product_hit_routes_sales_no_category(monkeypatch):
    # product alias poartă target_id (= product_id), de obicei fără target_value → category_key None
    _patch_lookup(monkeypatch, _alias("product", target_id="p99"))
    ctx = _ctx("crema cutare")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert ctx.route.route == Route.SALES and ctx.route.category_key is None
    assert ctx.reply is None
    assert any(
        e.type == "alias_lookup" and e.properties.get("target_kind") == "product"
        for e in ctx.events
    )


# --- guard ctx.route (single-writer) -----------------------------------------


async def test_skips_when_route_already_set(monkeypatch):
    # clarify_resume (NX-130) a rutat deja → alias NU suprascrie ruta și NU hijack-uiește turul (P3)
    calls = []

    async def track(*a, **k):
        calls.append(1)
        return _alias("faq", target_id="f1")

    monkeypatch.setattr(alias_mod, "lookup_alias", track)
    ctx = _ctx("retur")
    ctx.route = RouteDecision(route=Route.SALES)  # pus upstream de clarify_resume
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    assert calls == []  # nu atinge DB
    assert ctx.route.route == Route.SALES and ctx.reply is None  # rută neschimbată, fără hijack


async def test_route_hit_pipeline_continues_to_next_stage(monkeypatch):
    # un hit de rutare (fără reply) NU early-exit-ează → pipeline-ul continuă spre agent
    _patch_lookup(monkeypatch, _alias("category", target_value="creme-hidratante"))
    reached = []

    async def sentinel_next(ctx, deps):
        reached.append(1)

    ctx = _ctx("creme hidratante")
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=None), [alias_stage, sentinel_next])
    assert reached == [1]  # stagiul următor (agent) ESTE atins
    assert ctx.route.route == Route.SALES and ctx.reply is None


async def test_route_hit_preempts_cache_and_faq(monkeypatch):
    # FIX review NX-73: cache/FAQ RESPECTĂ ctx.route → nu deflectează rutarea deterministă de alias.
    # Contoare (nu `raise`): cache/faq prind Exception, deci un boom ar fi înghițit (false conf).
    _patch_lookup(monkeypatch, _alias("category", target_value="creme"))
    cache_calls, faq_calls, reached = [], [], []

    async def track_cache(*a, **k):
        cache_calls.append(1)
        return None

    async def track_faq(*a, **k):
        faq_calls.append(1)
        return None

    async def agent_sentinel(ctx, deps):
        reached.append(1)

    monkeypatch.setattr(cache_mod, "exact_lookup", track_cache)
    monkeypatch.setattr(faq_mod, "semantic_lookup", track_faq)
    ctx = _ctx("creme hidratante")
    await run_pipeline(
        ctx,
        PipelineDeps(conn=None, llm=_LLM()),
        [alias_stage, cache_stage, faq_stage, agent_sentinel],
    )
    assert cache_calls == [] and faq_calls == []  # ghidate de ctx.route → niciun lookup/embed
    assert reached == [1] and ctx.reply is None and ctx.route.route == Route.SALES


async def test_events_carry_no_phrase_norm_or_body(monkeypatch):
    # P12: alias_lookup NU expune phrase_norm / corpul mesajului (pot conține fragmente PII)
    _patch_lookup(monkeypatch, _alias("route", target_value="order"))
    ctx = _ctx("unde e comanda 0712345678")
    await alias_stage(ctx, PipelineDeps(conn=None, llm=None))
    for e in ctx.events:
        if e.type == "alias_lookup":
            assert "phrase_norm" not in e.properties and "body" not in e.properties
            assert all("0712345678" not in str(v) for v in e.properties.values())


# --- ordinea în DEFAULT_STAGES -----------------------------------------------


def test_alias_between_language_and_cache():
    names = [getattr(s, "__name__", "") for s in DEFAULT_STAGES]
    assert "alias_stage" in names
    i_lang = names.index("language_stage")
    i_alias = names.index("alias_stage")
    i_cache = names.index("cache_stage")
    assert i_lang < i_alias < i_cache  # între limbă și cache
    assert i_alias + 1 == i_cache  # IMEDIAT înainte de cache (anchor-ul cardului)


# --- query-uri (fake conn, fără DB) ------------------------------------------


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.captured = None

    async def fetchrow(self, sql, *args):
        self.captured = args
        return self._row


async def test_lookup_alias_returns_dict_and_scopes_business():
    conn = _FakeConn(
        {"id": "a1", "target_kind": "route", "target_id": None, "target_value": "order"}
    )
    out = await aliases_q.lookup_alias(conn, "biz-1", "unde e comanda")
    assert out["target_kind"] == "route" and out["target_value"] == "order"
    assert conn.captured == ("biz-1", "unde e comanda")  # business_id + phrase_norm (P7)


async def test_lookup_alias_none_on_no_rows():
    conn = _FakeConn(None)
    assert await aliases_q.lookup_alias(conn, "biz-1", "x") is None


async def test_get_faq_answer_filters_locale():
    conn = _FakeConn({"answer": "Retur în 14 zile."})
    out = await aliases_q.get_faq_answer(conn, "biz-1", "f1", "ro")
    assert out == "Retur în 14 zile."
    assert conn.captured == ("biz-1", "f1", "ro")  # business_id + id + locale (P7/P11)


async def test_get_faq_answer_none_on_no_rows():
    conn = _FakeConn(None)
    assert await aliases_q.get_faq_answer(conn, "biz-1", "f1", "hu") is None
