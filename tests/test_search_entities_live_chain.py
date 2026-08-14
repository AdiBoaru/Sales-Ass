"""NX-238 — lanțul candidatului `search_entities`: enforce real, degradări cu cod, izolare.

Diferența față de adaptorul live e exact ce se testează aici: candidatul EXECUTĂ hard constraints.
Un `rejected` nu are voie să iasă — nici pe drumul normal, nici reînviat de un rerank.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.pack import DomainPack
from src.domain.rerank_provider import RerankApplication
from src.domain.search_entities import EvidenceReference
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.retrieval import search_entities as candidate
from src.retrieval.port import RetrievalDeadline
from src.retrieval.search_entities import (
    DEGRADATION_EVIDENCE_FAILED,
    DEGRADATION_RERANK_RESURRECTION,
    DEGRADATION_SEMANTIC_NO_EMBEDDINGS,
    SearchEntitiesAdapter,
)
from src.tools.base import TOOL_REGISTRY

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

CHEAP = {"id": "p-cheap", "name": "Ser ieftin", "price": 50.0, "attributes": {"concerns": ["oily"]}}
PRICEY = {
    "id": "p-pricey",
    "name": "Ser scump",
    "price": 500.0,
    "attributes": {"concerns": ["oily"]},
}
UNKNOWN = {"id": "p-unknown", "name": "Ser fara date", "price": 60.0, "attributes": {}}

BUDGET = Constraint(facet="price", op="lte", value=100, strength="hard")


class _Deps:
    """Deps minimal care ÎNREGISTREAZĂ etichetele de operație (dovada de batching)."""

    def __init__(self, llm=None):
        self.llm = llm
        self.ops: list[str] = []

    def db(self, op: str):
        self.ops.append(op)

        @contextlib.asynccontextmanager
        async def _cm():
            yield object()

        return _cm()


class _Identifier:
    status = "search"
    product_id = None
    candidate_ids = ()


def _ctx(business_id: str = "biz-1") -> TurnContext:
    business = BusinessConfig(id=business_id, slug="s", name="n")
    business.domain_pack = DomainPack(vertical="ecommerce", facets=(_PRICE, _CONCERNS))
    return TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id=business_id),
        message=InboundMessage(provider_msg_id="m", body="ser"),
        conversation_id="conv",
        language="ro",
    )


def _spec(**kwargs) -> RuntimeQuerySpec:
    base = {
        "raw_query": "ser ieftin",
        "normalized_query": "ser ieftin",
        "search_text": "ser ieftin",
    }
    base.update(kwargs)
    return RuntimeQuerySpec(**base)


@pytest.fixture
def chain(monkeypatch):
    """Lanțul complet stubbit; testele suprascriu doar veriga pe care o exersează."""
    state = {
        "products": [CHEAP, PRICEY],
        "fts_ids": ["p-cheap", "p-pricey"],
        "evidence": {},
        "semantic": [],
        "has_embeddings": False,
        "calls": [],
    }

    async def fake_identifiers(conn, business_id):
        state["calls"].append(("identifiers", business_id))
        return []

    async def fake_fts(conn, business_id, text, **kwargs):
        state["calls"].append(("fts", business_id))
        return list(state["fts_ids"])

    async def fake_pool(conn, business_id, ids, **kwargs):
        state["calls"].append(("pool", business_id, tuple(ids)))
        by_id = {p["id"]: p for p in state["products"]}
        return [dict(by_id[i]) for i in ids if i in by_id]

    async def fake_evidence(conn, business_id, ids, **kwargs):
        state["calls"].append(("evidence", business_id))
        return state["evidence"]

    async def fake_has_embeddings(conn, business_id, **kwargs):
        return state["has_embeddings"]

    async def fake_semantic(conn, business_id, embedding, **kwargs):
        return list(state["semantic"])

    monkeypatch.setattr(candidate, "load_identifier_candidates", fake_identifiers)
    monkeypatch.setattr(candidate, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(candidate, "get_products_pool_by_ids", fake_pool)
    monkeypatch.setattr(candidate, "load_evidence_references", fake_evidence)
    monkeypatch.setattr(candidate, "has_embeddings", fake_has_embeddings)
    monkeypatch.setattr(candidate, "search_products_semantic", fake_semantic)
    monkeypatch.setattr(candidate, "resolve_identifier", lambda *_a, **_k: _Identifier())
    return state


# --- Izolare și inerție ---------------------------------------------------------------


def test_candidate_adapter_is_not_a_registered_tool():
    """Un import nu are voie să activeze nimic — altfel n-ar mai fi un candidat."""
    assert "search_entities" not in TOOL_REGISTRY
    assert "search_entities_shadow" not in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_business_id_comes_from_ctx_never_from_the_query_spec(chain):
    """P7: tenantul e SERVER-OWNED. Fiecare query primește business_id-ul din ctx."""
    deps = _Deps()
    await SearchEntitiesAdapter(_ctx("biz-tenant-A"), deps).retrieve(None, _spec())

    tenants = {call[1] for call in chain["calls"]}
    assert tenants == {"biz-tenant-A"}


# --- Enforcement de hard constraints ---------------------------------------------------


@pytest.mark.asyncio
async def test_hard_mismatch_is_excluded_and_bundle_claims_enforcement(chain):
    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None, _spec(constraints=(BUDGET,))
    )

    assert [c.product_id for c in bundle.candidates] == ["p-cheap"]
    assert bundle.rejected_ids == ()  # excluși, nu doar marcați
    assert bundle.constraints_enforced is True


@pytest.mark.asyncio
async def test_reranker_never_sees_a_hard_rejected_product(chain, monkeypatch):
    """Prima jumătate a invariantului: ce nu ajunge la provider nu poate fi întors de el."""
    seen: list[tuple[str, ...]] = []

    async def spy_rerank(products, decision, **kwargs):
        seen.append(tuple(p["id"] for p in products))
        return RerankApplication(products=tuple(products))

    monkeypatch.setattr(candidate, "apply_adaptive_rerank", spy_rerank)
    chain["fts_ids"] = ["p-cheap", "p-pricey", "p-unknown"]
    chain["products"] = [CHEAP, PRICEY, UNKNOWN]

    await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None,
        _spec(constraints=(BUDGET, Constraint(facet="concerns", op="eq", value="oily"))),
    )

    assert seen, "rerankul trebuia cerut (2 constrângeri, candidați suficienți)"
    assert "p-pricey" not in seen[0]


@pytest.mark.asyncio
async def test_rerank_cannot_resurrect_a_rejected_product(chain, monkeypatch):
    """A doua jumătate: chiar dacă un rejected reapare după rerank, moare la masca finală."""

    async def resurrecting_rerank(products, decision, **kwargs):
        return RerankApplication(products=(dict(PRICEY), *products))  # îl pune pe locul 1

    monkeypatch.setattr(candidate, "apply_adaptive_rerank", resurrecting_rerank)

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None,
        _spec(constraints=(BUDGET, Constraint(facet="concerns", op="eq", value="oily"))),
    )

    assert "p-pricey" not in [c.product_id for c in bundle.candidates]
    assert "p-pricey" not in [p["id"] for p in bundle.products]
    assert DEGRADATION_RERANK_RESURRECTION in bundle.degradations
    assert bundle.constraints_enforced is True


@pytest.mark.asyncio
async def test_unknown_is_kept_as_alternative_and_never_claimed_as_match(chain):
    """UNKNOWN ≠ MISMATCH: nu-l excludem (nu contrazice), dar nici nu pretindem potrivire."""
    chain["fts_ids"] = ["p-unknown"]
    chain["products"] = [UNKNOWN]

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None, _spec(constraints=(Constraint(facet="concerns", op="eq", value="oily"),))
    )

    assert bundle.alternative_ids == ("p-unknown",)
    assert bundle.exact_ids == ()
    assert bundle.rejected_ids == ()
    assert "concerns" in bundle.missing_information
    assert bundle.needs_refinement is True


@pytest.mark.asyncio
async def test_facet_without_coverage_does_not_become_a_mismatch(chain):
    """Fațetă necunoscută registrului → UNKNOWN/disclosure, NU excludere."""
    chain["fts_ids"] = ["p-cheap"]
    chain["products"] = [CHEAP]

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None, _spec(constraints=(Constraint(facet="fateta_inexistenta", op="eq", value="x"),))
    )

    assert bundle.alternative_ids == ("p-cheap",)
    assert bundle.rejected_ids == ()


# --- Batching, deadline, degradări -----------------------------------------------------


@pytest.mark.asyncio
async def test_pool_hydration_is_a_single_round_trip(chain):
    """Anti-N+1: un bazin de 30 nu are voie să devină 5 interogări secvențiale."""
    chain["fts_ids"] = [f"p-{i}" for i in range(30)]
    chain["products"] = [
        {"id": f"p-{i}", "name": f"P{i}", "price": 10.0, "attributes": {}} for i in range(30)
    ]
    deps = _Deps()

    bundle = await SearchEntitiesAdapter(_ctx(), deps).retrieve(None, _spec())

    assert deps.ops.count("search_entities_hydrate") == 1
    assert len([c for c in chain["calls"] if c[0] == "pool"]) == 1
    assert bundle.timing.db_query_count == 4  # identifiers + fts/semantic + pool + evidence
    assert bundle.timing.query_bucket == "3-5"


@pytest.mark.asyncio
async def test_expired_deadline_skips_retrievers_and_records_the_code(chain):
    deadline = RetrievalDeadline(budget_ms=5, started_at=time.monotonic() - 1)

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(None, _spec(), deadline=deadline)

    assert "retrieval_deadline_exceeded" in bundle.degradations
    assert bundle.timing.deadline_exceeded is True
    assert [c[0] for c in chain["calls"]] == ["identifiers"]  # niciun retriever nou pornit


@pytest.mark.asyncio
async def test_tenant_without_embeddings_falls_back_to_lexical_with_a_code(chain):
    """Tenant B fără embeddings: fallback lexical, degradare vizibilă, zero query cross-tenant."""

    class _LLM:
        async def embed(self, texts, *, model=None):
            return [[0.0] * 8 for _ in texts]

    chain["has_embeddings"] = False

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps(llm=_LLM())).retrieve(None, _spec())

    assert DEGRADATION_SEMANTIC_NO_EMBEDDINGS in bundle.degradations
    assert [c.product_id for c in bundle.candidates] == ["p-cheap", "p-pricey"]


@pytest.mark.asyncio
async def test_evidence_outage_degrades_without_losing_candidates(chain, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("evidence down")

    monkeypatch.setattr(candidate, "load_evidence_references", boom)

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(None, _spec())

    assert DEGRADATION_EVIDENCE_FAILED in bundle.degradations
    assert [c.product_id for c in bundle.candidates] == ["p-cheap", "p-pricey"]
    assert bundle.evidence.references == ()


@pytest.mark.asyncio
async def test_evidence_ids_are_attached_per_candidate(chain):
    chain["evidence"] = {"p-cheap": (EvidenceReference("ev-1", "p-cheap", "benefit"),)}

    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(None, _spec())

    assert bundle.candidates[0].evidence_ids == ("ev-1",)
    assert bundle.evidence.for_product("p-cheap")[0].evidence_id == "ev-1"


@pytest.mark.asyncio
async def test_safe_projection_of_a_real_bundle_has_no_query_or_product_text(chain):
    bundle = await SearchEntitiesAdapter(_ctx(), _Deps()).retrieve(
        None, _spec(raw_query="ma cheama Ion Popescu, 0722123456")
    )
    safe = str(bundle.to_safe_dict())

    assert "Ion Popescu" not in safe
    assert "0722123456" not in safe
    assert "Ser ieftin" not in safe
