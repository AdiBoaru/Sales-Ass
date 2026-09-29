"""Kernel `kernel.v1.1`, pasul 5 (NX-335) — adaptorul de interpretare. SINGURUL modul de kernel care
cheamă modelul (I13, rolul `adapter`).

Contractul (§„Normative models", granița de modul): adaptorul construiește promptul și schema, face
UN apel `complete_schema` strict și întoarce o `TurnInterpretation`. Nu scrie stare (I3), nu
construiește `SearchArgs` (I2), nu decide nimic: produce dovada pe care kernelul pur o judecă.
Validarea, amprenta vocabularului și evenimentul sunt ale modulului pur `interpretation_check`.

Trei lucruri, fiecare cu un singur proprietar:

- **Vederea** (`render_view`): starea ca text cu handle-uri, în blocuri cu etichete stabile.
  Handle-urile `cN` vin din `need_handles` (ACEEAȘI funcție care validează), pozițiile `#i` din
  `state.references.displayed_products` (ACEEAȘI sursă din care citește resolverul), cuvintele
  clientului din ACEEAȘI fereastră din care se derivă `UserWords`. Deci un handle randat, o poziție
  și un citat văzut de model nu pot însemna altceva pentru validator. Pe ecran: nume scurte, fără
  id-uri (I1) și fără prețuri (un preț pe ecran ar invita un număr în loc de `relative_to`).
- **Promptul și schema** (`system_prompt`, `interpretation_schema`): instrucțiuni GENERICE, în
  engleză, ca text pentru model (P11: limba clientului e dată și `locale` e numit; nicio frază în
  limba clientului în cod). Tot ce e al tenantului (dimensiuni, valori, etichete, rafturi) vine din
  pachet și vocabular, ordonat stabil și plafonat, ca prefixul să rămână cache-uibil. Niciun exemplu
  few-shot în cod: un exemplu ar purta cuvinte de domeniu (I14).
- **Apelul** (`interpret_turn`): UN apel, fără reparație (a doua cerere ar încălca I13). Nu aruncă
  (P6): orice eșec iese ca `outcome`, iar pasul 6 cade pe calea v1.

Fără apelant în producție până la pasul 6 (`INTERPRETED_TURN_ENABLED`). Îl cheamă scripturile
offline (`scripts/nx335_interpret_smoke.py`, `scripts/nx335_interpret_replay.py`)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.agent.llm import prompt_cache_scope
from src.agent.prompt_builder import shelf_size
from src.catalog.folding import fold_text
from src.catalog.render_text import cut_at_sentence, display_name
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import INTERPRET_EFFORTS, get_settings
from src.conversation.interpretation import (
    UNIVERSAL_DIMENSIONS,
    TurnInterpretation,
    build_interpretation_schema,
)
from src.conversation.interpretation_check import (
    INTERPRET_PROMPT_VERSION,
    Validated,
    format_value,
    interpretation_event,
    parse_reply,
    snapshot_id,
    validate,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.provenance import Handle, UserWords, need_handles, tenant_dimensions
from src.conversation.state_v2 import ConversationStateV2

log = logging.getLogger(__name__)

#: Numele schemei pe sârmă. `usage.CALL_PURPOSES` îl mapează pe `interpret` în `per_call` (I13).
SCHEMA_NAME = "turn_interpretation"
#: Fereastra de istoric (mesaje, ambele roluri), ca a stagiului de context (max 8). Din ACEEAȘI
#: fereastră se derivă `UserWords`: un citat pe care modelul l-a văzut e găsit de validator.
MAX_HISTORY_MESSAGES = 8
#: Replica botului, tăiată la graniță de propoziție (P4): în medie 750 de caractere, iar pentru
#: interpretare contează ce a întrebat sau a oferit, nu tot textul (NX-255).
MAX_BOT_CHARS = 400
#: Valorile unei dimensiuni în meniul din prompt. O dimensiune venită din catalog poate avea sute
#: de valori; fără plafon costul și prefixul n-ar mai avea margine. Codul re-rezolvă oricum orice
#: valoare prin vocabularul complet (`check_changes`), deci plafonul limitează doar sugestia.
MAX_MENU_VALUES = 20

DIMENSIONS_HEADER = "DIMENSIONS"
SHELVES_HEADER = "SHELVES"
NONE = "none"

Role = Literal["user", "bot"]


@dataclass(frozen=True)
class InterpretInput:
    """Tot ce intră în apel, adus de apelant (pasul 6: stagiul; aici: scripturile offline).
    Adaptorul nu face I/O de DB: meniul și vocabularul vin gata încărcate."""

    locale: str
    pack: Any | None  # `DomainPack` (duck-typed, ca restul kernelului)
    vocab: CatalogVocabulary | None
    category_menu: Sequence[tuple[str, int]]  # `list_category_menu`: cheie + mărime
    state: ConversationStateV2  # ÎNAINTE de reducer; ecranul e `references.displayed_products`
    history: Sequence[tuple[Role, str]]  # în ordine, fără turul curent
    message: str  # turul curent


@dataclass(frozen=True)
class InterpretedTurn:
    """Ieșirea adaptorului. `interpretation` există doar pe `outcome == "ok"`; `event` e evenimentul
    `turn_interpretation` (I18), construit de modulul pur."""

    outcome: str
    interpretation: TurnInterpretation | None
    validated: Validated | None
    event: dict[str, Any]
    vocabulary_snapshot: str


# --- vederea (§1) --------------------------------------------------------------------------------


def window(inp: InterpretInput) -> tuple[tuple[Role, str], ...]:
    """Fereastra de istoric: ultimele `MAX_HISTORY_MESSAGES` mesaje, în ordine. SINGURA sursă
    pentru `HISTORY` și pentru `UserWords`."""
    turns = [(role, text or "") for role, text in inp.history if role in ("user", "bot")]
    return tuple(turns[-MAX_HISTORY_MESSAGES:])


def user_words(inp: InterpretInput) -> UserWords:
    """Cuvintele CLIENTULUI: mesajul curent + turele lui din fereastră, cel mai recent primul."""
    earlier = tuple(text for role, text in reversed(window(inp)) if role == "user")
    return UserWords(current=inp.message, earlier=earlier)


def handles_of(inp: InterpretInput) -> tuple[Handle, ...]:
    """Handle-urile nevoilor active, prin ACEEAȘI funcție care validează."""
    return need_handles(inp.state.needs, NeedVocabulary.from_pack(inp.pack))


def display(name: str | None) -> str:
    """Numele de pe ecran: capul numelui (`render_text.display_name`, ca pe carduri). Două carduri
    cu același nume scurt rămân distincte prin poziție (`#i`), nu prin text."""
    return _one_line(display_name(name))


def _one_line(text: str) -> str:
    """Rupturile de rând devin spații, ca un mesaj să nu poată deschide un bloc nou în vedere
    (o linie care ar începe cu `c1` sau `#1`). Tokenii rămân aceiași, deci citatul se găsește."""
    return " ".join(text.splitlines()) if text else ""


def _label(pack: Any | None, dimension: str, code: str, locale: str) -> str | None:
    getter = getattr(pack, "value_label", None)
    label = getter(dimension, code, locale) if callable(getter) else None
    return label if label and fold_text(label) != fold_text(code) else None


def _value(pack: Any | None, dimension: str, value: object, locale: str) -> str:
    """Valoarea unei nevoi: eticheta pachetului, altfel codul canonic (declarat: pe o fațetă fără
    etichete modelul vede codul). Numerele și booleenii ies exact (`format_value`), fără
    exponent."""
    if isinstance(value, bool | int | float):
        return format_value(value)
    code = str(value)
    return _label(pack, dimension, code, locale) or code


def _constraints(inp: InterpretInput) -> list[str]:
    lines = []
    for h in handles_of(inp):
        operator = _operator(inp, h)
        op = "=" if operator == "eq" else operator
        value = _value(inp.pack, h.dimension, h.value, inp.locale)
        lines.append(f"{h.handle} {h.key} {op} {value} ({h.source})")
    return lines or [NONE]


def _operator(inp: InterpretInput, handle: Handle) -> str:
    for need in inp.state.needs:
        if need.is_active and need.key == handle.key and need.normalized_value == handle.value:
            return need.operator
    return "eq"


def _subject(inp: InterpretInput) -> list[str]:
    topic = inp.state.topic
    kind = topic.product_type
    shown_kind = _value(inp.pack, "product_type", kind, inp.locale) if kind else NONE
    lines = [f"shelf: {topic.category_key or NONE}", f"type: {shown_kind}"]
    if topic.type_umbrella:
        # NX-350 (recenzia, constatarea 8): tipul spus vag de client e al subiectului, deci
        # modelul îl vede (altfel ar citi `type: none` și ar ghici iar unul singur).
        kinds = " | ".join(
            _value(inp.pack, "product_type", k, inp.locale) for k in topic.type_umbrella
        )
        lines.append(f"type said broadly: {kinds}")
    parked = inp.state.parked
    if parked is None:
        return [*lines, f"PARKED: {NONE}"]
    # Recenzia NX-335: resolverul caută și în setul PARCAT (o referință `earlier` sau un nume
    # poate ținti acolo), deci modelul trebuie să-l vadă: nume scurte, fără id-uri, fără prețuri.
    items = " | ".join(display(d.name) for d in parked.shown) or NONE
    held = parked.topic
    name = held.category_key or held.product_type or " | ".join(held.type_umbrella) or NONE
    return [*lines, f"PARKED: {name}", f"parked items: {items}"]


def render_view(inp: InterpretInput) -> str:
    """Vederea turului (PURĂ): blocurile în ordine fixă, fiecare cu eticheta lui, `none` când e
    gol. Mesajul curent NU e aici: vine o singură dată, la sfârșitul mesajului `user`."""
    refs = inp.state.references
    screen = [f"#{i} {display(d.name)}" for i, d in enumerate(refs.displayed_products, 1)]
    earlier = [
        f"set {i}: " + " | ".join(display(d.name) for d in items)
        for i, items in enumerate(refs.recent_sets, 1)
    ]
    pending = inp.state.pending_clarification
    history = [
        f"{role}: "
        + (_one_line(text) if role == "user" else _one_line(cut_at_sentence(text, MAX_BOT_CHARS)))
        for role, text in window(inp)
    ]
    blocks = [
        ["CONSTRAINTS", *_constraints(inp)],
        ["SUBJECT", *_subject(inp)],
        ["ON SCREEN", *(screen or [NONE])],
        ["EARLIER", *(earlier or [NONE])],
        [f"PENDING: {pending.target_key if pending is not None else NONE}"],
        ["HISTORY", *(history or [NONE])],
    ]
    return "\n".join("\n".join(block) for block in blocks)


def user_prompt(inp: InterpretInput) -> str:
    """Mesajul `user`: vederea, apoi mesajul curent, o singură dată, ultimul."""
    return f"{render_view(inp)}\n\nCURRENT MESSAGE\n{inp.message}"


# --- promptul și schema (§2) ---------------------------------------------------------------------


def interpretation_schema(pack: Any | None) -> dict[str, Any]:
    """`response_format.json_schema` pentru tenant. Enumul de dimensiuni e `tenant_dimensions`,
    ACEEAȘI mulțime pe care o validează `check_changes`, sortată (prefix stabil)."""
    return {
        "name": SCHEMA_NAME,
        "strict": True,
        "schema": build_interpretation_schema(sorted(tenant_dimensions(pack))),
    }


_INSTRUCTIONS = """\
You interpret ONE turn of a conversation between a customer and the shopping assistant of an \
online store. You do not answer the customer. You describe, as data in the given JSON schema, what \
the customer said and wants. Code decides every fact, identifier, filter and state change after \
you, and checks every quote against the customer's messages.

The customer writes in locale "{locale}". Quote the customer's words exactly as written.

The user message holds the conversation state, then CURRENT MESSAGE, the turn to interpret:
- CONSTRAINTS: the customer's active requirements, each with a handle cN.
- SUBJECT: the current shelf and item type; PARKED is the subject set aside before this one, \
with the items that were shown for it.
- ON SCREEN: the items shown last, by position #i.
- EARLIER: item sets shown before the current screen, the most recent first.
- PENDING: the question the assistant is waiting on, if any.
- HISTORY: the latest messages; user lines are the customer, bot lines are the assistant.

FIELDS
- thread: "continue" for the ongoing request, which includes a question about an item and any new \
request for items; "aside" only for a question about the store itself (policies, delivery, \
payment) or small talk, which changes nothing; "resume" when the customer goes back to the PARKED \
subject.
- acts: what the customer asks for in this turn, in order; the LAST act is the primary one. An act \
says WHAT the customer asks for, never HOW, and never whether the store sells it: code decides \
availability. find: wants items, including items the store may not carry. show_more: more of the \
same results. compare, detail, link, cart: about items named by references. detail also covers a \
question about an item on screen or just discussed (how to use it, why), with a reference to that \
item. cart: only when the customer explicitly asks to buy or to add an item; \
otherwise the act the rest of the sentence asks for. bundle: several items meant to be used \
together, as one set or in a sequence, even when the customer does not name them. \
order_status: an order. store_info: store policies. chitchat: no request. other: only when no \
other act fits. A complaint that asks for something else is read as that request, and a \
misspelled word by the intent of its sentence. targets: ids of references declared in this \
interpretation. query: the customer's words, for find and store_info only.
- changes: how the customer's requirements change in this turn; empty when nothing changes. \
op set: one value; add: one more value; remove: drop the handle in target; replace: the handle in \
target gets a new value; clear: target "topic" drops the current subject, "all" drops everything. \
dimension: one from {dimensions_header}, "category" for a shelf from {shelves_header} (value = the \
shelf key), "price" for money, "unmapped" only as a last resort, when no dimension fits. A \
shelf the customer names as the kind of item they want or ask about, also inside a question, is a \
set on category with that shelf key; when the customer also names an item type, a shelf word that \
is part of its name or only says what it is for is not a shelf, and neither is a word that \
describes the customer. \
relation: eq; lte (at most); gte (at least); contains; avoid (the customer does not want it). \
value: a code from the menu only when the customer's words name that code, never a narrower code \
for a broader word, and always of the same kind as the dimension's other values; when no code \
fits, the customer's words. number and unit: only for a number the customer wrote. relative_to: \
a reference id when the value is relative to an item on screen, with relation lte or gte and no \
number. What the customer describes about themselves or about the items (a condition, a problem, \
a complaint about price) is a change when a menu value names its cause: when they describe \
themselves or the items, the value that names what they are or have, about the same part or item \
they describe, rather than one that names a result of it; a result the customer asks for is a \
change too. A complaint about the price of the items on screen is price with lte relative to them.
- quote: the customer's exact words that support the change, copied from CURRENT MESSAGE or from \
a user line in HISTORY, never from a bot line. A change with no customer words behind it is a \
guess: leave it out.
- references: every item the customer points at, with ids r1, r2, ... in order. kind: ordinal \
(position #i on screen, ordinal = i); deictic ("this one"); name (name = the words used); \
attribute (dimension + value); extreme (dimension + direction min or max); the_other; earlier \
(an item from EARLIER or from the PARKED items). text: the customer's words.
- ambiguities: only when the turn can honestly be read in more than one way and the readings \
lead to different answers. about: reference, scope or value; readings: the possible readings, as \
short values.
- corrects_previous_turn: true when the customer corrects what they said in the previous turn.
- A number with a unit belongs to the dimension of that unit. A number without a unit is a price \
only when the customer is talking about money."""


def _facets(pack: Any | None) -> dict[str, Any]:
    return {getattr(f, "key", ""): f for f in (getattr(pack, "facets", ()) or ())}


def _entries(inp: InterpretInput, facet: Any, key: str) -> tuple[VocabEntry, ...]:
    if inp.vocab is None:
        return ()
    source = getattr(facet, "source_key", None)
    return inp.vocab.entries(key) or (inp.vocab.entries(source) if source else ())


def _menu_values(inp: InterpretInput, key: str, facet: Any) -> list[str]:
    """Valorile unei dimensiuni în meniu: ale vocabularului (ce se poate servi), ordonate după
    mărimea ROTUNJITĂ (aceeași rotunjire ca rafturile, ca o mișcare de stoc să nu schimbe ordinea),
    apoi după cod; fără vocabular, valorile declarate de pachet, sortate. Plafonate."""
    entries = _entries(inp, facet, key)
    if entries:
        ranked = sorted(entries, key=lambda e: (-shelf_size(e.count), e.key))
        codes = [e.key for e in ranked]
    else:
        codes = sorted(str(v) for v in (getattr(facet, "values", ()) or ()))
    out = []
    for code in codes[:MAX_MENU_VALUES]:
        label = _label(inp.pack, key, code, inp.locale)
        out.append(f"{code} ({label})" if label else code)
    return out


def _value_type(facet: Any) -> str:
    kind = getattr(facet, "value_type", None)
    return str(getattr(kind, "value", kind) or "")


def _unit(inp: InterpretInput, key: str) -> str | None:
    specs = getattr(getattr(inp.pack, "units", None), "specs", None) or {}
    spec = specs.get(key)
    return getattr(spec, "canonical", None) if spec is not None else None


def _dimension_lines(inp: InterpretInput) -> list[str]:
    facets = _facets(inp.pack)
    lines = []
    for key in sorted(tenant_dimensions(inp.pack) - set(UNIVERSAL_DIMENSIONS)):
        facet = facets.get(key)
        kind = _value_type(facet)
        if kind == "number":
            unit = _unit(inp, key)
            lines.append(f"- {key} (number{', unit ' + unit if unit else ''})")
        elif kind == "bool":
            lines.append(f"- {key} (true or false)")
        elif values := _menu_values(inp, key, facet):
            lines.append(f"- {key}: " + ", ".join(values))
        else:
            lines.append(f"- {key} (the customer's words)")
    return lines


def system_prompt(inp: InterpretInput) -> str:
    """Prefixul cache-uibil: instrucțiunile generice, meniul de dimensiuni, meniul de rafturi. Nu
    depinde de stare sau de mesaj, iar ordinea e stabilă (fațete sortate, mărimi rotunjite)."""
    parts = [
        _INSTRUCTIONS.format(
            locale=inp.locale,
            dimensions_header=DIMENSIONS_HEADER,
            shelves_header=SHELVES_HEADER,
        )
    ]
    dims = _dimension_lines(inp)
    if dims:
        parts.append(
            f"{DIMENSIONS_HEADER} (closed menu for StateChange.dimension; values are canonical "
            "codes, with the store's label in parentheses when it has one)\n" + "\n".join(dims)
        )
    shelves = [f"- {key} ({shelf_size(int(n))})" for key, n in sorted(inp.category_menu) if key]
    if shelves:
        parts.append(
            f"{SHELVES_HEADER} (closed menu for dimension category: the value is the shelf key; "
            "a shelf's parent is the start of its key; the number is its approximate size)\n"
            + "\n".join(shelves)
        )
    return "\n\n".join(parts)


# --- apelul (§3) ---------------------------------------------------------------------------------


async def interpret_turn(
    llm: Any, inp: InterpretInput, *, business_id: str, effort: str | None = None
) -> InterpretedTurn:
    """UN apel. Nu aruncă (P6): orice eșec iese ca `outcome`, iar pasul 6 cade pe calea v1.
    Nicio rundă de reparație: a doua cerere ar încălca I13.

    `effort` = brațul unui replay (`none`/`low`); lipsă ⇒ `LLM_REASONING_EFFORT_INTERPRET`.
    `business_id` intră doar în cheia de cache, server-side (P7): schema n-are câmp de tenant."""
    chosen = ""
    snapshot = ""
    try:
        s = get_settings()
        raw = effort if effort is not None else s.llm_reasoning_effort_interpret
        # Evenimentul poartă EXACT valoarea de pe sârmă (`_sampling` o curăță la fel).
        chosen = str(raw).strip()
        if chosen not in INTERPRET_EFFORTS:
            raise ValueError(f"efort de interpretare necunoscut: {chosen!r}")
        snapshot = snapshot_id(inp.pack, inp.vocab)
        schema = interpretation_schema(inp.pack)
        system, user = system_prompt(inp), user_prompt(inp)
    except Exception as e:  # noqa: BLE001 — P6: o intrare stricată e un `outcome`, zero apeluri
        return _internal_error(e, chosen, snapshot)
    try:
        with prompt_cache_scope(f"{business_id}:{INTERPRET_PROMPT_VERSION}"):
            reply = await llm.complete_schema_raw(
                system,
                user,
                schema,
                reasoning_effort=chosen,
                temperature=s.llm_temperature_interpret,
            )
    except Exception as e:  # noqa: BLE001 — P6: eșecul furnizorului e un `outcome`, nu o excepție
        log.warning("turn_interpreter: provider_error (%s)", type(e).__name__)
        event = interpretation_event("provider_error", effort=chosen, snapshot=snapshot)
        return InterpretedTurn("provider_error", None, None, event, snapshot)
    try:
        outcome, interp = parse_reply(
            reply.content, refusal=reply.refusal, finish_reason=reply.finish_reason
        )
        validated = None
        if interp is not None:
            validated = validate(
                interp,
                words=user_words(inp),
                handles=handles_of(inp),
                vocab=inp.vocab,
                pack=inp.pack,
                locale=inp.locale,
            )
        event = interpretation_event(outcome, effort=chosen, snapshot=snapshot, validated=validated)
    except Exception as e:  # noqa: BLE001 — P6: după apel, tot un `outcome` (un singur apel)
        return _internal_error(e, chosen, snapshot)
    return InterpretedTurn(outcome, interp, validated, event, snapshot)


def _internal_error(error: Exception, effort: str, snapshot: str) -> InterpretedTurn:
    """`internal_error` (recenzia NX-335, abatere declarată de la cele șase `outcome` ale cardului):
    o defecțiune a NOASTRĂ (intrare stricată, efort necunoscut, validare care aruncă) nu scapă ca
    excepție (P6) și nu se amestecă cu eșecul furnizorului."""
    log.warning("turn_interpreter: internal_error (%s)", type(error).__name__)
    event = interpretation_event("internal_error", effort=effort, snapshot=snapshot)
    return InterpretedTurn("internal_error", None, None, event, snapshot)


__all__ = [
    "DIMENSIONS_HEADER",
    "INTERPRET_PROMPT_VERSION",
    "MAX_BOT_CHARS",
    "MAX_HISTORY_MESSAGES",
    "MAX_MENU_VALUES",
    "SCHEMA_NAME",
    "SHELVES_HEADER",
    "InterpretInput",
    "InterpretedTurn",
    "display",
    "handles_of",
    "interpret_turn",
    "interpretation_schema",
    "render_view",
    "system_prompt",
    "user_prompt",
    "user_words",
    "window",
]
