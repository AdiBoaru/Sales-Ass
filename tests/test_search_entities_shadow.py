import asyncio

import pytest

from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.domain.facets import FacetSource, FacetType, TypedFacet
from src.domain.identifier_resolution import IdentifierCandidate
from src.domain.search_entities import EvidenceReference
from src.tools import search_entities_shadow as shadow
from src.tools.base import TOOL_REGISTRY

_PRICE = TypedFacet(
    key="price",
    value_type=FacetType.NUMBER,
    source=FacetSource.COLUMN,
    source_key="price",
    operators=("lte",),
)


@pytest.mark.asyncio
async def test_shadow_orchestrator_uses_search_text_and_is_not_a_registered_live_tool(monkeypatch):
    calls = []

    async def fake_fts(conn, business_id, text, **kwargs):
        calls.append(("fts", conn, business_id, text, kwargs))
        return ["p-2", "p-1"]

    async def fake_products(conn, business_id, ids, **kwargs):
        calls.append(("products", conn, business_id, ids, kwargs))
        return [
            {"id": "p-2", "price": 50, "attributes": {}},
            {"id": "p-1", "price": 120, "attributes": {}},
        ]

    async def fake_evidence(conn, business_id, ids, **kwargs):
        calls.append(("evidence", conn, business_id, ids, kwargs))
        return {"p-2": (EvidenceReference("ev-2", "p-2", "benefit"),)}

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_evidence)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)
    spec = RuntimeQuerySpec(
        raw_query="telefon 0712345678 ser sub 80",
        normalized_query="telefon 0712345678 ser sub 80",
        search_text="ser sub 80",
        constraints=(),
    )

    result = await shadow.search_entities_shadow(
        object(), "business-1", spec, {"price": _PRICE}, locale="ro"
    )

    assert calls[0][0] == "fts" and calls[0][2:4] == ("business-1", "ser sub 80")
    assert calls[1][3] == ["p-2", "p-1"]
    assert calls[2][3] == ["p-2", "p-1"]
    assert [candidate.product_id for candidate in result.candidates] == ["p-2", "p-1"]
    assert result.candidates[0].evidence_ids == ("ev-2",)
    assert "search_entities_shadow" not in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_shadow_orchestrator_fuses_explicit_shadow_embeddings_and_falls_back_to_fts(
    monkeypatch,
):
    calls = []

    async def fake_fts(*_args, **_kwargs):
        return ["p-2", "p-1"]

    async def fake_products(*_args, **_kwargs):
        return [
            {"id": "p-2", "price": 50, "attributes": {}},
            {"id": "p-1", "price": 60, "attributes": {}},
        ]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def fake_has_embeddings(_conn, _business_id, **kwargs):
        calls.append(("has_embeddings", kwargs))
        return True

    async def fake_semantic(_conn, _business_id, embedding, **kwargs):
        calls.append(("semantic", embedding, kwargs))
        return [{"id": "p-3", "price": 40, "attributes": {}}]

    def fake_fuse(fts, semantic, **kwargs):
        calls.append(
            ("fuse", [item["id"] for item in fts], [item["id"] for item in semantic], kwargs)
        )
        return [*semantic, *fts]

    class _LLM:
        async def embed(self, texts):
            assert texts == ["ser sub 80"]
            return [[0.1, 0.2]]

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)
    monkeypatch.setattr(shadow, "has_embeddings", fake_has_embeddings)
    monkeypatch.setattr(shadow, "search_products_semantic", fake_semantic)
    monkeypatch.setattr(shadow, "fuse_candidates", fake_fuse)
    spec = RuntimeQuerySpec(
        raw_query="ser sub 80",
        normalized_query="ser sub 80",
        search_text="ser sub 80",
    )

    result = await shadow.search_entities_shadow(
        object(), "business-1", spec, {"price": _PRICE}, llm=_LLM()
    )

    assert calls[0] == ("has_embeddings", {"embedding_doc_type": "search_document_v1"})
    assert calls[1][0] == "semantic"
    assert calls[1][2]["embedding_doc_type"] == "search_document_v1"
    assert calls[2][0:3] == ("fuse", ["p-2", "p-1"], ["p-3"])
    assert [candidate.product_id for candidate in result.candidates] == ["p-3", "p-2", "p-1"]


@pytest.mark.asyncio
async def test_exact_identifier_skips_fts_and_embeddings(monkeypatch):
    async def exact_candidates(*_args):
        return [IdentifierCandidate("p-sku", "Ser X", skus=("SER-X",))]

    async def products(*_args, **_kwargs):
        return [{"id": "p-sku", "price": 50, "attributes": {}}]

    async def refs(*_args, **_kwargs):
        return {}

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("nu trebuie rulat pentru SKU exact")

    monkeypatch.setattr(shadow, "load_identifier_candidates", exact_candidates)
    monkeypatch.setattr(shadow, "get_products_by_ids", products)
    monkeypatch.setattr(shadow, "load_evidence_references", refs)
    monkeypatch.setattr(shadow, "search_shadow_fts", must_not_run)
    monkeypatch.setattr(shadow, "has_embeddings", must_not_run)
    spec = RuntimeQuerySpec(raw_query="SER-X", normalized_query="ser-x", search_text="SER-X")

    result = await shadow.search_entities_shadow(
        object(), "business-1", spec, {"price": _PRICE}, llm=object()
    )

    assert [candidate.product_id for candidate in result.candidates] == ["p-sku"]
    assert result.identifier_status == "resolve"
    assert result.needs_refinement is False
    assert result.rerank_decision and result.rerank_decision.requested is False


@pytest.mark.asyncio
async def test_shadow_excludes_verified_hard_not_recommended_and_penalizes_soft(monkeypatch):
    async def fake_fts(*_args, **_kwargs):
        return ["soft", "safe", "hard"]

    async def fake_products(*_args, **_kwargs):
        return [
            {
                "id": "soft",
                "attributes": {
                    "not_recommended_for": [{"value": "sensitive", "level": "soft"}],
                },
            },
            {"id": "safe", "attributes": {}},
            {
                "id": "hard",
                "attributes": {
                    "not_recommended_for": [
                        {
                            "value": "sensitive",
                            "level": "hard",
                            "source": "manufacturer_label",
                            "verified_at": "2026-07-16",
                        }
                    ],
                },
            },
        ]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)
    spec = RuntimeQuerySpec(
        raw_query="ser pentru ten sensibil",
        normalized_query="ser pentru ten sensibil",
        search_text="ser pentru ten sensibil",
        constraints=(Constraint(facet="concern", op="contains", value="sensitive"),),
    )

    result = await shadow.search_entities_shadow(object(), "business-1", spec, {"price": _PRICE})

    assert [candidate.product_id for candidate in result.candidates] == ["safe", "soft"]
    assert result.candidates[0].warning is None
    assert result.candidates[1].warning == "nu e ideal pentru sensitive"
    assert result.candidates[1].soft_penalty == 1


@pytest.mark.asyncio
async def test_shadow_does_not_exclude_not_recommended_without_matching_concern(monkeypatch):
    async def fake_fts(*_args, **_kwargs):
        return ["hard"]

    async def fake_products(*_args, **_kwargs):
        return [
            {
                "id": "hard",
                "attributes": {
                    "not_recommended_for": [
                        {"value": "sensitive", "level": "hard", "source": "x", "verified_at": "y"}
                    ]
                },
            }
        ]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)

    result = await shadow.search_entities_shadow(
        object(), "business-1", RuntimeQuerySpec("ser", "ser", "ser"), {"price": _PRICE}
    )

    assert [candidate.product_id for candidate in result.candidates] == ["hard"]


@pytest.mark.asyncio
async def test_shadow_starts_fts_and_semantic_retrieval_in_parallel(monkeypatch):
    fts_started = asyncio.Event()
    semantic_started = asyncio.Event()

    async def fake_fts(*_args, **_kwargs):
        fts_started.set()
        await semantic_started.wait()
        return ["p-1"]

    async def fake_has_embeddings(*_args, **_kwargs):
        semantic_started.set()
        await fts_started.wait()
        return True

    async def fake_products(*_args, **_kwargs):
        return [{"id": "p-1", "attributes": {}}]

    async def fake_semantic(*_args, **_kwargs):
        return []

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def no_identifiers(*_args):
        return []

    class _LLM:
        async def embed(self, texts):
            assert texts == ["ser"]
            return [[0.1]]

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "has_embeddings", fake_has_embeddings)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "search_products_semantic", fake_semantic)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)

    result = await asyncio.wait_for(
        shadow.search_entities_shadow(
            object(),
            "business-1",
            RuntimeQuerySpec("ser", "ser", "ser"),
            {"price": _PRICE},
            llm=_LLM(),
        ),
        timeout=0.2,
    )

    assert [candidate.product_id for candidate in result.candidates] == ["p-1"]


@pytest.mark.asyncio
async def test_shadow_skips_external_semantic_export_for_pii_like_query(monkeypatch):
    async def fake_fts(*_args, **_kwargs):
        return ["p-1"]

    async def fake_products(*_args, **_kwargs):
        return [{"id": "p-1", "attributes": {}}]

    async def fake_refs(*_args, **_kwargs):
        return {}

    async def no_identifiers(*_args):
        return []

    async def must_not_export(*_args, **_kwargs):
        raise AssertionError("nu trebuie apelat niciun serviciu semantic extern pentru PII")

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_refs)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)
    monkeypatch.setattr(shadow, "has_embeddings", must_not_export)
    result = await shadow.search_entities_shadow(
        object(),
        "business-1",
        RuntimeQuerySpec(
            raw_query="ser 0712 345 678",
            normalized_query="ser 0712 345 678",
            search_text="ser",
        ),
        {"price": _PRICE},
        llm=object(),
    )

    assert [candidate.product_id for candidate in result.candidates] == ["p-1"]


# --- NX-209: degradarea nu mai e tăcută -------------------------------------


def _spec(raw="ser pentru ten gras", normalized=None, search_text=None):
    return RuntimeQuerySpec(
        raw_query=raw,
        normalized_query=normalized if normalized is not None else raw,
        search_text=search_text if search_text is not None else raw,
        constraints=(),
    )


async def _run(monkeypatch, *, llm=None, spec=None, products=None, fts_ids=("p-1",), **over):
    async def fake_fts(conn, business_id, text, **kwargs):
        over.setdefault("fts_kwargs", kwargs)
        return list(fts_ids)

    async def fake_products(conn, business_id, ids, **kwargs):
        over["products_kwargs"] = kwargs
        return products if products is not None else [{"id": i, "attributes": {}} for i in ids]

    async def fake_evidence(*_a, **_k):
        return {}

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_evidence)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)
    result = await shadow.search_entities_shadow(
        object(), "business-1", spec or _spec(), {}, llm=llm, locale="ro"
    )
    return result, over


@pytest.mark.asyncio
async def test_pii_query_reports_the_degradation_instead_of_failing_silently(monkeypatch):
    """Un strat care cade tăcut e un strat despre care nu afli niciodată că e mort: rezultatul
    arată identic cu «n-a găsit nimic», doar că e mai slab."""

    class _Llm:
        async def embed(self, _texts):
            raise AssertionError("nu trimitem text cu identificator de persoană în afară")

    spec = _spec(raw="ma numesc ion popescu, caut un ser", search_text="ser")
    result, _ = await _run(monkeypatch, llm=_Llm(), spec=spec)

    assert "semantic_blocked_pii" in result.degradations
    assert all("ion" not in code and "popescu" not in code for code in result.degradations)


@pytest.mark.asyncio
async def test_semantic_failure_is_recorded_not_swallowed(monkeypatch):
    class _Llm:
        async def embed(self, _texts):
            raise RuntimeError("provider indisponibil")

    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(shadow, "has_embeddings", yes)
    result, _ = await _run(monkeypatch, llm=_Llm())

    assert "semantic_failed" in result.degradations
    assert result.candidates  # FTS rămâne disponibil — degradare, nu tăcere


@pytest.mark.asyncio
async def test_missing_shadow_embeddings_is_a_named_degradation(monkeypatch):
    class _Llm:
        async def embed(self, _texts):
            return [[0.0]]

    async def no(*_a, **_k):
        return False

    monkeypatch.setattr(shadow, "has_embeddings", no)
    result, _ = await _run(monkeypatch, llm=_Llm())

    assert "semantic_skipped_no_shadow_embeddings" in result.degradations


@pytest.mark.asyncio
async def test_no_llm_is_a_named_degradation(monkeypatch):
    result, _ = await _run(monkeypatch)

    assert result.degradations == ("semantic_skipped_no_llm",)


@pytest.mark.asyncio
async def test_discovery_path_filters_unpublished_products(monkeypatch):
    """E o cale de DISCOVERY (servește produse nevăzute), iar toate celelalte filtrează pe
    `published` (NX-171c). Fără filtru, shadow-ul ar fi comparat în benchmark un univers de
    candidați mai mare decât cel al căii live."""
    _, over = await _run(monkeypatch)

    assert over["products_kwargs"]["respect_content_status"] is True


@pytest.mark.asyncio
async def test_lexical_pool_is_larger_than_the_hydration_limit(monkeypatch):
    """`search_shadow_fts` întoarce doar id-uri, deci un bazin mai mare e aproape gratis — și e
    singura cale prin care decizia de rerank vede o listă reală de candidați."""
    captured = {}

    async def fake_fts(conn, business_id, text, **kwargs):
        captured.update(kwargs)
        return ["p-1"]

    async def fake_products(conn, business_id, ids, **kwargs):
        return [{"id": i, "attributes": {}} for i in ids]

    async def fake_evidence(*_a, **_k):
        return {}

    async def no_identifiers(*_args):
        return []

    monkeypatch.setattr(shadow, "search_shadow_fts", fake_fts)
    monkeypatch.setattr(shadow, "get_products_by_ids", fake_products)
    monkeypatch.setattr(shadow, "load_evidence_references", fake_evidence)
    monkeypatch.setattr(shadow, "load_identifier_candidates", no_identifiers)

    await shadow.search_entities_shadow(object(), "b", _spec(), {}, limit=6)

    assert captured["limit"] == shadow.FTS_POOL > 6
