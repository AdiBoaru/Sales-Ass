"""Meniurile închise ale agentului unic (NX-396): ce valori poate pune în filtre.

Filtrele vin DOAR din meniuri generate din catalog (rafturi, tipuri, mărci, nevoi) și din pachetul
de domeniu (familiile de rutină, momentele, pașii): raftul nu se mai poate inventa (NX-313/319),
iar o valoare din afara meniului e refuzată cu un mesaj care îi spune agentului ce să schimbe.
Meniurile intră în schemele uneltelor, deci în prefixul cache-uit al cererii: ordinea e stabilă.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

#: Câte valori dintr-o fațetă intră în meniul de nevoi (cele mai frecvente).
FACET_VALUES = 25
#: Fațetele care nu sunt nevoi ale clientului (raftul și tipul au meniul lor).
_NOT_NEEDS = frozenset({"category", "product_type", "compliance"})
#: Cât trăiește meniul de mărci în proces (vocabularul are cache-ul lui).
_BRANDS_TTL_S = 300.0
_brands_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


@dataclass(frozen=True)
class Menus:
    categories: tuple[str, ...]
    product_types: tuple[str, ...]
    brands: tuple[str, ...]
    needs: tuple[str, ...]  # „fațetă:valoare”
    families: tuple[str, ...] = ()
    moments: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()


def routine_menus(pack: Any) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Familiile, momentele și pașii din `domain_pack.routine_steps`. PUR."""
    spec = getattr(pack, "routine_steps", None)
    families = dict(getattr(spec, "families", {}) or {})
    moments = tuple(sorted(getattr(spec, "time_markers", {}) or {}))
    steps = tuple(dict.fromkeys(s for fam in families.values() for s in fam))
    return tuple(sorted(families)), moments, steps


def need_values(vocab: Any) -> tuple[str, ...]:
    """Meniul de nevoi: cele mai frecvente valori ale fiecărei fațete care descrie clientul. PUR."""
    needs: list[str] = []
    for dim in vocab.facet_names:
        if dim in _NOT_NEEDS:
            continue
        top = sorted(vocab.entries(dim), key=lambda e: (-e.count, e.key))[:FACET_VALUES]
        needs += [f"{dim}:{e.key}" for e in top if e.count > 0]
    return tuple(needs)


async def _brands(deps: Any, business_id: str) -> tuple[str, ...]:
    from src.db.queries.catalog import list_brand_names  # noqa: PLC0415

    now = time.monotonic()
    hit = _brands_cache.get(business_id)
    if hit is not None and now - hit[0] < _BRANDS_TTL_S:
        return hit[1]
    async with deps.db("assistant_brands") as conn:
        brands = tuple(await list_brand_names(conn, business_id))
    _brands_cache[business_id] = (now, brands)
    return brands


async def load_menus(deps: Any, business: Any) -> Menus:
    """Meniurile tenantului. Vocabularul indisponibil ⇒ meniuri goale (căutarea merge pe text)."""
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415

    vocab = await get_vocabulary(deps, business.id, op="assistant_vocabulary")
    families, moments, steps = routine_menus(getattr(business, "domain_pack", None))
    return Menus(
        categories=tuple(e.key for e in vocab.categories if e.count > 0),
        product_types=tuple(e.key for e in vocab.entries("product_type") if e.count > 0),
        brands=await _brands(deps, business.id),
        needs=need_values(vocab),
        families=families,
        moments=moments,
        steps=steps,
    )


def check(value: Any, menu: tuple[str, ...], name: str) -> None:
    """O valoare din afara meniului e refuzată, cu indicația de a alege una listată."""
    from src.assistant.memory import Refused  # noqa: PLC0415

    if value is not None and value not in menu:
        raise Refused(f"{name}={value!r} is not in the menu; use a listed value or leave it out")
