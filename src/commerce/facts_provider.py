"""NX-237 — hidratarea BATCH a faptelor comerciale: refs → `CommerceFacts`, un query per batch.

Contractul de aici e bugetul: pentru un coș de 10 linii + ținta comenzii se face UN round-trip
(`load_cart_facts_rows`), nu unul per linie (N+1 pe drumul sincron al turului ar fi exact bug-ul
pe care NX-234 l-a scos din contextul de pagină). Conversia rând → fapte e PURĂ (`build_facts`),
testabilă fără DB, cu semantica UNKNOWN moștenită din `context_resolver`:

  • preț/currency lipsă ⇒ `price` UNKNOWN — nu „0 lei";
  • `review_count == 0` ⇒ rating UNKNOWN (default-ul DB e 0; „0 stele" e o afirmație);
  • stoc NULL ⇒ cap necunoscut; `availability` rămâne faptul întreținut de catalog;
  • variantă cu stoc CUNOSCUT 0 ⇒ `out_of_stock` pentru ACEA variantă (faptul mai specific bate
    clasa produsului);
  • promoții/vouchere/ETA: FĂRĂ sursă canonică în mediul curent ⇒ nu apar deloc (structural
    unknown, ca `STRUCTURALLY_UNKNOWN` din NX-234) — nu se derivă din `updated_at` generic.

Prospețimea: `synced_at` (sync-ul de catalog) cu fallback `updated_at`; fără timestamp ⇒ stale
conservator. Pragul (`sla_s`) vine din config (`commerce_facts_sla_s`), nu e hardcodat.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from src.catalog.context_resolver import Freshness, _parse_ts
from src.commerce.cart_models import CommerceFacts
from src.db.queries.carts import load_cart_facts_rows

FactsKey = tuple[str, str | None]


@dataclass(frozen=True)
class FactsBatch:
    """Rezultatul unei hidratări: fapte pe (product_id, variant_id) + rândurile brute (pentru
    poarta de siguranță și validator) + numărul de query-uri (pentru bugetul N+1)."""

    facts: Mapping[FactsKey, CommerceFacts] = field(default_factory=dict)
    products: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    query_count: int = 0

    def get(self, product_id: str, variant_id: str | None) -> CommerceFacts | None:
        return self.facts.get((str(product_id), str(variant_id) if variant_id else None))

    def product(self, product_id: str) -> Mapping[str, Any] | None:
        return self.products.get(str(product_id))

    def variant_known(self, product_id: str, variant_id: str | None) -> bool:
        """NX-118: `variant_id` None → ok (linie fără variantă); altfel trebuie să existe în
        variantele HIDRATATE ale produsului — un id fabricat nu intră în coș."""
        if variant_id is None:
            return True
        row = self.products.get(str(product_id))
        if row is None:
            return False
        return any(str(v.get("id")) == str(variant_id) for v in (row.get("variants") or []))


def _variant_row(row: Mapping[str, Any], variant_id: str) -> Mapping[str, Any] | None:
    for v in row.get("variants") or []:
        if str(v.get("id")) == variant_id:
            return v
    return None


def _facts_for(
    row: Mapping[str, Any], variant_id: str | None, *, now: datetime, sla_s: int
) -> CommerceFacts | None:
    """Rândul canonic (+ eventual varianta) → fapte typed. None DOAR când varianta cerută nu
    aparține produsului — apelantul decide dacă e reject (mutație) sau UNKNOWN (afișare)."""
    unknown: set[str] = set()
    currency = row.get("currency") or None
    if currency is None:
        unknown.add("currency")
    price = row.get("price")
    list_price = row.get("list_price")
    stock = row.get("stock")
    availability = row.get("availability") or None
    label: str | None = None
    if variant_id is not None:
        v = _variant_row(row, variant_id)
        if v is None:
            return None
        # Faptele VARIANTEI bat faptele produsului acolo unde varianta e mai specifică: preț
        # (deja ajustat pe fereastra de sale în SQL), preț tăiat, stoc, etichetă.
        label = v.get("label")
        if v.get("price") is not None:
            price = v.get("price")
            list_price = v.get("list_price")
        if v.get("stock") is not None:
            stock = v.get("stock")
            if int(v["stock"]) <= 0:
                availability = "out_of_stock"
    if price is None:
        unknown.add("price")
    if stock is None:
        unknown.add("stock")
    if availability is None:
        unknown.add("availability")
    review_count = row.get("review_count")
    rating = row.get("rating")
    if not review_count:
        unknown.add("rating")
        rating = None
        review_count = None if review_count is None else 0
    review_summary = (row.get("review_summary") or "").strip() or None
    if review_summary is None:
        unknown.add("review_summary")
    # Fără sursă canonică în mediul curent (docs/CART-DATA-READINESS.md) — mereu absente:
    unknown.update({"delivery_promise", "promotion_eligibility", "voucher"})
    observed = _parse_ts(row.get("synced_at")) or _parse_ts(row.get("updated_at"))
    return CommerceFacts(
        product_id=str(row.get("id")),
        name=str(row.get("name") or ""),
        currency=currency,
        price=float(price) if price is not None else None,
        list_price=float(list_price) if list_price is not None else None,
        availability=availability,
        stock=int(stock) if stock is not None else None,
        variant_id=variant_id,
        variant_label=label,
        rating=float(rating) if rating is not None else None,
        review_count=review_count,
        review_summary=review_summary,
        freshness=Freshness.of(observed, now=now, sla_s=sla_s),
        unknown=frozenset(unknown),
        raw=row,
    )


def build_facts(
    rows: list[Mapping[str, Any]],
    refs: list[FactsKey],
    *,
    now: datetime,
    sla_s: int,
    query_count: int = 0,
) -> FactsBatch:
    """PUR: rânduri hidratate + refs cerute → batch. O pereche (produs, variantă) cu variantă
    care NU aparține produsului nu primește fapte (membership-ul e o verificare, nu o presupunere,
    NX-234)."""
    products = {str(r.get("id")): r for r in rows}
    facts: dict[FactsKey, CommerceFacts] = {}
    for pid, vid in refs:
        key = (str(pid), str(vid) if vid else None)
        if key in facts:
            continue
        row = products.get(key[0])
        if row is None:
            continue
        f = _facts_for(row, key[1], now=now, sla_s=sla_s)
        if f is not None:
            facts[key] = f
    return FactsBatch(facts=facts, products=products, query_count=query_count)


async def load_facts(
    conn: asyncpg.Connection,
    business_id: str,
    refs: list[FactsKey],
    *,
    now: datetime | None = None,
    sla_s: int,
) -> FactsBatch:
    """UN query pentru toate refs (bugetul anti-N+1), apoi conversia pură. `conn` vine de la
    apelant: în mutații e conexiunea TRANZACȚIEI (faptele critice se revalidează în tx), la
    snapshot read-only e checkout-ul scurt al citirii."""
    now = now or datetime.now(UTC)
    ids = list(dict.fromkeys(str(pid) for pid, _ in refs))
    rows = await load_cart_facts_rows(conn, business_id, ids) if ids else []
    return build_facts(rows, refs, now=now, sla_s=sla_s, query_count=1 if ids else 0)


__all__ = ["FactsBatch", "FactsKey", "build_facts", "load_facts"]
