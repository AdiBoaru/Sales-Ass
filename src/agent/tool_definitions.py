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
                    "features": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Ingrediente / caracteristici cheie cerute EXPLICIT de client (ex. "
                            "„cu niacinamidă”, „cu retinol”, „finisaj mat”). DOAR când cere o "
                            "componentă/proprietate anume, nu o nevoie; altfel null."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Câte produse (1-6).",
                    },
                    "sort_mode": {
                        "type": "string",
                        "enum": ["relevance", "price_asc", "price_desc", "rating_desc"],
                        "description": (
                            "Cum sortezi: 'price_asc' pentru «cel mai ieftin / mai ieftin», "
                            "'rating_desc' pentru «cel mai bun / cel mai bine cotat», altfel "
                            "'relevance'."
                        ),
                    },
                    "in_stock_only": {
                        "type": "boolean",
                        "description": (
                            "True DOAR dacă clientul cere explicit «în stoc / disponibil»; "
                            "altfel false."
                        ),
                    },
                    "product_name": {
                        "type": ["string", "null"],
                        "description": (
                            "Numele EXACT al unui produs ANUME cerut de client (ex. „Hidra "
                            "Boost Ultra”). Completează DOAR când numește un produs specific, "
                            "nu o nevoie sau categorie; altfel null."
                        ),
                    },
                },
                "required": [
                    "query",
                    "price_max",
                    "category",
                    "brand",
                    "concerns",
                    "features",
                    "limit",
                    "sort_mode",
                    "in_stock_only",
                    "product_name",
                ],
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
    "cart_add": {
        "type": "function",
        "function": {
            "name": "cart_add",
            "description": (
                "Adaugă un produs în coș (se acumulează între mesaje). Folosește când clientul "
                "vrea să mai pună ceva în coș fără să comande încă; apoi checkout_link când e gata."
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
    "reorder": {
        "type": "function",
        "function": {
            "name": "reorder",
            "description": (
                "Propune re-comanda ultimei comenzi a clientului. Folosește când clientul spune "
                "„vreau ce am comandat data trecută” / „trimite-mi același lucru”. Fără argumente."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
        },
    },
    "subscribe_back_in_stock": {
        "type": "function",
        "function": {
            "name": "subscribe_back_in_stock",
            "description": (
                "Abonează clientul la notificare când un produs fără stoc revine. Folosește când "
                "produsul cerut e indisponibil și clientul vrea să fie anunțat la reaprovizionare."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "id-ul produsului fără stoc (din rezultatele search).",
                    },
                    "variant_id": {
                        "type": ["string", "null"],
                        "description": "id-ul variantei, dacă a cerut una anume; altfel null.",
                    },
                },
                "required": ["product_id", "variant_id"],
            },
        },
    },
    "faq_lookup": {
        "type": "function",
        "function": {
            "name": "faq_lookup",
            "description": (
                "Caută în baza de cunoștințe a magazinului un fapt de business (livrare, retur, "
                "garanție, plată, facturare). Folosește când clientul întreabă o regulă/politică, "
                "NU pentru produse."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Întrebarea de business în limbaj natural (ex. livrarea).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    "request_human": {
        "type": "function",
        "function": {
            "name": "request_human",
            "description": (
                "Escaladează la un operator uman. Folosește când clientul cere explicit un om, e "
                "frustrat/nemulțumit, sau cererea e în afara a ce poți rezolva (reclamație, caz "
                "sensibil). Un coleg preia; spune-i clientului că revine cineva în scurt timp."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "De ce escaladezi, pe scurt (ex. client nemulțumit).",
                    },
                },
                "required": ["reason"],
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
