"""Contractul de `evidence_id` al creierului unic: modelul trebuie să POATĂ respecta ce i se cere.

Defectul reparat aici a fost găsit pe o conversație REALĂ (`sole-ro`, 2026-09-16): pe toate turele
cu produse, modelul a emis `evidence_ids` inventate (`search-1`, `search:<product_id>`), fiindcă
registrul de evidence se construia abia DUPĂ bucla de tool-calling — deci la momentul în care i se
cerea planul nu văzuse niciun id. Validatorul respingea planul cu `unknown_evidence`, iar reparația
pica pe `missing_product_evidence` fiindcă cita dovezile RELEVANTE (preț, link) în locul rândului
`identity`. Rezultat: clientul primea fallback-ul determinist în loc de recomandarea scrisă.

Nimic din aval nu putea prinde asta: produsele și prețurile sunt reale, deci validatorul de proză
și `grounding_guard` (porți de ADEVĂR) n-aveau ce respinge. Zero OpenAI / zero DB.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import brain as brain_mod
from src.agent.answer_plan import (
    AnswerPlan,
    AnswerPlanContext,
    EvidenceRecord,
    GroundedProduct,
    validate_answer_plan,
)
from src.agent.answer_plan_runtime import build_answer_plan_context
from src.agent.fallbacks import DETERMINISTIC_REPLY_MAX, grounded_fallback_reply
from src.agent.prompt_builder import PromptInputs
from src.agent.tool_executor import ToolRun
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.retrieval.port import RetrievalBundle
from src.worker.runner import PipelineDeps

_PRODUCT = {
    "id": "p1",
    "business_id": "b1",
    "name": "Ser LumaDerm",
    "price": 89.0,
    "availability": "in_stock",
    "product_url": "https://demo.example/p1",
    "variants": [{"id": "v1", "label": "30 ml", "price": 89.0, "stock": 4}],
}


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body="vreau un ser pentru ten uscat"),
        conversation_id="conv1",
    )


class _Port:
    provider_version = "current_live.v1"

    def __init__(self):
        self.last_result = None

    async def retrieve(self, snapshot, spec, active_needs=(), deadline=None):
        return RetrievalBundle(provider_version=self.provider_version, products=(_PRODUCT,))


class _CapturingLLM:
    """Reține CE a văzut modelul ca rezultat al tool-ului — adică fix ce lipsea."""

    model_agent = "model-de-test"

    def __init__(self):
        self.tool_views: list[str] = []
        self.repair_calls = 0

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        self.system = system
        self.tool_views.append(await execute("search_products", {"category": "ser"}))
        return None, 1

    async def complete_schema(self, system, user, schema, **kw):
        self.repair_calls += 1
        return None


async def _drive(ctx, llm, monkeypatch) -> ToolRun:
    monkeypatch.setattr(
        brain_mod,
        "select_provider",
        lambda **kw: SimpleNamespace(
            provider_version="current_live.v1",
            reason="kill_switch_off",
            pipeline_version="retrieval.v1",
            blocking_code="candidate_flag_off",
        ),
    )
    monkeypatch.setattr(brain_mod, "build_port", lambda ctx, deps, sel, **kw: _Port())
    deps = PipelineDeps(conn=None, llm=llm)
    run = ToolRun(ctx, deps)
    await brain_mod.run_main_brain(
        ctx,
        deps,
        run=run,
        inp=PromptInputs.build("Demo", "beauty", "ro", [], []),
        tools=[{"type": "function", "function": {"name": "search_products"}}],
        system="SYSTEM-DE-BAZA",
        user="Mesaj client: vreau un ser pentru ten uscat",
        query="vreau un ser pentru ten uscat",
    )
    return run


# --- defectul 1: id-urile nu existau în fața modelului ---------------------------


async def test_tool_result_carries_the_evidence_ids_the_validator_will_demand(monkeypatch):
    """Rezultatul de tool conține EXACT id-urile pe care le acceptă validatorul.

    Asta e regresia care contează: nu „există un bloc de evidence", ci „id-urile arătate sunt
    aceleași cu cele construite de `build_answer_plan_context`". Două locuri care compun id-uri
    după aceeași regulă pot diverge în tăcere, iar divergența arată exact ca defectul original."""
    ctx, llm = _ctx(), _CapturingLLM()
    run = await _drive(ctx, llm, monkeypatch)

    view = llm.tool_views[0]
    accepted = {
        item.evidence_id
        for item in build_answer_plan_context(
            business_id="b1", locale="ro", products=run.retrieved
        ).evidence
    }
    assert accepted, "fixture-ul nu produce evidence"
    shown = {line.split(" | ")[0] for line in view.splitlines() if line.startswith("product:")}
    assert shown, f"rezultatul de tool nu arată niciun evidence_id:\n{view}"
    assert shown <= accepted, f"id-uri arătate dar neacceptate: {shown - accepted}"
    # identitatea + varianta, adică exact ce cere `missing_product_evidence`
    assert "product:p1:identity" in shown
    assert "product:p1:variant:v1" in shown


async def test_tool_result_keeps_the_product_view(monkeypatch):
    """Blocul se ADAUGĂ, nu înlocuiește: fără numele și prețul produsului modelul n-are ce scrie."""
    ctx, llm = _ctx(), _CapturingLLM()
    await _drive(ctx, llm, monkeypatch)
    assert "LumaDerm" in llm.tool_views[0]


# --- defectul 2: cerința nu era derivabilă ---------------------------------------


def _ctx_with(evidence: tuple[EvidenceRecord, ...]) -> AnswerPlanContext:
    return AnswerPlanContext(
        business_id="b1",
        locale="ro",
        products=(
            GroundedProduct(
                product_id="p1", business_id="b1", resolution="exact", variant_ids=("v1",)
            ),
        ),
        evidence=evidence,
        hard_constraints=(),
        successful_action_ids=(),
        known_need_ids=(),
    )


def _record(evidence_id: str, kind: str, *, variant_id: str | None = None, product: str = "p1"):
    return EvidenceRecord(
        evidence_id=evidence_id,
        business_id="b1",
        product_id=product,
        variant_id=variant_id,
        kind=kind,
        value="x",
        source_version="v1",
        current=True,
    )


def _plan(evidence_ids: tuple[str, ...], *, variant_id: str | None = None) -> AnswerPlan:
    return AnswerPlan.model_validate(
        {
            "schema_version": 1,
            "business_id": "b1",
            "locale": "ro",
            "selected_products": [
                {
                    "product_id": "p1",
                    "variant_id": variant_id,
                    "evidence_ids": list(evidence_ids),
                }
            ],
            "claims": [],
            "facts": {"prices": [], "stocks": [], "urls": []},
            "uncertainties": [],
            "unmet_constraints": [],
            "confirmed_actions": [],
        }
    )


def test_relevant_evidence_proves_the_product_without_the_identity_row():
    """Prețul și linkul produsului sunt dovezi pline, nu jumătăți de dovadă.

    Forma exactă pe care a emis-o modelul în producție (`:url` + `:price`) și pe care validatorul
    o respingea, deși citase corect dovezile care chiar susțin recomandarea."""
    context = _ctx_with((_record("product:p1:url", "url"), _record("product:p1:price", "price")))
    result = validate_answer_plan(_plan(("product:p1:url", "product:p1:price")), context)
    assert "missing_product_evidence" not in result.failures
    assert "unknown_evidence" not in result.failures


def test_variant_is_proved_by_any_row_bound_to_that_variant():
    context = _ctx_with(
        (
            _record("product:p1:identity", "identity"),
            _record("product:p1:price:v1", "price", variant_id="v1"),
        )
    )
    result = validate_answer_plan(
        _plan(("product:p1:identity", "product:p1:price:v1"), variant_id="v1"), context
    )
    assert "missing_product_evidence" not in result.failures


def test_variant_claim_without_any_variant_evidence_still_fails():
    """Relaxarea nu are voie să șteargă poarta: o variantă numită cere o dovadă A EI."""
    context = _ctx_with((_record("product:p1:identity", "identity"),))
    result = validate_answer_plan(_plan(("product:p1:identity",), variant_id="v1"), context)
    assert "missing_product_evidence" in result.failures


def test_invented_evidence_id_still_fails():
    """Exact ce a emis modelul în producție la primul apel."""
    context = _ctx_with((_record("product:p1:identity", "identity"),))
    result = validate_answer_plan(_plan(("search-1",)), context)
    assert "unknown_evidence" in result.failures
    assert "missing_product_evidence" in result.failures


def test_evidence_of_another_product_proves_nothing():
    """Un rând al altui produs nu dovedește produsul ales, indiferent de tipul lui."""
    context = AnswerPlanContext(
        business_id="b1",
        locale="ro",
        products=(
            GroundedProduct(product_id="p1", business_id="b1", resolution="exact", variant_ids=()),
            GroundedProduct(product_id="p2", business_id="b1", resolution="exact", variant_ids=()),
        ),
        evidence=(_record("product:p2:identity", "identity", product="p2"),),
        hard_constraints=(),
        successful_action_ids=(),
        known_need_ids=(),
    )
    result = validate_answer_plan(_plan(("product:p2:identity",)), context)
    assert "unknown_evidence" in result.failures
    assert "missing_product_evidence" in result.failures


# --- defectul 3: textul numea 3, ecranul arăta 6 ---------------------------------


def test_fallback_names_exactly_the_products_it_attaches():
    products = [{"id": f"p{i}", "name": f"Produs {i}", "price": float(10 + i)} for i in range(6)]
    result = grounded_fallback_reply(products)
    assert result is not None
    text, named = result
    assert len(named) == DETERMINISTIC_REPLY_MAX
    for product in named:
        assert product["name"] in text
    # și niciunul dintre cele NEatașate nu apare în text
    for product in products[DETERMINISTIC_REPLY_MAX:]:
        assert product["name"] not in text


def test_fallback_without_usable_facts_stays_none():
    assert grounded_fallback_reply([{"name": "Fără preț"}]) is None
