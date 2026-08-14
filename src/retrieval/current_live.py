"""NX-238 — `CurrentLiveRetrievalAdapter`: traseul canonic de azi, văzut prin port.

**Regula acestui fișier: zero semantică nouă.** Adaptorul nu re-implementează căutarea — el APELEAZĂ
`search_products_tool`, exact tool-ul pe care îl cheamă agentul azi, și proiectează `ToolResult`-ul
în `RetrievalBundle`. Paritatea nu e ceva ce testăm și sperăm să țină; e adevărată prin construcție,
fiindcă nu există o a doua implementare care s-ar putea desincroniza. Scara de relaxare, sesiunile
căutare, gate-ul de siguranță, diversificarea, disclosure-urile, `state_patch` și evenimentele rămân
ale tool-ului.

Ce ADAUGĂ adaptorul e strict OBSERVAȚIE: rulează Match Gate-ul (NX-187) peste produsele întoarse ca
să atașeze verdicte tri-state și acoperirea pe fațete.

**De ce adnotare și nu enforcement aici:** a exclude un produs `rejected` de pe traseul live ar fi
exact NX-188/189 — înghețate până la GO-ul NX-210. Adaptorul PĂSTREAZĂ ordinea și mulțimea live,
marchează verdictele și lasă `constraints_enforced=False`. Consumatorul vede onest că are verdicte,
nu garanții. Candidatul (`SearchEntitiesAdapter`) e cel care execută excluderea — și el e OFF.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.agent.match_gate import build_match_set
from src.agent.query_spec import Constraint, RuntimeQuerySpec, constraints_from_needs
from src.retrieval.port import (
    DEGRADATION_CONSTRAINTS_NOT_ENFORCED,
    DEGRADATION_DEADLINE_EXCEEDED,
    DEGRADATION_NO_FACET_REGISTRY,
    DEGRADATION_PROVIDER_FAILED,
    EvidenceBundle,
    RetrievalBundle,
    RetrievalCandidate,
    RetrievalDeadline,
    RetrievalTiming,
    facets_by_key,
)
from src.tools.base import ToolResult
from src.tools.catalog_tools import search_products_tool

PROVIDER_VERSION = "current_live.v1"

#: Câmpurile pe care le acceptă `SearchArgs`. Construim argumentele din QuerySpec, dar NU inventăm
#: chei: un câmp în plus ar fi respins de Pydantic în mijlocul turului, nu la review.
_SORT_MODES = frozenset({"relevance", "price_asc", "price_desc", "rating_desc"})


def _merge_constraints(
    spec: RuntimeQuerySpec, active_needs: Sequence[Any]
) -> tuple[Constraint, ...]:
    """Constrângerile turului + cele derivate din nevoile ACTIVE ale conversației (NX-235).

    Nevoile din state vin cu `source='state'`; cele ale turului cu `source='current_turn'`. Le
    ținem pe amândouă și le dedublăm pe `(facet, op, value)` — o nevoie repetată în turul curent nu
    trebuie să dubleze rândurile din `coverage`, iar `strength` a turului curent are prioritate
    (clientul tocmai a spus-o).
    """
    merged: dict[tuple[str, str, Any], Constraint] = {}
    for constraint in (*constraints_from_needs(active_needs), *spec.constraints):
        merged[(constraint.facet, constraint.op, constraint.value)] = constraint
    return tuple(merged.values())


def _search_args(spec: RuntimeQuerySpec, constraints: Sequence[Constraint]) -> dict[str, Any]:
    """QuerySpec → argumentele `search_products`, fără să schimbe ce înseamnă ele azi.

    `search_text` (expandat de `query_rewrite`) e textul de căutare — aceeași alegere pe care o
    face shadow-ul. Constrângerile canonice se traduc DOAR acolo unde tool-ul are deja un
    parametru cu exact acel sens; restul rămân în `constraints` pentru Match Gate. A inventa aici
    o traducere nouă (ex. o fațetă oarecare → `features`) ar fi fix schimbarea semantică pe care
    adaptorul o interzice.
    """
    args: dict[str, Any] = {
        "query": spec.search_text or spec.raw_query,
        "sort_mode": spec.sort if spec.sort in _SORT_MODES else "relevance",
    }
    if spec.category:
        args["category"] = spec.category

    concerns: list[str] = []
    for c in constraints:
        if c.facet == "price" and c.op == "lte" and isinstance(c.value, (int, float)):
            # `price_max` e singurul filtru numeric al tool-ului; `bool` e subtip de int → exclus.
            if not isinstance(c.value, bool):
                args["price_max"] = float(c.value)
        elif c.facet == "brand" and c.op == "eq" and isinstance(c.value, str):
            args["brand"] = c.value
        elif c.facet in {"concern", "concerns"} and isinstance(c.value, str):
            concerns.append(c.value)
    if concerns:
        args["concerns"] = concerns
    return args


def _candidates_from(
    products: Sequence[dict[str, Any]],
    constraints: Sequence[Constraint],
    facets: dict[str, Any],
) -> tuple[tuple[RetrievalCandidate, ...], tuple[Any, ...], tuple[str, ...]]:
    """Produsele live → candidați cu verdicte, în ACEEAȘI ordine. Nimic nu se filtrează."""
    match_set = build_match_set(list(products), tuple(constraints), facets)
    verdict_by_id = {v.product_id: v for v in match_set.verdicts}
    candidates: list[RetrievalCandidate] = []
    for rank, product in enumerate(products):
        product_id = str(product.get("id") or product.get("product_id") or "")
        verdict = verdict_by_id.get(product_id)
        reason_codes = product.get("reason_codes")
        warning = product.get("warning")
        candidates.append(
            RetrievalCandidate(
                product_id=product_id,
                rank=rank,
                match_class=verdict.match_class if verdict else "alternative",
                constraint_results=verdict.constraint_results if verdict else (),
                soft_penalty=verdict.soft_penalty if verdict else 0,
                reason_codes=tuple(code for code in (reason_codes or []) if isinstance(code, str)),
                warning=warning if isinstance(warning, str) else None,
                score=None,  # traseul live nu expune un scor unic (RRF + resortare deterministă)
            )
        )
    missing = tuple(cov.facet for cov in match_set.coverage if cov.unknown > 0)
    return tuple(candidates), match_set.coverage, missing


class CurrentLiveRetrievalAdapter:
    """Portul peste `search_products_tool`. Fallbackul canonic — și singurul activ fără GO.

    Adaptorul e per-tur: `ctx`/`deps` sunt legate la construcție, ca semnătura `retrieve()` să
    rămână cea din contract (snapshot + spec + nevoi + deadline).
    """

    provider_version = PROVIDER_VERSION

    def __init__(self, ctx: Any, deps: Any) -> None:
        self._ctx = ctx
        self._deps = deps

    async def retrieve(
        self,
        snapshot: Any,
        runtime_query_spec: RuntimeQuerySpec,
        active_needs: Sequence[Any] = (),
        deadline: RetrievalDeadline | None = None,
    ) -> RetrievalBundle:
        deadline = deadline or RetrievalDeadline(budget_ms=0)
        constraints = _merge_constraints(runtime_query_spec, active_needs)
        degradations: list[str] = []

        result: ToolResult = await search_products_tool(
            self._ctx, self._deps, _search_args(runtime_query_spec, constraints)
        )
        if not result.ok:
            degradations.append(DEGRADATION_PROVIDER_FAILED)

        facets = dict(facets_by_key(getattr(self._ctx, "business", None)))
        if constraints and not facets:
            # Fără registru tipizat nu putem produce decât UNKNOWN. E o degradare REALĂ (nu tăcere):
            # altfel „zero mismatch-uri" ar arăta identic cu „am verificat și totul se potrivește".
            degradations.append(DEGRADATION_NO_FACET_REGISTRY)

        products = tuple(result.products or ())
        candidates, coverage, missing = _candidates_from(products, constraints, facets)

        if any(c.rejected for c in candidates):
            # Traseul live NU exclude (NX-188/189 înghețate). Spunem asta explicit, ca un consumator
            # să nu confunde „am verdicte" cu „am aplicat verdictele".
            degradations.append(DEGRADATION_CONSTRAINTS_NOT_ENFORCED)
        if deadline.expired():
            degradations.append(DEGRADATION_DEADLINE_EXCEEDED)

        return RetrievalBundle(
            provider_version=self.provider_version,
            candidates=candidates,
            products=products,
            evidence=EvidenceBundle(),  # evidence hydration e a candidatului, nu a traseului live
            coverage=coverage,
            missing_information=missing,
            degradations=tuple(dict.fromkeys(degradations)),
            timing=RetrievalTiming(
                elapsed_ms=deadline.elapsed_ms(),
                deadline_ms=None if deadline.unbounded else deadline.budget_ms,
                deadline_exceeded=deadline.expired(),
            ),
            needs_refinement=bool(missing) or not products,
            constraints_enforced=False,
        )
