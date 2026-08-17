"""NX-246 — raportul SLI/SLO, reproductibil, dintr-un singur artefact.

    PYTHONPATH=. python scripts/slo_report.py --business-id <uuid> --window 7d --json
    PYTHONPATH=. python scripts/slo_report.py --business-id <uuid> --window 1h \\
        --metrics-json reports/metrics_snapshot.json --out reports/slo/2026-08-17.json

De ce un script și nu un panel: un dashboard în care fiecare grafic își definește propria formulă
produce verdicte care nu se pot reproduce — și, mai rău, care nu se pot contesta. Aici formula e
`src/observability/slo.py` (`slo_policy.v1`, testată), iar raportul cară cu el fereastra, granițele
UTC, versiunile și indicatorul de completitudine. Alertele NX-248 se derivă din artefactul ăsta,
nu din praguri copiate de mână în UI.

**Tenant-scoped, obligatoriu** (`--business-id`). P7 nu are excepție pentru rapoarte: query-ul
atinge `web_turns`, care conține `response_json` — conținut de conversație. Fleet-wide se face
rulând scriptul per tenant, nu relaxând poarta.

Exit codes (pentru CI/NX-247): `0` PASS · `1` FAIL · `2` UNKNOWN/INSUFFICIENT. Lipsa datelor NU e
zero: un gate care trece pe „n-am găsit nimic" e un gate fail-open.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Consola Windows e cp1252: fără asta, orice diacritică din help/raport aruncă
# `UnicodeEncodeError` înainte să apuce să tipărească ceva (convenția din
# `scripts/action_drive.py`).
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import tenant_conn  # noqa: E402
from src.db.queries.web_turn_slo import load_turn_facts  # noqa: E402
from src.observability.slo import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_PASS,
    evaluate,
    window_bounds,
)

_EXIT = {VERDICT_PASS: 0, VERDICT_FAIL: 1}


def _accept_metrics(path: str | None) -> dict[str, float] | None:
    """Contoarele de margine dintr-un snapshot exportat de proces.

    Nu le putem citi direct: `web_turn_requests_total` trăiește în memoria procesului API, iar
    raportul rulează în alt proces. Fără snapshot, SLI-ul rămâne `UNKNOWN` — vizibil ca lipsă, nu
    absent din raport și nici presupus verde.
    """
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    counters = data.get("counters", data)
    out: dict[str, float] = {}
    for key, value in counters.items():
        if not key.startswith("web_turn_requests_total"):
            continue
        # `web_turn_requests_total{outcome=accepted,release_track=champion}` → `accepted`
        labels = key[key.find("{") + 1 : -1] if "{" in key else ""
        for part in labels.split(","):
            name, _, val = part.partition("=")
            if name == "outcome":
                out[val] = out.get(val, 0.0) + float(value)
    return out or None


async def _run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    window_from, window_to = window_bounds(now, args.window)
    # `tenant_conn` = rolul `bot_runtime` cu RLS ACTIV (nu `admin_conn`): raportul citește date de
    # tenant, deci trece prin aceeași plasă ca workerul. Un `business_id` greșit dă zero rânduri,
    # nu datele altui client.
    async with tenant_conn(args.business_id) as conn:
        page = await load_turn_facts(
            conn,
            args.business_id,
            window_from=window_from,
            window_to=window_to,
            limit=args.limit,
        )
    report = evaluate(
        page.facts,
        window_from=window_from,
        window_to=window_to,
        business_id=args.business_id,
        release_sha=args.release_sha,
        truncated=page.truncated,
        accept_metrics=_accept_metrics(args.metrics_json),
        ratified=args.ratified,
    )
    payload = report.as_dict()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(payload)
    return _EXIT.get(report.verdict, 2)


def _print_human(payload: dict) -> None:
    print(f"SLO {payload['policy_version']} — {payload['business_id']}")
    print(f"  fereastră: {payload['window']['from']} → {payload['window']['to']} (UTC)")
    print(f"  release:   {payload['release_sha']}")
    print(f"  VERDICT:   {payload['verdict']}")
    print()
    for sli in payload["slis"]:
        ratio = "n/a" if sli["ratio"] is None else f"{sli['ratio'] * 100:.3f}%"
        target = "—" if sli["target"] is None else f"{sli['target'] * 100:.3f}%"
        print(
            f"  {sli['verdict']:<12} {sli['name']:<24} {ratio:>10} / {target:<10} "
            f"n={sli['denominator']:<6} [{sli['source']}]"
        )
        if sli["note"]:
            print(f"               ↳ {sli['note']}")
        if sli["excluded"]:
            print(f"               ↳ excluse: {sli['excluded']}")
    print()
    print(f"  latență (ms): {payload['latency_ms']}")
    if payload["invalid_samples"]:
        print(f"  eșantioane invalide: {payload['invalid_samples']}")
    print(f"  completitudine: {payload['completeness']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Raport SLI/SLO din ledgerul web_turns (NX-246)")
    p.add_argument("--business-id", required=True, help="tenantul (P7: raportul e tenant-scoped)")
    p.add_argument("--window", default="7d", help="1h | 6h | 7d (default: 7d)")
    p.add_argument("--release-sha", default="unknown")
    p.add_argument("--metrics-json", default="", help="snapshot de metrici pentru accept SLI")
    p.add_argument("--out", default="", help="scrie artefactul JSON aici")
    p.add_argument("--json", action="store_true", help="printează JSON în loc de tabel")
    p.add_argument(
        "--limit", type=int, default=50_000, help="plafon de rânduri (trunchierea e RAPORTATĂ)"
    )
    p.add_argument(
        "--ratified",
        action="store_true",
        help="aplică pragurile de latență NX-241 ca verdict (implicit: raportate, nu judecate)",
    )
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
