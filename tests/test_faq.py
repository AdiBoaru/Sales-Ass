"""NX-74 — strat gratuit FAQ (faq_stage) + tool faq_lookup + query semantic_lookup.

`embed` și query-ul de lookup sunt monkeypatch-uite; ZERO apeluri OpenAI/DB reale (ca
test_cache_stage / test_check_order). Acoperă: hit peste prag → reply + early-exit (triaj
neatins), miss sub prag, izolare pe locale (param), fără LLM, eroare grațioasă, tool.
"""

from types import SimpleNamespace

from src.config import get_settings
from src.db.queries import faqs as faqs_q
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import faq_tools as ft
from src.tools.base import enabled_tools
from src.worker.runner import PipelineDeps, run_pipeline
from src.worker.stages import faq as faq_mod
from src.worker.stages.faq import faq_stage

FAQ_Q = "care e politica de retur"


class _LLM:
    def __init__(self, vec=None):
        self._vec = vec or [0.1, 0.2, 0.3, 0.4]

    async def embed(self, texts, *, model=None):
        return [self._vec for _ in texts]


def _ctx(body: str, *, locale: str = "ro") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language=locale,
    )


# --- faq_stage (strat gratuit) ----------------------------------------------


async def test_hit_above_threshold_serves_and_early_exits(monkeypatch):
    async def fake_lookup(conn, bid, locale, emb, **k):
        return {"id": "f1", "question": "retur?", "answer": "Retur în 14 zile.", "similarity": 0.9}

    monkeypatch.setattr(faq_mod, "semantic_lookup", fake_lookup)

    async def boom_triage(ctx, deps):
        raise AssertionError("triaj NU trebuie atins după un hit FAQ")

    ctx = _ctx(FAQ_Q)
    await run_pipeline(ctx, PipelineDeps(conn=None, llm=_LLM()), [faq_stage, boom_triage])

    assert ctx.reply is not None and ctx.reply.text == "Retur în 14 zile."
    assert ctx.reply.cacheable is True  # răspuns static reutilizabil → G5b îl prinde data viitoare
    assert any(e.type == "faq_hit" and e.properties["faq_id"] == "f1" for e in ctx.events)


async def test_miss_below_threshold_continues(monkeypatch):
    async def fake_lookup(conn, bid, locale, emb, **k):
        return {
            "id": "f2",
            "question": "x",
            "answer": "y",
            "similarity": 0.40,
        }  # sub faq_tau_policy (0.45) — FAQ_Q („retur") e politică → prag relaxat; 0.40 tot pică

    monkeypatch.setattr(faq_mod, "semantic_lookup", fake_lookup)
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None  # miss → pipeline continuă spre triaj
    assert any(e.type == "faq_lookup" and e.properties["layer"] == "miss" for e in ctx.events)


async def test_policy_question_relaxed_threshold_hits(monkeypatch):
    # FAQ_Q = „...retur" → întrebare de POLITICĂ → prag relaxat faq_tau_policy (0.45). 0.60 e SUB
    # faq_tau_high (0.78) dar PESTE 0.45 → HIT. Repară bug-ul „copy-paste": întrebarea de livrare/
    # politică (diluată de partea de produs) nu mai pică la agent, care re-recomanda.
    async def fake_lookup(conn, bid, locale, emb, **k):
        return {"id": "f3", "question": "x", "answer": "Retur în 14 zile.", "similarity": 0.60}

    monkeypatch.setattr(faq_mod, "semantic_lookup", fake_lookup)
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is not None and ctx.reply.text == "Retur în 14 zile."
    assert ctx.reply.cacheable is False  # hit relaxat pe mesaj de politică → NU se cache-uiește
    assert any(e.type == "faq_hit" and e.properties.get("policy") is True for e in ctx.events)


async def test_zero_rows_miss(monkeypatch):
    async def none_lookup(*a, **k):
        return None

    monkeypatch.setattr(faq_mod, "semantic_lookup", none_lookup)
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None
    assert any(e.properties.get("similarity") == 0.0 for e in ctx.events)


async def test_no_llm_skips_without_embed():
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=None))  # fără LLM → skip grațios
    assert ctx.reply is None
    assert not ctx.events  # nici măcar miss (n-a ajuns la lookup)


async def test_empty_body_noop(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("body gol → niciun lookup")

    monkeypatch.setattr(faq_mod, "semantic_lookup", boom)
    ctx = _ctx("   ")
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None


async def test_disabled_noop(monkeypatch):
    monkeypatch.setattr(get_settings(), "faq_enabled", False)

    async def boom(*a, **k):
        raise AssertionError("dezactivat → niciun lookup")

    monkeypatch.setattr(faq_mod, "semantic_lookup", boom)
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None


async def test_lookup_error_is_graceful_miss(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(faq_mod, "semantic_lookup", boom)
    ctx = _ctx(FAQ_Q)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))  # nu propagă excepția
    assert ctx.reply is None


# --- NX-124a: fallback de locale (gated) -------------------------------------


def _fallback_settings(**over):
    base = dict(
        faq_enabled=True,
        faq_tau_high=0.78,
        faq_tau_policy=0.45,
        faq_fallback_tau=0.85,
        faq_locale_fallback_enabled=True,
        model_embed="m1",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_locale_fallback_serves_default_locale(monkeypatch):
    # user pe HU → miss; default_locale RO are cunoștința → fallback prag-strict o servește.
    async def fake_lookup(conn, bid, locale, emb, **k):
        if locale == "ro":
            return {"id": "f-ro", "question": "q", "answer": "Retur 14 zile.", "similarity": 0.95}
        return None  # miss pe HU

    monkeypatch.setattr(faq_mod, "semantic_lookup", fake_lookup)
    monkeypatch.setattr(faq_mod, "get_settings", _fallback_settings)
    ctx = _ctx(FAQ_Q, locale="hu")  # business.default_locale = "ro" (default)
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is not None and ctx.reply.text == "Retur 14 zile."
    assert ctx.reply.cacheable is False  # cross-locale → NU se cache-uiește (evită otrăvirea)
    assert any(e.type == "faq_hit" and e.properties.get("locale_fallback") for e in ctx.events)


async def test_locale_unserved_when_no_fallback_hit(monkeypatch):
    async def none_lookup(*a, **k):
        return None

    monkeypatch.setattr(faq_mod, "semantic_lookup", none_lookup)
    monkeypatch.setattr(faq_mod, "get_settings", _fallback_settings)
    ctx = _ctx(FAQ_Q, locale="hu")
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert ctx.reply is None
    assert any(e.type == "locale_unserved" and e.properties["locale"] == "hu" for e in ctx.events)


async def test_locale_fallback_skipped_when_same_locale(monkeypatch):
    # ctx.language == default_locale → NU al doilea lookup (fără cost dublu); miss normal.
    calls = []

    async def fake_lookup(conn, bid, locale, emb, **k):
        calls.append(locale)
        return None

    monkeypatch.setattr(faq_mod, "semantic_lookup", fake_lookup)
    monkeypatch.setattr(faq_mod, "get_settings", _fallback_settings)
    ctx = _ctx(FAQ_Q, locale="ro")  # == default_locale
    await faq_stage(ctx, PipelineDeps(conn=None, llm=_LLM()))
    assert calls == ["ro"]  # un singur lookup, fără fallback
    assert not any(e.type == "locale_unserved" for e in ctx.events)


# --- semantic_lookup (query) — fake conn -------------------------------------


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.captured = None

    async def fetchrow(self, sql, *args):
        self.captured = args
        return self._row


async def test_query_returns_dict_and_passes_locale():
    conn = _FakeConn({"id": "f9", "question": "q", "answer": "a", "similarity": 0.88})
    out = await faqs_q.semantic_lookup(conn, "biz-1", "hu", [0.1, 0.2], embedding_model="m1")
    assert out["answer"] == "a"
    # business_id=$1, locale=$2, embedding_model=$4 trec în WHERE (izolare + limbă + model)
    assert conn.captured[0] == "biz-1" and conn.captured[1] == "hu"
    assert conn.captured[3] == "m1"  # NX-124a: filtru pe model


async def test_query_none_on_no_rows():
    conn = _FakeConn(None)
    assert await faqs_q.semantic_lookup(conn, "biz-1", "ro", [0.1], embedding_model="m1") is None


# --- tool faq_lookup ---------------------------------------------------------


def _deps(llm=None):
    return PipelineDeps(conn=object(), redis=None, llm=llm)


def test_faq_lookup_in_sales_toolset():
    assert "faq_lookup" in enabled_tools(None, "sales")
    # NX-128++ (FAQ-first): `faq_lookup` ȘI pe ORDER — o întrebare de proces/politică rutată acolo
    # (cum comand, ce retur, cât e livrarea) primește răspuns din baza de cunoștințe, FĂRĂ cont.
    assert "faq_lookup" in enabled_tools(None, "order")


async def test_tool_hit_returns_answer(monkeypatch):
    async def fake_lookup(conn, bid, locale, emb, **k):
        return {"id": "f1", "question": "q", "answer": "Livrare 1-3 zile.", "similarity": 0.85}

    monkeypatch.setattr(ft, "semantic_lookup", fake_lookup)
    res = await ft.faq_lookup_tool(_ctx(FAQ_Q), _deps(_LLM()), {"query": "cat e livrarea"})
    assert res.ok is True and res.llm_view == "Livrare 1-3 zile." and res.products == []


async def test_tool_miss_neutral(monkeypatch):
    async def fake_lookup(conn, bid, locale, emb, **k):
        return {
            "id": "f1",
            "question": "q",
            "answer": "x",
            "similarity": 0.50,
        }  # sub faq_tau_tool (0.66)

    monkeypatch.setattr(ft, "semantic_lookup", fake_lookup)
    res = await ft.faq_lookup_tool(_ctx(FAQ_Q), _deps(_LLM()), {"query": "ceva"})
    assert res.ok is True and "Nu am un răspuns" in res.llm_view


async def test_tool_no_llm(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("fără LLM → nu atinge DB")

    monkeypatch.setattr(ft, "semantic_lookup", boom)
    res = await ft.faq_lookup_tool(_ctx(FAQ_Q), _deps(None), {"query": "x"})
    assert res.ok is False and res.error == "no_llm"
