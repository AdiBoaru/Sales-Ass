"""NX-249 — checkerul de închidere a rutei v1. Refuză cât timp mai există trafic v1 în zbor.

    PYTHONPATH=. python scripts/cutover_check.py --business-id <uuid>

Etapa 7 („close v1") e singura ireversibilă ieftin: după ce ruta publică se închide, un client cu
un turn în zbor rămâne fără răspuns, iar P6 („niciodată tăcere") nu se mai poate respecta
retroactiv. De aceea checkerul e conservator prin construcție și eșuează în direcția sigură.

## Cum se recunoaște un turn v1 „in-flight"

Nu după timp și nu după rută (requestul HTTP e demult terminat). Structural: controllerul NX-249
capturează un `release_track` la FIECARE accept v2; acceptul sincron v1 nu capturează niciodată.
Deci un rând `accepted|running` FĂRĂ captură e, prin construcție, ori un turn v1, ori unul acceptat
înainte ca controllerul să fie pornit — și amândouă trebuie să blocheze închiderea.

Consecința e deliberată: pe un sistem unde controllerul tocmai a fost aprins, checkerul refuză până
când turele vechi se termină. Asta nu e un fals pozitiv, e chiar garanția.

Exit codes: `0` se poate închide · `1` NU se poate (există trafic) · `2` nu se poate stabili.
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

from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, get_pool, tenant_conn  # noqa: E402
from src.db.queries.release import count_active_turns  # noqa: E402
from src.release import policy_store  # noqa: E402
from src.release.models import MODE_CLOSED, STAGES_BY_INDEX, stage_for  # noqa: E402


def evaluate_closure(
    *,
    active: dict[str, int],
    policy_mode: str | None,
    policy_stage_index: int | None,
    soak_hours: float | None,
) -> dict:
    """Verdictul de închidere, PUR (testabil fără DB).

    Patru condiții, toate necesare. Ordinea din raport e ordinea în care se repară:
      1. există un policy și el descrie etapa 6 sau 7 (nu se sare de la 20% la închidere);
      2. zero ture v1 in-flight (rânduri active fără captură);
      3. zero ture active pe control (cohortul champion trebuie drenat, nu doar v1);
      4. soakul minim al etapei 6 s-a scurs.
    """
    reasons: list[str] = []
    uncaptured = int(active.get("unknown", 0))
    champion = int(active.get("champion", 0))
    candidate = int(active.get("candidate", 0))

    stage6 = STAGES_BY_INDEX[6]
    if policy_mode is None:
        reasons.append("nu există policy în vigoare — starea releaseului e necunoscută")
    elif policy_stage_index is None or policy_stage_index < 6:
        reasons.append(
            f"policy-ul e la etapa {policy_stage_index if policy_stage_index is not None else '?'}"
            "; închiderea v1 cere etapa 6 (100% conversații noi) parcursă"
        )
    if uncaptured:
        reasons.append(f"{uncaptured} ture active FĂRĂ captură de release (v1 in-flight)")
    if champion:
        reasons.append(f"{champion} ture active pe control — drenarea nu s-a terminat")
    if soak_hours is None:
        reasons.append("nu se poate calcula soakul (fără policy aplicat)")
    elif soak_hours < stage6.min_hours:
        reasons.append(f"soak {soak_hours:.1f}h < {stage6.min_hours:.0f}h ceruți de etapa 6")

    can_close = not reasons
    return {
        "schema_version": "release-cutover-check.v1",
        "can_close_v1": can_close,
        "verdict": "READY" if can_close else "BLOCKED",
        "active_turns": {
            "uncaptured_v1": uncaptured,
            "champion": champion,
            "candidate": candidate,
        },
        "policy_mode": policy_mode,
        "policy_stage_index": policy_stage_index,
        "soak_hours": None if soak_hours is None else round(soak_hours, 2),
        "blocking_reasons": reasons,
        "note": (
            "Un `READY` nu închide nimic: aplicarea modului `closed` cere policy nou, evidence "
            "packet PASS și aprobarea explicită a userului (etapa 7)."
        ),
    }


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        view = await policy_store.current(conn, s.release_env, force_refresh=True)
    async with tenant_conn(args.business_id) as conn:
        active = await count_active_turns(conn, args.business_id)

    stage = stage_for(view.policy) if view.policy else None
    soak = None
    if view.applied_at is not None:
        soak = max(0.0, (datetime.now(UTC) - view.applied_at).total_seconds() / 3600.0)
    report = evaluate_closure(
        active=active,
        policy_mode=view.policy.mode if view.policy else None,
        policy_stage_index=stage.index if stage else None,
        soak_hours=soak,
    )
    if view.policy is not None and view.policy.mode == MODE_CLOSED:
        report["note"] = "Ruta v1 e deja marcată închisă în policy (mod `closed`)."

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8"
        )
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    if view.policy is None:
        return 2
    return 0 if report["can_close_v1"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Se poate închide ruta v1? (NX-249, etapa 7)")
    p.add_argument("--business-id", required=True)
    p.add_argument("--out", default="")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
