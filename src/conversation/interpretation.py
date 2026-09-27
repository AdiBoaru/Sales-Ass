"""Kernel conversațional, contractul `kernel.v1.0` — modelele NORMATIVE (pasul 1, NX-328).

Sursa: `docs/KERNEL-CONTRACT-v1.md`, §„Normative models". Modulul nu are comportament: definește
ce scrie modelul (o singură interpretare a turului) și ce scrie DOAR codul (verdicte, referințe
rezolvate, planul turului). Componentele care le produc vin în pașii 2-5.

Două familii, cu reguli diferite:

- **Scrise de model** (`Act`, `StateChange`, `Reference`, `Ambiguity`, `TurnInterpretation`):
  compatibile cu `strict: true`. Fiecare câmp e prezent (niciun default), opționalele sunt
  nullable, iar obiectele nu acceptă chei în plus. Modelul nu are niciun câmp în care să pună un id
  de produs, o proveniență sau o tărie (I1, contractul §„Ownership").
- **Scrise doar de cod** (`CheckedChange`, `ResolvedRef`, `AmbiguityDecision`, `TurnPlan`,
  `AnswerPolicy`): nu pleacă niciodată spre model.

Modulul e PUR (poarta I13 din `tests/test_kernel_contract.py`): niciun client de model, niciun I/O,
niciun ceas. Schimbarea unui câmp scris de model e o schimbare de contract: snapshotul din
`tests/kernel/schema/` pică dacă `KERNEL_CONTRACT_VERSION` nu se schimbă în același diff."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.tools.catalog_tools import SearchArgs

#: `kernel.v1.1` (NX-333, MINOR): două câmpuri aditive în `SearchArgs`, scrise doar de planner
#: (`rank_terms`, `prefer`), și două rânduri aditive în tabelul plannerului. Schema scrisă de model
#: (`TurnInterpretation`) e identică cu cea din v1.0; snapshotul din `tests/kernel/schema/` diferă
#: doar prin versiune.
KERNEL_CONTRACT_VERSION = "kernel.v1.1"

# --- scrise de model ----------------------------------------------------------------------------

ActKind = Literal[
    "find",
    "show_more",
    "compare",
    "detail",
    "link",
    "cart",
    "bundle",
    "order_status",
    "store_info",
    "chitchat",
    "other",
]
ReferenceKind = Literal[
    "ordinal", "deictic", "name", "attribute", "extreme", "the_other", "earlier"
]

#: Dimensiunile pe care le are ORICE tenant, în afara fațetelor pachetului. `unmapped` = nicio
#: dimensiune nu se potrivește; codul o re-rezolvă o dată, la validare (contractul, „Provenance").
UNIVERSAL_DIMENSIONS: tuple[str, ...] = ("category", "price", "unmapped")


class _ModelWritten(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Act(_ModelWritten):
    kind: ActKind
    targets: list[str]  # id-uri de `Reference` declarate în aceeași interpretare (I22)
    query: str | None  # cuvintele clientului, pentru find / store_info


class StateChange(_ModelWritten):
    op: Literal["set", "add", "remove", "replace", "clear"]
    target: str | None  # handle „cN" (remove/replace) sau „topic" / „all" (clear)
    dimension: str | None  # enum per tenant, inclusiv `UNIVERSAL_DIMENSIONS`
    relation: Literal["eq", "lte", "gte", "contains", "avoid"] | None
    value: str | None
    number: float | None
    unit: str | None
    relative_to: str | None  # id de `Reference` declarat în aceeași interpretare (I22)
    quote: str  # cuvintele exacte ale clientului: dovadă, nu probă


class Reference(_ModelWritten):
    """O PROPUNERE semantică: codul o validează și o poate reclasifica sau respinge."""

    id: str  # „r1", local turului
    text: str
    kind: ReferenceKind
    ordinal: int | None
    name: str | None
    dimension: str | None  # propunere; apartenența o decide codul
    value: str | None  # propunere; valoarea canonică o decide codul
    direction: Literal["min", "max"] | None


class Ambiguity(_ModelWritten):
    about: Literal["reference", "scope", "value"]
    target: str | None
    readings: list[str]  # poarta le mapează pe valori de subiect; nu se arată niciodată


class TurnInterpretation(_ModelWritten):
    thread: Literal["continue", "aside", "resume"]
    acts: list[Act]  # în ordine; ultimul e actul principal
    changes: list[StateChange]  # gol = nimic schimbat; nu există KEEP
    references: list[Reference]
    ambiguities: list[Ambiguity]
    corrects_previous_turn: bool  # doar dovadă (I21)


# --- scrise doar de cod --------------------------------------------------------------------------

Provenance = Literal["explicit", "implicit", "inferred"]

ChangeReject = Literal[
    "unknown_dimension",
    "unknown_handle",
    "unknown_reference",
    "unit_mismatch",
    "polarity_conflict",
    "semantic_mismatch",
    "hard_conflict",
    "truncated",
    "invalid_reference_target",  # I24: ținta unui act nu denotă produse
]

Executor = Literal[
    "search",
    "page",
    "compare",
    "detail",
    "link",
    "cart",
    "bundle",
    "faq",
    "order",
    "ask",
    "delegate",
    "reply_only",
]


class _CodeWritten(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckedChange(_CodeWritten):
    """Un `StateChange` după validare."""

    change: StateChange
    dimension: str  # după re-rezolvarea lui „unmapped"
    canonical_value: str | float | None
    provenance: Provenance
    strength: Literal["hard", "soft", "ranking"]  # ranking = local turului, nepersistat (I23)
    rejected: ChangeReject | None


class ResolvedRef(_CodeWritten):
    ref_id: str
    kind: str  # tipul final, după o eventuală reclasificare de către cod
    outcome: Literal["exact", "ambiguous", "not_found", "stale"]
    product_ids: list[str]  # revalidate pe catalog în acest tur (I1)
    source: Literal["action", "shown_now", "shown_earlier", "parked", "page", "catalog"]
    reason: str | None


class AmbiguityDecision(_CodeWritten):
    verdict: Literal["act", "resolve_from_context", "act_both", "must_ask"]
    reason: str
    question: str | None  # șablon de pachet, interpolare deterministă


class TurnPlan(_CodeWritten):
    executor: Executor
    product_ids: list[str]
    search_args: SearchArgs | None
    depends_on: int | None  # indexul unui plan anterior din același tur (multi-act)


class AnswerPolicy(_CodeWritten):
    """Calculată DUPĂ unelte, ÎNAINTE de compunere."""

    verdict_allowed: bool  # False ⇒ insufficient_evidence
    missing: list[str]


# --- schema pentru model -------------------------------------------------------------------------


def _strictify(node: Any) -> None:
    """Forma cerută de `strict: true`: pe fiecare obiect, TOATE proprietățile sunt `required` și
    nicio cheie în plus. Pydantic pune deja `required` pe câmpurile fără default; aici se impune
    și pe ce ar scăpa, ca schema să nu depindă de un detaliu al generatorului."""
    if isinstance(node, dict):
        # Docstring-urile claselor (scrise pentru cod) ar pleca spre model ca `description`.
        # Instrucțiunile pentru model sunt ale adaptorului (pasul 5), nu ale docstring-urilor.
        # Doar pe NODURI de schemă: cheile din `properties`/`$defs` sunt NUME (un câmp numit
        # `title` ar fi dispărut din schemă, rămânând în `required`).
        node.pop("title", None)
        node.pop("description", None)
        if node.get("type") == "object" and "properties" in node:
            node["required"] = sorted(node["properties"])
            node["additionalProperties"] = False
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                for child in value.values():
                    _strictify(child)
            else:
                _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)


def _enum_nullable_str(values: Sequence[str]) -> dict[str, Any]:
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}


def build_interpretation_schema(dimensions: Sequence[str]) -> dict[str, Any]:
    """Schema `TurnInterpretation` pentru UN tenant. PUR, deterministă.

    `dimensions` = cheile de fațetă ale pachetului tenantului. Enumul final e reuniunea lor cu
    `UNIVERSAL_DIMENSIONS`, SORTATĂ: aceeași mulțime în altă ordine dă aceeași schemă, deci
    prefixul de prompt rămâne cache-uibil (contractul, §„Normative models").

    Cheile de categorie NU intră în schemă: `StateChange.value` e același câmp pentru toate
    dimensiunile, iar `strict` nu poate condiționa enumul valorii de dimensiune. Meniul de rafturi
    e dat în prompt, iar apartenența o decide codul (contractul, §„Ownership", rândul „Category
    key": „proposes from a closed menu", codul „owns")."""
    enum = sorted({*UNIVERSAL_DIMENSIONS, *(d for d in dimensions if d)})
    schema = copy.deepcopy(TurnInterpretation.model_json_schema())
    defs = schema.get("$defs", {})
    for name in ("StateChange", "Reference"):
        defs[name]["properties"]["dimension"] = _enum_nullable_str(enum)
    _strictify(schema)
    return schema
