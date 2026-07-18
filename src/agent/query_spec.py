"""NX-185 — QuerySpec: contractul TIPIZAT, canonic al constrângerilor de căutare (shadow mode).

Azi „ce căutăm" are 3 reprezentări concurente: `RouteDecision.filters` (triaj), `state.search_
constraints` (persistat) și argumentele `search_products` alese de tool-loop. QuerySpec devine sursa
UNICĂ (proiecție: SearchArgs). În SHADOW (NX-185) doar se CONSTRUIEȘTE + se emite telemetrie, ZERO
schimbare de comportament; ENFORCEMENT-ul (SearchArgs obligatoriu, hard neslăbibil) e în NX-188.

Ownership: extracție = triaj (`build_query_spec` din `RouteDecision`); merge = ACEST modul PUR
(`merge_query_spec`, nu agent.py). Constrângerea explicită din turul curent CÂȘTIGĂ; topic-switch
(categorie diferită) resetează moștenirea. Fără PII (facete/valori normalizate din triaj).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# hard/soft (Codex): buget/negație/brand/produs/variantă/stoc/safety = hard; „aș prefera"/ranking =
# soft. Toate constrângerile din triaj sunt EXPLICITE → hard. Softul vine din preferințe (viitor).


@dataclass(frozen=True)
class Constraint:
    """O constrângere tipizată. `op` ∈ {lte, gte, eq, contains}. `strength` ∈ {hard, soft}."""

    facet: str
    op: str
    value: Any
    strength: str = "hard"
    source: str = "current_turn"  # current_turn | inherited


@dataclass(frozen=True)
class QuerySpec:
    version: int = 1
    intent: str = "recommend"
    subject_category: str | None = None
    constraints: tuple[Constraint, ...] = ()
    sort: str = "relevance"
    reference_set: tuple[str, ...] = ()

    def hard(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.constraints if c.strength == "hard")

    def fingerprint(self) -> str:
        """Semnătură deterministă (fără PII — facete + op + valori normalizate), pt telemetrie."""
        parts = [self.subject_category or "-"]
        for c in sorted(self.constraints, key=lambda x: (x.facet, x.op, str(x.value))):
            parts.append(f"{c.facet}:{c.op}:{c.value}:{c.strength}")
        return "|".join(parts)


def build_query_spec(route_decision: Any, *, sort: str = "relevance") -> QuerySpec:
    """Extracție (owner: triaj): `RouteDecision.filters` + `category_key` → QuerySpec canonic. Toate
    constrângerile din triaj sunt explicite → hard. Robust la filters lipsă (→ spec gol)."""
    f = getattr(route_decision, "filters", None) or {}
    cons: list[Constraint] = []
    if f.get("budget_max"):
        cons.append(Constraint("price", "lte", float(f["budget_max"]), "hard"))
    for c in f.get("concerns") or []:
        if isinstance(c, str) and c.strip():
            cons.append(Constraint("concerns", "contains", c.strip(), "hard"))
    if f.get("brand"):
        cons.append(Constraint("brand", "eq", str(f["brand"]), "hard"))
    if f.get("suitable_for"):
        cons.append(Constraint("suitable_for", "eq", str(f["suitable_for"]), "hard"))
    return QuerySpec(
        subject_category=getattr(route_decision, "category_key", None),
        constraints=tuple(cons),
        sort=sort,
    )


def merge_query_spec(prev: QuerySpec, current: QuerySpec) -> QuerySpec:
    """Merger PUR (owner: acest modul, NU agent.py). Turul CURENT câștigă. Topic-switch (categorie
    diferită, ambele prezente) → RESET (doar curentul). Altfel constrângerile din `prev` pe fațete
    NE-suprascrise persistă ca `inherited`. Deterministic, fără I/O."""
    if (
        current.subject_category
        and prev.subject_category
        and current.subject_category != prev.subject_category
    ):
        return current  # schimbare de subiect → moștenirea nu se aplică
    cur_facets = {c.facet for c in current.constraints}
    inherited = tuple(
        Constraint(c.facet, c.op, c.value, c.strength, "inherited")
        for c in prev.constraints
        if c.facet not in cur_facets
    )
    return QuerySpec(
        intent=current.intent or prev.intent,
        subject_category=current.subject_category or prev.subject_category,
        constraints=current.constraints + inherited,
        sort=current.sort,
        reference_set=current.reference_set or prev.reference_set,
    )
