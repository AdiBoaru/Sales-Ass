"""NX-249 — evidence packetul unei etape: un artefact, un verdict, zero identificatori.

    PYTHONPATH=. python scripts/canary_report.py --business-id <uuid> --window 48h \\
        --slo reports/slo/2026-08-18.json \\
        --quality reports/nx246/quality_gate.json \\
        --e2e qa-suite/stage1/web-v2/run-certificate.json \\
        --deploy reports/nx248/evidence.json \\
        --feedback reports/nx246/feedback.json \\
        --out reports/nx249/packet-stage3.json

Raportul NU măsoară nimic nou. Citește ledgerul (cohorturile) și CITEAZĂ artefactele deja produse
de NX-246/247/248, apoi aplică porțile din `src/release/gates.py`. Motivul e că un al doilea loc
care calculează SLO ar produce, inevitabil, un al doilea adevăr — iar când cele două nu coincid,
se alege cel convenabil.

Exit codes: `0` PASS · `1` FAIL · `2` INSUFFICIENT/UNKNOWN. Lipsa datelor nu e zero (NX-246).

`--hard-stop CODE` marchează un incident confirmat manual (vocabular închis, vezi
`gates.HARD_STOPS`). Un singur cod trece verdictul pe FAIL, indiferent de restul cifrelor — asta e
regula „un singur hard grounding/P6/tenant failure → freeze, indiferent de conversie".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, get_pool, tenant_conn  # noqa: E402
from src.db.queries.release import load_cohort_facts  # noqa: E402
from src.release import policy_store  # noqa: E402
from src.release.gates import (  # noqa: E402
    HARD_STOP_SET,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from src.release.report import artifact_hash, build_packet, load_artifact  # noqa: E402

_EXIT = {VERDICT_PASS: 0, VERDICT_FAIL: 1}


def _window(spec: str, now: datetime) -> tuple[datetime, datetime]:
    unit, amount = spec[-1:].lower(), spec[:-1]
    try:
        n = int(amount)
    except ValueError as e:
        raise SystemExit(f"fereastră invalidă: {spec!r} (fix: 48h, 7d)") from e
    if n <= 0:
        raise SystemExit(f"fereastră invalidă: {spec!r}")
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}.get(unit)
    if delta is None:
        raise SystemExit(f"unitate necunoscută: {spec!r} (fix: m/h/d)")
    return now - delta, now


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    now = datetime.now(UTC)
    window_from, window_to = _window(args.window, now)

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        view = await policy_store.current(conn, s.release_env, force_refresh=True)
    if view.policy is None:
        # Fără policy nu există etapă, deci nu există ce raporta. Onest, nu un packet gol care ar
        # putea fi confundat cu „am măsurat și nu e nimic în neregulă".
        print(f"UNKNOWN: niciun policy în vigoare pe {s.release_env} (stare: {view.code})")
        return 2

    # `tenant_conn` (rolul `bot_runtime`, RLS activ): raportul citește date de tenant, deci trece
    # prin aceeași plasă ca workerul — ca la `slo_report.py`.
    async with tenant_conn(args.business_id) as conn:
        page = await load_cohort_facts(
            conn,
            args.business_id,
            window_from=window_from,
            window_to=window_to,
            limit=args.limit,
        )

    unknown_codes = sorted(set(args.hard_stop or []) - HARD_STOP_SET)
    if unknown_codes:
        # Nu-l respingem — un incident real nu trebuie să aștepte un PR ca să fie raportat — dar îl
        # spunem tare, fiindcă un cod în afara vocabularului nu se poate agrega între rapoarte.
        print(f"ATENȚIE: hard stop în afara vocabularului: {unknown_codes}", file=sys.stderr)

    packet = build_packet(
        view.policy,
        business_id=args.business_id,
        window_from=window_from,
        window_to=window_to,
        facts=page.facts,
        truncated=page.truncated,
        hard_stops=list(args.hard_stop or []),
        slo_artifact=load_artifact(args.slo) if args.slo else None,
        quality_artifact=load_artifact(args.quality) if args.quality else None,
        e2e_artifact=load_artifact(args.e2e) if args.e2e else None,
        deploy_evidence=load_artifact(args.deploy) if args.deploy else None,
        feedback_artifact=load_artifact(args.feedback) if args.feedback else None,
        artifact_hashes={
            "quality_packet_hash": artifact_hash(args.quality) if args.quality else "",
            "e2e_packet_hash": artifact_hash(args.e2e) if args.e2e else "",
            "deploy_manifest_hash": artifact_hash(args.deploy) if args.deploy else "",
        },
        incidents=list(args.incident or []),
        generated_at=now.isoformat(),
    )
    payload = packet.as_dict()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8"
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _print_human(payload)
    return _EXIT.get(packet.verdict, 2)


def _print_human(payload: dict) -> None:
    print(f"NX-249 evidence packet — etapa {payload['stage']}")
    print(f"  policy:    {payload['policy']['policy_id']} rev={payload['policy']['revision']}")
    print(f"  fereastră: {payload['window']['from']} → {payload['window']['to']} (UTC)")
    print(f"  tenant:    {payload['tenant_bucket']} (bucket, nu ID)")
    print(f"  VERDICT:   {payload['verdict']}")
    print()
    for gate in payload["gates"]["gates"]:
        print(f"  {gate['verdict']:<13} {gate['gate']}")
        if gate["note"]:
            print(f"                ↳ {gate['note']}")
    print()
    alloc = payload["allocation"]
    print(
        f"  alocare: {alloc['policy_percent']}% conversații noi · observat "
        f"{alloc['observed_turn_share']} din ture "
        f"(candidate={alloc['candidate_turns']}, control={alloc['control_turns']}, "
        f"fără captură={alloc['uncaptured_turns']})"
    )
    for track, cohort in payload["cohorts"].items():
        print(
            f"  {track:<10} n={cohort['turns']:<5} terminal={cohort['terminal']:<5} "
            f"failed={cohort['failed']:<4} latență={cohort['latency_ms']}"
        )
    print()
    print(f"  lipsesc: {payload['completeness']['missing']}")
    print(f"  amprentă: {payload['fingerprint']}")
    print(f"\n  {payload['gates']['promotion']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Evidence packet pentru o etapă de canary (NX-249)")
    p.add_argument("--business-id", required=True, help="tenantul (P7: raportul e tenant-scoped)")
    p.add_argument("--window", default="48h", help="48h | 7d | 30m (default: 48h)")
    p.add_argument("--slo", default="", help="artefactul NX-246 slo_report.py")
    p.add_argument("--quality", default="", help="artefactul NX-246 web_quality_eval.py")
    p.add_argument("--e2e", default="", help="certificatul NX-247")
    p.add_argument("--deploy", default="", help="evidence packetul NX-248")
    p.add_argument("--feedback", default="", help="artefactul NX-246 feedback_report.py")
    p.add_argument(
        "--hard-stop", action="append", default=[], help="cod de hard stop confirmat manual"
    )
    p.add_argument("--incident", action="append", default=[], help="referință de incident")
    p.add_argument("--out", default="", help="scrie packetul JSON aici")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=50_000)
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
