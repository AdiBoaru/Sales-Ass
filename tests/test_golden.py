"""G8-1 — Gate CI golden: rulează cazurile seed prin pipeline-ul REAL cu un LLM
SCRIPTAT (zero OpenAI) + stub-uri DB (monkeypatch). Pică build-ul la orice regresie
de rutare / grounding / anti-halucinație / niciodată-tăcere (P6).

ZERO apeluri reale: `ScriptedLLM` acoperă exact metodele pe care le cheamă pipeline-ul
(moderate/embed/classify_json/complete/run_tool_loop); query-urile de DB sunt
monkeypatch-uite (ca în `test_agent` / `test_cache_stage` / `test_triage`). Niciun
`@pytest.mark.integration` → rulează în CI ca testele unit.
"""

from pathlib import Path

import pytest

from src.agent.llm import ModerationResult
from src.config import get_settings
from src.evals.golden import GoldenExpect, evaluate_reply, load_cases, run_case
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.worker.runner import DEFAULT_STAGES, PipelineDeps
from src.worker.stages import cache as cache_mod
from src.worker.stages import gates as gates_mod
from src.worker.stages import triage as triage_mod

CASES = load_cases(Path(__file__).parent / "golden" / "cases.json")


# --- LLM scriptat (zero apeluri reale) ---------------------------------------


class ScriptedLLM:
    """LLM determinist scriptat din `fixtures`. Implementează exact metodele chemate
    de pipeline: `moderate` (gates), `embed` (cache + tool-uri), `classify_json`
    (triaj), `run_tool_loop` + `complete` (agent + validator retry)."""

    def __init__(self, fx: dict) -> None:
        self._fx = fx

    async def moderate(self, text, *, model=None):
        m = self._fx.get("moderation", {})
        return ModerationResult(
            flagged=bool(m.get("flagged")), categories=list(m.get("categories", []))
        )

    async def embed(self, texts, *, model=None):
        # vectori determiniști (zerouri) — search/cache-ul real e oricum stubat.
        return [[0.0] * 8 for _ in texts]

    async def classify_json(self, system, user, *, model=None):
        return dict(self._fx.get("triage", {}))

    async def complete(self, system, user, *, model=None):
        # textul de la validator-retry (poate fi tot invalid → fallback determinist).
        return self._fx.get("retry", "")

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        for name, args in self._fx.get("tool_calls", []):
            await execute(name, args)
        return self._fx.get("final", "")


# --- stub-uri DB + settings (hermetic, independent de .env local) -------------


def _apply_stubs(monkeypatch, fixtures: dict) -> None:
    catalog = list(fixtures.get("catalog", []))
    categories = list(fixtures.get("categories", []))
    by_id = {p["id"]: p for p in catalog}

    async def fake_categories(conn, business_id):
        return list(categories)

    async def fake_search(conn, business_id, vec, **kwargs):
        return list(catalog)

    async def has_emb(conn, business_id):  # NX-98: tenant cu embeddings → calea semantică
        return True

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        return [by_id[i] for i in ids if i in by_id]

    async def none_lookup(*args, **kwargs):
        return None

    async def noop_handoff(*args, **kwargs):
        return None

    # triaj → categorii valide ale cazului
    monkeypatch.setattr(triage_mod, "list_category_slugs", fake_categories)
    # tool-uri agent → catalogul cazului
    monkeypatch.setattr(ct, "has_embeddings", has_emb)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    # cache → miss curat (forțăm regenerarea prin pipeline)
    monkeypatch.setattr(cache_mod, "exact_lookup", none_lookup)
    monkeypatch.setattr(cache_mod, "semantic_lookup", none_lookup)
    # risc/handoff → no-op (nu atingem DB)
    monkeypatch.setattr(gates_mod, "set_handoff", noop_handoff)
    # moderation gate e premisa cazului moderation-neutral → pin True (independent de .env)
    monkeypatch.setattr(get_settings(), "moderation_enabled", True)


def _build_ctx(case) -> TurnContext:
    return TurnContext(
        turn_id=f"golden-{case.id}",
        business=BusinessConfig(id="biz-golden", slug="golden", name="Golden"),
        contact=Contact(id="contact-golden", business_id="biz-golden"),
        message=InboundMessage(provider_msg_id=f"m-{case.id}", body=case.input),
        conversation_id=f"conv-{case.id}",
        language=case.language,
    )


# --- gate CI: fiecare caz = un test -------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_golden_case(case, monkeypatch):
    _apply_stubs(monkeypatch, case.fixtures)
    ctx = _build_ctx(case)
    deps = PipelineDeps(conn=object(), redis=None, llm=ScriptedLLM(case.fixtures))

    result = await run_case(ctx, deps, DEFAULT_STAGES, case.expect, case_id=case.id)

    assert result.passed, f"{case.id}: {result.failures}"


def test_all_seed_cases_present():
    """Plasa de regresie: cele 6 cazuri din card sunt încărcate (niciunul scăpat)."""
    ids = {c.id for c in CASES}
    assert ids == {
        "greeting-simple",
        "sales-grounded",
        "invented-price-blocked",
        "moderation-neutral",
        "risk-handoff",
        "order-never-silent",
    }


# --- evaluate_reply (checker pur, fără pipeline) ------------------------------


def _ran_ctx(reply_text: str | None = None, route: Route | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="n"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )
    if reply_text is not None:
        ctx.set_reply(reply_text)
    if route is not None:
        ctx.route = RouteDecision(route=route)
    return ctx


def test_evaluate_reply_passes_on_correct_ctx():
    ctx = _ran_ctx("Crema la 82.99 lei", route=Route.SALES)
    res = evaluate_reply(
        ctx, GoldenExpect(route="sales", must_include=["82.99"], forbidden=["999"]), case_id="ok"
    )
    assert res.passed is True
    assert res.failures == []


def test_evaluate_reply_forbidden_present_fails():
    ctx = _ran_ctx("Crema la 999 lei")
    res = evaluate_reply(ctx, GoldenExpect(forbidden=["999"]), case_id="bad")
    assert res.passed is False
    assert any("interzis" in f for f in res.failures)


def test_evaluate_reply_missing_fact_fails():
    ctx = _ran_ctx("recomandare fără niciun preț")
    res = evaluate_reply(ctx, GoldenExpect(must_include=["82.99"]), case_id="bad")
    assert res.passed is False
    assert any("lipsă" in f for f in res.failures)


def test_evaluate_reply_silence_when_reply_expected_fails():
    ctx = _ran_ctx(None)  # niciun reply → tăcere
    res = evaluate_reply(ctx, GoldenExpect(expect_reply=True), case_id="silent")
    assert res.passed is False
    assert any("tăcere" in f for f in res.failures)


def test_evaluate_reply_expect_silence():
    # halt fără reply (tăcere intenționată) → pass când expect_reply=False
    silent = _ran_ctx(None)
    silent.halt = True
    assert evaluate_reply(silent, GoldenExpect(expect_reply=False), case_id="s").passed is True
    # reply prezent când așteptam tăcere → fail
    spoke = _ran_ctx("ceva")
    assert evaluate_reply(spoke, GoldenExpect(expect_reply=False), case_id="s").passed is False


def test_evaluate_reply_required_route_but_none_fails():
    ctx = _ran_ctx("ceva")  # ctx.route is None (gated înainte de triaj)
    res = evaluate_reply(ctx, GoldenExpect(route="sales"), case_id="gated")
    assert res.passed is False
    assert any("route" in f for f in res.failures)
