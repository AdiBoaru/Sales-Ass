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
    Câmpurile `expected_*` sunt opționale și cresc gate-ul de la "textul pare OK"
    la "pipeline-ul a folosit instrumentele/datele/constrângerile corecte".
    """

    route: str | None = None
    must_include: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    expect_reply: bool = True
    expected_tools: list[str] = field(default_factory=list)
    expected_product_ids: list[str] = field(default_factory=list)
    expected_constraints: dict[str, Any] = field(default_factory=dict)


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


def _ctx_tool_names(ctx: TurnContext) -> list[str]:
    """Tool-urile chemate în tur, din evenimentele emise de agent/tool executor."""
    names: list[str] = []
    for ev in ctx.events:
        if ev.type != "tool_call":
            continue
        name = ev.properties.get("tool") or ev.properties.get("name")
        if name:
            names.append(str(name))
    return names


def _ctx_product_ids(ctx: TurnContext) -> set[str]:
    """Product IDs observabile în tur: retrieval + reply payload (text/rich/comparison)."""
    ids: set[str] = set()
    if ctx.retrieval is not None:
        for p in ctx.retrieval.products:
            pid = p.get("product_id") or p.get("id")
            if pid:
                ids.add(str(pid))
    if ctx.reply is not None:
        for p in ctx.reply.products or []:
            pid = p.get("product_id") or p.get("id")
            if pid:
                ids.add(str(pid))
        if ctx.reply.rich is not None:
            ids.update(str(it.product_id) for it in ctx.reply.rich.items if it.product_id)
        if ctx.reply.comparison is not None:
            ids.update(
                str(col.product_id) for col in ctx.reply.comparison.columns if col.product_id
            )
    return ids


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

    if expect.expected_tools:
        tools = _ctx_tool_names(ctx)
        for tool in expect.expected_tools:
            if tool not in tools:
                failures.append(f"tool lipsă: {tool!r} (chemate: {tools!r})")

    if expect.expected_product_ids:
        product_ids = _ctx_product_ids(ctx)
        for pid in expect.expected_product_ids:
            if pid not in product_ids:
                failures.append(f"product_id lipsă: {pid!r} (observate: {sorted(product_ids)!r})")

    if expect.expected_constraints:
        constraints = ctx.state.search_constraints or {}
        for key, expected_value in expect.expected_constraints.items():
            actual_value = constraints.get(key)
            if actual_value != expected_value:
                failures.append(
                    f"constraint {key!r}: așteptat {expected_value!r}, primit {actual_value!r}"
                )

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
        expected_tools=list(raw.get("expected_tools", [])),
        expected_product_ids=list(raw.get("expected_product_ids", [])),
        expected_constraints=dict(raw.get("expected_constraints", {})),
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
