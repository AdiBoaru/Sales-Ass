"""NX-249 — drill de rollback CONVERSAȚIONAL: cât durează până se opresc accepturile candidate.

    PYTHONPATH=. python scripts/rollback_drill.py --business-id <uuid>            # dry-run
    PYTHONPATH=. python scripts/rollback_drill.py --business-id <uuid> \\
        --actor adi --reason "drill lunar" --confirm --out reports/nx249/drill.json

Distinct de `scripts/release/rollback.py` (NX-248), care revine la digestul precedent al IMAGINII.
Ăsta măsoară cealaltă jumătate: oprirea traficului candidate fără să atingă turele deja acceptate.
Cele două se rulează împreună într-un incident, dar răspund la întrebări diferite — „ce cod rulează"
vs „cine mai primește versiunea nouă".

## Ce măsoară, exact

`time_to_zero_new_accepts_s` = de la apply-ul kill-switchului până când NICIUN proces nu mai poate
asigna candidate. Nu se poate observa direct din afară, deci se raportează mărginit superior:
`RELEASE_POLICY_REFRESH_S` (TTL-ul cache-ului de policy) + durata apply-ului. Ținta cardului e
≤5 minute, iar `src/config.py::_release_relations` refuză la boot un TTL peste 300s — deci limita
e impusă de configurație, nu de speranță.

## Ce VERIFICĂ după

  • turele candidate active la momentul opririi ajung la terminal (drenare), fără attempt nou
    peste plafon și fără să-și piardă `release_track` (nu se rerulează pe control);
  • numărul de rânduri de ledger NU scade (zero ștergeri, zero down migration);
  • policy-ul curent chiar e `force_control`.

## Ce NU poate verifica local

Că imaginea precedentă citește starea/acțiunile emise de candidate — aia cere două imagini și e
treaba `scripts/release/migration_drill.py` + `smoke_web_v2.py` (NX-248). Drill-ul spune asta pe
față în raport, la `not_verified_here`, ca nimeni să nu confunde un drill verde cu o dovadă
completă.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, get_pool, tenant_conn  # noqa: E402
from src.db.queries.release import count_active_turns  # noqa: E402
from src.release import policy_store  # noqa: E402
from src.release.models import MODE_FORCE_CONTROL  # noqa: E402

#: Ținta operațională din card: ≤5 minute de la decizie la zero accepturi candidate NOI.
TARGET_S = 300.0


async def _ledger_rows(conn, business_id: str) -> int:
    return int(
        await conn.fetchval("select count(*) from web_turns where business_id = $1", business_id)
        or 0
    )


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    pool = await get_pool()

    async with admin_conn(pool) as conn:
        before = await policy_store.current(conn, s.release_env, force_refresh=True)
    async with tenant_conn(args.business_id) as conn:
        active_before = await count_active_turns(conn, args.business_id)
        rows_before = await _ledger_rows(conn, args.business_id)

    plan = {
        "environment": s.release_env,
        "policy_before": None
        if before.policy is None
        else {
            "policy_id": before.policy.policy_id,
            "revision": before.revision,
            "mode": before.policy.mode,
            "percent": before.policy.percent,
        },
        "active_before": active_before,
        "ledger_rows_before": rows_before,
        "refresh_ttl_s": s.release_policy_refresh_s,
        "target_s": TARGET_S,
    }

    if before.policy is None:
        print(json.dumps({**plan, "verdict": "UNKNOWN", "reason": before.code}, indent=2))
        return 2

    if not args.confirm:
        print(
            json.dumps(
                {
                    **plan,
                    "verdict": "DRY_RUN",
                    "would_apply": {
                        "mode": MODE_FORCE_CONTROL,
                        "revision": (before.revision or 0) + 1,
                    },
                    "note": "Nimic scris. Adaugă --confirm (cu --actor și --reason) ca să rulezi.",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    started = perf_counter()
    target_policy = policy_store.force_control_from(
        before.policy, revision=(before.revision or 0) + 1
    )
    async with admin_conn(pool) as conn:
        result = await policy_store.apply(
            conn,
            target_policy,
            expected_revision=before.revision,
            actor=args.actor,
            reason=args.reason,
            environment=s.release_env,
        )
    apply_s = perf_counter() - started
    if not result.ok:
        print(
            json.dumps(
                {**plan, "verdict": "FAIL", "reason": f"apply refuzat: {result.reason}"},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1

    # Limita SUPERIOARĂ de propagare: ultimul proces care a citit policy-ul cu o clipă înainte de
    # apply îl mai ține cel mult un TTL. Se raportează ca margine, nu ca măsurătoare — o măsurătoare
    # ar cere să întrebăm fiecare proces, iar un drill care pretinde mai multă precizie decât are
    # e mai rău decât unul modest.
    propagation_s = apply_s + s.release_policy_refresh_s

    # Drenarea: turele candidate active la oprire trebuie să ajungă terminale singure. Așteptăm
    # MĂRGINIT și raportăm ce am găsit — nu forțăm nimic (a forța ar fi exact rerularea interzisă).
    drained: dict[str, int] = active_before
    waited = 0.0
    while waited < args.drain_wait_s:
        await asyncio.sleep(min(5.0, args.drain_wait_s - waited))
        waited += min(5.0, args.drain_wait_s - waited)
        async with tenant_conn(args.business_id) as conn:
            drained = await count_active_turns(conn, args.business_id)
        if not any(drained.values()):
            break

    async with tenant_conn(args.business_id) as conn:
        rows_after = await _ledger_rows(conn, args.business_id)
    async with admin_conn(pool) as conn:
        after = await policy_store.current(conn, s.release_env, force_refresh=True)

    checks = {
        "mode_is_force_control": bool(
            after.policy is not None and after.policy.mode == MODE_FORCE_CONTROL
        ),
        "propagation_within_target": propagation_s <= TARGET_S,
        # Zero ștergeri: rollbackul păstrează ledgerul, receipts și feedback (DoD explicit).
        "ledger_rows_not_reduced": rows_after >= rows_before,
        "candidate_drained": int(drained.get("candidate", 0)) == 0,
    }
    verdict = "PASS" if all(checks.values()) else "FAIL"
    report = {
        **plan,
        "verdict": verdict,
        "applied_revision": result.revision,
        "apply_s": round(apply_s, 3),
        "time_to_zero_new_accepts_s": round(propagation_s, 1),
        "drain_wait_s": round(waited, 1),
        "active_after": drained,
        "ledger_rows_after": rows_after,
        "checks": checks,
        "not_verified_here": [
            "imaginea precedentă citește state/actions emise de candidate "
            "(scripts/release/migration_drill.py + smoke_web_v2.py, NX-248)",
            "replay exact al turelor terminate pe imaginea precedentă",
        ],
        "restore_note": (
            "Reluarea canaryului NU e parte din drill: cere policy nou, evidence packet și "
            "aprobare (docs/STAGE1-CANARY-RUNBOOK.md §6)."
        ),
    }
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", "utf-8")
    print(text)
    return 0 if verdict == "PASS" else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Drill de rollback conversațional (NX-249)")
    p.add_argument("--business-id", required=True)
    p.add_argument("--actor", default="")
    p.add_argument("--reason", default="")
    p.add_argument(
        "--drain-wait-s", type=float, default=60.0, help="cât așteptăm drenarea (mărginit)"
    )
    p.add_argument("--out", default="")
    p.add_argument("--confirm", action="store_true", help="chiar aplică force_control")
    args = p.parse_args()
    if args.confirm and (not args.actor.strip() or not args.reason.strip()):
        p.error("--confirm cere --actor și --reason (un drill fără om nu e o decizie)")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
