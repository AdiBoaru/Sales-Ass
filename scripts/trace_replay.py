"""NX-366 — rejoacă turele capturate în `conversation_traces` pe codul local, fără model (0 $).

    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --conversation <uuid>
    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --turn <uuid>
    PYTHONPATH=. python scripts/trace_replay.py --business sole-ro --since 2026-10-02 --fidelity

Read-only: fiecare checkout e o tranzacție `READ ONLY` anulată (`trace_replay.readonly_db`), deci
nicio conversație, mesaj sau eveniment nu se scrie. Pentru fiecare tur: statusul replay-ului
(`replayed` / `diverged` / `wrote` / `db_aborted` / `not_replayable`), dacă ce vede clientul (text,
carduri, chips, comparație) e identic cu înregistrarea, apelurile servite peste altă intrare,
driftul de catalog și de pachet, și detectorii NX-363 pe rezultatul rejucat comparați cu cei de pe
înregistrare (`fixed` = prindea, nu mai prinde), pe ACELEAȘI date de catalog.

`--fidelity` e poarta de validitate a INSTRUMENTULUI: rulat pe release-ul care a produs turele
(verificat: `HEAD` local == release-ul din trace, altfel verdictul e `WRONG_RELEASE`), replay-ul
trebuie să reproducă ≥ 95% din ture. Numitorul are toate turele rejucabile fără drift de catalog,
inclusiv cele `diverged` / `wrote` / `db_aborted`, care contează ca eșec. Sub prag, replay-ul e cel
stricat, nu produsul, și se repară înaintea oricărei judecăți de reparație.

`--export <dir> --set-report <raport>` scrie rândurile ca fișiere de caz, DOAR pentru conversațiile
din raportul unei rulări `prod_set_run.py` (seturile NOASTRE); traficul unui client nu intră.

Raportul complet e local (`reports/nx366/`, ignorat de git).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
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

#: Detectorii care măsoară MEDIUL, nu codul: latența rejucată e a mașinii locale, fără model.
#: Nu intră în `fixed` / `introduced`.
ENVIRONMENT_DETECTORS = frozenset({"slow_turn"})

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


def replay_as_defect_turn(
    recorded: td.Turn, res: tr.ReplayResult, catalog: dict[str, tuple[str | None, str | None]]
) -> td.Turn:
    """Turul rejucat în forma detectorilor, cu ACELEAȘI date de context ca înregistrarea:
    disponibilitatea și tipul cardurilor din catalogul de acum (la fel ca `td.load`) și numele
    afișate mai devreme din conversația înregistrată. Fără ele, detectorii de epuizat, de subiect
    și de nume dublat nu pot prinde nimic pe replay și ar ieși „reparați" (recenzia NX-366)."""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in res.events:
        events[e["type"]].append({k: v for k, v in e.items() if k != "type"})
    reply = res.reply or {}
    ids = [i for i in (td.card_id(c) for c in td.shown_cards(reply)) if i]
    return td.Turn(
        turn_id=res.turn_id,
        conversation_id=recorded.conversation_id,
        created_at=recorded.created_at,
        reply=reply,
        diagnostics=res.trace,
        events={k: tuple(v) for k, v in events.items()},
        availability={i: catalog.get(i, (None, None))[0] for i in ids},
        earlier_names=recorded.earlier_names,
        product_types={i: catalog.get(i, (None, None))[1] for i in ids},
    )


def detector_hits(turn: td.Turn) -> list[str]:
    hits = []
    for d in td.DETECTORS:
        if d.key in ENVIRONMENT_DETECTORS:
            continue
        try:
            if d.fn(turn) is True:
                hits.append(d.key)
        except Exception:  # noqa: BLE001 — un detector care nu se aplică formei rejucate
            continue
    return hits


def local_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


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


async def _recorded_turns(business: str, rows: list[dict[str, Any]], admin, tenant) -> dict:
    """Turele înregistrate în forma detectorilor (evenimente din analytics, catalogul de acum,
    numele afișate mai devreme). Fereastra acoperă toată conversația, ca `earlier_names` să fie
    complet și pe un singur tur cerut."""
    if not rows:
        return {}
    since = min(r["created_at"] for r in rows) - timedelta(hours=6)
    until = max(r["created_at"] for r in rows) + timedelta(seconds=1)
    turns = await td.load(business, since, until, admin=admin, tenant=tenant)
    return {t.turn_id: t for t in turns}


async def _catalog(tenant, business_id: str, results: list[tr.ReplayResult]) -> dict:
    ids = sorted(
        {
            i
            for res in results
            for i in (td.card_id(c) for c in td.shown_cards(res.reply or {}))
            if i and td._uuid_like(i)
        }
    )
    out: dict[str, tuple[str | None, str | None]] = {}
    if ids:
        async with tenant(business_id) as conn:
            for r in await conn.fetch(td.AVAILABILITY_SQL, business_id, ids):
                out[r["id"]] = (r["availability"], r["product_type"])
    return out


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
        recorded = await _recorded_turns(args.business, rows, lambda: admin_conn(pool), tenant_conn)
        replayed = [await tr.replay_turn(row, flags=args.flags) for row in rows]
        catalog = await _catalog(tenant_conn, business_id, replayed)
        head = local_head()
        results = []
        for row, res in zip(rows, replayed, strict=True):
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
                entry.update(
                    differs=fingerprint_diff(row["reply"], res.reply),
                    inputs_changed=res.inputs_changed,
                    catalog_drift=drift,
                    pack_drift=res.drift.get("pack"),
                    divergence=res.divergence,
                    unused_calls=res.unused_calls,
                    ignored_settings=res.ignored_settings,
                    release=ti["env"].get("release"),
                    same_release=bool(head) and ti["env"].get("release") == head,
                )
                rec_turn = recorded.get(res.turn_id)
                if res.status in ("replayed", "shortened") and rec_turn is not None:
                    # Doar pe un replay curat sau scurtat: unul divergent a rulat un fallback, iar
                    # un „reparat" acolo ar fi un artefact al instrumentului (recenzia NX-366).
                    was = detector_hits(rec_turn)
                    now = detector_hits(replay_as_defect_turn(rec_turn, res, catalog))
                    entry.update(
                        detectors_recorded=was,
                        detectors_replayed=now,
                        fixed=sorted(set(was) - set(now)),
                        introduced=sorted(set(now) - set(was)),
                    )
                if args.verbose:
                    entry["reply_recorded"] = tr.reply_fingerprint(row["reply"])
                    entry["reply_replayed"] = tr.reply_fingerprint(res.reply)
            results.append(entry)
        if args.export:
            allowed = _set_conversations(Path(args.set_report))
            for row, res in zip(rows, replayed, strict=True):
                if res.status != "not_replayable" and row["conversation_id"] in allowed:
                    _export(Path(args.export), row)
    finally:
        await close_pool()
    return summarize(results, args, head)


def _set_conversations(report: Path) -> set[str]:
    """Conversațiile create de o rulare `prod_set_run.py` (singurele care pot intra în repo)."""
    doc = json.loads(report.read_text(encoding="utf-8"))
    return {
        c["conversation_id"]
        for c in (doc.get("conversations") or {}).values()
        if c.get("conversation_id")
    }


def _export(out_dir: Path, row: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob(f"{row['conversation_id'][:8]}-*.json")))
    path = out_dir / f"{row['conversation_id'][:8]}-{n}.json"
    doc = {k: row[k] for k in ("turn_id", "conversation_id", "business_id", "client_text")}
    doc.update(
        created_at=str(row["created_at"]), reply=row["reply"], diagnostics=row["diagnostics"]
    )
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def fidelity(results: list[dict[str, Any]], *, gate: bool, head: str | None) -> dict[str, Any]:
    """Poarta instrumentului (PURĂ). Numitorul: toate turele rejucabile fără drift de catalog;
    `diverged` / `wrote` / `db_aborted` contează ca eșec, la fel o intrare schimbată (la același
    release, o intrare diferită e nedeterminism, nu o reparație)."""
    judged = [r for r in results if r["status"] != "not_replayable" and not r.get("catalog_drift")]
    same = [
        r
        for r in judged
        if r["status"] == "replayed" and not r.get("differs") and not r.get("inputs_changed")
    ]
    rate = len(same) / len(judged) if judged else None
    wrong_release = [r["turn_id"] for r in judged if not r.get("same_release")]
    if not gate:
        verdict = None
    elif head is None or wrong_release:
        verdict = "WRONG_RELEASE"
    elif rate is None:
        verdict = "INSUFFICIENT"
    else:
        verdict = "PASS" if rate >= FIDELITY_MIN else "FAIL"
    return {
        "judged": len(judged),
        "same": len(same),
        "rate": round(rate, 3) if rate is not None else None,
        "min": FIDELITY_MIN,
        "head": head,
        "wrong_release": len(wrong_release),
        "verdict": verdict,
    }


def summarize(
    results: list[dict[str, Any]], args: argparse.Namespace, head: str | None
) -> dict[str, Any]:
    return {
        "business": args.business,
        "flags": args.flags,
        "turns": len(results),
        "statuses": dict(Counter(r["status"] for r in results)),
        "fidelity": fidelity(results, gate=args.fidelity, head=head),
        "fixed": dict(Counter(k for r in results for k in r.get("fixed", []))),
        "introduced": dict(Counter(k for r in results for k in r.get("introduced", []))),
        "turns_detail": results,
    }


def render(report: dict[str, Any]) -> str:
    f = report["fidelity"]
    lines = [
        f"tenant {report['business']} · {report['turns']} ture · profil {report['flags']}",
        f"statusuri: {report['statuses']}",
        f"identic cu înregistrarea: {f['same']}/{f['judged']}"
        + (f" ({f['rate']:.1%})" if f["rate"] is not None else "")
        + (f" · verdict {f['verdict']}" if f["verdict"] else ""),
    ]
    if f["wrong_release"]:
        lines.append(f"ture de pe alt release decât HEAD ({f['head']}): {f['wrong_release']}")
    if report["fixed"]:
        lines.append(f"detectori care nu mai prind: {report['fixed']}")
    if report["introduced"]:
        lines.append(f"detectori noi pe replay: {report['introduced']}")
    for r in report["turns_detail"]:
        bits = [r["turn_id"][:8], r["status"]]
        if r["reason"]:
            bits.append(r["reason"])
        for key in ("differs", "inputs_changed", "fixed", "introduced"):
            if r.get(key):
                bits.append(f"{key}={r[key]}")
        if r.get("divergence"):
            bits.append(f"divergence@{r['divergence']['index']}")
        if r.get("catalog_drift"):
            bits.append(f"drift={len(r['catalog_drift'])}")
        lines.append("  " + " ".join(bits))
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
    p.add_argument("--export", help="scrie rândurile ca fișiere de caz (cere --set-report)")
    p.add_argument("--set-report", help="raportul `prod_set_run.py` care listează conversațiile")
    p.add_argument("--verbose", action="store_true", help="include amprentele răspunsurilor")
    args = p.parse_args(argv)
    if args.export and not args.set_report:
        p.error("--export cere --set-report: în repo intră doar conversațiile seturilor noastre")
    if not (args.turn or args.conversation):
        args.since = args.since or datetime.now(UTC) - timedelta(days=1)
    report = asyncio.run(_main(args))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"replay-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(render(report))
    print(f"raport local: {path.relative_to(ROOT)}")
    return 1 if report["fidelity"]["verdict"] in ("FAIL", "WRONG_RELEASE") else 0


if __name__ == "__main__":
    raise SystemExit(main())
