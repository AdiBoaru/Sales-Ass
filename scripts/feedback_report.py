"""NX-246 (felia 2) — raportul de feedback: `positive_feedback_rate` cu `n` și interval.

    PYTHONPATH=. python scripts/feedback_report.py --business-id <uuid> --window 7d
    PYTHONPATH=. python scripts/feedback_report.py --business-id <uuid> --window 30d --json

Tenant-scoped OBLIGATORIU (P7), pe `tenant_conn` (RLS activ), cu agregarea făcută ÎN SQL:
raportul nu vede voturi individuale, doar numere — deci nu are ce scrie într-un artefact.

**Nu tipărește niciodată „CSAT".** Sub `MIN_FEEDBACK_SAMPLE` verdictul e `insufficient_sample` și
NU există procent — nici 0%, nici „n/a" care s-ar citi ca zero. Vezi
`src/observability/feedback_stats.py` pentru de ce intervalul e Wilson și nu Wald.

Exit codes: `0` raport emis (chiar și `insufficient_sample` — lipsa de date nu e o eroare de
script), `2` fereastră invalidă / tabel absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import tenant_conn  # noqa: E402
from src.db.queries.feedback import tally_feedback  # noqa: E402
from src.observability.feedback_stats import (  # noqa: E402
    VERDICT_INSUFFICIENT,
    Tally,
    build_report,
)
from src.observability.slo import window_bounds  # noqa: E402
from src.web.action_models import FEEDBACK_TAXONOMY_VERSION  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    try:
        window_from, window_to = window_bounds(datetime.now(UTC), args.window)
    except ValueError as e:
        print(f"eroare: {e}")
        return 2
    async with tenant_conn(args.business_id) as conn:
        exists = await conn.fetchval("select to_regclass('public.web_feedback') is not null")
        if not exists:
            print("web_feedback lipsește (migrarea 042 neaplicată — rulează scripts/migrate.py)")
            return 2
        rows = await tally_feedback(
            conn, args.business_id, window_from=window_from, window_to=window_to
        )
    report = build_report(
        [Tally(r.rating, r.reason_code, r.release_track, r.n) for r in rows],
        window_from=window_from,
        window_to=window_to,
        business_id=args.business_id,
        taxonomy_version=FEEDBACK_TAXONOMY_VERSION,
        min_sample=args.min_sample,
    )
    payload = report.as_dict()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(payload)
    return 0


def _print_human(p: dict) -> None:
    print(f"Feedback — {p['business_id']}  ({p['taxonomy_version']})")
    print(f"  fereastră: {p['window']['from']} → {p['window']['to']} (UTC)")
    print(f"  voturi:    n={p['n']}  pozitive={p['positive']}  negative={p['negative']}")
    if p["verdict"] == VERDICT_INSUFFICIENT:
        # Deliberat FĂRĂ procent: un „0%" lângă „date insuficiente" ar fi citit ca zero.
        print(f"  VERDICT:   {p['verdict']} (prag: {p['min_sample']} voturi)")
    else:
        low, high = p["confidence_interval_95"]
        print(
            f"  positive_feedback_rate: {p['positive_feedback_rate'] * 100:.1f}% "
            f"(95% CI: {low * 100:.1f}% – {high * 100:.1f}%)"
        )
    if p["by_reason"]:
        print("\n  motive (voturi negative):")
        for reason, n in p["by_reason"].items():
            print(f"    {reason:<16} {n}")
    if p["by_release_track"]:
        print("\n  pe release track:")
        for track, entry in p["by_release_track"].items():
            rate = entry["positive_feedback_rate"]
            shown = f"{rate * 100:.1f}%" if rate is not None else entry["verdict"]
            print(f"    {track:<12} n={entry['n']:<6} {shown}")


def main() -> int:
    p = argparse.ArgumentParser(description="Raport de feedback web (NX-246)")
    p.add_argument("--business-id", required=True, help="tenantul (P7: raportul e tenant-scoped)")
    p.add_argument("--window", default="7d", help="1h | 6h | 7d | 30d (default: 7d)")
    p.add_argument("--min-sample", type=int, default=30, help="prag sub care nu se emite procent")
    p.add_argument("--out", default="", help="scrie artefactul JSON aici")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
