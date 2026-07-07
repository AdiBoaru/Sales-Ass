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
    Event,
    InboundMessage,
    RetrievalResult,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.worker.runner import DEFAULT_STAGES, PipelineDeps
from src.worker.stages import agent as agent_mod
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

    async def fake_lexical(conn, business_id, **kwargs):  # NX-113b: lexical rulează MEREU (hibrid)
        return []

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
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
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
    """Plasa de regresie: cazurile seed (G8-1) + securitate (NX-16) sunt încărcate."""
    ids = {c.id for c in CASES}
    assert ids == {
        # G8-1: pipeline de bază (rutare / grounding / anti-halucinație / P6)
        "greeting-simple",
        "sales-grounded",
        "invented-price-blocked",
        "prose-claim-scrubbed",
        "moderation-neutral",
        "risk-handoff",
        "order-never-silent",
        # NX-16: prompt-injection / jailbreak → guard determinist neutralizează
        "injection-fake-discount-blocked",
        "injection-fake-link-blocked",
        "injection-toxic-stays-neutral",
        "injection-legal-threat-handoff",
        "injection-ignore-instructions-stays-grounded",
    }


# --- NX-16: proba anti-teatru (fiecare caz PICĂ dacă guard-ul lui e scos) -----


async def _disable_guard(monkeypatch, which: str) -> None:
    """Dezactivează un guard determinist (pt proba că un caz de injection e load-bearing)."""
    if which == "validator":
        # NX-144: guardul de proză trăiește și în agent_stage (check direct) și în `finalize`
        # (`_finalize*`) → dezactivăm `_valid` în AMBELE, altfel atacul rămâne blocat de finalize.
        from src.agent import finalize as finalize_mod

        monkeypatch.setattr(agent_mod, "_valid", lambda *a, **k: True)  # acceptă orice text
        monkeypatch.setattr(finalize_mod, "_valid", lambda *a, **k: True)
    elif which == "moderation":

        async def _no_block(ctx, deps):
            return False

        monkeypatch.setattr(gates_mod, "_moderation_blocked", _no_block)
    elif which == "risk":
        monkeypatch.setattr(gates_mod, "detect_risk", lambda text: None)


@pytest.mark.parametrize(
    ("case_id", "guard"),
    [
        ("injection-fake-discount-blocked", "validator"),
        ("injection-fake-link-blocked", "validator"),
        ("injection-ignore-instructions-stays-grounded", "validator"),
        ("injection-toxic-stays-neutral", "moderation"),
        ("injection-legal-threat-handoff", "risk"),
    ],
)
async def test_injection_case_fails_without_its_guard(case_id, guard, monkeypatch):
    """Anti-teatru: cu guard-ul scos, atacul scriptat trece în reply → cazul TREBUIE să pice.
    Dacă ar trece și fără guard, cazul nu testează nimic (security theater)."""
    case = next(c for c in CASES if c.id == case_id)
    _apply_stubs(monkeypatch, case.fixtures)
    await _disable_guard(monkeypatch, guard)
    ctx = _build_ctx(case)
    deps = PipelineDeps(conn=object(), redis=None, llm=ScriptedLLM(case.fixtures))

    result = await run_case(ctx, deps, DEFAULT_STAGES, case.expect, case_id=case.id)

    assert not result.passed, f"{case_id}: a trecut cu guard-ul '{guard}' SCOS → security theater"


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


def test_evaluate_reply_checks_expected_tools():
    ctx = _ran_ctx("recomandare", route=Route.SALES)
    ctx.events.append(Event("tool_call", {"tool": "search_products"}))

    ok = evaluate_reply(
        ctx, GoldenExpect(expected_tools=["search_products"]), case_id="tool-ok"
    )
    bad = evaluate_reply(
        ctx, GoldenExpect(expected_tools=["compare_products"]), case_id="tool-bad"
    )

    assert ok.passed is True
    assert bad.passed is False
    assert any("tool lipsă" in f for f in bad.failures)


def test_evaluate_reply_checks_expected_product_ids():
    ctx = _ran_ctx("Crema A", route=Route.SALES)
    ctx.retrieval = RetrievalResult(products=[{"id": "p1", "name": "Crema A"}])

    ok = evaluate_reply(ctx, GoldenExpect(expected_product_ids=["p1"]), case_id="pid-ok")
    bad = evaluate_reply(ctx, GoldenExpect(expected_product_ids=["p2"]), case_id="pid-bad")

    assert ok.passed is True
    assert bad.passed is False
    assert any("product_id lipsă" in f for f in bad.failures)


def test_evaluate_reply_checks_expected_constraints():
    ctx = _ran_ctx("sub 80", route=Route.SALES)
    ctx.state.search_constraints = {"budget_max": 80.0, "category_key": "creme"}

    ok = evaluate_reply(
        ctx,
        GoldenExpect(expected_constraints={"budget_max": 80.0, "category_key": "creme"}),
        case_id="constraints-ok",
    )
    bad = evaluate_reply(
        ctx,
        GoldenExpect(expected_constraints={"budget_max": 50.0}),
        case_id="constraints-bad",
    )

    assert ok.passed is True
    assert bad.passed is False
    assert any("constraint" in f for f in bad.failures)
