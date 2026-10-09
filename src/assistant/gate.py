"""Poarta de adevăr pe tot ce scrie agentul unic (NX-396).

Nimic nu re-verifică `ctx.reply` după `agent_stage`, deci agentul se judecă aici, înainte de
răspuns, cu SURSA UNICĂ a validării de proză (`validate_prose`): prețuri, linkuri, cifre, afirmații
neverificabile, stoc și afirmații medicale, pe textul, motivele cardurilor și textele comparației,
judecate împreună. Sursele: produsele citite în tur (gated), regulile magazinului citite (un citat
literal trece, NX-346) și sumele care sunt fapte: totalul fiecărui set arătat (NX-391), al coșului,
sumele unei comenzi găsite. Sumele scrise de CLIENT nu sunt fapte: un preț „văzut pe alt site” ar
deveni preț permis, adică exact poarta anti-injecție (NX-121) deschisă.

Handle-urile (`P3`) sunt eticheta NOASTRĂ: scăpate în text devin numele produsului (NX-369), iar
unul necunoscut respinge răspunsul. O greșeală respinge răspunsul și agentul primește motivul; o
sugestie greșită doar iese. Funcție PURĂ pe faptele date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Lungimea maximă a unei sugestii (randorul web taie peste 56, `render._web_chips`).
SUGGESTION_MAX = 56
_HANDLE_IN_TEXT = re.compile(r"\bP[1-9][0-9]{0,3}\b")

HINTS = {
    "ungrounded_price": (
        "a price or sum is not in the tool results; use the exact prices, and do not repeat "
        "the customer's own figures as prices"
    ),
    "invented_link": "a link is not a product link from the tool results; remove it",
    "bare_number": "a number is not in the product facts; remove it or use the exact one",
    "text_claim": "remove the claim the facts do not support (best seller, most popular, …)",
    "stock_claim": "a stock statement contradicts the product's availability; fix it",
    "medical_claim": "remove the medical claim (treats, cures, safe in pregnancy and the like)",
    "unknown_card": "a card handle is not one you know; use handles from PRODUCTS YOU KNOW",
    "unavailable_card": "a card product cannot be shown now; choose another one",
    "comparison_handles": "a comparison needs 2-4 different products you can show",
    "handle_in_text": "write product names in the text, not handles; handles go only in fields",
    "empty": "the answer has no text; write what the customer reads",
}


@dataclass
class Checked:
    """Un `answer` judecat: ce s-ar servi și motivele de respingere (gol = acceptat)."""

    text: str
    cards: list[dict[str, str]]  # {handle, reason}
    kept_suggestions: list[str]
    dropped_suggestions: int
    comparison: dict[str, Any] | None
    notes: str
    rejected: list[str]


def name_handles(text: str, names: dict[str, str]) -> str:
    """Handle-urile cunoscute din text devin numele produsului. PUR."""
    return _HANDLE_IN_TEXT.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")


def has_handle(text: str) -> bool:
    return bool(_HANDLE_IN_TEXT.search(text or ""))


def check_answer(
    a: dict[str, Any],
    *,
    handles: dict[str, str],
    names: dict[str, str],
    short_names: dict[str, str],
    rows: dict[str, dict[str, Any]],
    shown_sets: list[list[str]],
    grounded: set[float],
    sources: list[str],
    order_found: bool,
    max_cards: int,
    max_suggestions: int,
    notes_max: int,
) -> Checked:
    """`handles` = handle → id; `names` = handle → numele distinct (textul); `short_names` =
    handle → numele scurt (sugestiile: randorul web aruncă parantezele); `rows` = id → rândul gated
    al turului; `shown_sets` = seturile de id-uri arătate mai devreme; `grounded` = sumele-fapt."""
    from src.agent.validator import set_total, validate_prose  # noqa: PLC0415

    rejected: list[str] = []
    cards: list[dict[str, str]] = []
    for c in a.get("cards") or []:
        h = str((c or {}).get("handle") or "")
        if h not in handles:
            rejected.append("unknown_card")
            continue
        if handles[h] not in rows:
            rejected.append("unavailable_card")
            continue
        if h not in {x["handle"] for x in cards}:
            reason = name_handles(str((c or {}).get("reason") or "").strip(), names)
            cards.append({"handle": h, "reason": reason})
    cards = cards[:max_cards]

    comp = a.get("comparison")
    if comp:
        wanted = list(dict.fromkeys(str(h) for h in comp.get("handles") or []))
        if not 2 <= len(wanted) <= 4 or any(
            h not in handles or handles[h] not in rows for h in wanted
        ):
            rejected.append("comparison_handles")
            comp = None
        else:
            comp = {
                "handles": wanted,
                **{
                    k: name_handles(str(comp.get(k) or ""), names)
                    for k in ("intro", "subtitle", "closing")
                },
            }

    sets = [*shown_sets, [handles[c["handle"]] for c in cards]]
    if comp:
        sets.append([handles[h] for h in comp["handles"]])
    sums = set(grounded)
    for ids in sets:
        total = set_total([rows[i] for i in ids if i in rows])
        if total is not None:
            sums.add(total)

    products = list(rows.values())
    text = name_handles(str(a.get("text") or "").strip(), names)
    body = "\n".join(
        [
            text,
            *(c["reason"] for c in cards if c["reason"]),
            *(str(comp.get(k) or "") for k in ("intro", "subtitle", "closing") if comp),
        ]
    ).strip()
    if not body:
        rejected.append("empty")
    else:
        if has_handle(body):
            rejected.append("handle_in_text")
        verdict = validate_prose(
            body,
            products=products,
            grounded_prices=sums,
            check_bare=not order_found,
            check_claims=True,
            grounded_sources=sources,
        )
        rejected += list(verdict.reasons)

    kept: list[str] = []
    dropped = 0
    for raw in a.get("suggestions") or []:
        s = name_handles(str(raw or "").strip(), short_names)
        if not s or s in kept:
            continue
        ok = (
            len(s) <= SUGGESTION_MAX
            and not has_handle(s)
            and validate_prose(
                s, products=products, grounded_prices=sums, check_bare=False, check_claims=True
            ).ok
        )
        if ok:
            kept.append(s)
        else:
            dropped += 1

    return Checked(
        text=text,
        cards=cards,
        kept_suggestions=kept[:max_suggestions],
        dropped_suggestions=dropped,
        comparison=comp,
        notes=str(a.get("notes") or "").strip()[:notes_max],
        rejected=list(dict.fromkeys(rejected)),
    )
