"""NX-328 felia 1c — harnessul de replay al kernelului `kernel.v1.0`.

Un journey al kernelului (`kernel-journey.v1`) e o conversație pe un pachet de fixture, cu
etichete pe STRATURI: fiecare tur spune ce trebuie să iasă din straturile care îl interesează
(`interpretation`, `checked`, `resolver`, `reducer`, `ambiguity`, `plan`, `executor`,
`answer_policy`). Comparatorul (`kernel_trace.first_divergence`) raportează primul strat greșit și
nu îi mai judecă pe cei de după el.

Formatul refolosește numele de câmpuri din journey-urile NX-246 (`journey_id`, `family`, `locale`,
`turns[].user_input`), dar e separat: schema NX-246 e închisă (`additionalProperties: false`) și
n-are unde purta etichete pe straturi.

În pasul 1 nu există încă niciun strat care să ruleze, deci harnessul se verifică pe traceuri
construite de mână. Etichetele se scriu COMPACT: câmpurile nule și listele goale pot lipsi, iar
`expand_interpretation` le completează înainte de validarea strictă, fiindcă modelul real trebuie
să le trimită pe toate, dar un om care scrie un caz de test nu trebuie să le repete."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.catalog.folding import fold_text
from src.conversation.interpretation import (
    AmbiguityDecision,
    AnswerPolicy,
    CheckedChange,
    Reference,
    ResolvedRef,
    StateChange,
    TurnInterpretation,
    TurnPlan,
)
from src.conversation.kernel_trace import LAYER_NAMES, Divergence, KernelTrace, first_divergence

ROOT = Path(__file__).resolve().parents[2]
JOURNEYS_DIR = ROOT / "tests" / "golden" / "kernel_journeys"
PACKS_DIR = ROOT / "tests" / "fixtures" / "packs"
SCHEMA_VERSION = "kernel-journey.v1"

_NULLABLE = {
    "acts": ("query",),
    "changes": tuple(name for name in StateChange.model_fields if name not in ("op", "quote")),
    "references": tuple(
        name for name in Reference.model_fields if name not in ("id", "text", "kind")
    ),
    "ambiguities": ("target",),
}
_LIST_DEFAULTS = {
    "acts": ("targets",),
    "ambiguities": ("readings",),
}
_TOP_LISTS = ("acts", "changes", "references", "ambiguities")


def expand_interpretation(compact: dict[str, Any]) -> dict[str, Any]:
    """Eticheta compactă → forma completă a lui `TurnInterpretation` (nule + liste goale)."""
    full: dict[str, Any] = {
        "thread": compact.get("thread", "continue"),
        "corrects_previous_turn": compact.get("corrects_previous_turn", False),
    }
    for key in _TOP_LISTS:
        items = []
        for item in compact.get(key, []):
            item = dict(item)
            for name in _NULLABLE.get(key, ()):
                item.setdefault(name, None)
            for name in _LIST_DEFAULTS.get(key, ()):
                item.setdefault(name, [])
            items.append(item)
        full[key] = items
    unknown = set(compact) - {"thread", "corrects_previous_turn", *_TOP_LISTS}
    if unknown:
        raise ValueError(f"chei necunoscute în eticheta de interpretare: {sorted(unknown)}")
    return full


@dataclass(frozen=True)
class JourneyTurn:
    user_input: str
    expect: dict[str, Any]
    state_before: dict[str, Any] = field(default_factory=dict)
    # NX-329 (aditiv, formatul rămâne v1): unde trăiesc produsele la care se poate referi turul,
    # cu id-uri de fixture — `shown_now`, `shown_earlier` (listă de seturi, recent întâi),
    # `parked`, `page`, `focus`, `thread`. Hrănește stratul `resolver`.
    sources: dict[str, Any] = field(default_factory=dict)
    # NX-331 (aditiv): ce a arătat EXECUTORUL turului (id-uri de fixture). Stratul `reducer`
    # rulează turele în lanț, deci setul arătat devine sursa turelor de după (ecran, seturi de mai
    # devreme, parcat) fără să fie scris de mână în `sources`.
    shown: tuple[str, ...] = ()


@dataclass(frozen=True)
class Journey:
    journey_id: str
    pack: str
    family: tuple[str, ...]
    locale: str
    source: str
    turns: tuple[JourneyTurn, ...]


def _parse_expect(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - set(LAYER_NAMES)
    if unknown:
        raise ValueError(f"straturi necunoscute: {sorted(unknown)}")
    out = dict(raw)
    if "interpretation" in out:
        out["interpretation"] = TurnInterpretation.model_validate(
            expand_interpretation(out["interpretation"])
        )
    if "resolver" in out:
        # NX-329: stratul resolverului, un `ResolvedRef` per referință. `product_ids` gol poate
        # lipsi (not_found / stale nu poartă id-uri).
        out["resolver"] = [
            ResolvedRef.model_validate({"product_ids": [], **item}) for item in out["resolver"]
        ]
    if "checked" in out:
        # NX-330: stratul validatorului de proveniență, un `CheckedChange` per schimbare, în
        # ordinea interpretării. Eticheta e COMPACTĂ: `change` se ia din interpretare după
        # poziție (nu se repetă), iar `canonical_value`/`rejected` absente sunt nule.
        changes = out["interpretation"].changes if "interpretation" in out else []
        if len(out["checked"]) != len(changes):
            raise ValueError("stratul `checked` cere câte o etichetă per schimbare")
        out["checked"] = [
            CheckedChange.model_validate(
                {"canonical_value": None, "rejected": None, **item, "change": change}
            )
            for item, change in zip(out["checked"], changes, strict=True)
        ]
    if "ambiguity" in out:
        # NX-332: verdictul porții; `question` absentă = nulă (un verdict fără întrebare).
        out["ambiguity"] = AmbiguityDecision.model_validate({"question": None, **out["ambiguity"]})
    if "plan" in out:
        # NX-333: planul actului principal. Eticheta e compactă: `product_ids` gol, `search_args` și
        # `depends_on` nule pot lipsi; `search_args` trece prin `SearchArgs`, deci câmpurile
        # nescrise iau valorile implicite ale uneltei (ca planul real).
        out["plan"] = TurnPlan.model_validate(
            {"product_ids": [], "search_args": None, "depends_on": None, **out["plan"]}
        )
    if out.get("answer_policy") is not None:
        # NX-332: politica de răspuns; `null` = actul nu cere o judecată, `missing` gol poate lipsi.
        out["answer_policy"] = AnswerPolicy.model_validate({"missing": [], **out["answer_policy"]})
    return out


def load_journeys(directory: Path = JOURNEYS_DIR) -> list[Journey]:
    journeys: list[Journey] = []
    for path in sorted(directory.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{path.name}: schema_version != {SCHEMA_VERSION}")
        for raw in doc["journeys"]:
            turns = tuple(
                JourneyTurn(
                    user_input=t["user_input"],
                    expect=_parse_expect(t.get("expect", {})),
                    state_before=t.get("state_before", {}),
                    sources=t.get("sources", {}),
                    shown=tuple(t.get("shown", ())),
                )
                for t in raw["turns"]
            )
            journeys.append(
                Journey(
                    journey_id=raw["journey_id"],
                    pack=raw["pack"],
                    family=tuple(raw.get("family", ())),
                    locale=raw.get("locale", "ro"),
                    source=raw.get("source", ""),
                    turns=turns,
                )
            )
    return journeys


#: Pachetul REAL al pilotului, lângă cele patru de fixture: designul §G cere 5 pachete, iar al
#: cincilea e cel pe care rulează producția. Stă în `db/seed/`, nu se copiază.
SOLE_PACK = ROOT / "db" / "seed" / "domain_pack_sole_ro.json"
FIXTURE_PACKS: tuple[str, ...] = ("electronics", "fashion", "furniture", "gifts")


def load_pack(name: str) -> dict[str, Any]:
    """Pachetul `name` în forma fixture-urilor: `domain_pack` + metadatele kernelului."""
    if name == "sole-ro":
        return {
            "name": "sole-ro",
            "vertical": "ecommerce",
            "locale": "ro",
            "domain_pack": json.loads(SOLE_PACK.read_text(encoding="utf-8")),
        }
    return json.loads((PACKS_DIR / f"{name}.json").read_text(encoding="utf-8"))


# --- igiena corpusului: etichetele trebuie să fie ele însele conforme cu contractul -------------


def label_problems(journey: Journey) -> list[str]:
    """Ce e greșit în ETICHETE (nu în sistem). Un caz de test care încalcă el însuși contractul ar
    învăța kernelul regula greșită: o țintă nedeclarată (I22) sau un citat pe care clientul nu
    l-a scris (atunci schimbarea ar fi `inferred`, nu ce spune eticheta)."""
    out: list[str] = []
    for index, turn in enumerate(journey.turns):
        interpretation = turn.expect.get("interpretation")
        if interpretation is None:
            continue
        where = f"{journey.journey_id}#{index}"
        declared = {r.id for r in interpretation.references}
        for act in interpretation.acts:
            out += [
                f"{where}: ținta {t} nedeclarată (I22)" for t in act.targets if t not in declared
            ]
        for change in interpretation.changes:
            if change.relative_to and change.relative_to not in declared:
                out.append(f"{where}: relative_to {change.relative_to} nedeclarat (I22)")
            if fold_text(change.quote) not in fold_text(turn.user_input):
                out.append(f"{where}: citatul „{change.quote}” nu e în mesajul clientului")
        for resolved in turn.expect.get("resolver", []):
            if resolved.ref_id not in declared:
                out.append(f"{where}: rezolvarea {resolved.ref_id} n-are referință declarată")
    return out


# --- replay --------------------------------------------------------------------------------------

#: Ce rulează straturile: (journey, indexul turului) → traceul turului. Pașii 2-6 îl construiesc
#: strat cu strat (`tests/kernel/fixture_catalog.py`: `resolver_trace`, `checked_trace`,
#: `reducer_trace`).
TracePipeline = Callable[[Journey, int], KernelTrace]


@dataclass(frozen=True)
class TurnOutcome:
    journey_id: str
    turn: int
    divergence: Divergence | None

    @property
    def passed(self) -> bool:
        return self.divergence is None


def replay(journeys: Iterable[Journey], pipeline: TracePipeline) -> list[TurnOutcome]:
    outcomes: list[TurnOutcome] = []
    for journey in journeys:
        for index, turn in enumerate(journey.turns):
            trace = pipeline(journey, index)
            outcomes.append(
                TurnOutcome(journey.journey_id, index, first_divergence(turn.expect, trace))
            )
    return outcomes
