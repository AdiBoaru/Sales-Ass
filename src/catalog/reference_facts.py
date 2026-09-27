"""NX-329 — faptele de catalog pentru resolverul de referințe v2, citite într-un SINGUR checkout.

Resolverul (`src/conversation/references.py`) e PUR: nu citește DB. Tot ce are voie să decidă vine
de aici, recitit în tur, pe `business_id = $1` (P7), deci un id din stare, dintr-un set vechi sau
parcat nu ajunge într-o țintă fără să treacă prin catalog (I1). Modulul NU e în registrul
kernelului: e partea de I/O, la fel ca `vocabulary_cache`.

Un checkout, cel mult `1 + MAX_NAMES` statement-uri: întâi numele de căutat (fiecare `limit 4`),
apoi UN `reference_facts` pe reuniunea id-urilor. Nimic extern în checkout (NX-231): vocabularul se
aduce din cache ÎNAINTE, de către apelant."""

from __future__ import annotations

import uuid
from typing import Any

from src.conversation.references import (
    CatalogLookup,
    ProductFacts,
    ReferenceFacts,
    name_key,
)
from src.db.queries.catalog import find_products_named, reference_facts

#: `availability` → „se poate cumpăra acum?". Aceleași clase ca restul codului (`low_stock` e în
#: stoc; `discontinued` nu). Orice altă valoare e necunoscută, nu falsă (UNKNOWN ≠ 0).
_AVAILABLE: dict[str, bool] = {
    "in_stock": True,
    "low_stock": True,
    "out_of_stock": False,
    "discontinued": False,
}


def _is_uuid(value: str) -> bool:
    """Un id care nu e UUID nu e al catalogului: ar crăpa `$2::uuid[]` pentru tot lotul, iar
    un singur ref stricat din stare ar stinge revalidarea pentru toți ceilalți."""
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def facts_from_row(row: dict[str, Any]) -> ProductFacts:
    attributes = row.get("attributes") or {}
    return ProductFacts(
        product_id=str(row["id"]),
        name=str(row.get("name") or ""),
        price=float(row["price"]) if row.get("price") is not None else None,
        available=_AVAILABLE.get(str(row.get("availability") or "")),
        rating=float(row["rating"]) if row.get("rating") is not None else None,
        brand=row.get("brand"),
        attributes=attributes if isinstance(attributes, dict) else {},
        variant_labels=tuple(str(v) for v in (row.get("variant_labels") or ()) if v),
    )


async def fetch_reference_facts(
    deps: Any, business_id: str, lookup: CatalogLookup
) -> ReferenceFacts:
    """Faptele pe care le cere `lookup`, pentru tenantul `business_id`. Ridică excepția DB-ului:
    apelantul decide degradarea (pe scurtături: calea NX-326, P6)."""
    ids = [i for i in lookup.ids if _is_uuid(i)]
    named: dict[str, tuple[tuple[str, int], ...]] = {}
    async with deps.db("reference_facts") as conn:
        for name in lookup.names:
            hits = await find_products_named(conn, business_id, name)
            if hits:
                named[name_key(name)] = tuple(hits)
        wanted = list(dict.fromkeys([*ids, *(pid for hits in named.values() for pid, _ in hits)]))
        rows = await reference_facts(conn, business_id, wanted, lookup.attributes)
    products = {str(r["id"]): facts_from_row(r) for r in rows}
    return ReferenceFacts(
        products=products,
        named=named,
        snapshot=f"catalog:{len(products)}/{len(wanted)}",
        attributes_read=tuple(lookup.attributes),
    )


__all__ = ["facts_from_row", "fetch_reference_facts"]
