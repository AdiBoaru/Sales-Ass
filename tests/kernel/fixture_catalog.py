"""NX-329 — catalogul unui pachet de fixture, OFFLINE, în tipurile resolverului.

Resolverul de referințe primește faptele ca argument (`ReferenceFacts`), deci suita lui și stratul
de replay nu au nevoie de DB: faptele se construiesc aici din produsele pachetului, cu ACELEAȘI
reguli ca interogările din `src/db/queries/catalog.py`:

- `available`: `in_stock` ⇒ True, `out_of_stock` ⇒ False, altceva ⇒ necunoscut;
- `named`: numele DISTINCTIV (capul dinaintea lui „ - " și a primei virgule, ≥ 8 caractere) conținut
  în numele cerut, cel mai lung primul (predicatul lui `find_product_named_in_query`);
- vocabularul: fiecare cheie din `attributes`, cu numărul de produse pe valoare (`load_vocabulary`
  fără pragurile de catalog mare, care pe 30 de produse ar tăia tot)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from src.catalog.folding import fold_text
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.references import (
    CatalogLookup,
    ProductFacts,
    ReferenceFacts,
    ShownItem,
    name_key,
)
from src.domain.loader import load_domain_pack
from src.domain.pack import DomainPack
from src.models import BusinessConfig
from tests.kernel import replay

#: Pragul de nume distinctiv din `db/queries/catalog.py` (`MIN_ANCHOR_NAME_LEN`).
MIN_DISTINCTIVE = 8
_AVAILABLE = {"in_stock": True, "out_of_stock": False}


@lru_cache(maxsize=8)
def _doc(name: str) -> dict[str, Any]:
    return replay.load_pack(name)


def products(name: str) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in _doc(name)["products"]}


def pack(name: str) -> DomainPack:
    doc = _doc(name)
    business = BusinessConfig(
        id=f"b-{name}",
        slug=name,
        name=name,
        vertical=doc["vertical"],
        settings={"domain_pack": doc["domain_pack"]},
    )
    loaded = load_domain_pack(business)
    assert loaded is not None
    return loaded


def shown(name: str, *ids: str, prices: dict[str, float] | None = None) -> tuple[ShownItem, ...]:
    """Produsele arătate, ca ref-uri; `prices` suprascrie prețul VĂZUT (un preț schimbat de
    atunci)."""
    catalog = products(name)
    return tuple(
        ShownItem(pid, catalog[pid]["name"], (prices or {}).get(pid, float(catalog[pid]["price"])))
        for pid in ids
    )


def _distinctive(product_name: str) -> str:
    head = product_name.split(" - ", 1)[0]
    return head.split(",", 1)[0].strip()


def _facts(product: dict[str, Any]) -> ProductFacts:
    attributes = dict(product.get("attributes") or {})
    return ProductFacts(
        product_id=product["id"],
        name=product["name"],
        price=float(product["price"]) if product.get("price") is not None else None,
        available=_AVAILABLE.get(product.get("availability", "")),
        rating=product.get("rating"),
        brand=attributes.get("brand"),
        attributes=attributes,
        variant_labels=tuple(product.get("variants") or ()),
    )


def named_in_catalog(name: str, requested: str) -> tuple[tuple[str, int], ...]:
    wanted = fold_text(requested)
    hits = [
        (pid, len(head))
        for pid, p in products(name).items()
        if len(head := _distinctive(p["name"])) >= MIN_DISTINCTIVE and fold_text(head) in wanted
    ]
    return tuple(sorted(hits, key=lambda h: (-h[1], h[0])))


def facts(
    name: str,
    lookup: CatalogLookup | None = None,
    *,
    drop: Iterable[str] = (),
    override: dict[str, dict[str, Any]] | None = None,
) -> ReferenceFacts:
    """Faptele catalogului. Cu `lookup`: DOAR id-urile cerute plus țintele numelor, exact ca
    `fetch_reference_facts`. `drop` scoate produse din catalog (șterse între ture), `override`
    schimbă câmpuri (un preț schimbat, un stoc epuizat)."""
    catalog = {pid: dict(p) for pid, p in products(name).items()}
    for pid, fields in (override or {}).items():
        catalog[pid] = {**catalog[pid], **fields}
    for pid in drop:
        catalog.pop(pid, None)
    named = {}
    if lookup is not None:
        named = {name_key(n): named_in_catalog(name, n) for n in lookup.names}
        wanted = set(lookup.ids) | {pid for hits in named.values() for pid, _ in hits}
        catalog = {pid: p for pid, p in catalog.items() if pid in wanted}
    return ReferenceFacts(
        products={pid: _facts(p) for pid, p in catalog.items()},
        named={k: v for k, v in named.items() if v},
        snapshot=f"fixture:{name}",
    )


def vocabulary(name: str) -> CatalogVocabulary:
    counts: dict[str, Counter[str]] = {}
    for product in products(name).values():
        for key, value in (product.get("attributes") or {}).items():
            if isinstance(value, str):
                counts.setdefault(key, Counter())[value] += 1
    dimensions = {
        key: tuple(VocabEntry(key=v, label=v, count=n) for v, n in sorted(c.items()))
        for key, c in counts.items()
    }
    return CatalogVocabulary(business_id=f"b-{name}", dimensions=dimensions)


__all__ = ["facts", "name_key", "named_in_catalog", "pack", "products", "shown", "vocabulary"]
