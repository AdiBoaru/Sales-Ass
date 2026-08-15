"""NX-240 — `WebViewProjectorV2`: funcția PURĂ care transformă fapte dovedite în ecran.

Contractul e o singură propoziție: **intră `GroundedAnswer` + identitatea turului + acțiunile deja
emise, iese `WebViewV2` complet display-ready.** Fără DB, fără HTTP, fără LLM, fără ceas implicit
(`now` e argument), fără `get_settings()` pe drumul de randare. Un test poate monkeypatch-ui orice
sursă de I/O ca să arunce și projectorul trebuie să treacă — invariantul `projector_io_violation`
nu e o convenție de review, e o proprietate testabilă.

**De ce funcție pură, nu „metodă pe context".** Randarea e singurul loc unde se decide ce vede
clientul. Dacă ar putea citi ceva, ar putea citi ALTCEVA decât ce s-a validat: un preț proaspăt
lângă un text vechi, un stoc de acum lângă o recomandare de acum trei secunde. Proiecția pură
peste fapte înghețate face categoria asta de bug imposibilă, nu improbabilă.

**Zero calcul în frontend.** Tot ce iese de aici e `str` localizat: prețul e `"89,00 lei"`,
reducerea e `"-18%"`, stocul e `"Ultimele 3 bucăți"`, cantitatea e `"2 buc."`. Nu există niciun
`float` în ViewModel, deci nu există aritmetică posibilă în browser. Regula nu e „FE-ul să nu
calculeze", e „FE-ul să nu AIBĂ cu ce".

**Omisiunea e o decizie, nu o scăpare.** Un câmp fără fapt nu primește placeholder, nu primește
`null` afișabil și nu primește „indisponibil": lipsește. Iar CTA-ul care depindea de el lipsește
odată cu el — un buton „adaugă în coș" peste un stoc necunoscut e o promisiune, nu un buton.

**P6 prin construcție.** Orice status terminal iese cu minimum un bloc randabil: dacă nu există
conținut, se emite un `notice` cu copy server-owned. Contractul NX-228 impune asta prin
`model_validator`; aici e garantat ÎNAINTE de validare, ca invariantul să nu fie o excepție.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agent.evidence_bundle import CartEvidence, EvidenceBundle, ProductEvidence
from src.agent.grounding_guard import (
    GroundedAnswer,
    GroundedComparison,
    GroundedProduct,
    ground_answer,
)
from src.web.action_models import action_label
from src.web.contracts_v2 import (
    MAX_ACTIONS_PER_ITEM,
    MAX_ACTIONS_PER_ROW,
    MAX_BLOCKS_PER_MESSAGE,
    MAX_CART_LINES,
    MAX_COMPARISON_COLUMNS,
    MAX_COMPARISON_ROWS,
    MAX_LABEL_LEN,
    MAX_MEMORY_CRITERIA,
    MAX_PRODUCT_ITEMS,
    MAX_TEXT_LEN,
    MAX_TITLE_LEN,
    MAX_VALUE_LEN,
    TERMINAL_STATUSES,
    VIEW_SCHEMA_VERSION,
    WebViewV2,
    _validate_url,
)
from src.web.localization import (
    copy_for,
    error_message,
    format_amount,
    format_availability,
    format_discount,
    format_money,
    format_quantity,
    format_rating,
    label,
    no_results_text,
    normalize_locale,
)

#: Codurile de eroare care merită un buton „încearcă din nou". Aceleași ca în proiecția NX-233 —
#: retryable e o proprietate a cauzei, nu a rendererului.
_RETRYABLE_CODES: frozenset[str] = frozenset(
    {"empty_result", "processing_error", "deadline_exceeded"}
)

#: Kind-urile de acțiune pe care projectorul le atașează unui CARD (restul intră în `action_row`).
_PRODUCT_ACTION_KINDS: frozenset[str] = frozenset(
    {"select_product", "request_details", "request_reviews", "cart_add_line"}
)

#: Acțiunile de coș, atașate blocului `cart_summary`.
_CART_ACTION_KINDS: frozenset[str] = frozenset({"checkout", "cart_clear", "cart_remove"})


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Identitatea turului pe sârmă. Vine din rândul de ledger (NX-232), nu din plan: cine e
    turul nu e o decizie semantică."""

    turn_id: str
    client_turn_id: str
    conversation_id: str
    conversation_revision: int = 0
    status: str = "completed"
    error_code: str | None = None


def _clip(value: Any, limit: int) -> str | None:
    """Text mărginit și cu spațiile normalizate, sau `None`. Normalizarea contează: contractul
    face `strip_whitespace` + `min_length=1`, deci un `"   "` ar fi respins ca payload invalid —
    mai bine îl transformăm în „câmp absent" aici decât să pice tot envelope-ul."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _safe_url(value: Any) -> str | None:
    """URL care trece EXACT regula contractului (https absolut sau rută relativă). Un URL care ar
    pica validarea e OMIS — cardul rămâne randabil, doar fără link (P6 la nivel de câmp)."""
    if not isinstance(value, str):
        return None
    try:
        return _validate_url(value)
    except ValueError:
        return None


def _price_view(product: ProductEvidence, locale: str) -> dict[str, Any] | None:
    """Prețul curent + (dacă e reducere REALĂ) prețul tăiat + procentul.

    Trei condiții pentru `previous`/`discount`, toate obligatorii: ambele fapte `usable`, ACEEAȘI
    monedă și `list_price > price`. Moneda se compară fiindcă „era 120 EUR, acum 89 RON" ar fi o
    reducere de 26% complet fictivă."""
    price = product.fact("price")
    currency = product.fact("currency")
    if not price.usable or not currency.usable:
        return None
    current = format_money(price.value, currency.value, locale)
    if current is None:
        return None
    view: dict[str, Any] = {"current": current[:MAX_VALUE_LEN]}
    list_price = product.fact("list_price")
    if list_price.usable:
        previous = format_money(list_price.value, currency.value, locale)
        discount = format_discount(price.value, list_price.value)
        if previous is not None and discount is not None:
            view["previous"] = previous[:MAX_VALUE_LEN]
            view["discount"] = discount[:MAX_VALUE_LEN]
    return view


def _subtitle(product: ProductEvidence) -> str | None:
    """Brand + variantă, când există. Compus din FAPTE, nu din proza modelului."""
    parts = [
        str(product.fact(name).value) for name in ("brand", "variant") if product.fact(name).usable
    ]
    return _clip(" · ".join(parts), MAX_VALUE_LEN) if parts else None


def _action_view(issued: Any, locale: str, *, appearance: str) -> dict[str, Any] | None:
    """`IssuedAction` → `ActionView`: etichetă server-owned + token opac. Fără kind, fără
    argumente, fără id de produs — ce înseamnă butonul rămâne exclusiv pe server (NX-236)."""
    text = action_label(issued.plan.kind, locale)
    if not text:
        return None
    return {
        "id": issued.action_id,
        "label": text[:40],
        "appearance": appearance,
        "activation": {"type": "submit", "token": issued.token},
    }


def _option_action_view(issued: Any, options: Sequence[str]) -> dict[str, Any] | None:
    """Acțiunea unei opțiuni de clarificare: eticheta e OPȚIUNEA noastră, pe poziția din token."""
    index = issued.plan.args.option_ref
    if index is None or index >= len(options):
        return None
    text = _clip(options[index], 40)
    if not text:
        return None
    return {
        "id": issued.action_id,
        "label": text,
        "appearance": "chip",
        "activation": {"type": "submit", "token": issued.token},
    }


def _cell_text(value: Any, locale: str) -> str | None:
    """Valoarea unei celule de comparație → text localizat. Numerele trec prin formatter (nu
    `str(float)`), boolenii prin copy, restul e text mărginit. `None` = necunoscut ONEST."""
    if value is None:
        return None
    if isinstance(value, bool):
        return label("yes" if value else "no", locale)
    if isinstance(value, (int, Decimal)) or isinstance(value, float):
        return _clip(format_amount(value, locale), MAX_VALUE_LEN)
    return _clip(value, MAX_VALUE_LEN)


# ── Blocuri ─────────────────────────────────────────────────────────────────────────────────
def _product_item(
    grounded: GroundedProduct,
    *,
    turn_id: str,
    index: int,
    locale: str,
    actions: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    product = grounded.evidence
    title = _clip(product.fact("title").value, MAX_TITLE_LEN) or "—"
    item: dict[str, Any] = {"view_id": f"{turn_id}:p{index}", "title": title}
    subtitle = _subtitle(product)
    if subtitle:
        item["subtitle"] = subtitle
    image_url = _safe_url(product.fact("image").value) if product.fact("image").usable else None
    if image_url:
        # `alt` = titlul produsului: singurul text pe care îl avem și care descrie chiar poza.
        item["image"] = {"src": image_url, "alt": title[:MAX_LABEL_LEN]}
    price = _price_view(product, locale)
    if price:
        item["price"] = price
    rating = product.fact("rating")
    review_count = product.fact("review_count")
    if rating.usable:
        text = format_rating(
            rating.value, review_count.value if review_count.usable else None, locale
        )
        if text:
            item["rating"] = text[:MAX_VALUE_LEN]
    availability = product.fact("availability")
    if availability.usable:
        stock = product.fact("stock")
        text = format_availability(
            availability.value, stock.value if stock.usable else None, locale
        )
        if text:
            item["availability"] = text[:MAX_VALUE_LEN]
    if grounded.reason:
        reason = _clip(grounded.reason, MAX_TEXT_LEN)
        if reason:
            item["reason"] = reason
    row_actions: list[dict[str, Any]] = []
    url = _safe_url(product.fact("url").value) if product.fact("url").usable else None
    if url:
        view_label = label("view_product", locale)
        if view_label:
            # `navigate` NU devine token: e un URL deja validat, fără comandă de purtat.
            row_actions.append(
                {
                    "id": f"{turn_id}:p{index}:view",
                    "label": view_label[:40],
                    "appearance": "secondary",
                    "activation": {"type": "navigate", "href": url, "target": "_blank"},
                }
            )
    row_actions.extend(actions)
    if row_actions:
        item["actions"] = row_actions[:MAX_ACTIONS_PER_ITEM]
    return item


def _comparison_block(
    comparison: GroundedComparison,
    bundle_by_id: dict[str, ProductEvidence],
    *,
    turn_id: str,
    locale: str,
) -> dict[str, Any] | None:
    headers: list[str] = []
    ids: list[str] = []
    for product_id in comparison.product_ids[:MAX_COMPARISON_COLUMNS]:
        product = bundle_by_id.get(product_id)
        title = _clip(product.fact("title").value, MAX_LABEL_LEN) if product else None
        if not title:
            continue
        headers.append(title)
        ids.append(product_id)
    if len(headers) < 2:
        return None
    rows: list[dict[str, Any]] = []
    for axis in comparison.axes[:MAX_COMPARISON_ROWS]:
        axis_label = _clip(axis, MAX_LABEL_LEN)
        if not axis_label:
            continue
        cells = [
            {"text": _cell_text(comparison.cells.get((product_id, axis)), locale)}
            for product_id in ids
        ]
        rows.append({"label": axis_label, "cells": cells})
    if not rows:
        return None
    return {"id": f"{turn_id}:cmp", "type": "comparison", "headers": headers, "rows": rows}


def _cart_block(
    cart: CartEvidence, *, turn_id: str, locale: str, actions: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """`cart_summary` din snapshotul canonic (NX-237). Totalul apare DOAR când e `known`: un
    total parțial prezentat ca total e o sumă greșită cu UI frumos."""
    lines: list[dict[str, Any]] = []
    for index, line in enumerate(cart.lines[:MAX_CART_LINES]):
        title = _clip(line.name, MAX_TITLE_LEN)
        quantity = format_quantity(line.quantity, locale)
        if not title or not quantity:
            continue
        entry: dict[str, Any] = {
            "view_id": f"{turn_id}:c{index}",
            "title": title,
            "quantity": quantity[:MAX_VALUE_LEN],
        }
        if line.facts_status == "known" and line.line_total is not None and line.currency:
            total = format_money(line.line_total, line.currency, locale)
            if total:
                entry["price"] = {"current": total[:MAX_VALUE_LEN]}
        lines.append(entry)
    if not lines:
        return None
    block: dict[str, Any] = {
        "id": f"{turn_id}:cart",
        "type": "cart_summary",
        "lines": lines,
    }
    title = label("cart_title", locale)
    if title:
        block["title"] = title[:MAX_TITLE_LEN]
    if cart.total_status == "known" and cart.total is not None and cart.currency:
        total = format_money(cart.total, cart.currency, locale)
        if total:
            block["total"] = {"current": total[:MAX_VALUE_LEN]}
    if actions:
        block["actions"] = list(actions)[:MAX_ACTIONS_PER_ROW]
    return block


def _blocks(
    answer: GroundedAnswer,
    *,
    identity: TurnIdentity,
    locale: str,
    issued_actions: Sequence[Any],
) -> list[dict[str, Any]]:
    """Blocurile, în ORDINEA DE CITIRE: răspunsul întâi, contextul după, butoanele la final.

    Ordinea nu e cosmetică — e răspunsul la „ce caută ochiul primul". Un client care întreabă
    ceva vrea răspunsul, nu un carusel; unul care primește „n-am găsit" vrea să știe de ce
    înainte să vadă alternative."""
    turn_id = identity.turn_id
    blocks: list[dict[str, Any]] = []

    # Acțiunile emise se împart pe destinație ÎNAINTE de blocuri: un card primește acțiunile care
    # îl NUMESC (`product_ref`), coșul pe ale lui, restul intră în rândul de la final.
    per_product: dict[str, list[dict[str, Any]]] = {}
    cart_actions: list[dict[str, Any]] = []
    row_actions: list[dict[str, Any]] = []
    option_actions: list[dict[str, Any]] = []
    options = tuple(answer.clarification.options) if answer.clarification is not None else ()
    allowed_commerce = {p.evidence.product_id for p in answer.products if p.commerce_allowed}
    for issued in issued_actions:
        kind = issued.plan.kind
        if kind == "answer_clarification":
            view = _option_action_view(issued, options)
            if view:
                option_actions.append(view)
            continue
        ref = issued.plan.args.product_ref
        if kind in _CART_ACTION_KINDS:
            view = _action_view(issued, locale, appearance="primary")
            if view:
                cart_actions.append(view)
            continue
        if ref and kind in _PRODUCT_ACTION_KINDS:
            # Poarta comercială: un CTA mutant apare DOAR pe un produs pe care guardul l-a
            # declarat vandabil. Un token emis pentru un produs devenit necunoscut nu se afișează.
            if kind == "cart_add_line" and ref not in allowed_commerce:
                continue
            view = _action_view(
                issued, locale, appearance="primary" if kind == "cart_add_line" else "secondary"
            )
            if view:
                per_product.setdefault(ref, []).append(view)
            continue
        view = _action_view(issued, locale, appearance="chip")
        if view:
            row_actions.append(view)

    text = _clip(answer.direct_answer, MAX_TEXT_LEN)
    if text:
        blocks.append({"id": f"{turn_id}:t0", "type": "text", "variant": "body", "text": text})

    if answer.no_results is not None:
        blocks.append(
            {
                "id": f"{turn_id}:nores",
                "type": "notice",
                "level": "info",
                "text": no_results_text(answer.no_results.reason_class, locale)[:MAX_TEXT_LEN],
            }
        )

    by_id = {p.evidence.product_id: p.evidence for p in answer.products}
    if answer.comparison is not None:
        block = _comparison_block(answer.comparison, by_id, turn_id=turn_id, locale=locale)
        if block:
            blocks.append(block)

    if answer.products:
        blocks.append(
            {
                "id": f"{turn_id}:pl",
                "type": "product_list",
                "items": [
                    _product_item(
                        grounded,
                        turn_id=turn_id,
                        index=index,
                        locale=locale,
                        actions=per_product.get(grounded.evidence.product_id, ()),
                    )
                    for index, grounded in enumerate(answer.products[:MAX_PRODUCT_ITEMS])
                ],
            }
        )

    if isinstance(answer.cart, CartEvidence) and answer.cart.lines:
        block = _cart_block(answer.cart, turn_id=turn_id, locale=locale, actions=cart_actions)
        if block:
            blocks.append(block)

    criteria = [c for c in (_clip(x, MAX_LABEL_LEN) for x in answer.memory_criteria) if c]
    if criteria:
        memory: dict[str, Any] = {
            "id": f"{turn_id}:mem",
            "type": "memory",
            "criteria": criteria[:MAX_MEMORY_CRITERIA],
        }
        title = label("memory_title", locale)
        if title:
            memory["title"] = title[:MAX_TITLE_LEN]
        blocks.append(memory)

    for index, disclosure in enumerate(answer.disclosures):
        body = _clip(disclosure, MAX_TEXT_LEN)
        if body:
            blocks.append(
                {
                    "id": f"{turn_id}:d{index}",
                    "type": "text",
                    "variant": "disclosure",
                    "text": body,
                }
            )

    if answer.clarification is not None:
        question = _clip(answer.clarification.question, MAX_TEXT_LEN)
        if question:
            blocks.append(
                {"id": f"{turn_id}:q", "type": "text", "variant": "lead", "text": question}
            )

    # Opțiunile de clarificare primele: sunt răspunsul la ce tocmai am întrebat.
    trailing = (option_actions + row_actions)[:MAX_ACTIONS_PER_ROW]
    if trailing:
        blocks.append({"id": f"{turn_id}:ar", "type": "action_row", "actions": trailing})
    return blocks[:MAX_BLOCKS_PER_MESSAGE]


def _envelope(identity: TurnIdentity, locale: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    copy = copy_for(locale)
    view: dict[str, Any] = {
        "schema_version": VIEW_SCHEMA_VERSION,
        "conversation": {
            "id": identity.conversation_id,
            "revision": max(0, identity.conversation_revision),
        },
        "turn": {
            "id": identity.turn_id,
            "client_turn_id": identity.client_turn_id,
            "status": identity.status,
        },
        "messages": (
            [{"id": f"{identity.turn_id}:m0", "role": "assistant", "blocks": blocks}]
            if blocks
            else []
        ),
        "composer": {"enabled": True, **copy["composer"]},
        "chrome": dict(copy["chrome"]),
        "a11y": {"announcements": dict(copy["announcements"])},
    }
    return view


def project(
    answer: GroundedAnswer,
    *,
    identity: TurnIdentity,
    locale: str,
    issued_actions: Sequence[Any] = (),
    now: datetime,
) -> WebViewV2:
    """Funcția PURĂ. `now` e argument și nu e citit din ceas nicăieri — două proiecții ale
    aceluiași turn, cu aceleași acțiuni, produc EXACT aceiași bytes.

    Ridică `ValueError` (prin `WebViewV2`) dacă rezultatul n-ar respecta contractul NX-228:
    apelantul tratează asta ca eșec de proiecție și livrează fallback-ul terminal, niciodată un
    payload pe jumătate."""
    del now  # semnătura îl cere (clock injectat explicit); niciun câmp curent nu depinde de el
    loc = normalize_locale(locale)
    blocks = _blocks(answer, identity=identity, locale=loc, issued_actions=issued_actions)
    if identity.status in TERMINAL_STATUSES and not blocks:
        # P6: un terminal fără conținut e imposibil de livrat, deci îl facem imposibil de produs.
        blocks = [
            {
                "id": f"{identity.turn_id}:notice",
                "type": "notice",
                "level": "warning" if identity.status == "cancelled" else "error",
                "text": error_message(identity.error_code or "empty_result", loc)[:MAX_TEXT_LEN],
            }
        ]
    view = _envelope(identity, loc, blocks)
    if identity.status == "failed":
        code = _clip(identity.error_code, MAX_LABEL_LEN) or "processing_error"
        message = error_message(code, loc)
        view["error"] = {
            "code": code,
            "message": message[:MAX_TEXT_LEN],
            "retryable": code in _RETRYABLE_CODES,
        }
    return WebViewV2.model_validate(view)


def project_plan(
    plan: Any,
    bundle: EvidenceBundle,
    *,
    identity: TurnIdentity,
    locale: str,
    issued_actions: Sequence[Any] = (),
    now: datetime,
    ask_clarification: bool = True,
    memory_criteria: tuple[str, ...] = (),
    commerce_enabled: bool = False,
) -> tuple[WebViewV2 | None, GroundedAnswer]:
    """Entrypoint-ul complet: grounding + proiecție. `(None, answer)` când guardul respinge —
    apelantul are `answer.failures` pentru telemetrie și livrează fallback-ul determinist.

    Ordinea e obligatorie și e jumătate din card: NIMIC nu se proiectează înainte să fie
    dovedit."""
    answer = ground_answer(
        plan,
        bundle,
        locale=locale,
        ask_clarification=ask_clarification,
        memory_criteria=memory_criteria,
        commerce_enabled=commerce_enabled,
    )
    if not answer.ok:
        return None, answer
    return (
        project(
            answer,
            identity=identity,
            locale=locale,
            issued_actions=issued_actions,
            now=now,
        ),
        answer,
    )


def view_index(answer: GroundedAnswer, *, turn_id: str) -> dict[str, str]:
    """`view_id → product_id`, maparea pe care browserul NU o primește.

    ViewModel-ul v2 ascunde id-ul de catalog: singurul lucru pe care frontendul îl face cu el ar
    fi să-l trimită înapoi, iar pentru asta există tokenul acțiunii. Dar EVALUATORUL are nevoie
    de urma spre catalog ca să verifice groundingul — și o produce aici, la projector, unde
    sursa e încă la îndemână (`src/evals/web_response.validate_web_view_v2`)."""
    return {
        f"{turn_id}:p{index}": grounded.evidence.product_id
        for index, grounded in enumerate(answer.products[:MAX_PRODUCT_ITEMS])
    }


__all__ = ["TurnIdentity", "project", "project_plan", "view_index"]
