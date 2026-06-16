"""G8-1 — Harness golden: rulează inputuri prin pipeline-ul REAL și verifică
rezultatul față de așteptări (rută / fapte obligatorii / interdicții / niciodată
tăcere).

`evaluate_reply` e un checker PUR peste un `TurnContext` deja rulat — reutilizabil
în CI (LLM scriptat + stub-uri DB → zero OpenAI) ȘI în producție (pipeline real,
deps reale). Sursa cazurilor în v1 = fixture JSON (`tests/golden/cases.json`);
citirea per client din `golden_tests` + scrierea în `conversation_evals` = follow-up.

Niciun câmp nou pe TurnContext: harness-ul DOAR citește `ctx` după rulare
(principiul „un singur proprietar per câmp" rămâne intact).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.worker.runner import run_pipeline

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps, Stage


@dataclass(frozen=True)
class GoldenExpect:
    """Contractul de așteptări (oglindește `golden_tests.expected`).

    `route`: ruta cerută (valoarea enum, ex. ``"sales"``) sau ``None`` = nu se
    verifică ruta (cazurile gated înainte de triaj n-au rută). `must_include` /
    `forbidden`: subșiruri care TREBUIE / NU AU VOIE să apară în reply
    (case-insensitive, în limba cazului — principiul 11). `expect_reply`: ``True``
    ⇒ un reply produs (P6: niciodată tăcere); ``False`` ⇒ tăcere/halt intenționat.
    """

    route: str | None = None
    must_include: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    expect_reply: bool = True


@dataclass(frozen=True)
class GoldenResult:
    """Rezultatul evaluării unui caz + motivele eșecului (listate, pt mesajul de test)."""

    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GoldenCase:
    """Un caz golden. `fixtures` = artefacte CI (script LLM + catalog + categorii),
    OPACE pentru checker (le interpretează doar harness-ul de test, nu `evaluate_reply`)."""

    id: str
    input: str
    expect: GoldenExpect
    language: str = "ro"
    fixtures: dict[str, Any] = field(default_factory=dict)


def evaluate_reply(ctx: TurnContext, expect: GoldenExpect, *, case_id: str) -> GoldenResult:
    """Checker PUR peste un `ctx` deja rulat prin pipeline. Verifică, în ordine:
    rută (când e cerută), `expect_reply` (P6), `must_include` (toate prezente),
    `forbidden` (niciunul prezent). Nu mutează `ctx`; reutilizabil în CI și prod."""
    failures: list[str] = []

    if expect.route is not None:
        actual = ctx.route.route.value if ctx.route is not None else None
        if actual != expect.route:
            failures.append(f"route: așteptat {expect.route!r}, primit {actual!r}")

    text = ctx.reply.text if ctx.reply is not None else None
    if expect.expect_reply and text is None:
        failures.append("niciun reply (tăcere) — P6 încălcat")
    elif not expect.expect_reply and text is not None:
        failures.append(f"așteptam tăcere/halt, dar a ieșit reply: {text!r}")

    haystack = (text or "").lower()
    for fact in expect.must_include:
        if fact.lower() not in haystack:
            failures.append(f"fapt lipsă: {fact!r}")
    for bad in expect.forbidden:
        if bad.lower() in haystack:
            failures.append(f"interzis prezent: {bad!r}")

    return GoldenResult(case_id=case_id, passed=not failures, failures=failures)


async def run_case(
    ctx: TurnContext,
    deps: PipelineDeps,
    stages: list[Stage],
    expect: GoldenExpect,
    *,
    case_id: str,
) -> GoldenResult:
    """`run_pipeline` (pipeline-ul REAL) + `evaluate_reply`. `stages` și `deps` vin
    de la caller (CI: LLM scriptat + stub-uri DB; prod: deps reale)."""
    await run_pipeline(ctx, deps, stages)
    return evaluate_reply(ctx, expect, case_id=case_id)


def _expect_from(raw: dict[str, Any]) -> GoldenExpect:
    return GoldenExpect(
        route=raw.get("route"),
        must_include=list(raw.get("must_include", [])),
        forbidden=list(raw.get("forbidden", [])),
        expect_reply=bool(raw.get("expect_reply", True)),
    )


def load_cases(path: str | Path) -> list[GoldenCase]:
    """Încarcă cazurile din fixture JSON. Format: listă de obiecte
    ``{id, input, language?, expect{...}, fixtures{...}}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        GoldenCase(
            id=c["id"],
            input=c["input"],
            language=c.get("language", "ro"),
            expect=_expect_from(c.get("expect", {})),
            fixtures=c.get("fixtures", {}),
        )
        for c in data
    ]
