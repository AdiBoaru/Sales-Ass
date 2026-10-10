"""Turul agentului unic (NX-396): bucla, răspunsul, faptele turului, starea și evenimentele.

`run_assistant_turn(ctx, deps)` e chemat din `agent_stage` pe conversațiile din procentul sticky.
Întoarce `True` când a pus un răspuns NOU; altfel restaurează contextul (`ContextSnapshot`, ca
kernelul) și turul merge pe calea de azi (P6), deci clientul nu primește niciodată tăcere.

Pe un tur servit, modulul e singurul scriitor al lui `ctx.reply`, `ctx.retrieval` și
`ctx.state_patch["assistant"]` (P3). Fraza de siguranță NX-173 o pune runner-ul, după, din
deciziile acumulate de `SafetyPolicy.gate` pe fiecare set de produse citit aici.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.assistant.gate import HINTS, Checked, check_answer
from src.assistant.memory import NOTES_MAX, Memory
from src.assistant.menus import load_menus
from src.assistant.mode import assistant_mode, canary_bucket  # noqa: F401 — re-export
from src.assistant.prompt import PROMPT_VERSION, instructions, render_view
from src.assistant.schemas import tool_schemas
from src.assistant.tools import Facts, Tools

log = logging.getLogger(__name__)

#: Motivele închise pentru care turul pleacă pe calea de azi.
FALLBACK_REASONS = frozenset(
    {
        "model_error",
        "model_unsupported",
        "round_timeout",
        "no_answer",
        "gate_failed",
        "timeout",
        "budget",
        "internal",
    }
)


def _diagnostic(event: Any) -> bool:
    """NX-401: evenimentele care rămân și când agentul cade: rundele lui și cele ale clientului de
    model (reîncercări, apeluri lente). Fără ele, primul blocaj din producție (42,5 s, apoi plasa)
    n-a putut fi explicat: ștergerea ramurii le luase cu ea."""
    return event.type == "assistant_round" or event.type.startswith("llm_")


#: Câte mesaje anterioare vede agentul (fereastra încărcată e de 20 cu cel curent).
HISTORY_MESSAGES = 19


class _Fallback(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Run:
    rounds: int = 0
    retries: int = 0
    gate_first: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    )
    #: Uneltele turului, puse aici imediat ce există: o cădere în mijlocul turului trebuie să
    #: vadă mutațiile deja făcute.
    tools: Any = None
    last_round_ms: int = 0
    last_round_attempts: int = 1


async def run_assistant_turn(ctx: Any, deps: Any) -> bool:
    from src.agent.interpreted_turn import ContextSnapshot  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    saved = ContextSnapshot.take(ctx)
    events_before = len(ctx.events)
    started = time.monotonic()
    run = _Run()
    try:
        await asyncio.wait_for(
            _serve(ctx, deps, settings, run), timeout=settings.assistant_turn_timeout_s
        )
        reason = None
    except _Fallback as e:
        reason = e.reason
    except TimeoutError:
        reason = "timeout"
    except Exception:  # noqa: BLE001 — orice eșec al agentului lasă turul pe calea de azi (P6)
        log.exception("assistant: turul a picat, calea de azi răspunde")
        reason = "internal"
    ms = round((time.monotonic() - started) * 1000)
    tools: Tools | None = run.tools
    if reason is None and ctx.reply is not None and ctx.reply != saved.fields.get("reply"):
        _emit_turn(ctx, run, tools, ms)
        return True
    facts = tools.facts if tools is not None else None
    mutated = bool(facts and facts.mutated)
    kept = [e for e in ctx.events[events_before:] if _diagnostic(e)]
    del ctx.events[events_before:]
    ctx.events.extend(kept)
    ctx.trace.pop("assistant", None)
    try:
        saved.restore(ctx)
    except Exception:  # noqa: BLE001
        log.exception("assistant: restaurarea contextului a picat")
    ctx.emit(
        "assistant_fallback",
        reason=reason or "no_answer",
        rounds=run.rounds,
        retries=run.retries,
        mutated=mutated,
        ms=ms,
    )
    if mutated and facts is not None:
        # O mutație deja făcută (coș, abonare) nu se pierde odată cu turul: starea și evenimentele
        # ei rămân, iar clientul află ce s-a întâmplat din fraza pachetului (rezerva P6). Calea de
        # azi n-ar mai avea voie s-o refacă (fără unelte de mutație), deci ar tăcea despre ea.
        ctx.state_patch.update(facts.mutation_patch)
        ctx.events.extend(facts.mutation_events)
        sentence = _mutation_sentence(ctx, facts)
        if sentence:
            ctx.set_reply(sentence, cacheable=False)
            return True
    return False


def _mutation_sentence(ctx: Any, facts: Facts) -> str | None:
    """Fraza pachetului pentru produsele puse în coș în turul căzut (`cart_added`), sau None."""
    from src.catalog.render_text import display_name  # noqa: PLC0415
    from src.domain.pack import kernel_sentence  # noqa: PLC0415

    pack = getattr(ctx.business, "domain_pack", None)
    template = kernel_sentence(pack, ctx.language, "cart_added")
    if not template:
        return None
    added = [pid for tool, pid in facts.mutations if tool == "cart_add"]
    names = [display_name(str(facts.rows.get(pid, {}).get("name") or "")) for pid in added]
    names = [n for n in names if n]
    if not names:
        return None
    return " ".join(template.replace("{product}", n) for n in dict.fromkeys(names))


async def _serve(ctx: Any, deps: Any, settings: Any, run: _Run) -> Tools:
    from src.agent.llm import (
        model_profile,  # noqa: PLC0415
        prompt_cache_scope,  # noqa: PLC0415
    )
    from src.config import card_slots, chip_slots  # noqa: PLC0415
    from src.privacy import make_safe  # noqa: PLC0415

    llm = deps.llm
    if llm is None:
        raise _Fallback("model_error")
    profile = model_profile(getattr(llm, "model_agent", "") or "")
    if profile is not None and not profile.responses_tools:
        # Modelul configurat nu poate rula unelte cu raționament pe `/v1/responses` (NX-320):
        # fiecare rundă ar da 400, deci turul pleacă direct pe calea de azi.
        raise _Fallback("model_unsupported")
    menus = await load_menus(deps, ctx.business)
    memory = Memory.from_state(getattr(ctx.state, "assistant", None))

    history, shown_sets, shown_ids = _history(ctx, memory)
    cart = [
        (memory.handle_of(str(line["product_id"])), int(line.get("quantity") or 1))
        for line in (ctx.state.cart or [])
        if line.get("product_id")
    ]
    tools = Tools(ctx, deps, menus, memory, shown_ids=shown_ids, facts=Facts())
    run.tools = tools
    await _rehydrate(tools)

    cart_total = _cart_total(cart, memory, tools.facts)
    message = make_safe(ctx.message.body or "").text
    grounded: set[float] = {cart_total} if cart_total is not None else set()

    system = instructions(
        store=str(getattr(ctx.business, "name", "") or ""),
        locale=str(ctx.language or getattr(ctx.business, "default_locale", "") or ""),
        families=menus.families,
        max_shown=card_slots(),
        chip_count=chip_slots(),
        recommend=settings.assistant_recommend_cards,
    )
    view = render_view(
        memory=memory,
        rows={
            h: tools.facts.rows[pid] for h, pid in memory.handles.items() if pid in tools.facts.rows
        },
        names=tools.names(),
        shown={h for h, pid in memory.handles.items() if pid in shown_ids},
        cart=cart,
        cart_total=cart_total,
        currency=_currency(ctx),
        safety=sorted(tools.policy.contexts),
        history=history,
        message=message,
    )
    schemas = tool_schemas(menus)
    items: list[Any] = [{"role": "user", "content": view}]
    budget = settings.assistant_max_rounds
    final: Checked | None = None
    last: Checked | None = None
    with prompt_cache_scope(f"{ctx.business.id}:{PROMPT_VERSION}"):
        while run.rounds < budget and final is None:
            run.rounds += 1
            # Ultima rundă permisă cere răspunsul: un tur care doar caută până la plafon ar pleca
            # pe calea de azi după ce a consumat tot timpul.
            choice: Any = (
                {"type": "function", "name": "answer"} if run.rounds >= budget else "required"
            )
            resp = await _round(ctx, llm, settings, run, system, items, schemas, choice)
            if resp is None:
                raise _Fallback("budget")
            _count_usage(run, resp)
            calls = []
            for item in getattr(resp, "output", None) or []:
                items.append(item.model_dump(exclude_none=True))
                if getattr(item, "type", None) == "function_call":
                    calls.append(item)
            ctx.emit(
                "assistant_round",
                n=run.rounds,
                outcome="ok",
                ms=run.last_round_ms,
                attempts=run.last_round_attempts,
                calls=[c.name for c in calls],
            )
            for call in calls:
                if final is not None:
                    # Răspunsul e acceptat: ce a mai cerut modelul în aceeași rundă (o mutație
                    # inclusiv) nu se execută, fiindcă textul servit nu-i poate reflecta rezultatul.
                    break
                try:
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if call.name == "answer" and final is None:
                    last = _check(args, tools, memory, shown_sets, grounded, card_slots, chip_slots)
                    if not last.rejected:
                        final = last
                        output = json.dumps({"ok": True})
                    else:
                        run.gate_first = run.gate_first or last.rejected
                        output = json.dumps(
                            {
                                "ok": False,
                                "rejected": last.rejected,
                                "fix": [HINTS.get(r, r) for r in last.rejected],
                            },
                            ensure_ascii=False,
                        )
                        if run.retries < 1:
                            run.retries += 1
                            budget += 1
                        else:
                            budget = run.rounds
                else:
                    output = await tools.run(call.name, args)
                items.append(
                    {"type": "function_call_output", "call_id": call.call_id, "output": output}
                )
    if final is None:
        raise _Fallback("gate_failed" if last is not None else "no_answer")
    compared = (
        await tools.detail_rows([memory.handles[h] for h in final.comparison["handles"]])
        if final.comparison
        else []
    )
    _reply(ctx, final, tools, memory, compared)
    memory.notes = final.notes or memory.notes
    # NX-407: ce a sugerat răspunsul ăsta, ca turul următor să nu ofere același lucru
    memory.offered = [make_safe(s).text for s in final.kept_suggestions]
    ctx.state_patch["assistant"] = memory.to_state()
    return tools


async def _round(
    ctx: Any,
    llm: Any,
    settings: Any,
    run: _Run,
    system: str,
    items: list[Any],
    schemas: list[dict[str, Any]],
    choice: Any,
) -> Any:
    """O rundă de model sub plafonul ei (`ASSISTANT_ROUND_TIMEOUT_S`), reîncercată O dată dacă
    furnizorul nu răspunde în timp. Un apel blocat nu mai consumă tot turul: a doua încercare
    pleacă imediat, iar dacă și ea stă, turul cade pe calea de azi cu motivul `round_timeout`."""
    for attempt in (1, 2):
        started = time.monotonic()
        run.last_round_attempts = attempt
        try:
            resp = await asyncio.wait_for(
                llm.respond_round(
                    instructions=system,
                    input=items,
                    tools=schemas,
                    effort=settings.llm_reasoning_effort_assistant,
                    tool_choice=choice,
                ),
                timeout=settings.assistant_round_timeout_s,
            )
        except TimeoutError:
            ms = round((time.monotonic() - started) * 1000)
            ctx.emit("assistant_round", n=run.rounds, outcome="timeout", ms=ms, attempts=attempt)
            log.warning("assistant: runda %s a depășit %s s", run.rounds, ms / 1000)
            continue
        except Exception as e:  # noqa: BLE001 — furnizor căzut, cerere refuzată, model nedeclarat
            ms = round((time.monotonic() - started) * 1000)
            ctx.emit(
                "assistant_round",
                n=run.rounds,
                outcome="error",
                ms=ms,
                attempts=attempt,
                error=type(e).__name__,
            )
            log.warning("assistant: runda de model a picat: %s", type(e).__name__)
            raise _Fallback("model_error") from e
        run.last_round_ms = round((time.monotonic() - started) * 1000)
        return resp
    raise _Fallback("round_timeout")


def _check(
    args: dict[str, Any],
    tools: Tools,
    memory: Memory,
    shown_sets: list[list[str]],
    grounded: set[float],
    card_slots: Any,
    chip_slots: Any,
) -> Checked:
    from src.assistant.gate import unit_aliases  # noqa: PLC0415
    from src.catalog.render_text import display_name  # noqa: PLC0415

    return check_answer(
        args,
        handles=dict(memory.handles),
        names=tools.names(),
        short_names={
            h: display_name(str(tools.facts.rows[pid].get("name") or ""))
            for h, pid in memory.handles.items()
            if pid in tools.facts.rows
        },
        rows=tools.facts.rows,
        shown_sets=shown_sets,
        grounded=grounded | {float(p) for p in tools.facts.order_prices},
        sources=tools.facts.sources,
        order_found=tools.facts.order_found,
        max_cards=card_slots(),
        max_suggestions=chip_slots(),
        notes_max=NOTES_MAX,
        facts=tools.facts_text(),
        units=unit_aliases(getattr(tools.ctx.business, "domain_pack", None)),
        product_facts={pid: tools.product_text(pid) for pid in tools.facts.rows},
    )


def _history(
    ctx: Any, memory: Memory
) -> tuple[list[tuple[str, str, list[str]]], list[list[str]], set[str]]:
    """Mesajele ANTERIOARE (redactate), cu handle-urile produselor arătate la fiecare răspuns,
    seturile arătate (pentru totaluri, NX-391) și id-urile tuturor produselor arătate."""
    from src.models import Direction  # noqa: PLC0415
    from src.privacy import make_safe  # noqa: PLC0415

    prior = list(ctx.history[:-1])[-HISTORY_MESSAGES:] if ctx.history else []
    lines: list[tuple[str, str, list[str]]] = []
    sets: list[list[str]] = []
    shown: set[str] = set()
    for m in prior:
        body = make_safe((m.body or "").strip()).text
        if m.direction == Direction.INBOUND:
            if body:
                lines.append(("client", body, []))
            continue
        ids = [
            str(r.get("product_id"))
            for r in ((m.payload or {}).get("shown") or [])
            if isinstance(r, dict) and r.get("product_id")
        ]
        handles = [memory.handle_of(pid, touch=False) for pid in ids]
        if ids:
            sets.append(ids)
            shown.update(ids)
        if body or handles:
            lines.append(("assistant", body, handles))
    displayed = [str(p.product_id) for p in (ctx.state.displayed_products or [])]
    for pid in displayed:
        memory.handle_of(pid, touch=False)
    if displayed:
        shown.update(displayed)
        if displayed not in sets:
            sets.append(displayed)
    return lines, sets, shown


async def _rehydrate(tools: Tools) -> None:
    """Faptele PROASPETE ale produselor cunoscute, într-o singură citire, prin siguranță. Un produs
    blocat sau dispărut rămâne în memorie, dar nu se mai arată și nu poate deveni card.

    `evaluate`, nu `gate`: produsele de mai devreme nu sunt un set servit în ACEST tur, deci
    excluderile lor nu intră în decizia turului. Altfel, într-o conversație cu un context de
    siguranță, un simplu «mersi» după un produs exclus ar părea un set golit de noi, iar
    `safety_compose.enforce` ar înlocui răspunsul cu fraza de excludere."""
    from src.db.queries.catalog import get_products_pool_by_ids  # noqa: PLC0415

    ids = list(dict.fromkeys(tools.memory.handles.values()))
    if not ids:
        return
    async with tools.deps.db("assistant_known") as conn:
        rows = await get_products_pool_by_ids(
            conn, tools.ctx.business.id, ids, pool=len(ids), respect_content_status=False
        )
    for r in tools.policy.evaluate(rows, purpose="rehydrate").kept:
        tools.facts.rows[str(r["id"])] = r


def _currency(ctx: Any) -> str:
    """Moneda tenantului, așa cum o scrie pachetul (P11: nu e o constantă în cod)."""
    pack = getattr(ctx.business, "domain_pack", None)
    return str(getattr(pack, "currency", None) or "")


def _cart_total(cart: list[tuple[str, int]], memory: Memory, facts: Facts) -> float | None:
    total = 0.0
    for h, qty in cart:
        row = facts.rows.get(memory.handles.get(h, ""))
        if row is None or row.get("price") is None:
            return None
        total += float(row["price"]) * qty
    return round(total, 2) if cart else None


def _count_usage(run: _Run, resp: Any) -> None:
    u = getattr(resp, "usage", None)
    if u is None:
        return
    run.tokens["input"] += getattr(u, "input_tokens", 0) or 0
    run.tokens["output"] += getattr(u, "output_tokens", 0) or 0
    details_in = getattr(u, "input_tokens_details", None)
    run.tokens["cached"] += getattr(details_in, "cached_tokens", 0) or 0
    details_out = getattr(u, "output_tokens_details", None)
    run.tokens["reasoning"] += getattr(details_out, "reasoning_tokens", 0) or 0


# --- răspunsul -----------------------------------------------------------------------------------


def _reply(
    ctx: Any, final: Checked, tools: Tools, memory: Memory, compared: list[dict[str, Any]]
) -> None:
    """`ctx.reply` + `ctx.retrieval` pe contractul web v1, neschimbat: comparația pe tabelul
    widgetului, cardurile pe calea bogată, altfel text. Cardurile poartă numele distinct (R4)."""
    from src.agent.voice import naturalize  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415
    from src.models import RetrievalResult, RichReply  # noqa: PLC0415
    from src.worker import compose as wc  # noqa: PLC0415

    names = tools.names()
    text = naturalize(final.text) or final.text
    # NX-403: sfatul stă sub carduri (paragraful „cum alegi” al widgetului, `education`), sub
    # tabelul comparației, sau după text când turul n-are nici carduri, nici comparație.
    advice = naturalize(final.advice) or final.advice
    suggestions = [naturalize(s) or s for s in final.kept_suggestions]
    lang = ctx.language
    cited: list[str] = []

    if final.comparison:
        rows = compared
        pack = getattr(ctx.business, "domain_pack", None)
        table = wc.build_comparison(rows, lang, pack.comparison_facets if pack else ())
        if table is not None:
            by_id = {pid: h for h, pid in memory.handles.items()}
            for col in table.columns:
                col.name = names.get(by_id.get(str(col.product_id), ""), col.name)
            intro = naturalize(str(final.comparison.get("intro") or "")) or ""
            table.intro = "\n\n".join(x for x in (text, intro) if x) or None
            table.subtitle = naturalize(str(final.comparison.get("subtitle") or "")) or None
            closing = naturalize(str(final.comparison.get("closing") or ""))
            table.closing = [x for x in (closing, advice) if x]
            flat = "\n\n".join(x for x in (table.intro or "", *table.closing) if x)
            ctx.set_comparison_reply(
                table, text=flat, products=wc.comparison_cards(table), chips=suggestions
            )
            cited = [str(r["id"]) for r in rows]
        else:
            # Tabelul nu s-a putut construi (un produs blocat sau dispărut între timp): textele
            # comparației rămân ale răspunsului, nu se pierd.
            extra = (final.comparison.get(k) for k in ("intro", "subtitle", "closing"))
            text = "\n\n".join(x for x in (text, *(naturalize(str(e or "")) for e in extra)) if x)
            text, advice = "\n\n".join(x for x in (text, advice) if x), ""

    if ctx.reply is None and final.cards:
        ids = [memory.handles[c["handle"]] for c in final.cards]
        rows, _hint = tools.gate([tools.facts.rows[i] for i in ids], "retrieval_final")
        by_id = {str(r["id"]): r for r in rows}
        routine = getattr(ctx, "routine", None)
        steps = routine.by_product() if routine is not None else {}
        items: list[Any] = []
        kinds: dict[str, str | None] = {}
        for c in final.cards:
            pid = memory.handles[c["handle"]]
            row = by_id.get(pid)
            if row is None or pid in kinds:
                continue
            reason = naturalize(c["reason"]) or None
            item, kinds[pid] = wc.hydrated_item(ctx, row, reason=reason, routine_by_product=steps)
            item.name = names.get(c["handle"], item.name)
            items.append(item)
        if items:
            items = wc.suppress_common_badges(ctx, items, kinds)
            rich = RichReply(
                intro=text,
                items=items,
                pick=None,
                education=advice or None,
                chips=wc._suggestion_chips(suggestions),
                disclaimer=wc.disclaimer(lang) if get_settings().ai_disclaimer_enabled else None,
            )
            ctx.set_rich_reply(rich, text=wc.flatten(rich, lang), products=wc.card_products(items))
            ctx.state_patch["active_search"] = None
            cited = [it.product_id for it in items]

    if ctx.reply is None:
        ctx.set_reply("\n\n".join(x for x in (text, advice) if x), cacheable=False)
        ctx.reply.suggestions = suggestions

    ctx.retrieval = RetrievalResult(
        products=[tools.facts.rows[i] for i in cited if i in tools.facts.rows],
        source="assistant",
        catalog_read=tools.facts.catalog_read,
        read_beyond_catalog=tools.facts.read_beyond_catalog,
    )
    # NX-404: unde stătea fiecare card în lista clasată a turului (poziție, din câți candidați) și
    # starea nevoii lui. Supraveghere, nu blocaj: un produs mai jos poate fi alegerea corectă pentru
    # o cerință pe care rankingul n-o vede („fără parfum”). `None` = produs fără loc în acest tur.
    ranks = [tools.facts.ranks.get(memory.handles[c["handle"]]) for c in final.cards]
    ctx.trace["assistant"] = {
        "prompt": PROMPT_VERSION,
        "card_positions": [r["position"] if r else None for r in ranks],
        "card_needs": [r["need"] if r else None for r in ranks],
        "candidates": max((r["of"] for r in ranks if r), default=None),
        "cards": [c["handle"] for c in final.cards],
        "comparison": bool(final.comparison),
        "advice": bool(final.advice),
        "suggestions": len(suggestions),
        "suggestions_dropped": final.dropped_suggestions,
    }


def _emit_turn(ctx: Any, run: _Run, tools: Tools | None, ms: int) -> None:
    calls = tools.calls if tools is not None else []
    trace = ctx.trace.get("assistant", {})
    ctx.emit(
        "assistant_turn",
        served=True,
        rounds=run.rounds,
        retries=run.retries,
        gate_first=run.gate_first,
        tools=[c["tool"] for c in calls],
        refused=sum(1 for c in calls if c.get("refused")),
        routine=any(c["tool"] == "routine_plan" for c in calls),
        mutated=bool(tools and tools.facts.mutated),
        cards=len(trace.get("cards", [])),
        card_positions=trace.get("card_positions", []),
        card_needs=trace.get("card_needs", []),
        candidates=trace.get("candidates"),
        comparison=trace.get("comparison", False),
        n_suggestions=trace.get("suggestions", 0),
        input_tokens=run.tokens["input"],
        cached_tokens=run.tokens["cached"],
        reasoning_tokens=run.tokens["reasoning"],
        ms=ms,
    )
    ctx.trace["assistant"] = {**trace, "rounds": run.rounds, "retries": run.retries}
