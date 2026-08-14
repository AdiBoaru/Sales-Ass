"""NX-238 — `SearchEntitiesAdapter`: candidatul, izolat până la un GO semnat.

Acest modul **nu are decorator `@register`** și nu se auto-instalează nicăieri. Singura cale prin
care poate ajunge pe drumul unui tur e `selector.select_provider`, care îl întoarce doar când există
un verdict GO verificat. Un import al acestui fișier nu schimbă nimic — asta e proprietatea pe care
o testăm, fiindcă un „candidat" care se activează prin simplul fapt că a fost importat n-ar mai fi
un candidat.

Față de `search_entities_shadow` (care doar MĂSOARĂ), adaptorul adaugă cele trei lucruri pe care
cardul le cere ca să fie promovabil:

1. **Masca de rejected, ÎNAINTE și DUPĂ rerank.** Înainte, ca providerul să nu primească niciodată
   un produs contrazis de date; după, fiindcă un provider extern poate întoarce orice, inclusiv un
   id pe care noi l-am exclus. „Am filtrat înainte" nu e o garanție dacă nimeni nu verifică după.
2. **UNKNOWN rămâne UNKNOWN.** Un hard UNKNOWN NU se exclude (nu contrazice nimic) și NU se
   prezintă ca potrivire — devine `alternative` + intră în `missing_information`, de unde
   agentul poate cere o clarificare. Fațeta fără acoperire nu produce MISMATCH.
3. **Deadline + degradări cu cod fix.** Fiecare strat care n-a rulat spune de ce.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from src.agent.match_gate import build_match_set
from src.agent.query_spec import Constraint, RuntimeQuerySpec
from src.db.queries.catalog import (
    get_products_pool_by_ids,
    has_embeddings,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates
from src.db.queries.search_entities import (
    load_evidence_references,
    load_identifier_candidates,
    search_shadow_fts,
)
from src.domain.identifier_resolution import resolve_identifier
from src.domain.rerank_policy import decide_adaptive_rerank
from src.domain.rerank_provider import RerankProvider, apply_adaptive_rerank
from src.retrieval.current_live import _merge_constraints
from src.retrieval.port import (
    DEGRADATION_DEADLINE_EXCEEDED,
    DEGRADATION_NO_FACET_REGISTRY,
    EvidenceBundle,
    RetrievalBundle,
    RetrievalCandidate,
    RetrievalDeadline,
    RetrievalTiming,
    facets_by_key,
)
from src.safety.external_data import external_query_text
from src.tools.reason_codes import annotate as annotate_reasons

log = logging.getLogger(__name__)

PROVIDER_VERSION = "search_entities.v1"

#: Bazinul de candidați cerut retrieverelor înainte de fuziune/rerank (vezi `FTS_POOL` în shadow).
FTS_POOL = 30

DEGRADATION_SEMANTIC_NO_LLM = "semantic_skipped_no_llm"
DEGRADATION_SEMANTIC_PII = "semantic_blocked_pii"
DEGRADATION_SEMANTIC_NO_EMBEDDINGS = "semantic_skipped_no_shadow_embeddings"
DEGRADATION_SEMANTIC_FAILED = "semantic_failed"
DEGRADATION_EVIDENCE_FAILED = "evidence_hydration_failed"
DEGRADATION_RERANK_RESURRECTION = "rerank_rejected_resurrection_blocked"


def _product_id(product: dict[str, Any]) -> str:
    return str(product.get("id") or product.get("product_id") or "")


def _hard_rejected_ids(
    products: Sequence[dict[str, Any]],
    constraints: Sequence[Constraint],
    facets: dict[str, Any],
) -> set[str]:
    """Id-urile cu ≥1 hard MISMATCH. UNKNOWN nu intră aici — nu putem exclude ce nu știm."""
    if not constraints:
        return set()
    match_set = build_match_set(list(products), tuple(constraints), facets)
    return set(match_set.rejected)


async def _semantic_products(
    conn: Any,
    business_id: str,
    query_text: str | None,
    llm: Any | None,
    degradations: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Calea semantică e degradabilă: orice eșec lasă FTS-ul disponibil, dar SE ÎNREGISTREAZĂ.

    Întoarce și numărul REAL de interogări făcute. Contorul trebuie să numere ce s-a executat, nu
    ce s-ar fi putut executa: un buget raportat pe intenție, nu pe fapt, ascunde exact regresia pe
    care ar trebui s-o prindă.
    """
    if llm is None:
        degradations.append(DEGRADATION_SEMANTIC_NO_LLM)
        return [], 0
    if not query_text:
        degradations.append(DEGRADATION_SEMANTIC_PII)
        return [], 0
    queries = 0
    try:
        queries += 1
        if not await has_embeddings(conn, business_id, embedding_doc_type="search_document_v1"):
            degradations.append(DEGRADATION_SEMANTIC_NO_EMBEDDINGS)
            return [], queries
        query_embedding = (await llm.embed([query_text]))[0]
        queries += 1
        return (
            await search_products_semantic(
                conn,
                business_id,
                query_embedding,
                pool=FTS_POOL,
                embedding_doc_type="search_document_v1",
            ),
            queries,
        )
    except Exception:  # noqa: BLE001 — FTS rămâne disponibil la eșec semantic
        log.warning("search_entities: calea semantică a eșuat (fallback FTS)", exc_info=True)
        degradations.append(DEGRADATION_SEMANTIC_FAILED)
        return [], queries


class SearchEntitiesAdapter:
    """Candidatul `search_entities` prin port. Inert fără GO; enforce când e chemat.

    Spre deosebire de adaptorul live, ACESTA execută hard constraints: un produs `rejected` nu
    iese din `retrieve()`. De aceea bundle-ul lui poartă `constraints_enforced=True` — o afirmație
    verificată de invariantul din `RetrievalBundle.__post_init__`.
    """

    provider_version = PROVIDER_VERSION

    def __init__(
        self,
        ctx: Any,
        deps: Any,
        *,
        reranker: RerankProvider | None = None,
        limit: int = 6,
    ) -> None:
        self._ctx = ctx
        self._deps = deps
        self._reranker = reranker
        self._limit = limit

    async def retrieve(
        self,
        snapshot: Any,
        runtime_query_spec: RuntimeQuerySpec,
        active_needs: Sequence[Any] = (),
        deadline: RetrievalDeadline | None = None,
    ) -> RetrievalBundle:
        deadline = deadline or RetrievalDeadline(budget_ms=0)
        business = getattr(self._ctx, "business", None)
        business_id = getattr(business, "id", None) or getattr(self._ctx, "business_id", "")
        locale = getattr(self._ctx, "language", None) or "ro"
        constraints = _merge_constraints(runtime_query_spec, active_needs)
        facets = dict(facets_by_key(business))
        degradations: list[str] = []
        db_queries = 0
        external_calls = 0

        if constraints and not facets:
            degradations.append(DEGRADATION_NO_FACET_REGISTRY)

        query_text = runtime_query_spec.search_text or runtime_query_spec.raw_query

        # --- 1. Identificator exact → fără FTS/semantic/rerank -------------------------------
        async with self._deps.db("search_entities_identifiers") as conn:
            identifiers = await load_identifier_candidates(conn, business_id)
        db_queries += 1
        identifier = resolve_identifier(query_text, identifiers)

        ids: list[str] = []
        semantic: list[dict[str, Any]] = []
        if identifier.status == "resolve":
            ids = [identifier.product_id] if identifier.product_id else []
        elif identifier.status == "clarify":
            ids = list(identifier.candidate_ids)
        elif deadline.expired():
            # Bugetul s-a consumat pe rezolvarea identificatorului: nu mai pornim retrievere noi.
            degradations.append(DEGRADATION_DEADLINE_EXCEEDED)
        else:
            # Embed-ul e EXTERN → nu ține conexiunea (NX-231). Îl rulăm cu poolul liber, apoi
            # deschidem un checkout scurt pentru ambele interogări de bazin.
            safe_text = external_query_text(
                runtime_query_spec.normalized_query, detect_on=runtime_query_spec.raw_query
            )
            async with self._deps.db("search_entities_pool") as conn:
                ids, (semantic, semantic_queries) = await asyncio.gather(
                    search_shadow_fts(conn, business_id, query_text, locale=locale, limit=FTS_POOL),
                    _semantic_products(conn, business_id, safe_text, self._deps.llm, degradations),
                )
            db_queries += 1 + semantic_queries
            if semantic_queries > 1:
                external_calls += 1  # embed-ul a rulat doar dacă am trecut de `has_embeddings`

        # --- 2. Hidratare bazin: UN singur round trip ----------------------------------------
        products: list[dict[str, Any]] = []
        if ids:
            async with self._deps.db("search_entities_hydrate") as conn:
                products = await get_products_pool_by_ids(
                    conn, business_id, ids, pool=FTS_POOL, respect_content_status=True
                )
            db_queries += 1
        if semantic:
            products = list(fuse_candidates(products, semantic, sort_mode="relevance"))

        # --- 3. Masca de rejected ÎNAINTE de rerank ------------------------------------------
        # Providerul de rerank nu are voie să vadă un produs contrazis de date: dacă nu-l vede,
        # nu-l poate întoarce. Prima jumătate a invariantului.
        concerns = [
            c.value
            for c in constraints
            if c.facet in {"concern", "concerns"}
            and c.op in {"eq", "contains"}
            and isinstance(c.value, str)
        ]
        products = list(annotate_reasons(products, concerns=concerns))
        rejected_before = _hard_rejected_ids(products, constraints, facets)
        products = [p for p in products if _product_id(p) not in rejected_before]

        # --- 4. Rerank (opțional, cu deadline) ------------------------------------------------
        rerank_decision = decide_adaptive_rerank(
            identifier_status=identifier.status,
            constraints=constraints,
            lexical_ids=ids,
            semantic_ids=[_product_id(p) for p in semantic],
        )
        if rerank_decision.requested and deadline.expired():
            degradations.append(DEGRADATION_DEADLINE_EXCEEDED)
        elif rerank_decision.requested:
            remaining = deadline.remaining_ms()
            application = await apply_adaptive_rerank(
                products,
                rerank_decision,
                normalized_query=runtime_query_spec.normalized_query,
                raw_query=runtime_query_spec.raw_query,
                provider=self._reranker,
                **({} if remaining is None else {"timeout_s": remaining / 1000}),
            )
            products = list(application.products)
            if application.degradation is not None:
                degradations.append(application.degradation)
            if self._reranker is not None:
                external_calls += 1

        # --- 5. Masca de rejected DUPĂ rerank -------------------------------------------------
        # A doua jumătate, și cea care contează la audit: un provider extern poate întoarce id-uri
        # pe care nu i le-am dat. Re-evaluăm pe setul FINAL, nu pe cel dinainte — dacă rerankul a
        # strecurat înapoi un rejected, aici moare, iar faptul devine o degradare vizibilă.
        rejected_after = _hard_rejected_ids(products, constraints, facets)
        if rejected_after:
            degradations.append(DEGRADATION_RERANK_RESURRECTION)
            products = [p for p in products if _product_id(p) not in rejected_after]

        products = products[: max(self._limit, 1)]
        product_ids = [_product_id(p) for p in products]

        # --- 6. Evidence, batch, tenant-scoped ------------------------------------------------
        evidence_by_product: dict[str, tuple[Any, ...]] = {}
        if product_ids:
            try:
                async with self._deps.db("search_entities_evidence") as conn:
                    evidence_by_product = await load_evidence_references(
                        conn, business_id, product_ids, locale=locale
                    )
                db_queries += 1
            except Exception:  # noqa: BLE001 — fără evidence rămânem cu candidați, nu cu tăcere
                log.warning("search_entities: hidratarea evidence a eșuat", exc_info=True)
                degradations.append(DEGRADATION_EVIDENCE_FAILED)

        # --- 7. Proiecție finală ---------------------------------------------------------------
        match_set = build_match_set(products, tuple(constraints), facets)
        verdict_by_id = {v.product_id: v for v in match_set.verdicts}
        references: list[Any] = []
        candidates: list[RetrievalCandidate] = []
        for rank, product in enumerate(products):
            pid = _product_id(product)
            refs = tuple(evidence_by_product.get(pid, ()))
            references.extend(refs)
            verdict = verdict_by_id.get(pid)
            warning = product.get("warning")
            reason_codes = product.get("reason_codes")
            candidates.append(
                RetrievalCandidate(
                    product_id=pid,
                    rank=rank,
                    match_class=verdict.match_class if verdict else "alternative",
                    constraint_results=verdict.constraint_results if verdict else (),
                    soft_penalty=(verdict.soft_penalty if verdict else 0)
                    + int(isinstance(warning, str)),
                    reason_codes=tuple(c for c in (reason_codes or []) if isinstance(c, str)),
                    evidence_ids=tuple(ref.evidence_id for ref in refs),
                    warning=warning if isinstance(warning, str) else None,
                )
            )

        if deadline.expired() and DEGRADATION_DEADLINE_EXCEEDED not in degradations:
            degradations.append(DEGRADATION_DEADLINE_EXCEEDED)

        missing = tuple(cov.facet for cov in match_set.coverage if cov.unknown > 0)
        return RetrievalBundle(
            provider_version=self.provider_version,
            candidates=tuple(candidates),
            products=tuple(products),
            evidence=EvidenceBundle(tuple(references)),
            coverage=match_set.coverage,
            missing_information=missing,
            degradations=tuple(dict.fromkeys(degradations)),
            timing=RetrievalTiming(
                elapsed_ms=deadline.elapsed_ms(),
                deadline_ms=None if deadline.unbounded else deadline.budget_ms,
                deadline_exceeded=deadline.expired(),
                db_query_count=db_queries,
                external_calls=external_calls,
            ),
            needs_refinement=bool(missing) or not match_set.exact or identifier.status == "clarify",
            identifier_status=identifier.status,
            constraints_enforced=True,
        )
