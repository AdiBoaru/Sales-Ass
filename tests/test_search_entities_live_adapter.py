"""NX-238 — `CurrentLiveRetrievalAdapter`: paritate cu traseul live + adnotare fără enforcement.

Testul central e cel de PARITATE: adaptorul nu are voie să schimbe ce întoarce
`search_products_tool`. Îl verificăm prin identitate de obiect (`is`), nu prin egalitate — dacă
adaptorul ar reconstrui produsele, egalitatea ar putea încă trece, iar diferența ar apărea abia
în producție, pe un câmp pe care nimeni nu-l compară.
"""

from __future__ import annotations

import pytest

from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.pack import DomainPack
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.retrieval import current_live as live
from src.retrieval.current_live import CurrentLiveRetrievalAdapter
from src.retrieval.port import (
    DEGRADATION_CONSTRAINTS_NOT_ENFORCED,
    DEGRADATION_NO_FACET_REGISTRY,
    DEGRADATION_PROVIDER_FAILED,
    RetrievalDeadline,
)
from src.tools.base import ToolResult
from src.worker.runner import PipelineDeps

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte",),
)
_CONCERNS = TypedFacet(
    key="concerns",
    value_type=FacetType.LIST,
    source=FacetSource.ATTRIBUTE,
    source_key="concerns",
    operators=("eq",),
    values=("oily", "dry"),
)

PRODUCTS = [
    {"id": "p-1", "name": "Ser A", "price": 80.0, "attributes": {"concerns": ["oily"]}},
    {"id": "p-2", "name": "Crema B", "price": 150.0, "attributes": {"concerns": ["dry"]}},
]


def _ctx(with_facets: bool = True) -> TurnContext:
    business = BusinessConfig(id="biz-1", slug="s", name="n")
    business.domain_pack = DomainPack(
        vertical="ecommerce", facets=(_PRICE, _CONCERNS) if with_facets else ()
    )
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="ser"),
        conversation_id="conv",
    )


def _spec(**kwargs) -> RuntimeQuerySpec:
    base = {
        "raw_query": "ser pentru ten gras",
        "normalized_query": "ser pentru ten gras",
        "search_text": "ser ten gras",
    }
    base.update(kwargs)
    return RuntimeQuerySpec(**base)


def _patch_tool(monkeypatch, result: ToolResult, spy: list | None = None):
    async def fake_tool(ctx, deps, args):
        if spy is not None:
            spy.append(args)
        return result

    monkeypatch.setattr(live, "search_products_tool", fake_tool)


@pytest.mark.asyncio
async def test_adapter_preserves_live_products_by_identity_and_order(monkeypatch):
    """Paritate: aceleași obiecte, aceeași ordine. Zero semantică nouă."""
    result = ToolResult(ok=True, products=list(PRODUCTS), llm_view="view")
    _patch_tool(monkeypatch, result)

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, _spec())

    assert [p["id"] for p in bundle.products] == ["p-1", "p-2"]
    assert bundle.products[0] is PRODUCTS[0]  # identitate, nu doar egalitate
    assert bundle.products[1] is PRODUCTS[1]
    assert [c.product_id for c in bundle.candidates] == ["p-1", "p-2"]
    assert [c.rank for c in bundle.candidates] == [0, 1]
    assert bundle.provider_version == "current_live.v1"


@pytest.mark.asyncio
async def test_adapter_never_enforces_hard_constraints_on_the_live_path(monkeypatch):
    """NX-188/189 sunt ÎNGHEȚATE: un `rejected` rămâne în rezultat, dar e marcat ca atare."""
    _patch_tool(monkeypatch, ToolResult(ok=True, products=list(PRODUCTS)))
    spec = _spec(constraints=(Constraint(facet="price", op="lte", value=100, strength="hard"),))

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, spec)

    # p-2 (150 lei) contrazice „sub 100" → rejected, dar NU e scos din rezultatul live.
    assert bundle.rejected_ids == ("p-2",)
    assert [p["id"] for p in bundle.products] == ["p-1", "p-2"]
    assert bundle.constraints_enforced is False
    assert DEGRADATION_CONSTRAINTS_NOT_ENFORCED in bundle.degradations


@pytest.mark.asyncio
async def test_unknown_facet_value_becomes_alternative_not_rejected(monkeypatch):
    """UNKNOWN ≠ MISMATCH: fără date pe fațetă, produsul e alternativă, nu respins."""
    products = [{"id": "p-9", "name": "X", "price": 50.0, "attributes": {}}]
    _patch_tool(monkeypatch, ToolResult(ok=True, products=products))
    spec = _spec(
        constraints=(Constraint(facet="concerns", op="eq", value="oily", strength="hard"),)
    )

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, spec)

    assert bundle.alternative_ids == ("p-9",)
    assert bundle.rejected_ids == ()
    assert "concerns" in bundle.missing_information
    assert bundle.needs_refinement is True


@pytest.mark.asyncio
async def test_missing_facet_registry_is_a_recorded_degradation(monkeypatch):
    """Fără registru tipizat, „zero mismatch" nu are voie să arate ca „am verificat tot"."""
    _patch_tool(monkeypatch, ToolResult(ok=True, products=list(PRODUCTS)))
    spec = _spec(constraints=(Constraint(facet="price", op="lte", value=100, strength="hard"),))

    bundle = await CurrentLiveRetrievalAdapter(_ctx(with_facets=False), PipelineDeps()).retrieve(
        None, spec
    )

    assert DEGRADATION_NO_FACET_REGISTRY in bundle.degradations
    assert bundle.rejected_ids == ()  # fără tip nu putem contrazice nimic


@pytest.mark.asyncio
async def test_failed_tool_degrades_without_raising(monkeypatch):
    """Principiul 6: retrievalul eșuat produce un bundle gol cu cod, nu o excepție."""
    _patch_tool(monkeypatch, ToolResult(ok=False, error="Boom", llm_view="Unealta a eșuat."))

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, _spec())

    assert bundle.is_empty
    assert DEGRADATION_PROVIDER_FAILED in bundle.degradations
    assert bundle.needs_refinement is True


@pytest.mark.asyncio
async def test_query_spec_maps_only_to_arguments_the_tool_already_has(monkeypatch):
    """Traducerea nu inventează filtre noi: doar price_max/brand/concerns/category/sort."""
    spy: list = []
    _patch_tool(monkeypatch, ToolResult(ok=True, products=[]), spy=spy)
    spec = _spec(
        category="serum",
        sort="price_asc",
        constraints=(
            Constraint(facet="price", op="lte", value=120, strength="hard"),
            Constraint(facet="brand", op="eq", value="BrandA", strength="hard"),
            Constraint(facet="concern", op="eq", value="oily", strength="soft"),
            Constraint(facet="inventata", op="eq", value="x", strength="hard"),
        ),
    )

    await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, spec)

    args = spy[0]
    assert args["query"] == "ser ten gras"  # search_text, nu raw_query
    assert args["price_max"] == 120.0
    assert args["brand"] == "BrandA"
    assert args["concerns"] == ["oily"]
    assert args["category"] == "serum"
    assert args["sort_mode"] == "price_asc"
    assert "inventata" not in args  # fațeta necunoscută NU devine argument de tool


@pytest.mark.asyncio
async def test_boolean_price_constraint_does_not_become_a_numeric_filter(monkeypatch):
    """`bool` e subtip de `int` — `price <= True` nu are voie să devină `price_max=1.0`."""
    spy: list = []
    _patch_tool(monkeypatch, ToolResult(ok=True, products=[]), spy=spy)
    spec = _spec(constraints=(Constraint(facet="price", op="lte", value=True, strength="hard"),))

    await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(None, spec)

    assert "price_max" not in spy[0]


@pytest.mark.asyncio
async def test_active_needs_from_state_are_merged_into_constraints(monkeypatch):
    """NX-235: nevoile active ale conversației devin constrângeri (`source='state'`)."""

    class _Need:
        key = "budget_max"
        status = "active"
        normalized_value = 90
        operator = "lte"
        strength = "hard"

    _patch_tool(monkeypatch, ToolResult(ok=True, products=list(PRODUCTS)))

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(
        None, _spec(), active_needs=[_Need()]
    )

    # Nevoia „sub 90" vine din state → p-2 (150) e contrazis, deși turul curent n-a spus nimic.
    assert bundle.rejected_ids == ("p-2",)


@pytest.mark.asyncio
async def test_unknown_status_need_produces_no_constraint(monkeypatch):
    """O nevoie fără valoare canonică nu filtrează nimic — UNKNOWN nu se transformă în filtru."""

    class _Need:
        key = "budget_max"
        status = "unknown"
        normalized_value = None
        operator = "lte"
        strength = "hard"

    _patch_tool(monkeypatch, ToolResult(ok=True, products=list(PRODUCTS)))

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(
        None, _spec(), active_needs=[_Need()]
    )

    assert bundle.rejected_ids == ()
    assert bundle.coverage == ()


@pytest.mark.asyncio
async def test_expired_deadline_is_reported_in_timing_and_degradations(monkeypatch):
    import time

    _patch_tool(monkeypatch, ToolResult(ok=True, products=list(PRODUCTS)))
    deadline = RetrievalDeadline(budget_ms=10, started_at=time.monotonic() - 5)

    bundle = await CurrentLiveRetrievalAdapter(_ctx(), PipelineDeps()).retrieve(
        None, _spec(), deadline=deadline
    )

    assert bundle.timing.deadline_exceeded is True
    assert bundle.timing.deadline_ms == 10
    assert "retrieval_deadline_exceeded" in bundle.degradations
