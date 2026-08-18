"""NX-249 — controlul releaseului din linia de comandă: `show | validate | plan | apply | history`.

    PYTHONPATH=. python scripts/release_control.py show
    PYTHONPATH=. python scripts/release_control.py validate --policy policies/pilot-5.json
    PYTHONPATH=. python scripts/release_control.py plan --policy policies/pilot-5.json --ids 10000
    PYTHONPATH=. python scripts/release_control.py apply --policy policies/pilot-5.json \\
        --expected-revision 3 --actor adi --reason "etapa 3, packet 7f2c" \\
        --evidence reports/nx249/packet-stage3.json --confirm
    PYTHONPATH=. python scripts/release_control.py apply --force-control \\
        --expected-revision 4 --actor oncall --reason "INC-42 grounding" --confirm

## De ce dry-run e implicit

`apply` fără `--confirm` NU scrie nimic: arată diff-ul (revizie veche → nouă, mod, procent) și se
oprește. Un kill-switch pe care îl apeși din greșeală în timp ce citești `--help` e un kill-switch
care nu va fi folosit când trebuie.

## Ce cere `apply`, fără excepție

`--expected-revision` (CAS: dacă altcineva a aplicat între timp, refuzăm — decizia lui poate fi
chiar oprirea incidentului), `--actor`, `--reason` și, pentru orice mod care livrează candidate,
un `--evidence` al cărui verdict e PASS și a cărui amprentă se recalculează pe loc. Un packet
editat manual nu trece: amprenta se reface din conținut, nu se citește din fișier.

**Niciun input din widget sau din model nu ajunge aici.** Nu există rută HTTP care să schimbe
policy-ul; singurul drum e CLI-ul ăsta, cu un credential de control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, get_pool  # noqa: E402
from src.db.queries.release import policy_history  # noqa: E402
from src.release import policy_store  # noqa: E402
from src.release.assignment import chi_square_uniformity, distribution  # noqa: E402
from src.release.models import (  # noqa: E402
    CANDIDATE_MODES,
    MODE_FORCE_CONTROL,
    PolicyError,
    ReleasePolicy,
    stage_for,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CONFLICT = 2
EXIT_INVALID = 3


def _load_policy(path: str) -> ReleasePolicy:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return ReleasePolicy.from_payload(raw)


def _describe(policy: ReleasePolicy) -> str:
    return (
        f"{policy.policy_id} rev={policy.revision} mode={policy.mode} percent={policy.percent}% "
        f"stage={stage_for(policy).label} "
        f"candidate={policy.candidate_release_sha[:12]} control={policy.control_release_sha[:12]}"
    )


async def _cmd_show(args: argparse.Namespace) -> int:
    s = get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        view = await policy_store.current(conn, s.release_env, force_refresh=True)
    print(f"mediu:   {s.release_env}")
    print(f"stare:   {view.code} (store {'ok' if view.available else 'INDISPONIBIL'})")
    if view.policy is None:
        print("policy:  — (asignarea e fail-closed la control)")
        return EXIT_OK
    print(f"policy:  {_describe(view.policy)}")
    print(f"aplicat: rev={view.revision} de {view.actor} la {view.applied_at}")
    print(f"amprentă:{view.policy.fingerprint}")
    print(f"valid:   {view.policy.not_before} → {view.policy.expires_at}")
    print(
        f"eligible: {len(view.policy.eligible_business_ids)} tenanți, "
        f"intern: {len(view.policy.internal_business_ids)}"
    )
    return EXIT_OK


async def _cmd_history(args: argparse.Namespace) -> int:
    s = get_settings()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await policy_history(conn, s.release_env, limit=args.limit)
    if not rows:
        print("(niciun policy aplicat)")
        return EXIT_OK
    for row in rows:
        print(
            f"rev={row.revision:<4} {row.applied_at.isoformat()} "
            f"mode={row.policy.get('mode', '?'):<14} percent={row.policy.get('percent', '?'):<4} "
            f"actor={row.actor} ticket={row.change_ticket or '—'}"
        )
        print(f"      motiv: {row.reason}")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        policy = _load_policy(args.policy)
    except (OSError, ValueError, PolicyError) as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return EXIT_INVALID
    stage = stage_for(policy)
    print(f"VALID: {_describe(policy)}")
    print(f"amprentă: {policy.fingerprint}")
    print(f"etapa cere: ≥{stage.min_hours:.0f}h și ≥{stage.min_candidate_turns} ture candidate")
    print(f"poartă suplimentară: {stage.extra_gate}")
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace) -> int:
    """Distribuția pe ID-uri SINTETICE, înainte de a atinge trafic real.

    ID-urile sunt generate aici, deterministe și evident false (`plan-<n>`): raportul nu conține
    și nu poate conține identificatori reali (manual drive-ul cardului o cere explicit — „zero
    ID-uri în report labels").
    """
    try:
        policy = _load_policy(args.policy)
    except (OSError, ValueError, PolicyError) as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return EXIT_INVALID
    salt = get_settings().release_assignment_salt
    business = policy.eligible_business_ids[0] if policy.eligible_business_ids else "plan-tenant"
    pairs = [(business, f"plan-conversation-{i}") for i in range(args.ids)]
    hist = distribution(policy, pairs, salt=salt)
    in_rollout = sum(n for b, n in hist.items() if b < policy.percent)
    chi2 = chi_square_uniformity(hist)
    print(f"policy:   {_describe(policy)}")
    print(f"ID-uri:   {args.ids} (sintetice, deterministe)")
    print(
        f"în canary: {in_rollout} ({in_rollout / max(1, args.ids) * 100:.2f}%) "
        f"— țintă {policy.percent}%"
    )
    print(f"bucketuri ocupate: {len(hist)}/100")
    print(f"χ² uniformitate: {chi2:.1f} (99 g.l.; ~123 e pragul 5% — diagnostic, nu verdict)")
    print(f"min/max per bucket: {min(hist.values(), default=0)}/{max(hist.values(), default=0)}")
    return EXIT_OK


def _evidence_ok(path: str) -> tuple[bool, str]:
    """Packetul de evidență: există, are verdict PASS, iar amprenta se RECALCULEAZĂ din conținut.

    Recalcularea e esența: fără ea, „PASS" ar fi un cuvânt scris de cine vrea, iar `apply` ar
    deveni un formular de bifat. Cu ea, cine editează raportul îi rupe amprenta.
    """
    if not path:
        return False, "lipsește --evidence"
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"packet ilizibil: {type(e).__name__}"
    if not isinstance(payload, dict):
        return False, "packetul nu e un obiect JSON"
    claimed = payload.get("fingerprint", "")
    body = {k: v for k, v in payload.items() if k not in ("fingerprint", "generated_at")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    actual = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if claimed != actual:
        return False, "amprenta packetului nu corespunde conținutului (editat manual?)"
    verdict = payload.get("verdict")
    if verdict != "PASS":
        return False, f"verdictul packetului e {verdict!r}, nu PASS"
    return True, actual


async def _cmd_apply(args: argparse.Namespace) -> int:
    s = get_settings()
    pool = await get_pool()

    if args.force_control:
        # Kill-switchul derivă din policy-ul CURENT: nu inventăm un document nou în mijlocul unui
        # incident, doar îi schimbăm modul. Restul (release SHA, allowlist, dovezi) rămâne intact,
        # deci istoricul arată exact ce a fost oprit.
        async with admin_conn(pool) as conn:
            view = await policy_store.current(conn, s.release_env, force_refresh=True)
        if view.policy is None:
            print(f"REFUZ: nu există policy curent de oprit (stare: {view.code})", file=sys.stderr)
            return EXIT_REFUSED
        target = policy_store.force_control_from(view.policy, revision=(view.revision or 0) + 1)
    else:
        try:
            target = _load_policy(args.policy)
        except (OSError, ValueError, PolicyError) as e:
            print(f"INVALID: {e}", file=sys.stderr)
            return EXIT_INVALID

    # Dovezile se cer DOAR pentru modurile care livrează candidate. Un `force_control` nu are nevoie
    # de evidence packet: oprirea traficului nu trebuie să aștepte un raport (asta ar fi exact
    # invers față de siguranță).
    if target.mode in CANDIDATE_MODES:
        ok, detail = _evidence_ok(args.evidence)
        if not ok:
            print(f"REFUZ: {detail}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"evidence: PASS ({detail})")

    print(f"aplic:    {_describe(target)}")
    print(f"mediu:    {s.release_env}")
    print(f"revizie așteptată: {args.expected_revision}")
    print(f"actor:    {args.actor} · motiv: {args.reason}")
    if not args.confirm:
        print("\nDRY-RUN — nimic scris. Adaugă --confirm ca să aplici.")
        return EXIT_OK

    async with admin_conn(pool) as conn:
        result = await policy_store.apply(
            conn,
            target,
            expected_revision=args.expected_revision,
            actor=args.actor,
            reason=args.reason,
            environment=s.release_env,
        )
    if not result.ok:
        print(f"REFUZAT: {result.reason} (revizia curentă: {result.revision})", file=sys.stderr)
        return EXIT_CONFLICT if result.conflict else EXIT_REFUSED
    print(f"APLICAT: revizia {result.revision}")
    if target.mode == MODE_FORCE_CONTROL:
        print(
            "Accepturile candidate NOI se opresc în cel mult "
            f"{s.release_policy_refresh_s:.0f}s (TTL-ul cache-ului de policy). "
            "Turele deja acceptate se DRENEAZĂ pe versiunea capturată — nu se rerulează."
        )
    return EXIT_OK


def main() -> int:
    p = argparse.ArgumentParser(description="Controlul releaseului web v2 (NX-249)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="policy-ul în vigoare")

    h = sub.add_parser("history", help="istoricul de policy (append-only)")
    h.add_argument("--limit", type=int, default=20)

    v = sub.add_parser("validate", help="validează un fișier de policy, fără DB")
    v.add_argument("--policy", required=True)

    pl = sub.add_parser("plan", help="distribuția bucketurilor pe ID-uri sintetice")
    pl.add_argument("--policy", required=True)
    pl.add_argument("--ids", type=int, default=10_000)

    a = sub.add_parser("apply", help="aplică o revizie nouă (dry-run implicit)")
    a.add_argument("--policy", default="")
    a.add_argument(
        "--force-control",
        action="store_true",
        help="kill-switch: derivă `force_control` din policy-ul curent",
    )
    a.add_argument(
        "--expected-revision",
        type=int,
        default=None,
        help="CAS: revizia pe care o aștepți (omis = te aștepți să nu existe niciun policy)",
    )
    a.add_argument("--actor", required=True)
    a.add_argument("--reason", required=True)
    a.add_argument(
        "--evidence",
        default="",
        help="packetul NX-249 (cerut pentru modurile care livrează candidate)",
    )
    a.add_argument("--confirm", action="store_true", help="chiar scrie (fără el: dry-run)")

    args = p.parse_args()
    if args.cmd == "validate":
        return _cmd_validate(args)
    if args.cmd == "plan":
        return _cmd_plan(args)
    if args.cmd == "apply":
        if not args.force_control and not args.policy:
            p.error("apply cere --policy sau --force-control")
        return asyncio.run(_cmd_apply(args))
    if args.cmd == "history":
        return asyncio.run(_cmd_history(args))
    return asyncio.run(_cmd_show(args))


if __name__ == "__main__":
    raise SystemExit(main())
