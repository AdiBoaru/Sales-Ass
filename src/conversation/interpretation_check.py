"""Kernel `kernel.v1.1`, pasul 5 (NX-335) — validarea interpretării scrise de model. PUR.

Contractul (§„Normative models", granița de modul): `turn_interpreter` e SINGURUL adaptor, face UN
apel și întoarce o `TurnInterpretation`; tot ce vine după el (validare, resolver, delta, reducer,
poartă, planner) e kernelul pur. Validarea stă deci AICI, cu rolul `pure` și toate porțile lui
(I13 inclusiv tranzitiv, text brut, I14), nu în adaptor, ale cărui porți sunt mai slabe.

Ce face modulul, fiecare lucru o singură dată:

- `parse_reply`: ce a întors furnizorul → `outcome` din vocabularul ÎNCHIS al cardului, în ordinea
  tabelului (refuz, tăiere, JSON invalid, schemă încălcată, ok);
- `validate`: compune ce există deja (`check_changes` și regula I22 pe ACEEAȘI mulțime ca poarta
  NX-332) și NU taie nimic. Tăierea de la coadă ar pierde actul principal („the last one is the
  primary act"), iar consumatorii aplică deja plafoanele: `check_changes` respinge schimbările de
  peste 10 cu `truncated`, poarta verifică toate țintele, plannerul păstrează actul principal.
  `overflow` doar numără;
- `snapshot_id`: amprenta pachetului + vocabularului turului, detector de drift (nu reproducere);
- `interpretation_event`: evenimentul `turn_interpretation` (I18), cu vocabular închis și FĂRĂ
  text de client (P12: textul apare doar în trace, ca citate).

`INTERPRET_PROMPT_VERSION` stă AICI, nu în adaptor: dacă ar sta în `turn_interpreter.py`, importul
lui din modulul pur ar încărca tranzitiv clientul de model (poarta I13 pică).

Ce NU face: nu cheamă modelul (I13), nu scrie stare (I3), nu citește textul brut (citatele intră
doar în `check_changes`, prin cititorul declarat `_read_quote`), nu conține literali de vertical."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, get_args

from pydantic import ValidationError

from src.catalog.vocabulary import CatalogVocabulary
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    ActKind,
    ChangeReject,
    CheckedChange,
    Provenance,
    TurnInterpretation,
)
from src.conversation.provenance import (
    MAX_ACTS,
    MAX_CHANGES,
    MAX_REFERENCES,
    Handle,
    UserWords,
    check_changes,
)
from src.domain.constraints import UnitRegistry

#: Versiunea promptului de interpretare. Intră în cheia de cache (`business_id:versiune`) și în
#: `snapshot_id`: un prompt nou nu caută în cache-ul celui vechi și nu se compară cu el pe replay.
INTERPRET_PROMPT_VERSION = "interpret.v1"

#: `outcome`, în ordinea în care se decide (§3 din card): primul care se potrivește câștigă.
#: `internal_error` (recenzia NX-335, P6) e în plus față de card: o intrare stricată (pachet,
#: vocabular, efort necunoscut) sau o validare care aruncă nu are voie să scape ca excepție; e al
#: nostru, nu al furnizorului, deci nu se amestecă cu `provider_error`.
OUTCOMES: tuple[str, ...] = (
    "internal_error",
    "provider_error",
    "refused",
    "truncated",
    "invalid_json",
    "schema_violation",
    "ok",
)
CHANGE_REJECTS: tuple[str, ...] = get_args(ChangeReject)
PROVENANCES: tuple[str, ...] = get_args(Provenance)
ACT_KINDS: tuple[str, ...] = get_args(ActKind)
OVERFLOW_KINDS: tuple[str, ...] = ("acts", "changes", "references")

#: Cheile evenimentului `turn_interpretation`. Un test cere ca fiecare eveniment construit să aibă
#: exact cheile astea, pe toate `outcome`-urile (I18).
EVENT_KEYS: frozenset[str] = frozenset(
    {
        "contract_version",
        "prompt_version",
        "vocabulary_snapshot",
        "outcome",
        "effort",
        "thread",
        "acts",
        "changes",
        "references",
        "ambiguities",
        "provenance",
        "rejected",
        "unknown_reference",
        "overflow",
        "corrects_previous_turn",
    }
)

#: Motivele de oprire ale furnizorului pe care le citește `parse_reply`.
_FINISH_FILTERED = "content_filter"
_FINISH_LENGTH = "length"


def format_value(value: object) -> str:
    """O valoare canonică ca text, EXACT: numerele fără exponent și fără `.0` inutil
    (`1000000.0` → `1000000`, `12345.67` → `12345.67`), booleenii `true`/`false`. O singură funcție
    pentru vederea modelului și pentru etichetele raportului, ca cele două să nu poată diverge."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | Decimal):
        number = Decimal(str(value))
        if number == number.to_integral_value():
            return str(number.quantize(Decimal(1)))
        return format(number.normalize(), "f")
    return str(value)


# --- răspunsul furnizorului ----------------------------------------------------------------------


def parse_reply(
    content: str | None, *, refusal: str | None, finish_reason: str | None
) -> tuple[str, TurnInterpretation | None]:
    """(`outcome`, interpretarea sau `None`), în ordinea tabelului din card. PUR.

    `provider_error` nu apare aici: e o excepție a apelului, pe care o prinde adaptorul. Un JSON
    valid care nu trece de model (sub `strict` n-ar trebui să existe) e `schema_violation`, numărat
    ca semnal, nu reparat: a doua cerere ar încălca I13."""
    if refusal or finish_reason == _FINISH_FILTERED:
        return "refused", None
    if finish_reason == _FINISH_LENGTH:
        return "truncated", None
    if not content or not content.strip():
        return "invalid_json", None
    try:
        doc = json.loads(content)
    except ValueError:
        return "invalid_json", None
    try:
        return "ok", TurnInterpretation.model_validate(doc)
    except ValidationError:
        return "schema_violation", None


# --- validarea -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Validated:
    """Interpretarea turului, validată. `interpretation` e NESCHIMBATĂ (nimic tăiat)."""

    interpretation: TurnInterpretation
    checked: list[CheckedChange]  # `check_changes`: indexul ≥ 10 iese `truncated`
    unknown_refs: list[str]  # I22, pe TOATE actele și TOATE referințele (ca poarta)
    overflow: Mapping[str, int]  # {"acts": n, "changes": n, "references": n}


def unknown_references(interp: TurnInterpretation) -> list[str]:
    """I22: țintele de act și `relative_to` care nu numesc o `Reference` DECLARATĂ, în ordinea
    apariției, FĂRĂ plafoane: aceeași mulțime pe care o judecă poarta NX-332 (toate actele, toate
    referințele), nu cea din `check_targets` (care vede doar `acts[:3]` / `references[:6]`). Altfel
    un act care țintește `r7` ar fi numărat aici și servit de poartă."""
    declared = {r.id for r in interp.references}
    unknown: list[str] = []
    for act in interp.acts:
        unknown += [t for t in act.targets if t not in declared and t not in unknown]
    for change in interp.changes:
        rel = change.relative_to
        if rel is not None and rel not in declared and rel not in unknown:
            unknown.append(rel)
    return unknown


def overflow_of(interp: TurnInterpretation) -> dict[str, int]:
    """Cât depășește fiecare listă plafonul ei de runtime (contractul, decizia 3). Doar numără."""
    return {
        "acts": max(0, len(interp.acts) - MAX_ACTS),
        "changes": max(0, len(interp.changes) - MAX_CHANGES),
        "references": max(0, len(interp.references) - MAX_REFERENCES),
    }


def validate(
    interp: TurnInterpretation,
    *,
    words: UserWords,
    handles: Sequence[Handle] = (),
    vocab: CatalogVocabulary | None = None,
    pack: object | None = None,
    units: UnitRegistry | None = None,
    locale: str | None = None,
) -> Validated:
    """PUR. Nu taie nimic: plafoanele le aplică deja cine le consumă.

    Un act cu țintă nedeclarată rămâne în interpretare, iar poarta NX-332 îl scoate (regula 0); un
    `relative_to` nedeclarat e respins de `check_changes`. Există UN singur loc care respinge
    fiecare; aici doar se raportează."""
    checked = check_changes(
        interp, words=words, handles=handles, vocab=vocab, units=units, pack=pack, locale=locale
    )
    return Validated(
        interpretation=interp,
        checked=checked,
        unknown_refs=unknown_references(interp),
        overflow=overflow_of(interp),
    )


# --- amprenta vocabularului ----------------------------------------------------------------------


def _plain(value: Any) -> str:
    """Enum → valoarea lui, orice altceva → text: forma canonică nu depinde de tipul Python."""
    return "" if value is None else str(getattr(value, "value", value))


def _facet_doc(facet: object) -> dict[str, Any]:
    """Câmpurile unei fațete pe care le CITEȘTE validarea (derivate din cod, recenzia NX-335):
    `check_changes`/`hard_capable` (`key`, `enforce_ready`), `facet_overlays` (`aliases`),
    `NeedVocabulary` (`value_type`, `operators`, `values`, `aliases`, `scope`, `enforce_ready`),
    iar `source_key` îl citesc `lookup_attributes` și meniul adaptorului."""
    return {
        "key": _plain(getattr(facet, "key", None)),
        "aliases": sorted(
            (str(k), str(v)) for k, v in (getattr(facet, "aliases", {}) or {}).items()
        ),
        "values": sorted(str(v) for v in (getattr(facet, "values", ()) or ())),
        "enforce_ready": bool(getattr(facet, "enforce_ready", False)),
        "value_type": _plain(getattr(facet, "value_type", None)),
        "operators": sorted(str(o) for o in (getattr(facet, "operators", ()) or ())),
        "scope": _plain(getattr(facet, "scope", None)),
        "source_key": _plain(getattr(facet, "source_key", None)),
    }


def _units_doc(units: object) -> dict[str, Any]:
    """Tabelul de unități (I9: `check_changes` decide pe el ce număr aparține cărei dimensiuni)."""
    specs = getattr(units, "specs", None) or {}
    return {
        "specs": sorted(
            [
                _plain(name),
                _plain(getattr(spec, "canonical", None)),
                sorted((str(k), str(v)) for k, v in (getattr(spec, "factors", {}) or {}).items()),
                _plain(getattr(spec, "default_op", None)),
            ]
            for name, spec in specs.items()
        ),
        "aliases": sorted(
            (str(k), str(v)) for k, v in (getattr(units, "alias_facet", {}) or {}).items()
        ),
        "ambiguous": sorted(str(a) for a in (getattr(units, "ambiguous", ()) or ())),
    }


def snapshot_id(pack: object | None, vocab: CatalogVocabulary | None) -> str:
    """Amprenta pachetului și a vocabularului turului: SHA-256 peste o formă canonică (JSON cu chei
    sortate), primele 16 caractere hex. PURĂ.

    Intră TOT ce citește validarea din pachet (recenzia NX-335: un `enforce_ready` schimbat mută
    tăria soft → hard fără să schimbe cheile): per fațetă cheia, aliasurile, valorile declarate,
    `enforce_ready`, `value_type`, `operators`, `scope`, `source_key`; tabelul de unități (I9);
    `searchable_facets`; overlay-ul de limbă (`concern_map`); intrările vocabularului pe
    dimensiuni, **inclusiv `count`** (rezolvarea
    departajează și taie pe numărul de produse, deci același text se poate rezolva altfel după o
    mișcare de stoc), plus `INTERPRET_PROMPT_VERSION`. Ordinea din pachet nu contează.

    Nu e amprenta promptului (promptul arată doar valori plafonate) și nu e cheie de cache: se
    schimbă cu stocul, deliberat. E un detector de drift, nu o reproducere: un id spune că
    vocabularul diferă, nu cum era (vocabularul însuși se păstrează lângă corpus)."""
    facets = sorted(
        (_facet_doc(f) for f in (getattr(pack, "facets", ()) or ())), key=lambda d: d["key"]
    )
    overlay = sorted(
        (str(k), str(v)) for k, v in (getattr(pack, "concern_map", None) or {}).items()
    )
    dimensions: dict[str, list[list[Any]]] = {}
    if vocab is not None:
        for name, entries in vocab.dimensions.items():
            dimensions[str(name)] = sorted([e.key, e.label, int(e.count), e.path] for e in entries)
    doc = {
        "prompt_version": INTERPRET_PROMPT_VERSION,
        "facets": facets,
        "overlay": overlay,
        "searchable": sorted(str(k) for k in (getattr(pack, "searchable_facets", ()) or ())),
        "units": _units_doc(getattr(pack, "units", None)),
        "vocabulary": dimensions,
    }
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- evenimentul ---------------------------------------------------------------------------------


def interpretation_event(
    outcome: str,
    *,
    effort: str,
    snapshot: str,
    validated: Validated | None = None,
) -> dict[str, Any]:
    """Evenimentul `turn_interpretation` (I18). PUR, vocabular ÎNCHIS, fără text de client.

    Pe un `outcome` ≠ `ok` (sau fără interpretare) cheile rămân toate, cu valori neutre: forma
    evenimentului nu depinde de ce a mers prost, deci un raport nu trebuie să ghicească o cheie
    lipsă. Proveniența se numără doar pe schimbările NErespinse; respingerile, pe motiv."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome necunoscut: {outcome!r}")
    interp = validated.interpretation if validated is not None else None
    provenance = dict.fromkeys(PROVENANCES, 0)
    rejected = dict.fromkeys(CHANGE_REJECTS, 0)
    for c in validated.checked if validated is not None else ():
        if c.rejected is None:
            provenance[c.provenance] += 1
        else:
            rejected[c.rejected] += 1
    overflow = dict(validated.overflow) if validated is not None else {}
    return {
        "contract_version": KERNEL_CONTRACT_VERSION,
        "prompt_version": INTERPRET_PROMPT_VERSION,
        "vocabulary_snapshot": snapshot,
        "outcome": outcome,
        "effort": str(effort),
        "thread": interp.thread if interp is not None else None,
        "acts": [a.kind for a in interp.acts] if interp is not None else [],
        "changes": len(interp.changes) if interp is not None else 0,
        "references": len(interp.references) if interp is not None else 0,
        "ambiguities": len(interp.ambiguities) if interp is not None else 0,
        "provenance": provenance,
        "rejected": rejected,
        "unknown_reference": len(validated.unknown_refs) if validated is not None else 0,
        "overflow": {k: int(overflow.get(k, 0)) for k in OVERFLOW_KINDS},
        "corrects_previous_turn": bool(interp.corrects_previous_turn) if interp else False,
    }


__all__ = [
    "ACT_KINDS",
    "CHANGE_REJECTS",
    "EVENT_KEYS",
    "INTERPRET_PROMPT_VERSION",
    "OUTCOMES",
    "OVERFLOW_KINDS",
    "PROVENANCES",
    "Validated",
    "format_value",
    "interpretation_event",
    "overflow_of",
    "parse_reply",
    "snapshot_id",
    "unknown_references",
    "validate",
]
