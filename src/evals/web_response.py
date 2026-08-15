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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# NX-228 — checker v2, SEPARAT. Nimic de mai sus nu se schimbă: v1 rămâne neatins până la
# cutoverul NX-249, iar cele două contracte au randori, endpointuri și verificatori distincți.
#
# `src/web/contracts_v2.py` garantează FORMA; ăsta verifică ADEVĂRUL: fiecare preț afișat vine
# dintr-un preț din sursă, fiecare discount afișat chiar rezultă din perechea lui de prețuri,
# fiecare produs afișat trimite la un produs real, fiecare link e din catalog.
#
# `view_index` (view_id → product_id) e obligatoriu tocmai pentru că v2 ASCUNDE `product_id` de
# browser. Maparea trăiește la projector (NX-240), unde există și sursa — deci grounding-ul se
# verifică acolo unde adevărul e încă la îndemână, nu ghicind dintr-un payload deja plecat.
# ═══════════════════════════════════════════════════════════════════════════════════════════

# „1.234,56 lei" (ro) și „1,234.56 lei" (en) cu aceeași expresie: separatorul ZECIMAL e ultimul
# dintre `.`/`,` care apare, restul sunt separatori de mii.
_DISPLAY_NUMBER_RE = re.compile("-?\\d[\\d.,  ]*\\d|-?\\d")
_PERCENT_RE = re.compile("-?\\d+\\s*%")


def _display_price(value: Any) -> float | None:
    """„89,00 lei" → 89.0. None dacă textul nu conține un număr citibil."""
    if not isinstance(value, str):
        return None
    match = _DISPLAY_NUMBER_RE.search(value)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").replace(" ", "")
    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot > last_comma:
        raw = raw.replace(",", "")
    elif last_comma > last_dot:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def _check_price_view(
    price: Any,
    *,
    allowed: set[float],
    have_source: bool,
    failures: list[str],
    prefix: str,
) -> set[float]:
    """Prețurile afișate trebuie să existe în sursă, iar `discount` să rezulte din perechea lui.

    Discountul e cazul care contează cel mai mult: în v1 îl calcula frontendul, deci nimeni nu
    îl verifica. Mutat în backend devine o afirmație — iar o afirmație se verifică.
    """
    seen: set[float] = set()
    if not isinstance(price, dict):
        return seen
    current = _display_price(price.get("current"))
    previous = _display_price(price.get("previous"))
    if price.get("current") is not None and current is None:
        failures.append(f"{prefix}: `price.current` nu conține un număr citibil")
    if current is not None:
        seen.add(current)
        if have_source and not _near(current, allowed):
            failures.append(f"{prefix}: prețul afișat {current} nu există în sursă")
    if previous is not None:
        seen.add(previous)
        if have_source and not _near(previous, allowed):
            failures.append(f"{prefix}: prețul tăiat {previous} nu există în sursă")
        if current is not None and previous <= current:
            failures.append(
                f"{prefix}: `previous` {previous} nu e peste `current` {current} — "
                "un preț tăiat care nu e mai mare e o reducere inventată"
            )
    discount = price.get("discount")
    if discount is not None:
        if current is None or previous is None:
            failures.append(f"{prefix}: `discount` fără pereche current/previous")
        else:
            match = _PERCENT_RE.search(str(discount))
            if not match:
                failures.append(f"{prefix}: `discount` {discount!r} nu e un procent")
            else:
                claimed = abs(int(match.group(0).replace("%", "").strip()))
                actual = (previous - current) / previous * 100
                # NX-240: rotunjirea e în JOS, deliberat (`localization.format_discount`), deci
                # regula nu e „aproximativ egal", ci ASIMETRICĂ — o reducere afișată nu are voie
                # să fie mai mare decât cea reală. Sub adevăr e o alegere; peste e o minciună.
                if claimed > actual:
                    failures.append(
                        f"{prefix}: discount afișat {claimed}% peste cel real {actual:.1f}% "
                        f"({previous} -> {current}) — o ofertă nu se rotunjește în sus"
                    )
                elif actual - claimed >= 2:
                    failures.append(
                        f"{prefix}: discount afișat {claimed}% mult sub cel real {actual:.1f}% "
                        f"({previous} -> {current}) — probabil o pereche greșită de prețuri"
                    )
    return seen


def _block_actions(block: dict[str, Any]) -> list[dict[str, Any]]:
    actions = list(block.get("actions") or [])
    for key in ("items", "lines"):
        for entry in block.get(key) or []:
            if isinstance(entry, dict):
                actions.extend(entry.get("actions") or [])
    return [a for a in actions if isinstance(a, dict)]


def validate_web_view_v2(
    view: dict[str, Any],
    *,
    source_products: Any = None,
    view_index: dict[str, str] | None = None,
) -> WebResponseCheck:
    """Grounding pentru un envelope `web-view.v2` DEJA validat structural.

    Nu re-validează forma — asta o face `parse_view`. Verifică faptele: prețuri, discounturi,
    identitatea produselor, linkurile și invariantul P6. Fără `source_products` verifică doar ce
    e intern-consistent; nu inventează un PASS din lipsă de date.
    """
    failures: list[str] = []
    if not isinstance(view, dict):
        return WebResponseCheck(False, ["view is not an object"])

    source_by_id = _source_map(source_products)
    index = view_index or {}
    have_source = bool(source_by_id)
    source_prices: set[float] = set()
    for src in source_by_id.values():
        for key in ("price", "list_price", "sale_price"):
            if (p := _price(src.get(key))) is not None:
                source_prices.add(p)
    source_urls = {
        str(u) for src in source_by_id.values() if (u := src.get("url") or src.get("product_url"))
    }

    shown_prices: set[float] = set()
    for mi, message in enumerate(view.get("messages") or []):
        if not isinstance(message, dict):
            failures.append(f"messages[{mi}] is not an object")
            continue
        for bi, block in enumerate(message.get("blocks") or []):
            if not isinstance(block, dict):
                failures.append(f"messages[{mi}].blocks[{bi}] is not an object")
                continue
            prefix = f"messages[{mi}].blocks[{bi}]"

            for ii, item in enumerate(block.get("items") or []):
                if not isinstance(item, dict) or "view_id" not in item:
                    continue
                view_id = str(item["view_id"])
                item_prefix = f"{prefix}.items[{ii}]"
                if have_source:
                    pid = index.get(view_id)
                    if pid is None:
                        failures.append(
                            f"{item_prefix}: view_id {view_id!r} nu e in view_index "
                            "(produs afisat fara urma spre catalog)"
                        )
                    elif pid not in source_by_id:
                        failures.append(
                            f"{item_prefix}: view_id {view_id!r} trimite la produsul {pid!r}, "
                            "absent din sursa"
                        )
                shown_prices |= _check_price_view(
                    item.get("price"),
                    allowed=source_prices,
                    have_source=have_source,
                    failures=failures,
                    prefix=item_prefix,
                )

            for li, line in enumerate(block.get("lines") or []):
                if isinstance(line, dict):
                    shown_prices |= _check_price_view(
                        line.get("price"),
                        allowed=source_prices,
                        # O linie de coș afișează `unit × cantitate`, nu un preț de catalog — la
                        # 2 bucăți a 89 lei nicio coloană din `products` nu conține 178. Rămân
                        # verificate consistența internă (previous > current, discount corect) și
                        # boundary-ul pasiv; suma o garantează `CartService` (NX-237), unde există
                        # cantitatea. A o „verifica" aici ar cere ghicitul cantității din text.
                        have_source=False,
                        failures=failures,
                        prefix=f"{prefix}.lines[{li}]",
                    )
            shown_prices |= _check_price_view(
                block.get("total"),
                allowed=source_prices,
                have_source=False,  # totalul e o SUMĂ, nu un preț de catalog
                failures=failures,
                prefix=f"{prefix}.total",
            )

            for ai, action in enumerate(_block_actions(block)):
                activation = action.get("activation")
                href = activation.get("href") if isinstance(activation, dict) else None
                # Rutele relative sunt acoperite de contract (n-au host, deci nu pot scoate
                # clientul din magazin); doar absolutele se verifică contra catalogului.
                if (
                    href
                    and str(href).startswith("http")
                    and source_urls
                    and str(href) not in source_urls
                ):
                    failures.append(
                        f"{prefix}.actions[{ai}]: link {href!r} nu e in catalog "
                        "(products.product_url)"
                    )

            text = block.get("text")
            if isinstance(text, str):
                reference = shown_prices | source_prices
                if reference:
                    for raw in _PRICE_RE.findall(text):
                        value = _price(raw)
                        if value is not None and not _near(value, reference):
                            failures.append(
                                f"{prefix}: pretul {value} din text nu apare in view/sursa"
                            )
                for url in _URL_RE.findall(text):
                    if source_urls and url not in source_urls:
                        failures.append(f"{prefix}: URL din text absent din catalog: {url}")

    turn = view.get("turn")
    status = turn.get("status") if isinstance(turn, dict) else None
    if status in {"completed", "failed", "cancelled"}:
        renderable = sum(
            1
            for m in (view.get("messages") or [])
            if isinstance(m, dict)
            for b in (m.get("blocks") or [])
            if isinstance(b, dict) and b.get("type") != "divider"
        )
        if renderable == 0:
            failures.append(f"status terminal {status!r} fara niciun bloc randabil (P6)")

    failures.extend(passive_boundary_failures(view))
    return WebResponseCheck(passed=not failures, failures=failures)


# Singurul număr care are ce căuta pe sârmă: revizia conversației. E o VERSIUNE (client-ul o
# compară, nu o calculează), nu o valoare comercială.
_ALLOWED_NUMERIC_PATHS: frozenset[str] = frozenset({"$.conversation.revision"})


def passive_boundary_failures(node: Any, path: str = "$") -> list[str]:
    """NX-240 — boundary-ul „frontend pasiv", verificat pe payload, nu pe intenție.

    Un frontend nu poate calcula ce nu are. Dacă în ViewModel apare un `float` sau un `int` acolo
    unde ar trebui text display-ready, cineva va scădea două prețuri în browser mai devreme sau
    mai târziu — nu pentru că e neglijent, ci pentru că datele îl invită. Verificarea e
    structurală tocmai ca să nu depindă de disciplină."""
    if isinstance(node, dict):
        return [f for k, v in node.items() for f in passive_boundary_failures(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [f for i, v in enumerate(node) for f in passive_boundary_failures(v, f"{path}[{i}]")]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if path not in _ALLOWED_NUMERIC_PATHS:
            return [
                f"{path}: valoare numerică {node!r} pe sârmă — ViewModel-ul v2 livrează text "
                "display-ready, altfel calculul se întoarce în browser"
            ]
    return []
