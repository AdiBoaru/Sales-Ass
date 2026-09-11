"""NX-290 — se poate ȘTERGE transportul async v1? Raportul de telemetrie al retragerii.

    PYTHONPATH=. python scripts/legacy_route_report.py --business-id <uuid> [--hours 672]

Raportul răspunde la o SINGURĂ întrebare, și e important care: „a chemat cineva rutele retrase în
fereastra observată?". NU răspunde la „e sigur să ștergem" — aia are două condiții, iar asta e
doar una. A doua (inventarul de clienți: ce cheamă repo-urile pe care le deținem) nu se poate citi
din telemetrie și se consemnează în `docs/WEB-TRANSPORT-CONSOLIDATION.md`.

De ce patru verdicte și nu două: „zero apeluri" are două cauze care nu seamănă deloc. Ori nimeni
nu cheamă ruta (dovadă), ori n-a rulat nimic în fereastră (absența dovezii). Un raport care le
confundă e mai periculos decât unul care lipsește, fiindcă produce un GO din tăcere — exact
capcana numită la NX-238 și NX-246. De aceea numitorul (ture observate) e obligatoriu, iar
`UNKNOWN` e un verdict de sine stătător.

Exit: `0` SAFE_TO_REMOVE · `1` BLOCKED (există apeluri) · `2` UNKNOWN/INSUFFICIENT.
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

from src.db.connection import admin_conn, get_pool  # noqa: E402
from src.db.queries.analytics import tally_legacy_route_calls  # noqa: E402
from src.web import deprecation  # noqa: E402

# Câte ture trebuie să fi rulat în fereastră ca o absență de apeluri să însemne ceva. Pragul e
# mic deliberat: nu măsurăm o proporție, ci existența unui client. Un client real cheamă ruta la
# FIECARE conversație, deci câteva zeci de ture servite fac tăcerea concludentă.
MIN_OBSERVED_TURNS = 50


def evaluate_removal(
    *,
    calls: dict[str, dict[str, int]],
    observed_turns: int,
    window_hours: float,
    sunset_passed: bool,
    min_observed_turns: int = MIN_OBSERVED_TURNS,
) -> dict:
    """Verdictul, PUR (testabil fără DB). Ordinea condițiilor e ordinea în care se repară."""
    total = sum(sum(o.values()) for o in calls.values())
    reasons: list[str] = []

    if total:
        verdict = "BLOCKED"
        reasons.append(f"{total} apeluri pe rutele retrase în ultimele {window_hours:.0f}h")
    elif observed_turns == 0:
        verdict = "UNKNOWN"
        reasons.append(
            "zero ture servite în fereastră — absența apelurilor nu dovedește nimic "
            "(sistemul n-a rulat, deci nimeni n-avea ce chema)"
        )
    elif observed_turns < min_observed_turns:
        verdict = "INSUFFICIENT"
        reasons.append(f"{observed_turns} ture observate < {min_observed_turns} cerute")
    elif not sunset_passed:
        verdict = "INSUFFICIENT"
        reasons.append("data de sunset anunțată public n-a trecut încă")
    else:
        verdict = "SAFE_TO_REMOVE"

    return {
        "schema_version": "legacy-route-report.v1",
        "verdict": verdict,
        "window_hours": round(window_hours, 1),
        "calls_total": total,
        "calls_by_route": calls,
        "observed_turns": observed_turns,
        "min_observed_turns": min_observed_turns,
        "sunset_passed": sunset_passed,
        "routes": [
            {"path": e.path, "successor": e.successor, "sunset": e.sunset_at.isoformat()}
            for e in deprecation.LEGACY_ASYNC_ROUTES
        ],
        "blocking_reasons": reasons,
        "note": (
            "Verdictul acoperă DOAR poarta de telemetrie. Ștergerea mai cere inventarul de "
            "clienți (repo-urile proprii, verificat pe main și pe build) — vezi "
            "docs/WEB-TRANSPORT-CONSOLIDATION.md §5."
        ),
    }


async def _run(args: argparse.Namespace) -> int:
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    # `bot_runtime` are DOAR INSERT pe `analytics_events` (append-only, 003) → un raport de ops
    # citește pe `admin_conn`, exact ca `scripts/turn_replay.py`. Izolarea rămâne în COD: ambele
    # interogări filtrează explicit `business_id` (P7). Zero PII în ieșire — se agregă doar rute,
    # rezultate și numere.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        calls, observed = await tally_legacy_route_calls(conn, args.business_id, since)

    report = evaluate_removal(
        calls=calls,
        observed_turns=observed,
        window_hours=float(args.hours),
        sunset_passed=deprecation.sunset_passed(
            deprecation.LEGACY_ASYNC_ROUTES[0], datetime.now(UTC)
        ),
    )
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", "utf-8")
    print(text)
    return {"SAFE_TO_REMOVE": 0, "BLOCKED": 1}.get(report["verdict"], 2)


def main() -> int:
    p = argparse.ArgumentParser(description="Se poate șterge transportul async v1? (NX-290)")
    p.add_argument("--business-id", required=True)
    p.add_argument("--hours", type=int, default=672, help="fereastra observată (implicit 28 zile)")
    p.add_argument("--out", default="")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
