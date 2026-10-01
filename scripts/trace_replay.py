"""NX-366 — rejoacă turele capturate în `conversation_traces` pe codul local, fără model (0 $).

    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --conversation <uuid>
    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --turn <uuid>
    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --since 2026-10-02 --fidelity

Read-only: fiecare checkout e o tranzacție `READ ONLY` anulată (`trace_replay.readonly_db`), deci
nicio conversație, mesaj sau eveniment nu se scrie. Pentru fiecare tur: statusul replay-ului
(`replayed` / `diverged` / `db_aborted` / `not_replayable`), dacă ce vede clientul (text, carduri,
chips, comparație) e identic cu înregistrarea, driftul de catalog și de pachet, și detectorii NX-363
pe rezultatul rejucat comparați cu cei de pe înregistrare (`fixed` = prindea, nu mai prinde).

`--fidelity` e poarta de validitate a INSTRUMENTULUI: rulat pe release-ul care a produs turele,
replay-ul trebuie să reproducă ≥ 95% din ele (fără cele cu drift de catalog). Sub prag, replay-ul
e cel stricat, nu produsul, și se repară înaintea oricărei judecăți de reparație.

`--export <dir>` scrie rândurile ca fișiere de caz. Doar pe `--conversation`, și doar pentru
conversațiile NOASTRE (seturile de test): traficul unui client nu intră în repo.

Raportul complet e local (`reports/nx366/`, ignorat de git).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import turn_defects as td  # noqa: E402
from src.evals import trace_replay as tr  # noqa: E402

OUT_DIR = ROOT / "reports" / "nx366"
FIDELITY_MIN = 0.95

ROWS_SQL = """
select turn_id::text as turn_id, conversation_id::text as conversation_id,
       business_id::text as business_id, created_at, client_text, reply, diagnostics
from conversation_traces
where business_id = $1::uuid and {where}
order by created_at
"""


def _json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw


def fingerprint_diff(recorded: Any, replayed: Any) -> list[str]:
    """Câmpurile vizibile clientului care diferă (vocabular închis: text/products/…)."""
    a, b = tr.reply_fingerprint(_json(recorded)), tr.reply_fingerprint(replayed)
    return [k for k in a if a[k] != b[k]]


def replay_as_defect_turn(row: dict[str, Any], res: tr.ReplayResult) -> td.Turn:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in res.events:
        props = {k: v for k, v in e.items() if k != "type"}
        events[e["type"]].append(props)
    created = row["created_at"]
    return td.Turn(
        turn_id=res.turn_id,
        conversation_id=row["conversation_id"],
        created_at=created.isoformat() if hasattr(created, "isoformat") else str(created),
        reply=res.reply or {},
        diagnostics=res.trace,
        events={k: tuple(v) for k, v in events.items()},
    )


def detector_hits(turn: td.Turn) -> list[str]:
    hits = []
    for d in td.DETECTORS:
        try:
            if d.fn(turn) is True:
                hits.append(d.key)
        except Exception:  # noqa: BLE001 — un detector care nu se aplică formei rejucate
            continue
    return hits


async def _rows(conn: Any, business_id: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.turn:
        sql, arg = ROWS_SQL.format(where="turn_id = $2::uuid"), args.turn
    elif args.conversation:
        sql, arg = ROWS_SQL.format(where="conversation_id = $2::uuid"), args.conversation
    else:
        sql, arg = ROWS_SQL.format(where="created_at >= $2"), args.since
    rows = [dict(r) for r in await conn.fetch(sql, business_id, arg)]
    if args.until and not (args.turn or args.conversation):
        rows = [r for r in rows if r["created_at"] < args.until]
    for r in rows:
        r["reply"] = _json(r["reply"])
        r["diagnostics"] = _json(r["diagnostics"]) or {}
    return rows


async def _recorded_hits(business: str, rows: list[dict[str, Any]], admin, tenant) -> dict:
    """Detectorii NX-363 pe înregistrare, pentru aceleași ture (evenimentele din analytics)."""
    if not rows:
        return {}
    since = min(r["created_at"] for r in rows) - timedelta(seconds=1)
    until = max(r["created_at"] for r in rows) + timedelta(seconds=1)
    turns = await td.load(business, since, until, admin=admin, tenant=tenant)
    wanted = {r["turn_id"] for r in rows}
    return {t.turn_id: detector_hits(t) for t in turns if t.turn_id in wanted}


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            business_id = await conn.fetchval(td.BUSINESS_SQL, args.business)
        if business_id is None:
            raise SystemExit(f"tenant necunoscut: {args.business!r}")
        async with tenant_conn(business_id) as conn:
            rows = await _rows(conn, business_id, args)
        recorded = await _recorded_hits(args.business, rows, lambda: admin_conn(pool), tenant_conn)
        results = []
        for row in rows:
            res = await tr.replay_turn(row, flags=args.flags)
            entry: dict[str, Any] = {
                "turn_id": res.turn_id,
                "conversation_id": row["conversation_id"],
                "status": res.status,
                "reason": res.reason,
            }
            if res.status != "not_replayable":
                ti = row["diagnostics"]["turn_input"]
                drift = await tr.catalog_drift(
                    tr.readonly_db(business_id), business_id, ti["env"].get("catalog") or {}
                )
                replayed_hits = detector_hits(replay_as_defect_turn(row, res))
                was = recorded.get(res.turn_id, [])
                entry.update(
                    differs=fingerprint_diff(row["reply"], res.reply),
                    catalog_drift=drift,
                    pack_drift=res.drift.get("pack"),
                    divergence=res.divergence,
                    unused_calls=res.unused_calls,
                    ignored_settings=res.ignored_settings,
                    detectors_recorded=was,
                    detectors_replayed=replayed_hits,
                    fixed=sorted(set(was) - set(replayed_hits)),
                    introduced=sorted(set(replayed_hits) - set(was)),
                    release=ti["env"].get("release"),
                )
                if args.verbose:
                    entry["reply_recorded"] = tr.reply_fingerprint(row["reply"])
                    entry["reply_replayed"] = tr.reply_fingerprint(res.reply)
            results.append(entry)
            if args.export and res.status != "not_replayable":
                _export(Path(args.export), row)
    finally:
        await close_pool()
    return summarize(results, args)


def _export(out_dir: Path, row: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob(f"{row['conversation_id'][:8]}-*.json")))
    path = out_dir / f"{row['conversation_id'][:8]}-{n}.json"
    doc = {k: row[k] for k in ("turn_id", "conversation_id", "business_id", "client_text")}
    doc.update(
        created_at=str(row["created_at"]), reply=row["reply"], diagnostics=row["diagnostics"]
    )
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def summarize(results: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    statuses = Counter(r["status"] for r in results)
    judged = [r for r in results if r["status"] == "replayed" and not r.get("catalog_drift")]
    same = [r for r in judged if not r.get("differs")]
    fidelity = len(same) / len(judged) if judged else None
    report = {
        "business": args.business,
        "flags": args.flags,
        "turns": len(results),
        "statuses": dict(statuses),
        "fidelity": {
            "judged": len(judged),
            "same": len(same),
            "rate": round(fidelity, 3) if fidelity is not None else None,
            "min": FIDELITY_MIN,
            "verdict": (
                None
                if not args.fidelity
                else "INSUFFICIENT"
                if fidelity is None
                else "PASS"
                if fidelity >= FIDELITY_MIN
                else "FAIL"
            ),
        },
        "fixed": Counter(k for r in results for k in r.get("fixed", [])),
        "introduced": Counter(k for r in results for k in r.get("introduced", [])),
        "turns_detail": results,
    }
    return report


def render(report: dict[str, Any]) -> str:
    f = report["fidelity"]
    lines = [
        f"tenant {report['business']} · {report['turns']} ture · profil {report['flags']}",
        f"statusuri: {report['statuses']}",
        f"identic cu înregistrarea: {f['same']}/{f['judged']}"
        + (f" ({f['rate']:.1%})" if f["rate"] is not None else "")
        + (f" · verdict {f['verdict']}" if f["verdict"] else ""),
    ]
    if report["fixed"]:
        lines.append(f"detectori care nu mai prind: {dict(report['fixed'])}")
    if report["introduced"]:
        lines.append(f"detectori noi pe replay: {dict(report['introduced'])}")
    for r in report["turns_detail"]:
        tail = ""
        if r.get("differs"):
            tail += f" differs={r['differs']}"
        if r.get("divergence"):
            tail += f" divergence@{r['divergence']['index']}"
        if r.get("catalog_drift"):
            tail += f" drift={len(r['catalog_drift'])}"
        if r.get("fixed"):
            tail += f" fixed={r['fixed']}"
        lines.append(
            f"  {r['turn_id'][:8]} {r['status']}{(' ' + r['reason']) if r['reason'] else ''}{tail}"
        )
    return "\n".join(lines)


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--business", required=True, help="slug-ul sau id-ul tenantului")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--turn")
    which.add_argument("--conversation")
    p.add_argument("--since", type=_date, help="pe fereastră: început (ISO), implicit acum − 1 zi")
    p.add_argument("--until", type=_date)
    p.add_argument("--flags", choices=("recorded", "current"), default="recorded")
    p.add_argument("--fidelity", action="store_true", help="aplică poarta de validitate (≥ 95%)")
    p.add_argument("--export", help="scrie rândurile ca fișiere de caz (doar cu --conversation)")
    p.add_argument("--verbose", action="store_true", help="include amprentele răspunsurilor")
    args = p.parse_args(argv)
    if args.export and not args.conversation:
        p.error("--export cere --conversation: traficul real nu se exportă pe fereastră")
    if not (args.turn or args.conversation):
        args.since = args.since or datetime.now(UTC) - timedelta(days=1)
    report = asyncio.run(_main(args))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"replay-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(render(report))
    print(f"raport local: {path.relative_to(ROOT)}")
    verdict = report["fidelity"]["verdict"]
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
