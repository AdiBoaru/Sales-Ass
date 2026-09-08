"""Sondă READ-ONLY: ce primește agentul din DB și ce SQL plătește pentru asta.

De ce există: tool-urile de catalog sunt singurul loc în care faptele ajung la model, iar forma lor
(`llm_view`) și costul lor (query-uri, round-trip-uri, planuri) se măsoară, nu se presupun (D15).
Scriptul rulează tool-urile REALE (`search_products`, `get_product_details`, `compare_products`,
`related_products`, paginarea de sesiune, `prompt_inputs`, `load_vocabulary`) pe tenantul dat, prin
ACELAȘI provider tenant-scoped ca în producție, cu o singură diferență: conexiunea e împachetată
într-un recorder care reține fiecare statement (SQL, parametri, durată, rânduri) și apoi îl
re-rulează cu `EXPLAIN (ANALYZE, BUFFERS)` pe aceeași conexiune `bot_runtime` — deci planul
măsurat e cel pe care îl vede RLS-ul, nu cel al superuserului.

Ce NU face: nu cheamă niciun model (zero credite OpenAI) — deci brațul SEMANTIC al căutării nu
rulează aici, iar rezultatele sunt cele ale scării lexicale. Nu scrie nimic în DB.

Utilizare:
    python scripts/db_query_probe.py                       # tenantul sole-ro, scenariile implicite
    python scripts/db_query_probe.py --business-id <uuid> --out reports/db-query-probe.md
    python scripts/db_query_probe.py --no-explain          # doar capturează, fără planuri
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.vocabulary_cache import clear_vocabulary_cache  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.models import (  # noqa: E402
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    TurnContext,
)
from src.worker.runner import PipelineDeps  # noqa: E402

SOLE_BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"


# --- recorder --------------------------------------------------------------------------------


@dataclass
class Stmt:
    op: str
    method: str
    sql: str
    params: tuple[Any, ...]
    ms: float
    rows: int
    plan: dict[str, Any] | None = None


@dataclass
class Checkout:
    op: str
    stmts: list[Stmt] = field(default_factory=list)
    ms: float = 0.0


class Recorder:
    """Proxy peste `asyncpg.Connection`: înregistrează fiecare statement, delegă restul."""

    def __init__(self, conn: Any, checkout: Checkout) -> None:
        self._conn = conn
        self._co = checkout

    async def _rec(self, method: str, sql: str, args: tuple[Any, ...]) -> Any:
        t = time.perf_counter()
        fn = getattr(self._conn, method)
        res = await fn(sql, *args)
        ms = (time.perf_counter() - t) * 1000.0
        if method == "fetch":
            n = len(res)
        elif method == "fetchrow":
            n = 0 if res is None else 1
        elif method == "fetchval":
            n = 0 if res is None else 1
        else:
            n = -1
        self._co.stmts.append(Stmt(self._co.op, method, sql, args, ms, n))
        return res

    async def fetch(self, sql: str, *args: Any, **kw: Any) -> Any:
        return await self._rec("fetch", sql, args)

    async def fetchrow(self, sql: str, *args: Any, **kw: Any) -> Any:
        return await self._rec("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args: Any, **kw: Any) -> Any:
        return await self._rec("fetchval", sql, args)

    async def execute(self, sql: str, *args: Any, **kw: Any) -> Any:
        return await self._rec("execute", sql, args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class Ledger:
    def __init__(self) -> None:
        self.checkouts: list[Checkout] = []

    def provider(self, business_id: str):
        ledger = self

        @asynccontextmanager
        async def _cm(operation: str = "unlabeled"):
            co = Checkout(op=operation)
            ledger.checkouts.append(co)
            t = time.perf_counter()
            async with tenant_conn(business_id) as conn:
                try:
                    yield Recorder(conn, co)
                finally:
                    co.ms = (time.perf_counter() - t) * 1000.0

        return _cm

    def drain(self) -> list[Checkout]:
        out, self.checkouts = self.checkouts, []
        return out


# --- explain --------------------------------------------------------------------------------

_SEQ = re.compile(r"Seq Scan")


def _walk(node: dict[str, Any], acc: list[dict[str, Any]]) -> None:
    acc.append(node)
    for child in node.get("Plans") or []:
        _walk(child, acc)


def _plan_summary(plan_json: Any) -> dict[str, Any]:
    root = plan_json[0]
    plan = root["Plan"]
    nodes: list[dict[str, Any]] = []
    _walk(plan, nodes)
    seq = [
        {
            "rel": n.get("Relation Name"),
            "alias": n.get("Alias"),
            "rows": n.get("Actual Rows"),
            "loops": n.get("Actual Loops"),
            "removed": n.get("Rows Removed by Filter"),
            "ms": n.get("Actual Total Time"),
        }
        for n in nodes
        if n.get("Node Type") == "Seq Scan"
    ]
    idx = sorted(
        {
            f"{n.get('Index Name')}"
            for n in nodes
            if n.get("Node Type") in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
            and n.get("Index Name")
        }
    )
    loops_heavy = [
        {
            "node": n.get("Node Type"),
            "rel": n.get("Relation Name") or n.get("Alias"),
            "loops": n.get("Actual Loops"),
            "ms_total": round((n.get("Actual Total Time") or 0) * (n.get("Actual Loops") or 1), 2),
        }
        for n in nodes
        if (n.get("Actual Loops") or 1) >= 40
    ]
    return {
        "exec_ms": root.get("Execution Time"),
        "plan_ms": root.get("Planning Time"),
        "shared_hit": plan.get("Shared Hit Blocks"),
        "shared_read": plan.get("Shared Read Blocks"),
        "root": plan.get("Node Type"),
        "seq_scans": seq,
        "indexes": idx,
        "hot_loops": loops_heavy,
        "n_nodes": len(nodes),
    }


async def explain_all(business_id: str, stmts: list[Stmt]) -> None:
    async with tenant_conn(business_id) as conn:
        for s in stmts:
            head = s.sql.lstrip().lower()
            if not head.startswith(("select", "with")):
                continue
            try:
                raw = await conn.fetchval(
                    "explain (analyze, buffers, format json) " + s.sql, *s.params
                )
                s.plan = _plan_summary(json.loads(raw) if isinstance(raw, str) else raw)
            except Exception as e:  # noqa: BLE001 — planul lipsă e informație, nu eșec
                s.plan = {"error": f"{type(e).__name__}: {e}"[:200]}


# --- scenarii -------------------------------------------------------------------------------


def _ctx(biz: BusinessConfig, body: str, turn_id: str) -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(
            provider_msg_id=f"probe-{turn_id}", body=body, channel_kind="webchat"
        ),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )


@dataclass
class Scenario:
    name: str
    tool: str
    args: dict[str, Any]
    note: str = ""


SEARCH_SCENARIOS: list[Scenario] = [
    Scenario(
        "S01 frază naturală", "search_products", {"query": "crema hidratanta pentru ten uscat"}
    ),
    Scenario(
        "S02 nevoie (concerns)",
        "search_products",
        {"query": "ser pentru pete pigmentare", "concerns": ["pete pigmentare"]},
    ),
    Scenario("S03 buget", "search_products", {"query": "crema antirid", "price_max": 150}),
    Scenario(
        "S04 categorie",
        "search_products",
        {"query": "sampon pentru par gras", "category": "par-ingrijirea-parului"},
    ),
    Scenario(
        "S05 sort preț",
        "search_products",
        {"query": "protectie solara spf", "sort_mode": "price_asc"},
    ),
    Scenario("S06 brand real", "search_products", {"query": "toner", "brand": "COSRX"}),
    Scenario("S07 typo", "search_products", {"query": "sampoon anti matreata"}),
    Scenario("S08 ingredient", "search_products", {"query": "ser", "features": ["niacinamida"]}),
    Scenario(
        "S09 doar pe stoc",
        "search_products",
        {"query": "gel de curatare ten gras", "in_stock_only": True},
    ),
    Scenario("S10 brand absent", "search_products", {"query": "crema", "brand": "Chanel"}),
    Scenario(
        "S11 tip explicit", "search_products", {"query": "crema de fata", "concerns": ["ten uscat"]}
    ),
]


def _fmt_ms(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}"


def _short_sql(sql: str, n: int = 160) -> str:
    s = " ".join(sql.split())
    return s if len(s) <= n else s[:n] + " …"


def _sql_shape(sql: str) -> str:
    """Amprenta FORMEI: SQL fără literali/numere de placeholder, ca să numărăm statementele
    distincte, nu execuțiile."""
    s = " ".join(sql.split())
    s = re.sub(r"\$\d+", "$?", s)
    return s


async def run(business_id: str, *, explain: bool, out: pathlib.Path) -> int:
    ledger = Ledger()
    db = ledger.provider(business_id)
    deps = PipelineDeps(llm=None, db=db)

    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
        if biz is None:
            print(f"EROARE: business {business_id} inexistent", file=sys.stderr)
            return 2
        oos = await conn.fetchval(
            "select p.id::text from products p where p.business_id = $1 and p.status = 'active'"
            " and p.availability = 'out_of_stock' and exists (select 1 from product_relations r"
            " where r.business_id = p.business_id and r.product_id = p.id"
            " and r.kind = 'substitute') order by p.id limit 1",
            business_id,
        )

    import src.tools.catalog_tools  # noqa: F401, PLC0415 — înregistrează tool-urile
    from src.tools.base import TOOL_REGISTRY  # noqa: PLC0415

    def get_tool(name: str):
        return TOOL_REGISTRY[name]

    report: list[dict[str, Any]] = []

    async def call(sc: Scenario, ctx: TurnContext) -> dict[str, Any]:
        tool = get_tool(sc.tool)
        t = time.perf_counter()
        res = await tool(ctx, deps, sc.args)
        wall = (time.perf_counter() - t) * 1000.0
        cos = ledger.drain()
        stmts = [s for c in cos for s in c.stmts]
        if explain:
            await explain_all(business_id, stmts)
        return {
            "scenario": sc.name,
            "tool": sc.tool,
            "args": sc.args,
            "note": sc.note,
            "wall_ms": wall,
            "ok": res.ok,
            "error": res.error,
            "n_products": len(res.products),
            "product_ids": [str(p.get("id")) for p in res.products],
            "llm_view": res.llm_view,
            "llm_view_chars": len(res.llm_view or ""),
            "state_patch_keys": sorted(res.state_patch.keys()),
            "events": [e.type for e in ctx.events],
            "checkouts": [
                {
                    "op": c.op,
                    "ms": c.ms,
                    "stmts": [
                        {
                            "method": s.method,
                            "sql": s.sql,
                            "shape": _sql_shape(s.sql),
                            "params": [
                                (str(p)[:60] if not isinstance(p, list) else f"list[{len(p)}]")
                                for p in s.params
                            ],
                            "ms": s.ms,
                            "rows": s.rows,
                            "plan": s.plan,
                        }
                        for s in c.stmts
                    ],
                }
                for c in cos
            ],
        }

    # 0) vocabularul: se încarcă o dată per tenant (cache 5 min) — îl măsurăm separat, ca să nu
    #    polueze prima căutare.
    clear_vocabulary_cache()
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415

    t = time.perf_counter()
    await get_vocabulary(deps, business_id)
    wall = (time.perf_counter() - t) * 1000.0
    cos = ledger.drain()
    stmts = [s for c in cos for s in c.stmts]
    if explain:
        await explain_all(business_id, stmts)
    report.append(
        {
            "scenario": "S00 load_vocabulary (o dată / 5 min / tenant)",
            "tool": "get_vocabulary",
            "args": {},
            "wall_ms": wall,
            "ok": True,
            "n_products": 0,
            "llm_view": "",
            "llm_view_chars": 0,
            "events": [],
            "checkouts": [
                {
                    "op": c.op,
                    "ms": c.ms,
                    "stmts": [
                        {
                            "method": s.method,
                            "sql": s.sql,
                            "shape": _sql_shape(s.sql),
                            "params": [str(p)[:60] for p in s.params],
                            "ms": s.ms,
                            "rows": s.rows,
                            "plan": s.plan,
                        }
                        for s in c.stmts
                    ],
                }
                for c in cos
            ],
        }
    )

    # 1) prompt_inputs — ce citește stagiul agent ÎNAINTE de orice tool.
    from src.db.queries.catalog import list_category_names, list_routing_aliases  # noqa: PLC0415

    t = time.perf_counter()
    async with db("prompt_inputs") as conn:
        cats = await list_category_names(conn, business_id)
        aliases = await list_routing_aliases(conn, business_id)
    wall = (time.perf_counter() - t) * 1000.0
    cos = ledger.drain()
    stmts = [s for c in cos for s in c.stmts]
    if explain:
        await explain_all(business_id, stmts)
    report.append(
        {
            "scenario": "S0P prompt_inputs (fiecare tur cu agent)",
            "tool": "_load_prompt_inputs",
            "args": {},
            "wall_ms": wall,
            "ok": True,
            "n_products": 0,
            "llm_view": f"{len(cats)} categorii, {len(aliases)} aliasuri",
            "llm_view_chars": 0,
            "events": [],
            "checkouts": [
                {
                    "op": c.op,
                    "ms": c.ms,
                    "stmts": [
                        {
                            "method": s.method,
                            "sql": s.sql,
                            "shape": _sql_shape(s.sql),
                            "params": [str(p)[:60] for p in s.params],
                            "ms": s.ms,
                            "rows": s.rows,
                            "plan": s.plan,
                        }
                        for s in c.stmts
                    ],
                }
                for c in cos
            ],
        }
    )

    # 2) căutările
    first_ctx: TurnContext | None = None
    first_products: list[dict[str, Any]] = []
    first_patch: dict[str, Any] = {}
    for sc in SEARCH_SCENARIOS:
        ctx = _ctx(biz, sc.args["query"], sc.name)
        r = await call(sc, ctx)
        report.append(r)
        if first_ctx is None and r["n_products"] >= 2:
            first_ctx = ctx
            tool = get_tool(sc.tool)
            # re-obținem produsele fără să înregistrăm (deja măsurate)
            res = await tool(ctx, deps, sc.args)
            ledger.drain()
            first_products = res.products
            # sesiunea de căutare e scrisă de tool DIRECT în `ctx.state_patch`, nu în ToolResult
            first_patch = dict(ctx.state_patch)

    if first_ctx is None:
        print("EROARE: nicio căutare n-a întors ≥2 produse", file=sys.stderr)
        return 2

    top = [str(p["id"]) for p in first_products]

    # 3) detaliu / comparație / related / pagina 2, ancorate pe rezultatele reale
    ctx = _ctx(biz, "spune-mi mai multe despre primul", "S12")
    report.append(
        await call(
            Scenario("S12 detaliu produs", "get_product_details", {"product_id": top[0]}), ctx
        )
    )
    ctx = _ctx(biz, "compara primele doua", "S13")
    report.append(
        await call(Scenario("S13 comparație", "compare_products", {"product_ids": top[:2]}), ctx)
    )
    ctx = _ctx(biz, "cu ce il combin", "S14")
    report.append(
        await call(
            Scenario(
                "S14 related routine_next",
                "related_products",
                {"anchor_id": top[0], "relation": "routine_next", "limit": 4},
            ),
            ctx,
        )
    )
    ctx = _ctx(biz, "ce merge cu el", "S15")
    report.append(
        await call(
            Scenario(
                "S15 related complement",
                "related_products",
                {"anchor_id": top[0], "relation": "complement", "limit": 4},
            ),
            ctx,
        )
    )
    # pagina 2: aceeași cerere, cu sesiunea semănată de prima căutare + produsele deja afișate
    ctx = _ctx(biz, "mai arata-mi", "S16")
    ctx.state.active_search = first_patch.get("active_search")
    ctx.state.displayed_products = [
        ProductRef(product_id=str(p["id"]), name=p["name"], price=float(p["price"] or 0))
        for p in first_products
    ]
    report.append(
        await call(
            Scenario("S16 pagina 2 (sesiune)", "search_products", dict(SEARCH_SCENARIOS[0].args)),
            ctx,
        )
    )
    if oos:
        ctx = _ctx(biz, "mai aveti produsul asta", "S17")
        report.append(
            await call(
                Scenario(
                    "S17 detaliu produs EPUIZAT (substitut)",
                    "get_product_details",
                    {"product_id": oos},
                ),
                ctx,
            )
        )

    write_report(report, out, business_id)
    out.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(f"raport: {out}  (+ .json)")
    return 0


def write_report(report: list[dict[str, Any]], out: pathlib.Path, business_id: str) -> None:
    lines: list[str] = [
        f"# Sondă DB → agent · business `{business_id}`",
        "",
        "Rulat FĂRĂ model (zero OpenAI): brațul semantic nu apare; doar scara lexicală + filtre.",
        "Planurile sunt măsurate pe conexiunea `bot_runtime` (RLS activ), ca în producție.",
        "",
        "## Rezumat",
        "",
        "| scenariu | tool | wall ms | checkouts | statements | DB ms (sumă) | produse "
        "| llm_view chars |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    shapes: dict[str, dict[str, Any]] = {}
    for r in report:
        n_co = len(r["checkouts"])
        stmts = [s for c in r["checkouts"] for s in c["stmts"]]
        db_ms = sum(s["ms"] for s in stmts)
        lines.append(
            f"| {r['scenario']} | {r['tool']} | {r['wall_ms']:.0f} | {n_co} | {len(stmts)} | "
            f"{db_ms:.0f} | {r['n_products']} | {r['llm_view_chars']} |"
        )
        for s in stmts:
            sh = shapes.setdefault(
                s["shape"], {"n": 0, "ms": 0.0, "ops": set(), "max_ms": 0.0, "plan": None}
            )
            sh["n"] += 1
            sh["ms"] += s["ms"]
            sh["max_ms"] = max(sh["max_ms"], s["ms"])
            sh["ops"].add(r["tool"])
            if s.get("plan") and not sh["plan"]:
                sh["plan"] = s["plan"]
    lines += ["", "## Statementele distincte (formă), ordonate după timp total", ""]
    lines.append(
        "| # | execuții | total ms | max ms | seq scans (rel: rows/removed) | indexuri "
        "| apelanți | SQL |"
    )
    lines.append("|---:|---:|---:|---:|---|---|---|---|")
    for i, (shape, sh) in enumerate(sorted(shapes.items(), key=lambda kv: -kv[1]["ms"]), start=1):
        plan = sh["plan"] or {}
        seq = "; ".join(
            f"{x['rel']}: {x['rows']}/{x['removed']}×{x['loops']}"
            for x in plan.get("seq_scans") or []
        )
        idx = ", ".join(plan.get("indexes") or [])
        lines.append(
            f"| {i} | {sh['n']} | {sh['ms']:.0f} | {sh['max_ms']:.0f} | {seq} | {idx} | "
            f"{', '.join(sorted(sh['ops']))} | `{_short_sql(shape, 110)}` |"
        )
    lines += ["", "## Per scenariu", ""]
    for r in report:
        lines.append(f"### {r['scenario']}")
        lines.append("")
        lines.append(f"- tool: `{r['tool']}` · args: `{json.dumps(r['args'], ensure_ascii=False)}`")
        lines.append(
            f"- ok={r['ok']} error={r.get('error')} produse={r['n_products']} "
            f"wall={r['wall_ms']:.0f}ms · events: {', '.join(r['events']) or '-'}"
        )
        for c in r["checkouts"]:
            lines.append(f"- checkout `{c['op']}` ({c['ms']:.0f}ms, {len(c['stmts'])} stmt)")
            for s in c["stmts"]:
                p = s.get("plan") or {}
                pl = ""
                if p:
                    if "error" in p:
                        pl = f" · plan: {p['error']}"
                    else:
                        seq = "; ".join(
                            f"{x['rel']} {x['rows']}r/{x['removed']}rm×{x['loops']}"
                            for x in p.get("seq_scans") or []
                        )
                        hot = "; ".join(
                            f"{x['node']}@{x['rel']}×{x['loops']}={x['ms_total']}ms"
                            for x in p.get("hot_loops") or []
                        )
                        pl = (
                            f" · exec {_fmt_ms(p.get('exec_ms'))}ms"
                            f" plan {_fmt_ms(p.get('plan_ms'))}ms"
                            f" buf hit/read {p.get('shared_hit')}/{p.get('shared_read')}"
                            + (f" · SEQ[{seq}]" if seq else "")
                            + (f" · LOOPS[{hot}]" if hot else "")
                        )
                lines.append(
                    f"  - {s['method']} {s['ms']:.1f}ms rows={s['rows']} params={s['params']}{pl}"
                )
                lines.append(f"    `{_short_sql(s['sql'], 200)}`")
        if r["llm_view"]:
            lines.append("")
            lines.append("**llm_view (exact ce vede modelul):**")
            lines.append("")
            lines.append("```")
            lines.append(r["llm_view"])
            lines.append("```")
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="sondă read-only: DB → tool → agent")
    ap.add_argument("--business-id", default=SOLE_BIZ)
    ap.add_argument("--no-explain", action="store_true")
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=ROOT / "reports" / f"db-query-probe-{SOLE_BIZ[:8]}.md",
    )
    args = ap.parse_args()

    async def _run() -> int:
        try:
            return await run(args.business_id, explain=not args.no_explain, out=args.out)
        finally:
            await close_pool()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
