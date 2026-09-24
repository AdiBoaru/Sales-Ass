"""NX-74 — unealta `faq_lookup` fără embeddings (2026-09-24) + query-ul `list_active`.

Unealta aduce setul ACTIV de FAQ al tenantului, pe locale, iar modelul alege răspunsul. Query-ul
e monkeypatch-uit; ZERO apeluri OpenAI/DB reale. Ce se pinuiește: izolarea pe tenant și pe limbă
(P7/P11), fallback-ul de locale doar sub flag, plafonul vederii (intrări întregi, nicio regulă
tăiată la mijloc) și că NICIUN apel de model nu mai pleacă din unealtă.
"""

from src.config import get_settings
from src.db.queries import faqs as faqs_q
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import faq_tools as ft
from src.tools.base import enabled_tools
from src.worker.runner import PipelineDeps

_ROWS = [
    {"id": "f1", "question": "Cât costă livrarea?", "answer": "Livrare gratuită peste 199 lei."},
    {"id": "f2", "question": "Pot returna un produs?", "answer": "Da, în 30 de zile."},
]


def _ctx(*, locale: str = "ro", default_locale: str | None = "ro") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n", default_locale=default_locale),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="cat e livrarea"),
        conversation_id="conv",
        language=locale,
    )


class _NoEmbedLLM:
    async def embed(self, texts, *, model=None):  # pragma: no cover - nu trebuie chemat
        raise AssertionError("unealta FAQ nu mai cheamă niciun model")


def _deps():
    return PipelineDeps(conn=object(), redis=None, llm=_NoEmbedLLM())


def _patch_rows(monkeypatch, by_locale):
    calls = []

    async def fake(conn, bid, locale, *, limit):
        calls.append((bid, locale, limit))
        return list(by_locale.get(locale, []))

    monkeypatch.setattr(ft, "list_active", fake)
    return calls


def test_faq_lookup_in_sales_and_order_toolsets():
    assert "faq_lookup" in enabled_tools(None, "sales")
    # NX-128++ (FAQ-first): și pe ORDER — o întrebare de proces/politică primește răspuns FĂRĂ cont.
    assert "faq_lookup" in enabled_tools(None, "order")


async def test_tool_returns_the_whole_active_set_for_the_model_to_choose(monkeypatch):
    calls = _patch_rows(monkeypatch, {"ro": _ROWS})
    res = await ft.faq_lookup_tool(_ctx(), _deps(), {"query": "cat e livrarea"})
    assert res.ok is True and res.products == []
    assert "Livrare gratuită peste 199 lei." in res.llm_view
    assert "Da, în 30 de zile." in res.llm_view  # modelul vede setul, nu un top-1 ghicit
    assert "nu o compune" in res.llm_view  # instrucțiunea de a nu inventa o regulă
    assert calls == [("biz-1", "ro", ft.MAX_FAQS)]  # tenantul și limba vin din ctx (P7/P11)


async def test_empty_set_is_a_neutral_answer_not_an_invented_rule(monkeypatch):
    _patch_rows(monkeypatch, {})
    res = await ft.faq_lookup_tool(_ctx(), _deps(), {"query": "garantie"})
    assert res.ok is True and "nu ai informația" in res.llm_view


async def test_locale_fallback_only_under_flag(monkeypatch):
    calls = _patch_rows(monkeypatch, {"ro": _ROWS})
    s = get_settings()
    monkeypatch.setattr(s, "faq_locale_fallback_enabled", False)
    res = await ft.faq_lookup_tool(_ctx(locale="en"), _deps(), {"query": "shipping"})
    assert "nu ai informația" in res.llm_view  # fără flag: nicio regulă din altă limbă
    assert [c[1] for c in calls] == ["en"]

    calls.clear()
    monkeypatch.setattr(s, "faq_locale_fallback_enabled", True)
    res = await ft.faq_lookup_tool(_ctx(locale="en"), _deps(), {"query": "shipping"})
    assert "199 lei" in res.llm_view
    assert [c[1] for c in calls] == ["en", "ro"]


def test_view_keeps_whole_entries_under_the_cap(monkeypatch):
    monkeypatch.setattr(ft, "MAX_VIEW_CHARS", 500)
    rows = [{"id": str(i), "question": f"Q{i}?", "answer": "x" * 60} for i in range(10)]
    view = ft.render_view(rows)
    kept = [ln for ln in view.splitlines() if ln.strip().startswith("Răspuns:")]
    assert 0 < len(kept) < len(rows)  # plafonul a lăsat afară intrări întregi
    for line in view.splitlines():
        if line.strip().startswith("Răspuns:"):
            assert line.strip() == "Răspuns: " + "x" * 60  # niciun răspuns tăiat la mijloc


def test_view_has_no_pause_dash_or_semicolon_in_instructions():
    """P13: promptul se scrie în vocea pe care o cere."""
    view = ft.render_view(_ROWS)
    header = view.splitlines()[0]
    assert " — " not in header and " – " not in header and ";" not in header


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.args = None

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return self._rows


async def test_list_active_filters_tenant_locale_and_active():
    conn = _FakeConn([dict(r) for r in _ROWS])
    out = await faqs_q.list_active(conn, "biz-1", "ro", limit=40)
    assert out == _ROWS
    assert conn.args == ("biz-1", "ro", 40)
    sql = " ".join(conn.sql.split())
    assert "business_id = $1" in sql and "locale = $2" in sql and "is_active = true" in sql
    assert "embedding" not in sql  # nicio dependență de vectori
