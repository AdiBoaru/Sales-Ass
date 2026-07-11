"""Pure checker for the web response contract.

The frontend renders whatever the backend sends. This module validates the
payload shape and the facts that are easy to hallucinate: product ids, prices,
URLs, stock and delivery claims. It has no DB/Redis/LLM dependency, so it can run
in CI and against recorded pilot payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PRICE_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d{1,2})?)\s*(?:lei|ron|eur|usd)\b", re.I)
_URL_RE = re.compile(r"https?://[^\s)>\"]+", re.I)
_STOCK_RE = re.compile("\\b(?:in stoc|\u00een stoc|pe stoc|available|in stock)\\b", re.I)
# Doar un claim CONCRET de livrare (verb de livrare + un reper de timp) cere surs\u0103 \u2014 nu
# cuvinte generice ("livrare rapid\u0103", "transport gratuit"), care nu sunt claim-uri factuale.
_DELIVERY_ETA_RE = re.compile(
    "\\b(?:livr\\w+|delivery|ships?|transport)\\b[^.\n]{0,40}?"
    "\\b(?:azi|m\u00e2ine|maine|poim\u00e2ine|\\d+\\s*(?:zile|ore|days|hours)|today|tomorrow|"
    "luni|mar\u021bi|marti|miercuri|joi|vineri|s\u00e2mb\u0103t\u0103|sambata|duminic\u0103)\\b",
    re.I,
)


@dataclass(frozen=True)
class WebResponseCheck:
    passed: bool
    failures: list[str] = field(default_factory=list)


def _price(value: Any) -> float | None:
    try:
        return round(float(str(value).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def _near(value: float, allowed: set[float], *, tolerance: float = 0.01) -> bool:
    return any(abs(value - p) <= tolerance for p in allowed)


def _source_map(source_products: Any) -> dict[str, dict[str, Any]]:
    if source_products is None:
        return {}
    if isinstance(source_products, dict):
        return {str(k): dict(v) for k, v in source_products.items() if isinstance(v, dict)}
    out: dict[str, dict[str, Any]] = {}
    for p in source_products or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("product_id") or p.get("id")
        if pid:
            out[str(pid)] = p
    return out


def _validate_product_card(
    card: dict[str, Any],
    *,
    source_by_id: dict[str, dict[str, Any]],
    failures: list[str],
    prefix: str,
) -> tuple[str | None, set[float], set[str]]:
    pid = card.get("product_id")
    name = card.get("name")
    price = _price(card.get("price"))
    urls: set[str] = set()
    prices: set[float] = set()

    if not pid:
        failures.append(f"{prefix}: missing product_id")
        return None, prices, urls
    pid = str(pid)
    if not name:
        failures.append(f"{prefix} {pid}: missing name")
    if price is None:
        failures.append(f"{prefix} {pid}: missing/invalid price")
    else:
        prices.add(price)

    source = source_by_id.get(pid)
    if source_by_id and source is None:
        failures.append(f"{prefix} {pid}: product_id not in source data")
    if source is not None:
        source_price = _price(source.get("price"))
        if price is not None and source_price is not None and abs(price - source_price) > 0.01:
            failures.append(f"{prefix} {pid}: price {price} != source price {source_price}")
        source_url = source.get("url") or source.get("product_url")
        if card.get("url") and source_url and card["url"] != source_url:
            failures.append(f"{prefix} {pid}: url does not match source")

    url = card.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            failures.append(f"{prefix} {pid}: invalid url")
        else:
            urls.add(url)

    list_price = _price(card.get("list_price"))
    if list_price is not None:
        prices.add(list_price)
        if price is not None and list_price <= price:
            failures.append(f"{prefix} {pid}: list_price must be greater than price")
    variants = card.get("variants")
    if variants is not None:
        if not isinstance(variants, list):
            failures.append(f"{prefix} {pid}: variants must be a list")
        else:
            for i, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    failures.append(f"{prefix} {pid}: variants[{i}] is not an object")
                    continue
                if not variant.get("variant_id"):
                    failures.append(f"{prefix} {pid}: variants[{i}] missing variant_id")
                if not variant.get("label"):
                    failures.append(f"{prefix} {pid}: variants[{i}] missing label")
                vprice = _price(variant.get("price"))
                if variant.get("price") is not None and vprice is None:
                    failures.append(f"{prefix} {pid}: variants[{i}] invalid price")
                elif vprice is not None:
                    prices.add(vprice)
                vlist = _price(variant.get("list_price"))
                if vlist is not None:
                    prices.add(vlist)
                    if vprice is not None and vlist <= vprice:
                        failures.append(
                            f"{prefix} {pid}: variants[{i}] list_price must be greater than price"
                        )
                stock = variant.get("stock")
                if stock is not None:
                    try:
                        if int(stock) < 0:
                            failures.append(f"{prefix} {pid}: variants[{i}] stock must be >= 0")
                    except (TypeError, ValueError):
                        failures.append(f"{prefix} {pid}: variants[{i}] invalid stock")
    return pid, prices, urls


def validate_web_payload(
    payload: dict[str, Any],
    *,
    source_products: Any = None,
    allow_stock_claim: bool = False,
    allow_delivery_claim: bool = False,
    allow_empty: bool = False,
) -> WebResponseCheck:
    """Validate a rendered web payload against source facts.

    `source_products` can be a list of product dicts or a dict keyed by product id.
    When provided, every emitted product id and product price must match it.
    `allow_empty` permits an empty `content` (intentional silence / handoff — Gates
    may produce a bot-less reply; that payload is valid, not a hallucination).
    """
    failures: list[str] = []
    if not isinstance(payload, dict):
        return WebResponseCheck(False, ["payload is not an object"])

    source_by_id = _source_map(source_products)
    content = payload.get("content")
    if not isinstance(content, str):
        failures.append("content must be a string")
    elif not content.strip() and not allow_empty:
        failures.append("content is empty")

    products = payload.get("products")
    if not isinstance(products, list):
        failures.append("products must be a list")
        products = []
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list):
        failures.append("suggestions must be a list")

    emitted_ids: set[str] = set()
    allowed_prices: set[float] = set()
    allowed_urls: set[str] = set()
    for i, card in enumerate(products):
        if not isinstance(card, dict):
            failures.append(f"products[{i}] is not an object")
            continue
        pid, prices, urls = _validate_product_card(
            card, source_by_id=source_by_id, failures=failures, prefix=f"products[{i}]"
        )
        if pid:
            emitted_ids.add(pid)
        allowed_prices.update(prices)
        allowed_urls.update(urls)

    comparison = payload.get("comparison")
    if comparison is not None:
        if not isinstance(comparison, dict):
            failures.append("comparison must be an object")
        else:
            columns = comparison.get("columns")
            rows = comparison.get("rows")
            if not isinstance(columns, list) or len(columns) < 2:
                failures.append("comparison.columns must contain at least 2 columns")
                columns = []
            if not isinstance(rows, list):
                failures.append("comparison.rows must be a list")
                rows = []
            for i, col in enumerate(columns):
                if not isinstance(col, dict):
                    failures.append(f"comparison.columns[{i}] is not an object")
                    continue
                pid, prices, urls = _validate_product_card(
                    col,
                    source_by_id=source_by_id,
                    failures=failures,
                    prefix=f"comparison.columns[{i}]",
                )
                if pid:
                    emitted_ids.add(pid)
                allowed_prices.update(prices)
                allowed_urls.update(urls)
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    failures.append(f"comparison.rows[{i}] is not an object")
                    continue
                values = row.get("values")
                if not isinstance(row.get("label"), str) or not row["label"]:
                    failures.append(f"comparison.rows[{i}]: missing label")
                if not isinstance(values, list):
                    failures.append(f"comparison.rows[{i}]: values must be a list")
                elif len(values) != len(columns):
                    failures.append(f"comparison.rows[{i}]: values length != columns length")

    offer = payload.get("offer")
    if offer is not None:
        if not isinstance(offer, dict):
            failures.append("offer must be an object")
        else:
            url = offer.get("url")
            if url:
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    failures.append("offer.url is invalid")
                else:
                    allowed_urls.add(url)

    # Prețuri de referință = cele din carduri + TOATE prețurile din sursă. Când avem sursă (ground
    # truth), un preț din `content` care nu se potrivește e suspect CHIAR dacă nu există carduri —
    # cazul text-only cu preț inventat, exact unde lipsește orice grounding.
    source_prices = {
        p for src in source_by_id.values() if (p := _price(src.get("price"))) is not None
    }
    price_reference = allowed_prices | source_prices
    have_ground_truth = bool(price_reference) or bool(source_by_id)
    for raw in _PRICE_RE.findall(content or ""):
        value = _price(raw)
        if value is not None and have_ground_truth and not _near(value, price_reference):
            failures.append(f"content price {value} is not in payload/source prices")

    for url in _URL_RE.findall(content or ""):
        if url not in allowed_urls:
            failures.append(f"content URL not present in payload: {url}")

    lower_content = (content or "").lower()
    for pid, source in source_by_id.items():
        name = str(source.get("name") or "").strip().lower()
        if name and name in lower_content and pid not in emitted_ids:
            failures.append(f"content mentions product {pid!r} but it is not in products")

    if _STOCK_RE.search(content or "") and not allow_stock_claim:
        if not any(
            source_by_id.get(pid, {}).get("availability") is not None
            or source_by_id.get(pid, {}).get("stock_total") is not None
            for pid in emitted_ids
        ):
            failures.append("stock claim without source availability/stock_total")
    if _DELIVERY_ETA_RE.search(content or "") and not allow_delivery_claim:
        failures.append("delivery ETA claim without explicit source")

    return WebResponseCheck(passed=not failures, failures=failures)
