"""Schemele OpenAI (function-calling) pentru tool-urile agentului (G7).

Prefix STATIC (ordine fixă) → prompt caching OpenAI pe tokenii de schemă. `strict: True`
(Structured Outputs) → argumentele vin valide din construcție, mai puține retry-uri.
`business_id` NU apare în scheme — se ia din `ctx` în tool (izolare, principiul 7).
"""

from typing import Any

_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_products": {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Caută produse în catalog după nevoia clientului (semantic + filtre dure: preț, "
                "categorie, brand, concerns). Folosește pentru orice cerere de tip "
                "„caut/recomandă/ce aveți pentru…”."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Nevoia clientului în limbaj natural (ex. cremă ten uscat).",
                    },
                    "price_max": {
                        "type": ["number", "null"],
                        "description": "Buget maxim în lei, dacă e menționat; altfel null.",
                    },
                    "category": {
                        "type": ["string", "null"],
                        "description": (
                            "Slug-ul categoriei dacă e clar (din «Categorie probabilă» din prompt "
                            "sau cererea clientului); altfel null."
                        ),
                    },
                    "brand": {
                        "type": ["string", "null"],
                        "description": "Brandul, doar dacă l-a cerut explicit; altfel null.",
                    },
                    "concerns": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Nevoile/atributele în cuvintele clientului (ex. „ten gras”, "
                            "„piele sensibilă”, „acnee”); altfel null."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Câte produse (1-6).",
                    },
                },
                "required": ["query", "price_max", "category", "brand", "concerns", "limit"],
            },
        },
    },
    "get_product_details": {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": (
                "Detalii complete despre UN produs (preț, rating, rezumat de recenzii, "
                "plusuri/minusuri). Folosește când clientul vrea mai multe despre un produs anume."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "id-ul produsului (din rezultatele search_products).",
                    },
                },
                "required": ["product_id"],
            },
        },
    },
    "compare_products": {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": (
                "Compară 2-3 produse (preț, rating, plusuri/minusuri). Folosește când clientul "
                "ezită între produse sau cere o comparație."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-3 id-uri de produs (din rezultatele search_products).",
                    },
                },
                "required": ["product_ids"],
            },
        },
    },
    "checkout_link": {
        "type": "function",
        "function": {
            "name": "checkout_link",
            "description": (
                "Creează un link de cumpărare pentru produsele alese. Folosește DOAR când "
                "clientul e gata de cumpărare sau cere explicit linkul. Întoarce un URL de trimis."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cart_items": {
                        "type": "array",
                        "description": "Produsele de pus în coș (din rezultatele search_products).",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "product_id": {
                                    "type": "string",
                                    "description": "id-ul produsului.",
                                },
                                "variant_id": {
                                    "type": ["string", "null"],
                                    "description": "id-ul variantei, dacă există; altfel null.",
                                },
                                "quantity": {
                                    "type": "integer",
                                    "description": "Cantitatea (≥1).",
                                },
                            },
                            "required": ["product_id", "variant_id", "quantity"],
                        },
                    },
                },
                "required": ["cart_items"],
            },
        },
    },
    "check_order": {
        "type": "function",
        "function": {
            "name": "check_order",
            "description": (
                "Verifică statusul + livrarea unei comenzi. Folosește când clientul întreabă de "
                "o comandă (unde e comanda mea, status ORD-123). Caută pe contul clientului."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "order_ref": {
                        "type": ["string", "null"],
                        "description": (
                            "Numărul comenzii dacă l-a dat clientul; altfel null → ultimele "
                            "comenzi ale contactului."
                        ),
                    },
                },
                "required": ["order_ref"],
            },
        },
    },
}


def tool_schemas(names: list[str]) -> list[dict[str, Any]]:
    """Schemele OpenAI pentru tool-urile active (ordine stabilă → prompt caching)."""
    return [_SCHEMAS[n] for n in names if n in _SCHEMAS]
