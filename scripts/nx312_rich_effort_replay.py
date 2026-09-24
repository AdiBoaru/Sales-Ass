"""NX-312 felia 5 — replay al apelului de compunere bogată pe `reasoning_effort` diferite.

De ce replay și nu flip: pe 50 de ture, `model_ms` p50 e 7,4s la raționament mic și 27,2s la
raționament mare, dar corelația e CONTAMINATĂ (turele grele gândesc mai mult). Singurul mod de a
separa efectul lui `reasoning_effort` de dificultatea turului e același input, rulat de mai multe
ori. Decizia pe `LLM_REASONING_EFFORT_AGENT` e separată și se ia pe raportul ăsta (D15).

Ce reface, per tur real din `conversation_traces` (turele în care apelul 3 a rulat, `rich_raw`):
  * produsele: `agent_prompt.retrieval_ids` (setul acumulat de bucla de tool-uri), hidratate prin
    `get_products_by_ids`; rezervă = cardurile servite, dacă evenimentul lipsește;
  * istoricul: `messages` la momentul turului, prin `conversation_transcript` (ca `agent_stage`);
  * system / mesaj / schemă: `build_rich_system`, `finalize.rich_user_message`, `_rich_schema`,
    adică EXACT funcțiile producției, nu copii.
Apoi cheamă `LLMClient.complete_schema` (calea reală: buget, retry, sampling) o dată per efort, în
ordine AMESTECATĂ per tur (system-ul e identic, deci al doilea apel prinde cache-ul primului; o
ordine fixă ar favoriza sistematic efortul rulat al doilea), și trece fiecare JSON prin
`compose.assemble` (membership + scrub), ca pe drumul real.

Ce NU reface, declarat (niciuna nu diferă între eforturi, deci comparația rămâne pe același input):
  * modelarea setului din `planner` (mai ieftin, masca de relevanță) și nota de comerț (`notes`);
  * forma de tur NX-315 (flagurile ei sunt stinse în producție);
  * regulile de rutină (`routine=True`) — turele de rutină primesc system-ul obișnuit;
  * catalogul de ACUM, nu cel de atunci (prețuri/stoc pot fi altele).

Ieșire, în `reports/nx312/replay-<business>-<timestamp>/`:
  * `results.json` — per tur × efort: ms, tokeni (in/cached/out/raționament), cost, ok, JSON brut,
    ce a supraviețuit la `assemble`;
  * `pairs.md` — perechi OARBE (A/B amestecat cu `--seed`) efort vs `--baseline`, de adjudecat;
  * `pairs_key.json` — cheia perechilor; NU o deschide înainte de adjudecare.

**Consumă credite OpenAI.** Fără `--yes`, scriptul se oprește după ce spune câte apeluri ar face.
`--dry-run` reface tot inputul și raportează mărimile, cu ZERO apeluri de model.

    PYTHONPATH=. python scripts/nx312_rich_effort_replay.py --business sole-ro --dry-run
    PYTHONPATH=. python scripts/nx312_rich_effort_replay.py --business sole-ro --limit 30 --yes
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
from src.agent.finalize import _rich_schema, rich_omissions, rich_user_message  # noqa: E402
from src.agent.prompt_builder import PromptInputs, build_rich_system  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import (  # noqa: E402
    Author,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.worker import compose  # noqa: E402

#: Valorile pe care `gpt-6-luna` (MODEL_AGENT din 2026-09-24) le acceptă pentru apelul fără
#: tool-uri. `minimal` a ieșit: nu există pe familia GPT-6. `none` a intrat, deliberat, deși
#: schimbă DOUĂ lucruri deodată (raționament oprit ⇒ `_sampling` trimite și temperatura): exact așa
#: ar pleca cererea în producție cu `LLM_REASONING_EFFORT_AGENT=none`, deci măsurăm configul, nu
#: un parametru izolat. Rândul `none` trebuie citit ca „efort none + temperatura configurată".
EFFORTS = ("none", "low", "medium", "high", "xhigh")
DEFAULT_EFFORTS = ("none", "low", "medium", "high")
_MAX_PRODUCTS = 12  # două căutări paralele × 6; peste asta turul nu e unul de recomandare obișnuit


@dataclass(frozen=True)
class ReplayCase:
    """Un tur real, redus la ce intră în apelul 3. Fără text de mesaj în rapoartele agregate."""

    turn_id: str
    query: str
    language: str
    history: str
    products: tuple[dict[str, Any], ...]
    products_source: str  # "retrieval_ids" | "served"


@dataclass
class EffortResult:
    effort: str
    ok: bool
    ms: float
    tokens_in: int = 0
    cached: int = 0
    tokens_out: int = 0
    reasoning_tokens: int | None = None
    cost_usd: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None
    served: dict[str, Any] = field(default_factory=dict)
    visible: str = ""


# --- construcția apelului: exclusiv prin funcțiile producției -------------------------------------


def make_ctx(business: Any, case: ReplayCase) -> TurnContext:
    ctx = TurnContext(
        turn_id=f"replay-{case.turn_id}",
        business=business,
        contact=Contact(id="replay", business_id=business.id),
        message=InboundMessage(provider_msg_id="replay", body=case.query, channel_kind="webchat"),
        conversation_id="replay",
        state=ConversationState(),
    )
    ctx.language = case.language
    return ctx


def build_call(
    ctx: TurnContext, inp: PromptInputs, case: ReplayCase, omit: frozenset[str]
) -> tuple[str, str, dict[str, Any]]:
    """(system, user, schema) — exact ce ar trimite `_finalize_rich` pentru turul ăsta."""
    products = [dict(p) for p in case.products]
    system = build_rich_system(inp, omit=omit)
    user = rich_user_message(ctx, case.query, products, case.history)
    return system, user, _rich_schema(omit)


def visible_text(rich: Any) -> str:
    """Ce citește clientul pe web: încadrarea (intro + education) și motivul de pe fiecare card.
    Prețurile și ratingurile le pune codul, identic pe toate eforturile, deci nu intră."""
    if rich is None:
        return "(fără răspuns bogat: turul cade pe rezervă)"
    lines = [compose.flatten_framing(rich, None) or "(fără text de încadrare)", ""]
    lines += [f"- {it.name}: {it.reason or '(fără motiv)'}" for it in rich.items]
    return "\n".join(lines)


def served_summary(ctx: TurnContext, j: dict[str, Any], products: list[dict[str, Any]]) -> tuple:
    """Trece JSON-ul prin `compose.assemble`, ca pe drumul real: câte carduri rămân după
    apartenență, dacă intro-ul și education au trecut de scrub."""
    emitted = [it for it in (j.get("items") or []) if isinstance(it, dict) and it.get("product_id")]
    rich = compose.assemble(ctx, j, products)
    return (
        {
            "model_items": len(emitted),
            "cards": len(rich.items),
            "intro": bool(rich.intro),
            "education": bool(rich.education),
        },
        rich,
    )


async def run_effort(
    llm: Any,
    effort: str,
    ctx: TurnContext,
    call: tuple[str, str, dict[str, Any]],
    products: list[dict[str, Any]],
) -> EffortResult:
    """Un apel pe calea REALĂ (`complete_schema`), cu `llm_reasoning_effort_agent` schimbat doar
    pe durata apelului și restaurat și pe eroare."""
    system, user, schema = call
    s = get_settings()
    before = s.llm_reasoning_effort_agent
    acc, token = usage.push()
    started = perf_counter()
    try:
        s.llm_reasoning_effort_agent = effort
        j = await llm.complete_schema(system, user, schema)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001 — un apel picat e un rezultat, nu un motiv de oprire
        j, ok, err = None, False, type(e).__name__
    finally:
        s.llm_reasoning_effort_agent = before
        usage.pop(token)
    wall = (perf_counter() - started) * 1000
    row = acc.call_rows[-1] if acc.call_rows else {}
    res = EffortResult(
        effort=effort,
        ok=ok,
        ms=float(row.get("ms", wall)),
        tokens_in=int(row.get("tokens_in", 0)),
        cached=int(row.get("cached", 0)),
        tokens_out=int(row.get("tokens_out", 0)),
        reasoning_tokens=row.get("reasoning_tokens"),
        cost_usd=round(acc.cost_usd, 6),
        error=err,
        raw=j,
    )
    if j is not None:
        res.served, rich = served_summary(ctx, j, products)
        res.visible = visible_text(rich)
    else:
        res.visible = visible_text(None)
    return res


async def replay(
    cases: list[tuple[TurnContext, ReplayCase, tuple[str, str, dict[str, Any]]]],
    llm: Any,
    efforts: tuple[str, ...],
    *,
    seed: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Per tur, eforturile în ordine amestecată (seed). `dry_run` ⇒ niciun apel de model."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for ctx, case, call in cases:
        order = list(efforts)
        rng.shuffle(order)
        entry: dict[str, Any] = {
            "turn_id": case.turn_id,
            "products": len(case.products),
            "products_source": case.products_source,
            "system_chars": len(call[0]),
            "user_chars": len(call[1]),
            "order": order,
            "results": {},
        }
        if not dry_run:
            for effort in order:
                res = await run_effort(llm, effort, ctx, call, [dict(p) for p in case.products])
                entry["results"][effort] = res.__dict__
        out.append(entry)
    return out


# --- raportare ---------------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)


def summarize(entries: list[dict[str, Any]], efforts: tuple[str, ...]) -> dict[str, Any]:
    """Per efort: latență p50/p90, raționament p50, cost total, câte apeluri OK, cât a supraviețuit
    la `assemble`. Doar numere (P12)."""
    report: dict[str, Any] = {}
    for effort in efforts:
        rows = [e["results"][effort] for e in entries if effort in e["results"]]
        ok = [r for r in rows if r["ok"]]
        reasoning = [r["reasoning_tokens"] for r in ok if r["reasoning_tokens"] is not None]
        report[effort] = {
            "calls": len(rows),
            "ok": len(ok),
            "ms_p50": _pct([r["ms"] for r in ok], 0.5),
            "ms_p90": _pct([r["ms"] for r in ok], 0.9),
            "reasoning_tokens_p50": _pct(reasoning, 0.5),
            "tokens_out_p50": _pct([r["tokens_out"] for r in ok], 0.5),
            "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 4),
            "cards_mean": round(statistics.mean(r["served"]["cards"] for r in ok), 2)
            if ok
            else None,
            "no_cards": sum(1 for r in ok if not r["served"].get("cards")),
            "intro_kept": sum(1 for r in ok if r["served"].get("intro")),
            "education_kept": sum(1 for r in ok if r["served"].get("education")),
        }
    return report


def blind_pairs(
    entries: list[dict[str, Any]], baseline: str, efforts: tuple[str, ...], *, seed: int
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Perechi efort vs `baseline`, cu A/B amestecat. Întoarce (perechi fără etichete, cheie).

    Ordinea contează: un evaluator care știe ce e „high" îl citește ca pe referință. Cheia stă în
    alt fișier, iar perechile în care unul dintre apeluri a picat nu intră (n-ai ce compara)."""
    rng = random.Random(seed + 1)
    pairs: list[dict[str, Any]] = []
    key: dict[str, dict[str, str]] = {}
    for e in entries:
        base = e["results"].get(baseline)
        if not base or not base["ok"]:
            continue
        for other in efforts:
            if other == baseline:
                continue
            cand = e["results"].get(other)
            if not cand or not cand["ok"]:
                continue
            pair_id = f"{e['turn_id'][:8]}-{len(pairs) + 1:03d}"
            sides = [(baseline, base["visible"]), (other, cand["visible"])]
            rng.shuffle(sides)
            pairs.append(
                {"pair_id": pair_id, "turn_id": e["turn_id"], "A": sides[0][1], "B": sides[1][1]}
            )
            key[pair_id] = {"A": sides[0][0], "B": sides[1][0]}
    return pairs, key


def pairs_markdown(pairs: list[dict[str, Any]], queries: dict[str, str]) -> str:
    lines = [
        "# NX-312 felia 5 — perechi oarbe",
        "",
        "Pentru fiecare pereche notează `A`, `B` sau `egal` (care răspuns ajută mai mult clientul",
        "să aleagă). Cheia e în `pairs_key.json`; deschide-o doar după ce ai terminat.",
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


async def load_cases(
    business: Any, conn: Any, admin: Any, *, days: int, limit: int
) -> list[ReplayCase]:
    """Turele în care apelul 3 a rulat (`rich_raw` în diagnoză), cele mai recente primele.

    `analytics_events` se citește pe `admin` (cu `business_id` explicit, ca
    `llm_call_budget_probe`): `bot_runtime` are doar INSERT pe tabel, prin design (append-only)."""
    from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415
    from src.worker.context import conversation_transcript  # noqa: PLC0415

    bid = business.id
    traces = await conn.fetch(
        """
        select turn_id::text as turn_id, conversation_id::text as conversation_id,
               client_text, coalesce(language, 'ro') as language, created_at,
               reply->'rich'->'items' as items
        from conversation_traces
        where business_id = $1::uuid and created_at > now() - make_interval(days => $2)
          and diagnostics ? 'rich_raw' and coalesce(client_text, '') <> ''
        order by created_at desc
        limit $3
        """,
        bid,
        days,
        limit,
    )
    turn_ids = [t["turn_id"] for t in traces]
    retrieval: dict[str, list[str]] = {}
    for r in await admin.fetch(
        """
        select turn_id::text as turn_id, properties->'retrieval_ids' as ids
        from analytics_events
        where business_id = $1::uuid and event_type = 'agent_prompt'
          and turn_id::text = any($2::text[])
        """,
        bid,
        turn_ids,
    ):
        ids = json.loads(r["ids"]) if isinstance(r["ids"], str) else r["ids"]
        if ids:
            retrieval[r["turn_id"]] = [str(i) for i in ids][:_MAX_PRODUCTS]

    cases: list[ReplayCase] = []
    for t in traces:
        ids, source = retrieval.get(t["turn_id"]), "retrieval_ids"
        if not ids:
            items = json.loads(t["items"]) if isinstance(t["items"], str) else t["items"] or []
            ids, source = [str(i.get("product_id")) for i in items if i.get("product_id")], "served"
        if not ids:
            continue
        products: list[dict[str, Any]] = []
        for i in range(0, len(ids), 6):  # `get_products_by_ids` are plafon dur de 6
            products += await get_products_by_ids(conn, bid, ids[i : i + 6], limit=6)
        if not products:
            continue
        rows = await conn.fetch(
            """
            select direction, author, body, content_type, created_at
            from (
              select direction, author, body, content_type, created_at
              from messages
              where business_id = $1::uuid and conversation_id = $2::uuid
                and created_at <= (
                  select max(created_at) from messages
                  where business_id = $1::uuid and conversation_id = $2::uuid
                    and direction = 'inbound' and created_at <= $3
                )
              order by created_at desc
              limit 8
            ) recent
            order by created_at asc
            """,
            bid,
            t["conversation_id"],
            t["created_at"],
        )
        history = [
            Message(
                direction=Direction(r["direction"]),
                author=Author(r["author"]),
                body=r["body"],
                content_type=r["content_type"] or "text",
                created_at=r["created_at"],
            )
            for r in rows
        ]
        cases.append(
            ReplayCase(
                turn_id=t["turn_id"],
                query=t["client_text"],
                language=t["language"],
                history=conversation_transcript(history),
                products=tuple(products),
                products_source=source,
            )
        )
    return cases


class _TenantDeps:
    """Adaptorul minim pe care îl cere `_load_prompt_inputs`: `db(op)` = checkout tenant-scoped.
    Așa categoriile și stilul vin din ACEEAȘI funcție ca în producție."""

    def __init__(self, business_id: str):
        self._bid = business_id

    def db(self, _op: str):
        from src.db.connection import tenant_conn  # noqa: PLC0415

        return tenant_conn(self._bid)


def _parse_efforts(raw: str) -> tuple[str, ...]:
    out = tuple(e.strip() for e in raw.split(",") if e.strip())
    bad = [e for e in out if e not in EFFORTS]
    if bad or len(set(out)) != len(out) or len(out) < 2:
        raise SystemExit(f"--efforts: cel puțin două valori distincte din {EFFORTS}, nu {raw!r}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=30, help="câte ture reale (cele mai recente)")
    ap.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS))
    # Referința e efortul CONFIGURAT azi, nu o constantă: NX-313 l-a coborât deja `high` → `medium`,
    # iar cardul (scris înainte) spunea `high`. Comparăm cu ce rulează, nu cu ce rula.
    ap.add_argument(
        "--baseline",
        default=get_settings().llm_reasoning_effort_agent,
        help="efortul față de care se compară (implicit: cel configurat azi)",
    )
    ap.add_argument("--seed", type=int, default=312)
    ap.add_argument("--dry-run", action="store_true", help="reface inputul, zero apeluri de model")
    ap.add_argument("--yes", action="store_true", help="confirmă că rularea consumă credite")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "nx312")
    args = ap.parse_args()
    efforts = _parse_efforts(args.efforts)
    if args.baseline not in efforts:
        raise SystemExit(f"--baseline {args.baseline!r} trebuie să fie printre --efforts")

    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415
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
                cases = await load_cases(business, conn, admin, days=args.days, limit=args.limit)
        omit = rich_omissions()
        prepared = []
        for case in cases:
            ctx = make_ctx(business, case)
            inp = await _load_prompt_inputs(_TenantDeps(bid), ctx)
            prepared.append((ctx, case, build_call(ctx, inp, case, omit)))
    finally:
        await close_pool()

    calls = len(prepared) * len(efforts)
    print(f"ture reale: {len(prepared)}  eforturi: {efforts}  apeluri de model: {calls}")
    print(f"omisiuni rich (NX-312 felia 4): {sorted(omit) or 'niciuna'}")
    if not prepared:
        return
    if not args.dry_run and not args.yes:
        print("Rularea consumă credite OpenAI. Repornește cu --yes (sau --dry-run).")
        return

    llm = None
    if not args.dry_run:
        from src.agent.llm import get_llm  # noqa: PLC0415

        llm = get_llm()
        if llm is None:
            raise SystemExit("lipsește OPENAI_API_KEY")
    entries = await replay(prepared, llm, efforts, seed=args.seed, dry_run=args.dry_run)

    if args.dry_run:
        for e in entries:
            print(
                f"  {e['turn_id'][:8]}  produse={e['products']:>2} ({e['products_source']})  "
                f"system={e['system_chars']}  user={e['user_chars']}  ordine={e['order']}"
            )
        return

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out / f"replay-{args.business}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(entries, efforts)
    pairs, key = blind_pairs(entries, args.baseline, efforts, seed=args.seed)
    queries = {case.turn_id: case.query for _, case, _ in prepared}
    (out_dir / "results.json").write_text(
        json.dumps(
            # Modelul intră în raport: aceleași nivele de efort nu înseamnă același lucru pe alt
            # model, iar un raport fără el nu se mai poate compara cu următorul.
            {"model": llm.model_agent, "summary": summary, "entries": entries},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (out_dir / "pairs.md").write_text(pairs_markdown(pairs, queries), encoding="utf-8")
    (out_dir / "pairs_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nraport: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
