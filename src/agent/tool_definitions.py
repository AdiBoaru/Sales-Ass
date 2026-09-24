"""Schemele OpenAI (function-calling) pentru tool-urile agentului (G7).

Prefix STATIC (ordine fixă) → prompt caching OpenAI pe tokenii de schemă. `strict: True`
(Structured Outputs) → argumentele vin valide din construcție, mai puține retry-uri.
`business_id` NU apare în scheme — se ia din `ctx` în tool (izolare, principiul 7).
"""

import re
from typing import Any

from src.config import card_slots
from src.domain import vocab_examples
from src.observability import turn_latency

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
                        "description": (
                            "Câte produse (1-{CARD_SLOTS}). Când clientul descrie o nevoie sau o "
                            "categorie, deci când are de ales, cere {CARD_SLOTS}. Când cere un "
                            "produs anume sau pune o întrebare punctuală, 1-2."
                        ),
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
    # NX-297 felia 2: opțiunile REALE pe care le poate onora magazinul, înaintea unei întrebări de
    # clarificare. Descrierea spune CÂND; regulile de formulare călătoresc în `llm_view`, adică se
    # plătesc doar pe turul care chiar întreabă (vezi `src/tools/clarify_tools.py`).
    "clarify_options": {
        "type": "function",
        "function": {
            "name": "clarify_options",
            "description": (
                "Opțiunile REALE din catalogul magazinului, pentru o întrebare de clarificare. "
                "Întoarce rafturile și caracteristicile care există CHIAR în catalog, plus dacă "
                "ce cere clientul nu se găsește deloc. Fără argumente. "
                "CHEAM-O când cererea numește doar o categorie sau un tip LARG, fără niciun "
                "calificator util: nici nevoie, nici buget, nici caz de folosire, nici ocazie, "
                "nici destinatar. "
                "NU o chema când cererea are măcar un calificator (o nevoie, un buget, un tip "
                "anume, un brand) — atunci caută și arată produse, apoi rafinează. "
                "NU o chema nici când clientul a pomenit sarcină, alăptare, o afecțiune sau "
                "alergii: acolo se caută, ca produsele contraindicate să fie filtrate."
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
                "Aduce regulile magazinului (livrare, retur, garanție, plată, facturare) ca listă "
                "de întrebări frecvente, din care alegi răspunsul potrivit. Folosește când "
                "clientul întreabă o regulă/politică, NU pentru produse."
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
    # NX-292 — secvența completă a unei familii, compusă de SERVER. Distinct de `related_products`:
    # acela pornește de la o ancoră și urmează muchii (deci depinde de ce muchii există), ăsta
    # umple TOȚI pașii declarați ai familiei din fațeta `routine_step` și spune explicit care pas
    # n-are produs. Modelul nu poate ști care dintre șase produse e gelul de curățare — numele de
    # catalog are mediana 200 de caractere și e nume plus reclamă (NX-280/049).
    #
    # `family` e `enum` completat din pachetul TENANTULUI. Niciun cuvânt de cosmetică aici:
    # familiile sunt declarate în `domain_pack.routine_steps`, iar la alt vertical aceeași poziție
    # e ocupată de altceva (pașii de instalare).
    "routine_plan": {
        "type": "function",
        "function": {
            "name": "routine_plan",
            "description": (
                "Secvența completă de pași a unei familii, cu un produs pe pas, în ordine. "
                "Folosește când clientul cere o rutină, pași, sau ce să folosească întâi și apoi. "
                "Serverul alege produsele și spune care pas n-are produs."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "family": {
                        "type": "string",
                        "enum": [],  # completat per tenant; gol ⇒ tool-ul nu se oferă deloc
                        "description": "Pentru ce e rutina.",
                    },
                    "concerns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Nevoile clientului{NEED_EXAMPLES}. Gol dacă n-a spus niciuna."
                        ),
                    },
                    "budget_max": {
                        "type": ["number", "null"],
                        "description": (
                            "Bugetul TOTAL al rutinei, dacă l-a spus clientul. Null altfel."
                        ),
                    },
                    "anchor_id": {
                        "type": ["string", "null"],
                        "description": (
                            "Id-ul unui produs pe care clientul îl are deja sau îl discută, ca "
                            "pașii să pornească de la el. Null dacă nu există unul."
                        ),
                    },
                    # Momentul zilei schimbă și CE pași se aplică, și ORDINEA în care cedează la
                    # buget: pe catalogul SOLE protecția solară apare în 97,3% din rutinele de
                    # dimineață și în 10,7% din cele de seară.
                    #
                    # Valorile stau în DESCRIERE, nu în `enum`, și asta e o decizie de risc, nu
                    # una de stil. Parametrul trebuie să accepte `null` („clientul n-a precizat"),
                    # iar `enum` restrânge TOATE valorile, deci `null` ar fi trebuit pus în enum ca
                    # să rămână valid — o construcție pe care n-o putem verifica fără un apel real,
                    # și al cărei eșec ar fi un 400 pe FIECARE tur de rutină al oricărui tenant cu
                    # momente. Câștigul enum-ului e mic aici: un moment inventat e ignorat de
                    # handler și se cade pe ordinea de zi întreagă, deci nu poate produce un
                    # răspuns greșit — spre deosebire de `family`, unde o valoare inventată ar
                    # însemna o interogare pe gol prezentată ca răspuns onest.
                    "moment": {
                        "type": ["string", "null"],
                        "description": (
                            "Momentul zilei, dacă clientul l-a spus{MOMENT_VALUES}. Null dacă "
                            "vrea rutina întreagă sau n-a precizat."
                        ),
                    },
                },
                "required": ["family", "concerns", "budget_max", "anchor_id", "moment"],
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
_EXAMPLE_MARKERS = (
    "{NEED_EXAMPLES}",
    "{FEATURE_EXAMPLES}",
    "{MOMENT_VALUES}",
    # NX-298: `{CARD_SLOTS}` nu vine din pachetul tenantului, ci de la proprietarul cifrei
    # (`Settings.card_slots`) — dar trece prin ACEEAȘI poartă, fiindcă defectul pe care poarta îl
    # prinde e același: un marcator nedeclarat pleacă LITERAL în descrierea citită de model.
    "{CARD_SLOTS}",
)


def _assert_markers_declared() -> None:
    """Poartă de IMPORT: un marcator scris într-o schemă și nedeclarat oprește procesul.

    Fără ea, marcatorul pleacă LITERAL în descrierea pe care o citește modelul — nu o eroare, o
    instrucțiune stricată. S-a întâmplat la `{MOMENT_VALUES}` (NX-292) și n-a fost prins de nimic:
    schema era validă, testele treceau, doar textul era absurd."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.update(re.findall(r"\{[A-Z_]+\}", node))

    walk(_SCHEMAS)
    if undeclared := found - set(_EXAMPLE_MARKERS):
        raise ValueError(
            f"marcatori nedeclarați în _SCHEMAS: {sorted(undeclared)} — ar pleca literal în "
            "descrierea citită de model. Adaugă-i în _EXAMPLE_MARKERS și umple-i în tool_schemas."
        )


_assert_markers_declared()


def _fill(schema: dict[str, Any], filled: dict[str, str]) -> dict[str, Any]:
    """Înlocuiește marcatorii într-o schemă, recursiv, fără să mute nimic în original.

    Copie, nu mutație: `_SCHEMAS` e o constantă de modul partajată între tenanți, iar o singură
    scriere în ea ar face ca al doilea tenant să primească exemplele primului — un bug de izolare
    care n-ar da nicio eroare."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            out[key] = _fill(value, filled)
        elif isinstance(value, str) and any(m in value for m in filled):
            # Condiția e pe cheile lui `filled`, nu pe un registru paralel de marcatori: un marcator
            # nou adăugat într-o schemă și uitat din registru pleca la model LITERAL („{MOMENT_
            # VALUES}"), fără nicio eroare. Aici, orice marcator pe care apelantul îl umple e
            # înlocuit prin construcție; `_assert_markers_declared` prinde cazul invers.
            for marker, text in filled.items():
                value = value.replace(marker, text)
            out[key] = value
        else:
            out[key] = value
    return out


#: Enumuri completate din pachetul TENANTULUI: tool → parametru → numele setului de valori.
#:
#: Un registru, nu câte o funcție per tool: regula e aceeași (valori închise, sortate, din pachet),
#: iar a doua copie a ei ar fi locul unde a treia ar uita `sorted()` și ar strica prompt caching-ul.
_TENANT_ENUMS: dict[str, dict[str, str]] = {
    "related_products": {"relation": "relation_kinds"},
    "routine_plan": {"family": "families"},
}

#: `(tool, param) → cheia de valori` pentru parametrii care DISPAR când tenantul n-a declarat
#: nimic, în loc să rămână ca întrebare fără răspuns posibil.
#:
#: Distincția e între un parametru fără care tool-ul n-are sens (`family`: fără familii nu se oferă
#: tool-ul deloc) și un RAFINAMENT (`moment`: o rutină e validă și fără el). Al doilea trebuie să
#: dispară, nu să rămână: un vertical fără momente ale zilei (un service auto n-are dimineață) ar
#: primi altfel un parametru obligatoriu pe care modelul nu are cum să-l completeze corect.
_DROP_PARAM_IF_NO_VALUES: dict[tuple[str, str], str] = {("routine_plan", "moment"): "moments"}


def tenant_enum_values(pack: Any) -> dict[str, tuple[str, ...]]:
    """Valorile de enum ale TENANTULUI, citite din pachet — UN singur loc (P3).

    Erau citite doar în `brain.py`, cu trei `getattr` scrise acolo. Cât timp creierul unic era
    singurul care oferea `routine_plan`/`related_products`, asta era suficient; când NX-297 le-a
    pus în toolsetul v1, apelantul nou n-avea de unde ști ce trebuie pasat și le-a lăsat goale —
    adică exact clasa „o verificare legată de o COPIE a adevărului, nu de adevărul însuși".

    Cheile sunt EXACT numele parametrilor lui `tool_schemas`, ca apelantul să nu le poată încurca:
    `tool_schemas(names, examples, **tenant_enum_values(pack))`."""
    registry = getattr(pack, "relation_kinds", None)
    steps = getattr(pack, "routine_steps", None)
    return {
        "relation_kinds": tuple(getattr(registry, "specs", {}) or ()),
        "families": tuple(getattr(steps, "families", {}) or ()),
        "moments": tuple(getattr(steps, "time_markers", {}) or ()),
    }


#: NX-322: uneltele al căror `concerns` devine meniu închis + citat, când meniul e aprins.
_NEED_MENU_TOOLS = frozenset({"search_products", "routine_plan"})

#: Descrierile noului `concerns`. Scrise în vocea pe care o cer (P13): fără liniuță, fără punct și
#: virgulă. Meniul intră în descriere, nu în enum, doar ca SENS (`cheie = etichetă`): enumul ține
#: valorile, descrierea îl ajută pe model să aleagă.
_NEED_MENU_DESCRIPTION = (
    "Nevoile clientului, alese din lista de mai jos după SENSUL a ce a scris, nu după cuvinte. "
    "Fiecare cu citatul EXACT din mesajele clientului care o susține. Nu alege o nevoie pe care "
    "doar tu ai presupus-o. Gol dacă n-a spus niciuna. Lista: {menu}."
)
_NEED_QUOTE_DESCRIPTION = (
    "Cuvintele clientului, copiate exact din mesajul lui (cel puțin două cuvinte), care arată "
    "nevoia. Nu parafraza și nu cita ce ai scris tu."
)


def _with_need_menu(schema: dict[str, Any], menu: Any) -> dict[str, Any]:
    """NX-322: `concerns` devine listă de `{key, quote}`, cu `key` din meniul ÎNCHIS al tenantului.

    Pe conversația `bc7a356e` schema cerea nevoile „în cuvintele clientului", modelul a transcris
    «se usucă după duș», iar rezoluția pe frază exactă n-a găsit nimic, deși pachetul avea `dry`.
    Cu meniul, modelul alege sensul, iar citatul îi dă codului ce să verifice (`need_menu`).
    Meniu gol (flag stins, vocabular căzut) ⇒ schema de azi, byte-identică."""
    options = tuple(getattr(menu, "options", ()) or ())
    fn = schema.get("function") or {}
    if not options or fn.get("name") not in _NEED_MENU_TOOLS:
        return schema
    params = fn["parameters"]
    old = params["properties"].get("concerns")
    if old is None:
        return schema
    nullable = isinstance(old.get("type"), list) and "null" in old["type"]
    item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "enum": [o.key for o in options]},
            "quote": {"type": "string", "description": _NEED_QUOTE_DESCRIPTION},
        },
        "required": ["key", "quote"],
    }
    concerns = {
        "type": ["array", "null"] if nullable else "array",
        "items": item,
        "description": _NEED_MENU_DESCRIPTION.format(menu=menu.description()),
    }
    props = {**params["properties"], "concerns": concerns}
    return {**schema, "function": {**fn, "parameters": {**params, "properties": props}}}


def tool_schemas(
    names: list[str],
    examples: vocab_examples.VocabExamples = vocab_examples.EMPTY_EXAMPLES,
    relation_kinds: tuple[str, ...] = (),
    families: tuple[str, ...] = (),
    moments: tuple[str, ...] = (),
    need_menu: Any = None,
) -> list[dict[str, Any]]:
    """Schemele OpenAI pentru tool-urile active (ordine stabilă → prompt caching).

    `examples` vine din pachetul tenantului. Absent → marcatorii se înlocuiesc cu ȘIRUL GOL, deci
    descrierea rămâne o propoziție corectă fără clauza „(ex. …)". Substituția e deterministă, deci
    schemele rămân byte-identice pentru același pachet — condiția de caching."""
    filled = {
        "{NEED_EXAMPLES}": vocab_examples.clause(examples.needs),
        "{FEATURE_EXAMPLES}": vocab_examples.clause(examples.features),
        # Momentele intră în DESCRIERE, nu în enum — vezi comentariul de la parametrul `moment`.
        "{MOMENT_VALUES}": vocab_examples.clause(tuple(sorted(moments))),
        # NX-298: câte produse are voie să ceară modelul. Aceeași cifră pe care o citesc promptul
        # de vânzare, promptul rich și tăierea cardurilor — schema nu mai poate declara alt plafon
        # decât cel pe care îl aplică în fapt codul.
        "{CARD_SLOTS}": str(card_slots()),
    }
    values = {"relation_kinds": relation_kinds, "families": families, "moments": moments}
    out: list[dict[str, Any]] = []
    for name in names:
        if name not in _SCHEMAS:
            continue
        schema = _with_tenant_enums(_fill(_SCHEMAS[name], filled), values)
        if schema is None:
            # Unealta nu poate fi CHEMATĂ: enumul ei de tenant e gol, deci niciun argument valid nu
            # există. A o oferi oricum e cea mai proastă dintre variante — furnizorul refuză schema
            # cu 400, iar 4xx e TERMINAL în `_with_retry` și înghițit de `agent_stage`, deci moare
            # tot drumul de vânzare și numai el (triajul rămâne sănătos deasupra: exact incidentul
            # din 2026-08-24). Direcția corectă de degradare e invers: turul pierde o unealtă, nu
            # răspunsul (P6). Numărat, ca să nu fie tăcut.
            turn_latency.degrade("tool_dropped_empty_tenant_enum")
            continue
        out.append(_with_need_menu(schema, need_menu))
    return out


def _with_tenant_enums(
    schema: dict[str, Any], values: dict[str, tuple[str, ...]]
) -> dict[str, Any] | None:
    """Completează enumurile care vin din pachetul TENANTULUI (vezi `_TENANT_ENUMS`).

    De ce enum și nu string liber: `strict: true` cere valori închise, iar un `relation`/`family`
    liber ar lăsa modelul să inventeze un tip de muchie sau o familie care nu există — o interogare
    pe gol care arată ca un răspuns onest („nu am legături de tipul ăsta") fără să fie.

    Sortat: pentru același pachet ies aceiași octeți, deci schema rămâne cache-uibilă (felia 3).
    Enum GOL înseamnă că tenantul n-a declarat nimic; apelantul nu trebuie să ofere tool-ul deloc
    (vezi `turn_profile.select`), iar dacă totuși o face, un enum vid e refuzat de furnizor —
    zgomotos, nu tăcut.

    Separat de enumuri, parametrii din `_DROP_PARAM_IF_NO_VALUES` DISPAR când tenantul n-are valori
    pentru ei. Cele două mecanisme nu se suprapun: unul închide mulțimea de valori a unui parametru
    indispensabil, celălalt scoate un parametru de rafinament care n-are ce întreba.

    Întoarce `None` când un enum INDISPENSABIL ar ieși gol — adică „unealta asta nu se oferă".
    Regula era DECLARATĂ aici de la NX-292 („fără familii nu se oferă tool-ul deloc"), dar trăia
    doar în `turn_profile.select`, adică într-un singur apelant. NX-297 a adăugat al doilea
    (`_SALES_TOOLS`), care n-o cunoștea, și atunci o regulă respectată prin disciplină a devenit o
    schemă invalidă pe fiecare tur. Acum e impusă în locul prin care trec TOȚI apelanții."""
    fn = schema.get("function") or {}
    name = str(fn.get("name") or "")
    spec = _TENANT_ENUMS.get(name)
    droppable = {p: k for (tool, p), k in _DROP_PARAM_IF_NO_VALUES.items() if tool == name}
    if not spec and not droppable:
        return schema
    params_in = fn["parameters"]
    props = dict(params_in["properties"])
    # `required` se rescrie doar dacă exista: a-l ADĂUGA gol acolo unde n-a fost ar schimba schema
    # unui tool care nu are legătură cu enumurile tenantului, iar `strict: true` îl citește.
    required = list(params_in["required"]) if "required" in params_in else None
    for param, key in droppable.items():
        if param in props and not (values.get(key) or ()):
            props.pop(param)
            if required is not None:
                required = [r for r in required if r != param]
    for param, key in (spec or {}).items():
        if param not in props:
            continue
        allowed = sorted(set(values.get(key) or ()))
        if not allowed:
            return None
        props[param] = {**props[param], "enum": allowed}
    params = {**params_in, "properties": props}
    if required is not None:
        params["required"] = required
    return {**schema, "function": {**fn, "parameters": params}}
