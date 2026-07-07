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

from src.models import Author, Direction, InboundMessage, Message, ProductRef
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
class GoldenTurn:
    """Un tur dintr-o conversație multi-tur: input + așteptări + fixtures (script LLM +
    catalog) proprii turului. Fixtures per-tur pentru că comportamentul modelului scriptat
    și așteptările diferă de la un tur la altul („mai ieftin", „compară primele două")."""

    input: str
    expect: GoldenExpect
    fixtures: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoldenCase:
    """Un caz golden. `fixtures` = artefacte CI (script LLM + catalog + categorii),
    OPACE pentru checker (le interpretează doar harness-ul de test, nu `evaluate_reply`).
    `turns` (opțional) = conversație MULTI-TUR pe aceeași conversație (state-ul curge între
    tururi); când e nevid, `input`/`expect`/`fixtures` de nivel-caz sunt ignorate."""

    id: str
    input: str
    expect: GoldenExpect
    language: str = "ro"
    fixtures: dict[str, Any] = field(default_factory=dict)
    turns: list[GoldenTurn] = field(default_factory=list)


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


def _displayed_from_reply(products: list[dict[str, Any]]) -> list[ProductRef]:
    """Reply.products (dict-uri) → ref-uri compacte pt state (P8). Defensiv: sare
    produsele fără id/name/price (ca `ConversationState.from_jsonb`)."""
    refs: list[ProductRef] = []
    for p in products:
        pid = p.get("product_id") or p.get("id")
        name = p.get("name")
        price = p.get("price")
        if pid and name is not None and price is not None:
            refs.append(ProductRef(product_id=str(pid), name=str(name), price=float(price)))
    return refs


def advance_turn(ctx: TurnContext, message: InboundMessage) -> None:
    """Mută `ctx` la turul URMĂTOR pe ACEEAȘI conversație. Pliază reply-ul turului curent
    în `state` + `history` (mimă merge-ul canonic al processor-ului,
    `src/worker/processor.py:566-598`), apoi resetează câmpurile per-tur.

    Ce PERSISTĂ (memoria scurtă verificată de cazurile multi-tur): `search_constraints` /
    `constraints` / `asked_intents` (scrise in-place de agent pe `ctx.state`),
    `displayed_products` (din reply), `cart` / `active_search` (din `state_patch`),
    `pending_question` (clarify) și `history` (ultimele mesaje). Ce se RESETEAZĂ: rută,
    retrieval, reply, halt, from_cache, state_patch, events, usage."""
    reply = ctx.reply
    # 1. pliază reply → state (displayed_products + pending_question), ca processor-ul
    if reply is not None and reply.products:
        ctx.state.displayed_products = _displayed_from_reply(reply.products)
    ctx.state.pending_question = reply.pending_question if reply is not None else None
    # 2. state_patch (Agent: cart / active_search) — ultimul, are întâietate (processor:597)
    patch = ctx.state_patch or {}
    if "active_search" in patch:
        ctx.state.active_search = patch["active_search"]
    if "cart" in patch:
        ctx.state.cart = list(patch["cart"] or [])
    # 3. history: inbound-ul turului + outbound-ul botului (context builder vede ultimele 8)
    ctx.history.append(
        Message(direction=Direction.INBOUND, author=Author.CONTACT, body=ctx.message.body)
    )
    if reply is not None and reply.text:
        ctx.history.append(
            Message(direction=Direction.OUTBOUND, author=Author.BOT, body=reply.text)
        )
    # 4. reset câmpuri per-tur
    ctx.message = message
    ctx.route = None
    ctx.retrieval = None
    ctx.reply = None
    ctx.halt = False
    ctx.from_cache = False
    ctx.state_patch = {}
    ctx.events = []
    ctx.usage = None


async def run_conversation(
    ctx: TurnContext,
    deps: PipelineDeps,
    stages: list[Stage],
    turns: list[GoldenTurn],
    *,
    case_id: str,
    on_turn: Any = None,
) -> list[GoldenResult]:
    """Rulează o conversație MULTI-TUR prin pipeline-ul REAL, păstrând `ctx.state` +
    `history` între tururi (via `advance_turn`). Un `GoldenResult` per tur (id-ul
    `<case_id>#<n>`). `on_turn(index, turn)` (opțional) e chemat ÎNAINTE de fiecare tur —
    CI-ul îl folosește ca să re-aplice stub-urile DB + să comute LLM-ul scriptat pe turul
    curent; în prod nu e nevoie (deps reale, un singur LLM)."""
    results: list[GoldenResult] = []
    for i, turn in enumerate(turns):
        if i > 0:
            advance_turn(ctx, InboundMessage(provider_msg_id=f"m-{case_id}-{i}", body=turn.input))
        if on_turn is not None:
            on_turn(i, turn)
        await run_pipeline(ctx, deps, stages)
        results.append(evaluate_reply(ctx, turn.expect, case_id=f"{case_id}#{i}"))
    return results


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


def _turn_from(raw: dict[str, Any]) -> GoldenTurn:
    return GoldenTurn(
        input=raw["input"],
        expect=_expect_from(raw.get("expect", {})),
        fixtures=raw.get("fixtures", {}),
    )


def load_cases(path: str | Path) -> list[GoldenCase]:
    """Încarcă cazurile din fixture JSON. Format: listă de obiecte
    ``{id, input, language?, expect{...}, fixtures{...}}`` sau, pentru cazuri MULTI-TUR,
    ``{id, language?, turns: [{input, expect{...}, fixtures{...}}, ...]}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[GoldenCase] = []
    for c in data:
        turns = [_turn_from(t) for t in c.get("turns", [])]
        cases.append(
            GoldenCase(
                id=c["id"],
                input=c.get("input", turns[0].input if turns else ""),
                language=c.get("language", "ro"),
                expect=_expect_from(c.get("expect", {})),
                fixtures=c.get("fixtures", {}),
                turns=turns,
            )
        )
    return cases
