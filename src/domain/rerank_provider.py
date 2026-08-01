"""NX-209 adaptive rerank boundary for the shadow search pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.domain.normalize import normalize
from src.domain.rerank_policy import RerankDecision
from src.safety.external_data import contains_pii, external_query_text

_SAFE_ATTRIBUTE_KEYS = (
    "concerns",
    "suitable_for",
    "finish",
    "coverage",
    "texture",
    "key_ingredients",
    "key_benefit",
    "usage",
    "routine_step",
)


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """Safe, provider-facing projection of one catalog product."""

    product_id: str
    document: str


class RerankProvider(Protocol):
    """Async provider contract; implementations stay outside the search domain."""

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> Sequence[str]:
        """Return candidate IDs in descending relevance order."""


@dataclass(frozen=True, slots=True)
class RerankApplication:
    """Applied order plus a fixed degradation code when the provider was not used."""

    products: tuple[dict, ...]
    degradation: str | None = None


def _product_id(product: dict) -> str:
    return str(product.get("id") or product.get("product_id") or "")


def _safe_attribute_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(item for item in value if isinstance(item, str))
    else:
        return ()

    safe_values: list[str] = []
    for item in values:
        normalized = normalize(item).strip()
        if normalized and not contains_pii(item):
            safe_values.append(normalized)
    return tuple(safe_values)


def build_rerank_candidates(products: Sequence[dict]) -> tuple[RerankCandidate, ...]:
    """Project catalog facts without exporting free-form product content.

    Product names, brands, summaries, reviews, URLs and IDs supplied by callers are intentionally
    not serialized into the provider document. The allowlist mirrors stable catalog facets only.
    """

    candidates: list[RerankCandidate] = []
    for product in products:
        product_id = _product_id(product)
        if not product_id:
            continue
        attributes = product.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}

        fields: list[str] = []
        for key in _SAFE_ATTRIBUTE_KEYS:
            values = _safe_attribute_values(attributes.get(key))
            if values:
                fields.append(f"{key}: {' '.join(values)}")
        candidates.append(RerankCandidate(product_id=product_id, document="; ".join(fields)))
    return tuple(candidates)


def _fallback(products: Sequence[dict], degradation: str) -> RerankApplication:
    return RerankApplication(products=tuple(products), degradation=degradation)


async def apply_adaptive_rerank(
    products: Sequence[dict],
    decision: RerankDecision,
    *,
    normalized_query: str,
    raw_query: str | None,
    provider: RerankProvider | None,
    timeout_s: float = 2.0,
) -> RerankApplication:
    """Apply provider order only when requested; every failure preserves retrieval output."""

    original = tuple(products)
    if not decision.requested:
        return RerankApplication(products=original)
    if provider is None:
        return _fallback(original, "rerank_skipped_no_provider")

    query = external_query_text(normalized_query, detect_on=raw_query)
    if query is None:
        return _fallback(original, "rerank_blocked_pii")

    candidates = build_rerank_candidates(original)
    candidate_ids = [candidate.product_id for candidate in candidates]
    if len(candidates) < 2 or len(set(candidate_ids)) != len(candidate_ids):
        return _fallback(original, "rerank_skipped_no_safe_candidates")

    try:
        ordered = await asyncio.wait_for(
            provider.rerank(query, candidates),
            timeout=max(float(timeout_s), 0.001),
        )
    except asyncio.TimeoutError:
        return _fallback(original, "rerank_timeout")
    except Exception:  # noqa: BLE001 - provider outage must leave retrieval available
        return _fallback(original, "rerank_failed")

    if isinstance(ordered, (str, bytes)):
        return _fallback(original, "rerank_invalid_response")
    ordered_ids = list(ordered)
    if (
        not ordered_ids
        or any(not isinstance(product_id, str) for product_id in ordered_ids)
        or len(set(ordered_ids)) != len(ordered_ids)
        or any(product_id not in candidate_ids for product_id in ordered_ids)
    ):
        return _fallback(original, "rerank_invalid_response")

    products_by_id = {_product_id(product): product for product in original}
    ranked_ids = ordered_ids + [
        product_id for product_id in candidate_ids if product_id not in ordered_ids
    ]
    return RerankApplication(
        products=tuple(products_by_id[product_id] for product_id in ranked_ids),
    )
