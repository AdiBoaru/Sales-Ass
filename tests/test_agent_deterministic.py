"""NX-143 — teste de gating pentru intențiile deterministe PRE-loop (`src/agent/deterministic.py`).

Comportamentul handler-elor (link/compare) e acoperit end-to-end de `test_link_intent.py` /
`test_agent.py`; aici testăm predicatele de gating izolat (True/False), pe un `ctx` minimal.
"""

from types import SimpleNamespace

from src.agent import deterministic as det
from src.models import Route


def _ctx(body, *, route=Route.SALES, filters=None, active_search=None, displayed=None):
    return SimpleNamespace(
        route=SimpleNamespace(route=route, filters=filters),
        message=SimpleNamespace(body=body),
        state=SimpleNamespace(active_search=active_search, displayed_products=displayed or []),
    )


def _settings(monkeypatch, **flags):
    base = dict(search_sessions_enabled=True, link_intent_enabled=True, compare_intent_enabled=True)
    base.update(flags)
    monkeypatch.setattr(det, "get_settings", lambda: SimpleNamespace(**base))


# --- is_show_more ----------------------------------------------------------- #


def test_show_more_true_on_active_session(monkeypatch):
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("mai arată-mi", active_search={"fp": "x"})) is True


def test_show_more_false_without_session(monkeypatch):
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("mai arată-mi", active_search=None)) is False


def test_show_more_false_with_new_filters(monkeypatch):
    # constrângere nouă = RAFINARE, nu paginare → cade pe bucla LLM
    _settings(monkeypatch)
    ctx = _ctx("mai multe sub 50", active_search={"fp": "x"}, filters={"budget_max": 50})
    assert det.is_show_more(ctx) is False


def test_show_more_false_on_cheaper(monkeypatch):
    # „mai ieftin" = cheaper_intent (post-loop), nu paginare
    _settings(monkeypatch)
    assert det.is_show_more(_ctx("ceva mai ieftin", active_search={"fp": "x"})) is False


def test_show_more_false_when_disabled(monkeypatch):
    _settings(monkeypatch, search_sessions_enabled=False)
    assert det.is_show_more(_ctx("mai arată-mi", active_search={"fp": "x"})) is False


def test_show_more_false_on_order_route(monkeypatch):
    _settings(monkeypatch)
    ctx = _ctx("mai arată-mi", route=Route.ORDER, active_search={"fp": "x"})
    assert det.is_show_more(ctx) is False


# --- try_pre_intents (guard-uri) -------------------------------------------- #


async def test_pre_intents_false_on_order(monkeypatch):
    _settings(monkeypatch)
    assert await det.try_pre_intents(_ctx("dă-mi linkul", route=Route.ORDER), object()) is False


async def test_pre_intents_false_on_empty_query(monkeypatch):
    _settings(monkeypatch)
    assert await det.try_pre_intents(_ctx("   "), object()) is False


async def test_pre_intents_false_link_with_new_filters(monkeypatch):
    # „link la ceva sub 50" = căutare nouă (filtru) → NU intenție de link → False (lasă bucla)
    _settings(monkeypatch)
    ctx = _ctx(
        "dă-mi linkul la o cremă sub 50", displayed=[SimpleNamespace()], filters={"budget_max": 50}
    )
    assert await det.try_pre_intents(ctx, object()) is False


async def test_pre_intents_link_calls_handler(monkeypatch):
    # gating True → handlerul de link rulează (îl stub-uim să confirme calea)
    _settings(monkeypatch)
    called = {}

    async def fake_handle(ctx, deps):
        called["link"] = True

    monkeypatch.setattr(det, "_handle_link_intent", fake_handle)
    ctx = _ctx("dă-mi linkul direct", displayed=[SimpleNamespace()])
    assert await det.try_pre_intents(ctx, object()) is True
    assert called.get("link") is True
