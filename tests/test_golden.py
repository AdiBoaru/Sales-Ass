"""G8-1 — Gate CI golden: rulează cazurile seed prin pipeline-ul REAL cu un LLM
SCRIPTAT (zero OpenAI) + stub-uri DB (monkeypatch). Pică build-ul la orice regresie
de rutare / grounding / anti-halucinație / niciodată-tăcere (P6).

ZERO apeluri reale: `ScriptedLLM` acoperă exact metodele pe care le cheamă pipeline-ul
(moderate/embed/classify_json/complete/run_tool_loop + `run_tool_loop_structured`/
`complete_schema` sub creierul unic); query-urile de DB sunt monkeypatch-uite (ca în
`test_agent` / `test_cache_stage` / `test_triage`). Niciun `@pytest.mark.integration` →
rulează în CI ca testele unit.

**Calea testată e a HARNESSULUI, nu a `.env`-ului mașinii.** `single_brain_enabled` se
pinuiește explicit, ca `moderation_enabled`: altfel aceleași cazuri măsurau lucruri diferite
pe mașini diferite, iar cine aprindea flagul local vedea 70 de cazuri „picate" care de fapt
nu fuseseră niciodată rulate pe calea aia. Fiecare caz rulează pe AMBELE contracte până la
cutoverul NX-249 — v1 (`run_tool_loop` → text) și v2 (`run_tool_loop_structured` → plan).
"""

from pathlib import Path

import pytest

from src.agent import deterministic as deterministic_mod
from src.agent import planner as planner_mod
from src.config import get_settings
from src.evals.golden import (
    GoldenExpect,
    evaluate_reply,
    load_cases,
    run_case,
    run_conversation,
)
from src.evals.scripted_llm import ScriptedLLM, brain_gap
from src.models import (
    BusinessConfig,
    Comparison,
    ComparisonColumn,
    ComparisonRow,
    Contact,
    Event,
    InboundMessage,
    RetrievalResult,
    RichItem,
    RichReply,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.worker.runner import DEFAULT_STAGES, PipelineDeps
from src.worker.stages import agent as agent_mod
from src.worker.stages import cache as cache_mod
from src.worker.stages import gates as gates_mod

CASES = load_cases(Path(__file__).parent / "golden" / "cases.json")
CONVERSATIONS = load_cases(Path(__file__).parent / "golden" / "conversations.json")


# --- LLM scriptat: definit în `src/evals/scripted_llm.py` --------------------
#
# Era definit AICI, iar `scripts/eval_regression.py` avea a doua copie — care a rămas în urmă
# fix pe calea creierului unic. Definiția s-a mutat în `src/`, unde o pot importa amândouă
# fără ca scriptul să depindă de un modul de test (vezi antetul modulului).


# --- stub-uri DB + settings (hermetic, independent de .env local) -------------


def _apply_stubs(monkeypatch, fixtures: dict, *, single_brain: bool = False) -> None:
    """Stub-uri DB pentru un caz single-tur (fixtures fix)."""
    _apply_stubs_dyn(monkeypatch, lambda: fixtures, single_brain=single_brain)


def _apply_stubs_dyn(monkeypatch, get_fx, *, single_brain: bool = False) -> None:
    """Stub-uri DB care citesc fixture-urile turului CURENT prin `get_fx()` (multi-tur:
    catalogul/categoriile pot diferi de la un tur la altul)."""

    async def fake_categories(conn, business_id):
        return list(get_fx().get("categories", []))

    async def fake_search(conn, business_id, vec, **kwargs):
        return list(get_fx().get("catalog", []))

    async def fake_lexical(conn, business_id, **kwargs):  # NX-113b: lexical rulează MEREU (hibrid)
        return []

    async def has_emb(conn, business_id):  # NX-98: tenant cu embeddings → calea semantică
        return True

    async def fake_by_ids(conn, business_id, ids, **kwargs):
        by_id = {p["id"]: p for p in get_fx().get("catalog", [])}
        return [by_id[i] for i in ids if i in by_id]

    # NX-145 multi-tur: re-hidratarea grounded a produselor AFIȘATE (agent.py:494) cheamă
    # `get_products_by_ids` importat direct în agent.py (binding propriu, ≠ catalog_tools) →
    # stub pe modulul agent ca follow-up-urile neclasificate să răspundă din catalogul turului.
    async def fake_by_ids_direct(conn, business_id, ids, **kwargs):
        by_id = {p["id"]: p for p in get_fx().get("catalog", [])}
        return [by_id[i] for i in ids if i in by_id]

    async def fake_search_cheaper_than(conn, business_id, ref_ids, baseline, **kwargs):
        return list(get_fx().get("cheaper", []))

    async def fake_complementary(conn, business_id, product_id, **kwargs):
        return list(get_fx().get("complementary", []))

    # PRE-loop deterministic intents (`link` / `compare`) import `get_products_by_ids` directly.
    monkeypatch.setattr(deterministic_mod, "get_products_by_ids", fake_by_ids_direct)
    monkeypatch.setattr(planner_mod, "get_products_by_ids", fake_by_ids_direct)
    monkeypatch.setattr(planner_mod, "search_cheaper_than", fake_search_cheaper_than)
    monkeypatch.setattr(planner_mod, "get_complementary_products", fake_complementary)

    async def none_lookup(*args, **kwargs):
        return None

    # tool-uri agent → catalogul cazului
    monkeypatch.setattr(ct, "has_embeddings", has_emb)
    monkeypatch.setattr(ct, "search_products_semantic", fake_search)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "get_products_by_ids", fake_by_ids)
    # cache → miss curat (forțăm regenerarea prin pipeline)
    monkeypatch.setattr(cache_mod, "exact_lookup", none_lookup)
    monkeypatch.setattr(cache_mod, "semantic_lookup", none_lookup)
    # moderation gate e premisa cazului moderation-neutral → pin True (independent de .env)
    monkeypatch.setattr(get_settings(), "moderation_enabled", True)
    # Calea (v1 legacy / creier unic) e a HARNESSULUI, nu a `.env`-ului mașinii — vezi antetul.
    monkeypatch.setattr(get_settings(), "single_brain_enabled", single_brain, raising=False)


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


# --- goluri DECLARATE ale căii `brain` ---------------------------------------
#
# Un gate care își ascunde golurile e mai periculos decât unul care le arată, deci sunt
# `xfail(strict=True)`: dacă cineva le repară, suita o SEMNALEAZĂ (XPASS), nu le lasă să treacă
# tăcut. Exact așa au fost prinse cele trei defecte reparate între timp (fallback sărac,
# clarificare aruncată ca `ungrounded_price`, `ctx.retrieval` nescris pe calea brain) — lista de
# mai jos a scăzut de la 20 la 1, iar scăderea a fost RAPORTATĂ de suită, nu declarată de mine.

# Lista trăiește lângă derivarea planului (`src/evals/scripted_llm.py`): golul e al HARNESSULUI,
# deci aparține harnessului, iar `scripts/eval_regression.py` are nevoie de aceeași listă ca să nu
# raporteze ca regresie un caz despre care se știe că n-are text scriptat pe calea brain.
_brain_gap = brain_gap


@pytest.mark.parametrize("single_brain", [False, True], ids=["v1", "brain"])
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_golden_case(case, single_brain, monkeypatch, request):
    """Acelaşi caz, ambele contracte: v1 (text + validator NX-211) și creierul unic (plan V2 +
    validator V2). Ce e adevărat despre grounding trebuie să fie adevărat pe amândouă — un caz
    care trece doar pe una e exact regresia pe care cutoverul ar livra-o."""
    gap = _brain_gap(case.id) if single_brain else None
    if gap:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=gap))
    _apply_stubs(monkeypatch, case.fixtures, single_brain=single_brain)
    ctx = _build_ctx(case)
    llm = ScriptedLLM(case.fixtures, business_id="biz-golden", locale=case.language)
    deps = PipelineDeps(conn=object(), redis=None, llm=llm)

    result = await run_case(ctx, deps, DEFAULT_STAGES, case.expect, case_id=case.id)

    assert result.passed, f"{case.id}: {result.failures}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_golden_case_with_nx315_shape_flags_on(case, monkeypatch):
    """NX-315: aceleași cazuri, pe v1, cu cele trei flaguri de formă aprinse. Flagurile schimbă
    CE i se cere compunerii rich, deci trebuie să nu atingă nimic din ce verifică golden-ul:
    rutare, grounding, anti-halucinație, niciodată-tăcere. Forma în sine (întrebarea, „cum
    alegi", instrucțiunile) se verifică în `test_narrowing_question`, `test_plan_guidance` și
    `test_explain_shape`: harnessul golden n-are JSON rich scriptat, deci n-ar avea ce măsura."""
    _apply_stubs(monkeypatch, case.fixtures, single_brain=False)
    for flag in (
        "guidance_required_enabled",
        "narrowing_question_enabled",
        "howto_from_catalog_enabled",
    ):
        monkeypatch.setattr(get_settings(), flag, True)
    ctx = _build_ctx(case)
    llm = ScriptedLLM(case.fixtures, business_id="biz-golden", locale=case.language)
    deps = PipelineDeps(conn=object(), redis=None, llm=llm)

    result = await run_case(ctx, deps, DEFAULT_STAGES, case.expect, case_id=case.id)

    assert result.passed, f"{case.id}: {result.failures}"


# --- gate CI: conversații MULTI-TUR (state curge între tururi) ----------------


def _build_conv_ctx(case) -> TurnContext:
    """ctx seed pentru o conversație: primul tur; `advance_turn` mută la tururile 2+."""
    first = case.turns[0].input if case.turns else case.input
    return TurnContext(
        turn_id=f"golden-{case.id}",
        business=BusinessConfig(id="biz-golden", slug="golden", name="Golden"),
        contact=Contact(id="contact-golden", business_id="biz-golden"),
        message=InboundMessage(provider_msg_id=f"m-{case.id}-0", body=first),
        conversation_id=f"conv-{case.id}",
        language=case.language,
    )


@pytest.mark.parametrize("case", CONVERSATIONS, ids=[c.id for c in CONVERSATIONS])
async def test_golden_conversation(case, monkeypatch):
    """Rulează o conversație multi-tur prin pipeline-ul REAL, comutând fixtures-ul LLM +
    stub-urile DB pe turul curent. Fiecare tur trebuie să treacă (rută/tool-uri/produse/
    constrângeri/text) — verifică memoria scurtă care curge prin `ctx.state` + history."""
    holder = {"fx": case.turns[0].fixtures if case.turns else {}}
    _apply_stubs_dyn(monkeypatch, lambda: holder["fx"])
    ctx = _build_conv_ctx(case)
    deps = PipelineDeps(conn=object(), redis=None, llm=ScriptedLLM(lambda: holder["fx"]))

    def on_turn(i, turn):
        holder["fx"] = turn.fixtures

    results = await run_conversation(
        ctx, deps, DEFAULT_STAGES, case.turns, case_id=case.id, on_turn=on_turn
    )

    for res in results:
        assert res.passed, f"{res.case_id}: {res.failures}"


def test_all_conversations_present():
    """Plasa de regresie pentru cazurile MULTI-TUR (NX-145 felia 2): ≥10 conversații care
    acoperă memoria (constraint carry / topic-switch reset), limbi non-RO (EN) și
    adversarial mid-conversație (preț/produs inventat). Fiecare conversație are ≥1 tur."""
    ids = {c.id for c in CONVERSATIONS}
    required = {
        "conv-greeting-then-sales",
        "conv-sales-refine-carries-category",
        "conv-sales-cheaper-link-3turn",
        "conv-sales-then-injection-price-blocked",
        "conv-order-then-thanks",
        "conv-sales-topic-switch-resets-constraints",
        "conv-sales-no-result-no-invention",
        "conv-greeting-sales-refine-3turn",
        "conv-en-sales-refine",
        "conv-sales-then-invented-product-blocked",
    }
    assert required <= ids
    assert len(CONVERSATIONS) >= 10
    assert all(c.turns for c in CONVERSATIONS), "orice conversație are ≥1 tur"


def test_nx172_catalog_v3_scenarios_present():
    """NX-172: cele 12 scenarii golden de validare a catalogului v3 (10 single-tur + 2 conversații)
    acoperă intențiile-cheie: ten gras/sensibil, ingredient, fără parfum, gramaj, utilizare, fond
    mat, nuanță, contraindicație, rutină, comparație, alternativă mai ieftină."""
    single = {c.id for c in CASES if c.id.startswith("nx172-")}
    multi = {c.id for c in CONVERSATIONS if c.id.startswith("nx172-")}
    required_single = {
        "nx172-ten-gras",
        "nx172-ten-sensibil",
        "nx172-ingredient-niacinamida",
        "nx172-fara-parfum",
        "nx172-gramaj",
        "nx172-utilizare",
        "nx172-fond-mat",
        "nx172-nuanta",
        "nx172-contraindicatie",
        "nx172-rutina-completa",
    }
    required_multi = {"nx172-conv-compare-diffs", "nx172-conv-cheaper-alternative"}
    assert required_single <= single
    assert required_multi <= multi
    assert len(single) + len(multi) >= 12


def test_all_seed_cases_present():
    """Plasa de regresie: cazurile seed (G8-1) + securitate (NX-16) sunt încărcate."""
    ids = {c.id for c in CASES}
    required = {
        # G8-1: pipeline de bază (rutare / grounding / anti-halucinație / P6)
        "greeting-simple",
        "sales-grounded",
        "invented-price-blocked",
        "prose-claim-scrubbed",
        "moderation-neutral",
        "risk-human-request-answered",
        "order-never-silent",
        # NX-16: prompt-injection / jailbreak → guard determinist neutralizează
        "injection-fake-discount-blocked",
        "injection-fake-link-blocked",
        "injection-toxic-stays-neutral",
        "injection-legal-threat-answered",
        "injection-ignore-instructions-stays-grounded",
    }
    adversarial = [c for c in CASES if c.id.startswith(("injection-", "adversarial-"))]
    assert required <= ids
    assert len(CASES) >= 50
    assert len(adversarial) >= 8


# --- NX-16: proba anti-teatru (fiecare caz PICĂ dacă guard-ul lui e scos) -----


async def _disable_guard(monkeypatch, which: str) -> None:
    """Dezactivează un guard determinist (pt proba că un caz de injection e load-bearing)."""
    if which == "validator":
        # NX-144: guardul de proză trăiește și în agent_stage (check direct) și în `finalize`
        # (`_finalize*`) → dezactivăm `_valid` în AMBELE, altfel atacul rămâne blocat de finalize.
        from src.agent import finalize as finalize_mod

        monkeypatch.setattr(agent_mod, "_valid", lambda *a, **k: True)  # acceptă orice text
        monkeypatch.setattr(finalize_mod, "_valid", lambda *a, **k: True)
        monkeypatch.setattr(planner_mod, "_valid", lambda *a, **k: True)
    elif which == "moderation":

        async def _no_block(ctx, deps):
            return False

        monkeypatch.setattr(gates_mod, "_moderation_blocked", _no_block)


@pytest.mark.parametrize(
    ("case_id", "guard"),
    [
        ("injection-fake-discount-blocked", "validator"),
        ("injection-fake-link-blocked", "validator"),
        ("injection-ignore-instructions-stays-grounded", "validator"),
        ("injection-toxic-stays-neutral", "moderation"),
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

    ok = evaluate_reply(ctx, GoldenExpect(expected_tools=["search_products"]), case_id="tool-ok")
    bad = evaluate_reply(ctx, GoldenExpect(expected_tools=["compare_products"]), case_id="tool-bad")

    assert ok.passed is True
    assert bad.passed is False
    assert any("tool lipsă" in f for f in bad.failures)


def test_evaluate_reply_fails_on_unexpected_extra_tool():
    # P2: tool-urile chemate trebuie să fie ⊆ expected_tools — un tool extra neasteptat pică
    ctx = _ran_ctx("recomandare", route=Route.SALES)
    ctx.events.append(Event("tool_call", {"tool": "search_products"}))
    ctx.events.append(Event("tool_call", {"tool": "cart_add"}))

    res = evaluate_reply(
        ctx, GoldenExpect(expected_tools=["search_products"]), case_id="tool-extra"
    )

    assert res.passed is False
    assert any("tool neasteptat" in f or "tool neașteptat" in f for f in res.failures)


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


# --- NX-172: checkere noi de validare catalog v3 (load-bearing) ----------------------------------


def test_evaluate_reply_forbidden_categories():
    """Audit off-category (regula 7): un produs de păr într-o căutare de makeup → pică."""
    ok_ctx = _ran_ctx("fond de ten", route=Route.SALES)
    ok_ctx.retrieval = RetrievalResult(
        products=[{"id": "p1", "name": "Fond mat", "category": "fond-de-ten"}]
    )
    bad_ctx = _ran_ctx("fond de ten", route=Route.SALES)
    bad_ctx.retrieval = RetrievalResult(
        products=[
            {"id": "p1", "name": "Fond mat", "category": "fond-de-ten"},
            {"id": "px", "name": "Șampon", "category": "sampoane"},  # off-category
        ]
    )
    ok = evaluate_reply(
        ok_ctx,
        GoldenExpect(forbidden_categories=["sampoane", "ingrijirea-parului"]),
        case_id="offcat-ok",
    )
    bad = evaluate_reply(
        bad_ctx,
        GoldenExpect(forbidden_categories=["sampoane", "ingrijirea-parului"]),
        case_id="offcat-bad",
    )
    assert ok.passed is True
    assert bad.passed is False
    assert any("off-category" in f for f in bad.failures)


def test_evaluate_reply_min_compare_diffs():
    """Comparația trebuie să aibă ≥N rânduri cu valori care DIFERĂ între coloane."""
    cols = [
        ComparisonColumn(product_id="p1", name="A", price=50.0),
        ComparisonColumn(product_id="p2", name="B", price=80.0),
    ]
    rows_diff = [
        ComparisonRow(label="Preț", values=["50", "80"]),  # diferă
        ComparisonRow(label="Rating", values=["4.5", "4.5"]),  # identic
    ]
    ctx = _ran_ctx("compară", route=Route.SALES)
    ctx.reply.comparison = Comparison(columns=cols, rows=rows_diff)
    ok = evaluate_reply(ctx, GoldenExpect(min_compare_diffs=1), case_id="cmp-ok")
    bad = evaluate_reply(ctx, GoldenExpect(min_compare_diffs=2), case_id="cmp-bad")
    assert ok.passed is True
    assert bad.passed is False
    assert any("diferențe reale" in f for f in bad.failures)
    # fără comparație → pică
    no_cmp = evaluate_reply(
        _ran_ctx("x", route=Route.SALES), GoldenExpect(min_compare_diffs=1), case_id="cmp-none"
    )
    assert no_cmp.passed is False


def test_evaluate_reply_require_reason():
    """Fiecare produs recomandat are un motiv: best_for/reason_codes retrieval SAU rich.reason."""
    # via best_for în retrieval
    ctx = _ran_ctx("recomandare", route=Route.SALES)
    ctx.retrieval = RetrievalResult(products=[{"id": "p1", "name": "A", "best_for": "ten uscat"}])
    assert evaluate_reply(ctx, GoldenExpect(require_reason=True), case_id="rsn-ok").passed is True
    # fără motiv → pică
    bad_ctx = _ran_ctx("recomandare", route=Route.SALES)
    bad_ctx.retrieval = RetrievalResult(products=[{"id": "p1", "name": "A"}])
    bad = evaluate_reply(bad_ctx, GoldenExpect(require_reason=True), case_id="rsn-bad")
    assert bad.passed is False
    assert any("fără motiv" in f for f in bad.failures)
    # via rich.reason
    rich_ctx = _ran_ctx("recomandare", route=Route.SALES)
    rich_ctx.reply.rich = RichReply(
        intro=None,
        items=[RichItem(product_id="p1", name="A", price=50.0, reason="perfect pt ten uscat")],
        pick=None,
        education=None,
        chips=[],
        disclaimer="",
    )
    assert (
        evaluate_reply(rich_ctx, GoldenExpect(require_reason=True), case_id="rsn-rich").passed
        is True
    )
