"""NX-320 felia 1 — replay al PRIMEI runde de tool-calling: fără raționament vs cu raționament.

Întrebarea: dacă modelul ar gândi când alege argumentele căutării (raft, nevoi, preț maxim), ar
alege mai puține filtre greșite? Pe `chat.completions` nu poate: `gpt-6-luna` acceptă unelte doar cu
`reasoning_effort=none`. Pe `/v1/responses` poate. Decizia de a muta bucla live se ia pe raportul
ăsta, nu pe intuiție (D15), cu regula PRE-ÎNREGISTRATĂ în `GO_RULE`.

Brațe, pe ACELAȘI input, în ordine amestecată per tur:
  * `chat:none`       — exact cererea de azi (`LLMClient.tool_round(via="chat")` → `_chat`);
  * `responses:none`  — CONTROL: alt endpoint, tot fără raționament. Separă efectul endpointului de
                        efectul raționamentului; fără el, o diferență n-ar avea cauză atribuibilă;
  * `responses:<efort>` pentru fiecare efort din `--efforts` (implicit `low`).

Ce reface, per tur real cu `search_products` (evenimentul `tool_call`), prin funcțiile PRODUCȚIEI:
  * system: `build_agent_system` + `_apply_turn_profile` (sufixul de profil, ca pe v1);
  * unelte: `tool_loop_tools(..., unrouted=True)` (NX-297: turul nu mai e clasificat);
  * mesaj: `tool_loop_user_parts`, cu istoricul de atunci (`history_at`, comun cu NX-312) și
    produsele afișate refăcute din `recommended` al turului ANTERIOR (ancora lui «mai ieftin»).

Ce NU reface, declarat (identic pe toate brațele, deci comparația rămâne pe același input):
  * stiva de constrângeri (`search_constraints`) și subiectul NX-314 de atunci — starea din DB e
    cea de ACUM, nu cea de la momentul turului;
  * profilul clientului, rezumatul, memoria de fapte, contextul de pagină;
  * catalogul de atunci (evaluarea rulează pe catalogul de ACUM).

Cum se judecă un braț: argumentele fiecărui `search_products` cerut trec prin căutarea REALĂ
(`search_products_tool`, DB read-only, fără model: brațul vector e stins), iar verdictele vin din
evenimentele ei, adică din exact porțile de producție: `guessed_filter_probe` (NX-313),
`price_bound_provenance` (NX-319), `category_off_menu` (cd98a513). Nimic nu se reimplementează.
Mutațiile (coș, checkout, abonare) NU se execută niciodată; se numără doar ca nume.

**Consumă credite OpenAI.** Fără `--yes` se oprește după ce spune câte apeluri ar face.
`--dry-run` reface tot inputul, cu ZERO apeluri de model.

    PYTHONPATH=. python scripts/nx320_tool_round_replay.py --business sole-ro --dry-run
    PYTHONPATH=. python scripts/nx320_tool_round_replay.py --business sole-ro --limit 40 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent import usage  # noqa: E402
from src.models import Contact, ConversationState, InboundMessage, TurnContext  # noqa: E402

#: Baseline-ul: cererea de azi a buclei de producție.
BASELINE = "chat:none"
#: Controlul de endpoint (vezi docstring-ul modulului).
CONTROL = "responses:none"
EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORTS = ("low",)
#: Uneltele care SCRIU. Nu se execută niciodată în replay, indiferent ce cere modelul.
MUTATIONS = frozenset({"cart_add", "checkout_link", "subscribe_back_in_stock"})

#: Regula de decizie, PRE-ÎNREGISTRATĂ în card (NX-320) înainte de orice rulare. Nu se ajustează
#: după ce se văd cifrele: altfel „regula" devine o descriere a rezultatului.
GO_RULE = {
    "min_error_reduction": 0.50,  # (contrazise + preț fără sursă) scad cu ≥ 50% față de baseline
    "max_p50_increase_ms": 3000,  # iar runda nu devine mai lentă cu peste 3 s la p50
    "min_baseline_errors": 4,  # sub atâtea greșeli în baseline, o reducere de 50% e zgomot
}


@dataclass(frozen=True)
class ReplayCase:
    turn_id: str
    query: str
    language: str
    history: tuple[Any, ...]
    displayed: tuple[dict[str, Any], ...]


@dataclass
class ArmResult:
    arm: str
    ok: bool
    ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int | None = None
    cost_usd: float = 0.0
    error: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""


def arms_for(efforts: tuple[str, ...]) -> tuple[str, ...]:
    return (BASELINE, CONTROL, *(f"responses:{e}" for e in efforts))


# --- construcția rundei: exclusiv prin funcțiile producției ---------------------------------------


def make_ctx(business: Any, case: ReplayCase) -> TurnContext:
    ctx = TurnContext(
        turn_id=f"replay-{case.turn_id}",
        business=business,
        contact=Contact(id="replay", business_id=business.id),
        message=InboundMessage(provider_msg_id="replay", body=case.query, channel_kind="webchat"),
        conversation_id="replay",
        state=ConversationState.from_jsonb({"displayed_products": list(case.displayed)}),
    )
    ctx.language = case.language
    ctx.history = list(case.history)
    return ctx


def build_round(ctx: TurnContext, inp: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """(system, user, tools) — exact ce ar trimite `agent_stage` în prima rundă, minus ce e declarat
    în docstring-ul modulului."""
    from src.agent import prompt_builder  # noqa: PLC0415
    from src.worker.context import context_blocks, conversation_transcript  # noqa: PLC0415
    from src.worker.stages.agent import (  # noqa: PLC0415
        _apply_turn_profile,
        tool_loop_tools,
        tool_loop_user_parts,
    )

    _names, tools = tool_loop_tools(ctx.business, "sales", unrouted=True)
    system, tools = _apply_turn_profile(ctx, prompt_builder.build_agent_system(inp), tools)
    user = tool_loop_user_parts(
        language=ctx.language,
        history=conversation_transcript(ctx.history),
        hints="",
        context=context_blocks(ctx),
        query=ctx.message.body,
    ).legacy()
    return system, user, tools


def _parse(raw: str) -> dict[str, Any]:
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


async def run_arm(llm: Any, arm: str, system: str, user: str, tools: list) -> ArmResult:
    """Un apel pe calea REALĂ a adaptorului (buget, retry, sampling, usage), fără execuție."""
    via, effort = arm.split(":", 1)
    acc, token = usage.push()
    started = perf_counter()
    try:
        rnd = await llm.tool_round(system, user, tools, via=via, effort=effort)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001 — un apel picat e un rezultat, nu un motiv de oprire
        rnd, ok, err = None, False, type(e).__name__
    finally:
        usage.pop(token)
    row = acc.call_rows[-1] if acc.call_rows else {}
    return ArmResult(
        arm=arm,
        ok=ok,
        ms=round((perf_counter() - started) * 1000, 1),
        tokens_in=acc.tokens_in,
        tokens_out=acc.tokens_out,
        reasoning_tokens=row.get("reasoning_tokens"),
        cost_usd=round(acc.cost_usd, 6),
        error=err,
        calls=[{"name": n, "args": _parse(a)} for n, a in (rnd.calls if rnd else ())],
        text=(rnd.text if rnd else "")[:400],
    )


# --- evaluarea: căutarea REALĂ, verdictele din evenimentele ei ------------------------------------


async def evaluate_search(business: Any, case: ReplayCase, deps: Any, args: dict) -> dict:
    """Rulează `search_products` cu argumentele modelului pe un context curat și citește porțile."""
    from src.catalog.render_text import display_name  # noqa: PLC0415
    from src.tools.base import TOOL_REGISTRY  # noqa: PLC0415

    ctx = make_ctx(business, case)
    try:
        result = await TOOL_REGISTRY["search_products"](ctx, deps, dict(args))
    except Exception as e:  # noqa: BLE001 — o căutare care crapă e o constatare, nu un abort
        return {"error": type(e).__name__}
    ev: dict[str, dict[str, Any]] = {}
    for e in ctx.events:
        ev.setdefault(e.type, e.properties)
    probe = ev.get("guessed_filter_probe") or {}
    price = ev.get("price_bound_provenance")
    return {
        "category": args.get("category"),
        "concerns": args.get("concerns"),
        "price_max": args.get("price_max"),
        "sort_mode": args.get("sort_mode"),
        "category_guessed": bool(probe.get("category_guessed")),
        "facets_guessed": bool(probe.get("facets_guessed")),
        "contradicted": probe.get("verdict") == "contradicted",
        "price_unsourced": price is not None and not price.get("kept", True),
        "off_menu": "category_off_menu" in ev,
        "lexical_step": (ev.get("product_search") or {}).get("lexical_step"),
        "served": len(result.products or []),
        "top": [display_name(str(p.get("name")))[:48] for p in (result.products or [])][:4],
    }


# --- agregare + verdict (PURE, testate) -----------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarize(entries: list[dict[str, Any]], arms: tuple[str, ...]) -> dict[str, Any]:
    """Per braț: latență, tokeni de raționament, și greșelile de argumente pe CĂUTĂRI (nu pe ture:
    un tur cu două căutări are două șanse să greșească)."""
    out: dict[str, Any] = {}
    for arm in arms:
        res = [e["results"][arm] for e in entries if arm in e["results"]]
        ok = [r for r in res if r["ok"]]
        searches = [s for r in ok for s in r.get("evals", []) if "error" not in s]
        reasoning = [r["reasoning_tokens"] for r in ok if r.get("reasoning_tokens") is not None]
        out[arm] = {
            "rounds": len(res),
            "ok": len(ok),
            "ms_p50": _pct([r["ms"] for r in ok], 0.5),
            "ms_p90": _pct([r["ms"] for r in ok], 0.9),
            "reasoning_tokens_p50": statistics.median(reasoning) if reasoning else None,
            "cost_usd_total": round(sum(r["cost_usd"] for r in res), 4),
            "no_tool_rounds": sum(1 for r in ok if not r["calls"]),
            "mutations_requested": sum(1 for r in ok for c in r["calls"] if c["name"] in MUTATIONS),
            "searches": len(searches),
            "category_sent": sum(1 for s in searches if s["category"]),
            "category_guessed": sum(1 for s in searches if s["category_guessed"]),
            "contradicted": sum(1 for s in searches if s["contradicted"]),
            "price_unsourced": sum(1 for s in searches if s["price_unsourced"]),
            "off_menu": sum(1 for s in searches if s["off_menu"]),
            "empty": sum(1 for s in searches if s["served"] == 0),
            "search_errors": sum(1 for r in ok for s in r.get("evals", []) if "error" in s),
        }
    return out


def errors_of(row: dict[str, Any]) -> int:
    """Greșelile pe care le judecă regula: raft contrazis de cerere + preț maxim fără sursă."""
    return int(row["contradicted"]) + int(row["price_unsourced"])


def verdict(summary: dict[str, Any], candidate: str) -> dict[str, Any]:
    """`GO` / `NO-GO` / `INSUFFICIENT` pe `GO_RULE`. INSUFFICIENT ≠ NO-GO (ca la NX-238/246):
    „n-am avut destule greșeli în baseline ca să măsor o reducere" nu e „am măsurat și a picat"."""
    base, cand = summary[BASELINE], summary[candidate]
    base_err, cand_err = errors_of(base), errors_of(cand)
    if base_err < GO_RULE["min_baseline_errors"]:
        return {
            "verdict": "INSUFFICIENT",
            "why": f"baseline are {base_err} greșeli, regula cere minimum "
            f"{GO_RULE['min_baseline_errors']} ca o reducere să însemne ceva",
            "baseline_errors": base_err,
            "candidate_errors": cand_err,
        }
    reduction = 1 - cand_err / base_err
    delta = (cand["ms_p50"] or 0) - (base["ms_p50"] or 0)
    go = (
        reduction >= GO_RULE["min_error_reduction"]
        and delta <= GO_RULE["max_p50_increase_ms"]
        and cand["ok"] == cand["rounds"]
    )
    return {
        "verdict": "GO" if go else "NO-GO",
        "baseline_errors": base_err,
        "candidate_errors": cand_err,
        "error_reduction": round(reduction, 3),
        "p50_delta_ms": round(delta, 1),
        "candidate_failed_rounds": cand["rounds"] - cand["ok"],
    }


def blind_pairs(
    entries: list[dict[str, Any]], candidate: str, *, seed: int
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Perechi OARBE baseline vs candidat, pe ARGUMENTE și pe primele produse aduse de ele."""
    rng = random.Random(seed)
    pairs: list[dict[str, Any]] = []
    key: dict[str, dict[str, str]] = {}
    for i, e in enumerate(entries, 1):
        res = e["results"]
        if BASELINE not in res or candidate not in res:
            continue
        sides = [(BASELINE, _render(res[BASELINE])), (candidate, _render(res[candidate]))]
        rng.shuffle(sides)
        pid = f"{e['turn_id'][:8]}-{i:03d}"
        pairs.append({"pair_id": pid, "turn_id": e["turn_id"], "A": sides[0][1], "B": sides[1][1]})
        key[pid] = {"A": sides[0][0], "B": sides[1][0]}
    return pairs, key


def _render(r: dict[str, Any]) -> str:
    if not r["ok"]:
        return f"(apel eșuat: {r['error']})"
    if not r["calls"]:
        return f"(nicio unealtă; text: {r['text'][:200] or '-'})"
    lines = []
    for c in r["calls"]:
        a = {k: v for k, v in c["args"].items() if v not in (None, [], "")}
        lines.append(f"- `{c['name']}` {json.dumps(a, ensure_ascii=False)}")
    for s in r.get("evals", []):
        if "top" in s:
            lines.append(f"  → {', '.join(s['top']) or '(zero produse)'}")
    return "\n".join(lines)


def pairs_markdown(pairs: list[dict[str, Any]], queries: dict[str, str]) -> str:
    lines = [
        "# NX-320 felia 1 — perechi oarbe (prima rundă de tool-calling)",
        "",
        "Pentru fiecare pereche: care căutare servește mai bine CEREREA clientului? `A`, `B` sau",
        "`egal`. Cheia e în `pairs_key.json`; deschide-o doar după ce ai terminat.",
        "",
    ]
    for p in pairs:
        lines += [
            f"## {p['pair_id']}",
            "",
            f"**Clientul:** {queries.get(p['turn_id'], '')}",
            "",
            "**A**",
            "",
            p["A"],
            "",
            "**B**",
            "",
            p["B"],
            "",
            "Verdict: ",
            "",
        ]
    return "\n".join(lines)


# --- DB: ture reale (read-only, tenant-scoped) ---------------------------------------------------


async def load_cases(conn: Any, admin: Any, bid: str, *, days: int, limit: int) -> list:
    """Turele cu cel puțin un `search_products`, cele mai recente primele.

    `analytics_events` se citește pe `admin` cu `business_id` explicit (`bot_runtime` are doar
    INSERT pe tabel, prin design); restul, pe conexiunea tenant."""
    from scripts.nx312_rich_effort_replay import history_at  # noqa: PLC0415

    turn_ids = [
        r["turn_id"]
        for r in await admin.fetch(
            """
            select turn_id::text as turn_id, max(created_at) as at
            from analytics_events
            where business_id = $1::uuid and event_type = 'tool_call'
              and properties->>'name' = 'search_products'
              and created_at > now() - make_interval(days => $2)
            group by turn_id
            order by at desc
            limit $3
            """,
            bid,
            days,
            limit,
        )
    ]
    cases: list[ReplayCase] = []
    for tid in turn_ids:
        t = await conn.fetchrow(
            """
            select conversation_id::text as conversation_id, client_text,
                   coalesce(language, 'ro') as language, created_at
            from conversation_traces
            where business_id = $1::uuid and turn_id = $2::uuid
            """,
            bid,
            tid,
        )
        if t is None or not (t["client_text"] or "").strip():
            continue
        prev = await conn.fetchval(
            """
            select recommended from conversation_traces
            where business_id = $1::uuid and conversation_id = $2::uuid and created_at < $3
            order by created_at desc
            limit 1
            """,
            bid,
            t["conversation_id"],
            t["created_at"],
        )
        prev = json.loads(prev) if isinstance(prev, str) else prev
        cases.append(
            ReplayCase(
                turn_id=tid,
                query=t["client_text"],
                language=t["language"],
                history=tuple(await history_at(conn, bid, t["conversation_id"], t["created_at"])),
                displayed=tuple(p for p in (prev or []) if isinstance(p, dict)),
            )
        )
    return cases


async def replay(prepared, llm, arms, business, deps, *, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    entries = []
    for case, (system, user, tools) in prepared:
        order = list(arms)
        rng.shuffle(order)  # al doilea apel prinde cache-ul primului: ordinea fixă ar favoriza unul
        results: dict[str, Any] = {}
        for arm in order:
            r = await run_arm(llm, arm, system, user, tools)
            evals = []
            for c in r.calls:
                if c["name"] == "search_products":
                    evals.append(await evaluate_search(business, case, deps, c["args"]))
            results[arm] = {**r.__dict__, "evals": evals}
        entries.append({"turn_id": case.turn_id, "order": order, "results": results})
        print(
            f"  {case.turn_id[:8]}  "
            + "  ".join(f"{a}={len(results[a]['calls'])}u/{results[a]['ms']:.0f}ms" for a in arms)
        )
    return entries


class _TenantDb:
    """`deps.db(op)` = checkout tenant-scoped, ca în producție (RLS pe `bot_runtime`)."""

    def __init__(self, business_id: str) -> None:
        self._bid = business_id

    def __call__(self, _op: str = "unlabeled"):
        from src.db.connection import tenant_conn  # noqa: PLC0415

        return tenant_conn(self._bid)


def _parse_efforts(raw: str) -> tuple[str, ...]:
    out = tuple(e.strip() for e in raw.split(",") if e.strip())
    bad = [e for e in out if e not in EFFORTS]
    if bad or not out or len(set(out)) != len(out):
        raise SystemExit(f"--efforts: valori distincte din {EFFORTS}, nu {raw!r}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=40, help="câte ture reale (cele mai recente)")
    ap.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS))
    ap.add_argument("--seed", type=int, default=320)
    ap.add_argument("--dry-run", action="store_true", help="reface inputul, zero apeluri de model")
    ap.add_argument("--yes", action="store_true", help="confirmă că rularea consumă credite")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "nx320")
    args = ap.parse_args()
    arms = arms_for(_parse_efforts(args.efforts))

    import src.tools.catalog_tools  # noqa: F401, PLC0415 — înregistrează `search_products`
    from scripts.nx312_rich_effort_replay import _TenantDeps  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415
    from src.worker.runner import PipelineDeps  # noqa: PLC0415
    from src.worker.stages.agent import _load_prompt_inputs  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as admin:
            bid = await admin.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", args.business
            )
            if bid is None:
                raise SystemExit(f"tenant necunoscut: {args.business!r}")
            async with tenant_conn(bid) as conn:
                business = await load_business(conn, bid)
                cases = await load_cases(conn, admin, bid, days=args.days, limit=args.limit)
        prepared = []
        for case in cases:
            ctx = make_ctx(business, case)
            inp = await _load_prompt_inputs(_TenantDeps(bid), ctx)
            prepared.append((case, build_round(ctx, inp)))

        calls = len(prepared) * len(arms)
        print(f"ture reale: {len(prepared)}  brațe: {arms}  apeluri de model: {calls}")
        if not prepared:
            return
        if args.dry_run:
            for case, (system, user, tools) in prepared:
                print(
                    f"  {case.turn_id[:8]}  afișate={len(case.displayed)}  unelte={len(tools)}  "
                    f"system={len(system)}  user={len(user)}  «{case.query[:50]}»"
                )
            return
        if not args.yes:
            print("Rularea consumă credite OpenAI. Repornește cu --yes (sau --dry-run).")
            return

        from src.agent.llm import get_llm  # noqa: PLC0415

        llm = get_llm()
        if llm is None:
            raise SystemExit("lipsește OPENAI_API_KEY")
        deps = PipelineDeps(llm=None, db=_TenantDb(bid))  # căutarea nu cheamă niciun model
        entries = await replay(prepared, llm, arms, business, deps, seed=args.seed)
    finally:
        await close_pool()

    summary = summarize(entries, arms)
    verdicts = {arm: verdict(summary, arm) for arm in arms if arm != BASELINE}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out / f"replay-{args.business}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "model": llm.model_agent,
        "go_rule": GO_RULE,
        "summary": summary,
        "verdicts": verdicts,
        "entries": entries,
    }
    (out_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    queries = {case.turn_id: case.query for case, _ in prepared}
    candidate = f"responses:{_parse_efforts(args.efforts)[0]}"
    pairs, key = blind_pairs(entries, candidate, seed=args.seed)
    (out_dir / "pairs.md").write_text(pairs_markdown(pairs, queries), encoding="utf-8")
    (out_dir / "pairs_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "verdicts": verdicts}, ensure_ascii=False, indent=2))
    print(f"\nraport: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
