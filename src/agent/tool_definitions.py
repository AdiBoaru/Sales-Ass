"""Schemele OpenAI (function-calling) pentru tool-urile agentului (G7).

Prefix STATIC (ordine fixă) → prompt caching OpenAI pe tokenii de schemă. `strict: True`
(Structured Outputs) → argumentele vin valide din construcție, mai puține retry-uri.
`business_id` NU apare în scheme — se ia din `ctx` în tool (izolare, principiul 7).
"""

from typing import Any

from src.domain import vocab_examples

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
                        "description": "Nevoia clientului în limbaj natural, așa cum a scris-o el.",
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
                            "Nevoile/atributele în cuvintele clientului{NEED_EXAMPLES}; "
                            "altfel null."
                        ),
                    },
                    "features": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Componente sau caracteristici cheie cerute EXPLICIT de "
                            "client{FEATURE_EXAMPLES}. DOAR când cere o componentă/proprietate "
                            "anume, nu o nevoie; altfel null."
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
                    "variant_label": {
                        "type": ["string", "null"],
                        "description": (
                            "Eticheta EXACTĂ de variantă cerută de client (nuanță/mărime, ex. "
                            "„Warm Beige”, „03”, „50 ml”). Completează DOAR când cauți produse "
                            "care AU acea variantă (fallback: alte game care chiar au nuanța "
                            "cerută); altfel null."
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
                    "variant_label",
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
                "Compară 2-4 produse (preț, rating, plusuri/minusuri). Folosește când clientul "
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
                        "description": "2-4 id-uri de produs (din rezultatele search_products).",
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


# NX-236 — registrul de TOOL-uri (ce poate chema MODELUL) e distinct de registrul de ACȚIUNI
# (ce poate apăsa CLIENTUL, `src/web/action_models.py`). Cele două nu se ating: dispatch-ul de
# acțiuni primește un `ActionSpec` deja rezolvat, nu un nume, deci nu există nicio cale prin care
# un token opac să numească un tool. `TOOL_NAMES` există ca invariantul să fie VERIFICABIL
# (`assert_registry_disjoint`), nu doar afirmat într-un comentariu.
TOOL_NAMES: tuple[str, ...] = tuple(_SCHEMAS)


# NX-273 — marcatorii pe care îi umple pachetul tenantului. Sunt în DESCRIERI, iar descrierea unui
# parametru nu e documentație, e o INSTRUCȚIUNE: un model care citește „ex. «ten gras»" învață ce
# fel de valori se așteaptă acolo. Scrise de mână, făceau sistemul mai bun pe clientul de azi și
# mai prost pe următorul, fără niciun semnal.
_EXAMPLE_MARKERS = ("{NEED_EXAMPLES}", "{FEATURE_EXAMPLES}")


def _fill(schema: dict[str, Any], filled: dict[str, str]) -> dict[str, Any]:
    """Înlocuiește marcatorii într-o schemă, recursiv, fără să mute nimic în original.

    Copie, nu mutație: `_SCHEMAS` e o constantă de modul partajată între tenanți, iar o singură
    scriere în ea ar face ca al doilea tenant să primească exemplele primului — un bug de izolare
    care n-ar da nicio eroare."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            out[key] = _fill(value, filled)
        elif isinstance(value, str) and any(m in value for m in _EXAMPLE_MARKERS):
            for marker, text in filled.items():
                value = value.replace(marker, text)
            out[key] = value
        else:
            out[key] = value
    return out


def tool_schemas(
    names: list[str], examples: vocab_examples.VocabExamples = vocab_examples.EMPTY_EXAMPLES
) -> list[dict[str, Any]]:
    """Schemele OpenAI pentru tool-urile active (ordine stabilă → prompt caching).

    `examples` vine din pachetul tenantului. Absent → marcatorii se înlocuiesc cu ȘIRUL GOL, deci
    descrierea rămâne o propoziție corectă fără clauza „(ex. …)". Substituția e deterministă, deci
    schemele rămân byte-identice pentru același pachet — condiția de caching."""
    filled = {
        "{NEED_EXAMPLES}": vocab_examples.clause(examples.needs),
        "{FEATURE_EXAMPLES}": vocab_examples.clause(examples.features),
    }
    return [_fill(_SCHEMAS[n], filled) for n in names if n in _SCHEMAS]
