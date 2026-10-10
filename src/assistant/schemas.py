"""Schemele uneltelor agentului unic (NX-396), STRICTE.

Toate obiectele au toate câmpurile cerute și nimic în plus (opționalele sunt `null`-abile), deci
modelul nu poate trimite un câmp inventat. Unde agentul numește un produs, schema cere forma
`P<n>`: un PATTERN, nu un enum, fiindcă enumul s-ar schimba la fiecare produs nou, iar schemele
stau în prefixul cache-uit al cererii. Schemele depind doar de meniuri: byte-identice pe toată
conversația. Forma e cea plată a `/v1/responses`.
"""

from __future__ import annotations

from typing import Any

from src.assistant.memory import HANDLE_PATTERN
from src.assistant.menus import Menus

HANDLE: dict[str, Any] = {"type": "string", "pattern": HANDLE_PATTERN}
SORTS = ("relevance", "price_asc", "price_desc", "rating_desc")


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _enum(values: tuple[str, ...]) -> dict[str, Any]:
    """Un meniu închis. Gol (vocabular indisponibil, tenant fără fațetă) ⇒ text liber în schemă:
    modul strict refuză un `enum` gol, iar unealta refuză oricum o valoare din afara meniului."""
    return {"type": "string", "enum": list(values)} if values else {"type": "string"}


def tool_schemas(menus: Menus) -> list[dict[str, Any]]:
    search = {
        "name": "search_catalog",
        "description": (
            "Search the store's catalog. Returns up to 8 products with the facts that decide "
            "whether they fit (type, needs, price, stock). `match` says how the text matched: "
            "strict, relaxed, typo or filters_only (no text match, only the filters)."
        ),
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "words to search for"},
                "category": _nullable(_enum(menus.categories)),
                "product_types": {"type": "array", "items": _enum(menus.product_types)},
                "brand": _nullable(_enum(menus.brands)),
                "needs": {
                    "type": "array",
                    "description": (
                        "the customer's stated needs, facet:value: products that state them come "
                        "first, products whose facts do not say come after (need: unknown), "
                        "products that contradict them are left out"
                    ),
                    "items": _enum(menus.needs),
                },
                "price_max": _nullable({"type": "number"}),
                "price_min": _nullable({"type": "number"}),
                "in_stock_only": {"type": "boolean"},
                "sort": _enum(SORTS),
                "exclude": {
                    "type": "array",
                    "description": "handles of products not to return (already shown)",
                    "items": HANDLE,
                },
            }
        ),
    }
    details = {
        "name": "product_details",
        "description": "The full sheet of up to 3 products (usage, ingredients, attributes).",
        "parameters": _obj({"handles": {"type": "array", "items": HANDLE}}),
    }
    rules = {
        "name": "store_rules",
        "description": "All active store rules (delivery, returns, payment, vouchers).",
        "parameters": _obj({}),
    }
    cart = {
        "name": "add_to_cart",
        "description": "Add a product you showed to the customer's cart.",
        "parameters": _obj({"handle": HANDLE, "quantity": {"type": "integer"}}),
    }
    order = {
        "name": "check_order",
        "description": (
            "The status of the customer's order, by its number, or their latest orders. "
            "An anonymous visitor gets a message about logging in."
        ),
        "parameters": _obj({"order_ref": _nullable({"type": "string"})}),
    }
    restock = {
        "name": "back_in_stock",
        "description": "Notify the customer when an out-of-stock product you showed is back.",
        "parameters": _obj({"handle": HANDLE}),
    }
    answer = {
        "name": "answer",
        "description": (
            "Finish the turn: the text the customer reads, the advice under the cards, the "
            "cards, the suggestions, an optional comparison and your notes about what the "
            "customer said."
        ),
        "parameters": _obj(
            {
                "text": {"type": "string"},
                "advice": {
                    "type": "string",
                    "description": "shown under the cards or the table; empty when not needed",
                },
                "cards": {
                    "type": "array",
                    "items": _obj({"handle": HANDLE, "reason": {"type": "string"}}),
                },
                "suggestions": {"type": "array", "items": {"type": "string"}},
                "comparison": _nullable(
                    _obj(
                        {
                            "handles": {"type": "array", "items": HANDLE},
                            "intro": {"type": "string"},
                            "subtitle": _nullable({"type": "string"}),
                            "closing": {"type": "string"},
                        }
                    )
                ),
                "notes": {"type": "string"},
            }
        ),
    }
    tools = [search, details, rules, cart, order, restock]
    if menus.families:
        moment = _nullable(_enum(menus.moments)) if menus.moments else {"type": "null"}
        tools.append(
            {
                "name": "routine_plan",
                "description": (
                    "The steps of a routine for one area, in the store's order, with the product "
                    "the store would put on each step and the steps it has no product for."
                ),
                "parameters": _obj(
                    {
                        "family": _enum(menus.families),
                        "moment": moment,
                        "needs": {"type": "array", "items": _enum(menus.needs)},
                        "budget_max": _nullable({"type": "number"}),
                        "steps": {
                            "type": "array",
                            "description": "only the steps the customer named; empty = all",
                            "items": _enum(menus.steps),
                        },
                        "anchor": _nullable(HANDLE),
                    }
                ),
            }
        )
    tools.append(answer)
    return [{"type": "function", **t, "strict": True} for t in tools]
