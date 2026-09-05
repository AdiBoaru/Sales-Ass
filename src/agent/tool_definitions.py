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
    # NX-275 felia 5 — singura DOVADĂ la care creierul nu ajunge azi: vecinii unei ancore în graful
    # de relații. Cele ~37k de muchii din `product_relations` sunt citite doar de cross-sell-ul
    # determinist de după `cart_add`; niciun tool nu le expune, deci modelul nu poate compune o
    # secvență de pași nici când datele există.
    #
    # `relation` e `enum` (cerință `strict`), completat din registrul TENANTULUI la
    # `tool_schemas(...)`. Nu apare niciun tip de muchie scris de mână aici: `routine_next` e un
    # cuvânt de cosmetică, iar la electrocasnice aceeași poziție e ocupată de pașii de instalare.
    "related_products": {
        "type": "function",
        "function": {
            "name": "related_products",
            "description": (
                "Produsele legate de un produs ANCORĂ prin graful de relații al magazinului. "
                "Folosește când clientul cere o secvență de pași sau ce merge împreună cu un "
                "produs. Serverul decide cât adânc se urmează fiecare tip de legătură."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "anchor_id": {
                        "type": "string",
                        "description": (
                            "Id-ul produsului de la care pornim (din rezultatele unei căutări, "
                            "din produsul discutat sau din pagina pe care e clientul)."
                        ),
                    },
                    "relation": {
                        "type": "string",
                        "enum": [],  # completat per tenant; gol ⇒ tool-ul nu se oferă deloc
                        "description": "Tipul de legătură urmărit.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Câte produse cel mult (1-6).",
                    },
                },
                "required": ["anchor_id", "relation", "limit"],
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
    names: list[str],
    examples: vocab_examples.VocabExamples = vocab_examples.EMPTY_EXAMPLES,
    relation_kinds: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Schemele OpenAI pentru tool-urile active (ordine stabilă → prompt caching).

    `examples` vine din pachetul tenantului. Absent → marcatorii se înlocuiesc cu ȘIRUL GOL, deci
    descrierea rămâne o propoziție corectă fără clauza „(ex. …)". Substituția e deterministă, deci
    schemele rămân byte-identice pentru același pachet — condiția de caching."""
    filled = {
        "{NEED_EXAMPLES}": vocab_examples.clause(examples.needs),
        "{FEATURE_EXAMPLES}": vocab_examples.clause(examples.features),
    }
    out = [_fill(_SCHEMAS[n], filled) for n in names if n in _SCHEMAS]
    return [_with_relation_enum(s, relation_kinds) for s in out]


def _with_relation_enum(schema: dict[str, Any], kinds: tuple[str, ...]) -> dict[str, Any]:
    """Completează enumul de relații al lui `related_products` din registrul TENANTULUI.

    De ce enum și nu string liber: `strict: true` cere valori închise, iar un `relation` liber ar
    lăsa modelul să inventeze un tip de muchie care nu există — o interogare pe gol care arată ca
    un răspuns onest („nu am legături de tipul ăsta") fără să fie.

    Sortat: pentru același pachet ies aceiași octeți, deci schema rămâne cache-uibilă (felia 3).
    Enum GOL înseamnă că tenantul n-a declarat nimic; apelantul nu trebuie să ofere tool-ul deloc
    (vezi `turn_profile.select`), iar dacă totuși o face, un enum vid e refuzat de furnizor —
    zgomotos, nu tăcut."""
    fn = schema.get("function", {})
    if fn.get("name") != "related_products":
        return schema
    props = dict(fn["parameters"]["properties"])
    props["relation"] = {**props["relation"], "enum": sorted(set(kinds))}
    params = {**fn["parameters"], "properties": props}
    return {**schema, "function": {**fn, "parameters": params}}
