"""NX-203 — adaptor între harness și retrieval-ul REAL (lexical + semantic + RRF).

Leagă `retrieve_fn` al harness-ului de calea de producție (`search_products_lexical` +
`search_products_semantic` + `fuse_candidates`), fără bucla de relaxare și fără arg-extraction —
ca să MĂSOARE retrieval-ul pur, izolat de înțelegerea query-ului (aia e NX-208). Două regimuri:

- `raw`: doar textul brut → hibrid, ZERO filtre. „Ce scoate căutarea pe fraza clientului."
- `with_constraints`: aplică `price_max` + `category` + fațetele structurale (`product_type`,
  `key_ingredients`, …) din hard_constraints. „Retrieval cu
  înțelegere perfectă a cererii" (plafonul). Diferența raw↔constrained arată cât ține de
  retrieval și cât de query understanding.

Async (DB + embed). Runner-ul pre-încarcă rezultatele, apoi hrănește harness-ul sincron.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from src.agent.llm import LLMClient
from src.agent.query_rewrite import build_query_spec
from src.db.queries.catalog import (
    has_embeddings,
    search_products_lexical,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates
from src.domain.normalize import normalize

if TYPE_CHECKING:
    from src.domain.pack import DomainPack

_POOL = 50  # același ca _FUSION_POOL din catalog_tools

# 046 — locala tenantului, memoizată per business_id. Există pentru că `search_products_lexical`
# alege lista de cuvinte goale după locale: dacă harness-ul n-o trimite, măsoară o căutare cu ALT
# comportament decât producția, iar diferența e tăcută. Un instrument de măsură care diverge de
# sistemul măsurat e mai rău decât niciun instrument. Un singur SELECT per tenant, nu per query.
_LOCALE_CACHE: dict[str, str | None] = {}


async def _tenant_locale(conn: asyncpg.Connection, business_id: str) -> str | None:
    if business_id not in _LOCALE_CACHE:
        _LOCALE_CACHE[business_id] = await conn.fetchval(
            "select default_locale from businesses where id = $1", business_id
        )
    return _LOCALE_CACHE[business_id]


def _pid(p: dict[str, Any]) -> str:
    return str(p.get("id") or p.get("product_id"))


#: Fațetele STRUCTURALE care nu sunt nici preț, nici categorie. Trec în `facet_filters`, adică exact
#: pe drumul pe care le aplică și retrievalul real.
#:
#: Fără ele, regimul `with_constraints` era INERT pe orice corpus modern: extragea doar `price_max`
#: și `category`, iar corpusul SOLE își exprimă constrângerile în `product_type` și
#: `key_ingredients`. Măsurat pe felia de tuning, cele două regimuri ieșeau identice la a treia
#: zecimală — adică „plafonul cu înțelegere perfectă a cererii" nu măsura nimic, iar diferența
#: raw↔constrained, care există tocmai ca să separe retrievalul de rezolvarea cererii, era zero prin
#: construcție. Lista nu e închisă la cosmetice: sunt cheile din `attributes`, oricare ar fi ele.
_FACET_OPS = ("eq", "in", "contains")


def _constraints(
    hard: list[dict[str, Any]] | None,
) -> tuple[float | None, str | None, dict[str, list[str]]]:
    """`(price_max, category, facet_filters)` din hard_constraints, pentru `with_constraints`."""
    price_max = category = None
    facets: dict[str, list[str]] = {}
    for hc in hard or []:
        facet, op = hc.get("facet"), hc.get("op", "eq")
        if facet == "price" and op == "lte":
            price_max = float(hc["value"])
        elif facet == "category" and op == "eq":
            category = str(hc["value"])
        elif facet and op in _FACET_OPS:
            value = hc.get("value")
            values = [str(v) for v in value] if isinstance(value, list | tuple) else [str(value)]
            facets.setdefault(str(facet), []).extend(values)
    return price_max, category, facets


async def retrieve_products(
    conn: asyncpg.Connection,
    llm: LLMClient | None,
    business_id: str,
    query: str,
    *,
    hard_constraints: list[dict[str, Any]] | None = None,
    apply_constraints: bool = False,
) -> list[str]:
    """Listă ordonată de product_id (cel mai relevant primul), prin calea reală hibrid + RRF.

    Degradare grațioasă: fără embeddings/LLM → lexical-only (ca în producție, P6)."""
    price_max = category = None
    facet_filters: dict[str, list[str]] = {}
    if apply_constraints:
        price_max, category, facet_filters = _constraints(hard_constraints)

    lexical = await search_products_lexical(
        conn,
        business_id,
        query_text=query,
        price_max=price_max,
        category=category,
        facet_filters=facet_filters or None,
        locale=await _tenant_locale(conn, business_id),
        pool=_POOL,
    )
    vector: list[dict[str, Any]] = []
    if llm is not None and await has_embeddings(conn, business_id):
        try:
            qvec = (await llm.embed([query]))[0]
            vector = await search_products_semantic(
                conn,
                business_id,
                qvec,
                price_max=price_max,
                category=category,
                pool=_POOL,
            )
        except Exception:  # noqa: BLE001 — embed/semantic pică → lexical-only (P6)
            vector = []

    fused = fuse_candidates(lexical, vector, sort_mode="relevance")
    return [_pid(p) for p in fused]


def _excluded_by_reference(product: dict[str, Any], reference_terms: tuple[str, ...]) -> bool:
    """True dacă produsul E chiar referința dintr-un „ca X dar mai ieftin" (i-o excludem: căutăm
    ALTERNATIVE, iar referința e de obicei chiar produsul interzis)."""
    name = normalize(str(product.get("name") or ""))
    return any(ref and (ref in name or name in ref) for ref in reference_terms)


async def retrieve_products_rewritten(
    conn: asyncpg.Connection,
    llm: LLMClient | None,
    business_id: str,
    raw_query: str,
    domain_pack: DomainPack | None,
) -> list[str]:
    """Regimul NX-208 — retrieval pe query-ul RESCRIS determinist (query understanding), fără
    oracol de constrângeri. Izolează exact câștigul de ÎNȚELEGERE peste `raw_hybrid`:

    `build_query_spec` expandează textul (vocabular canonic din DomainPack) și prinde pattern-uri
    de limbă (preț, referință). Căutăm pe `search_text`, apoi excludem referința.
    ZERO filtre hard din adevăr — doar ce se poate deriva din mesajul clientului."""
    spec = build_query_spec(raw_query, domain_pack)

    lexical = await search_products_lexical(
        conn,
        business_id,
        query_text=spec.search_text,
        locale=await _tenant_locale(conn, business_id),
        pool=_POOL,
    )
    vector: list[dict[str, Any]] = []
    if llm is not None and await has_embeddings(conn, business_id):
        try:
            qvec = (await llm.embed([spec.search_text]))[0]
            vector = await search_products_semantic(conn, business_id, qvec, pool=_POOL)
        except Exception:  # noqa: BLE001 — embed/semantic pică → lexical-only (P6)
            vector = []

    fused = fuse_candidates(lexical, vector, sort_mode="relevance")
    kept = [p for p in fused if not _excluded_by_reference(p, spec.reference_terms)]
    return [_pid(p) for p in kept]
