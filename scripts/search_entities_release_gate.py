"""NX-238 — poarta de release pentru candidatul `search_entities`.

Produce artefactul de decizie (`reports/nx238/decision.json`) din readiness-ul REAL, cu amprentă
recalculabilă. Nu poate emite `GO`: instrumentul are voie să spună `NOT_READY`, `NO_GO` sau
`candidate_for_adi_review`, iar promovarea cere semnătura lui Adi peste amprentă (`sign`).

Rulare (verdictul curent, pe manifestul de azi):

    PYTHONPATH=. python scripts/search_entities_release_gate.py evaluate \\
        --retrieval-qrels tests/golden/qrels_confirmed.json \\
        --out reports/nx238/decision.json

Ieșire 0 = promovabil (nu se întâmplă fără dataset sigilat); 2 = blocat, cu blockers listați.
Semnarea unui GO, DUPĂ decizia umană:

    PYTHONPATH=. RETRIEVAL_DECISION_KEY=... python scripts/search_entities_release_gate.py sign \\
        --decision reports/nx238/decision.json --by "Adi Boaru"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.evals.nx210_h3 import assess_h3_readiness
from src.evals.retrieval.readiness import gate_readiness
from src.evals.retrieval.schema import QrelsSet
from src.retrieval.selector import (
    VERDICT_GO,
    VERDICT_NO_GO,
    VERDICT_NOT_READY,
    compute_fingerprint,
    load_decision,
    sign_fingerprint,
    verify_decision,
)

#: Pragurile din card. Sunt AICI, în cod, ca să nu poată fi „relaxate" editând un raport.
THRESHOLDS = {
    "recall_at_20": 0.90,
    "ndcg_at_6": 0.85,
    "relevant_in_top6_rate": 0.90,
    "max_hard_constraint_violations": 0,
    "max_simple_fact_regressions": 0,
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_qrels(path: Path) -> QrelsSet:
    raw = _read_json(path)
    return QrelsSet(**{k: v for k, v in raw.items() if not k.startswith("_")})


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Ce lume a fost măsurată. Un verdict e valabil DOAR pentru manifestul lui.

    Dacă qrels-ul, politica sau commit-ul se schimbă, amprenta artefactului nu se mai potrivește
    cu realitatea, iar `verify_decision(expected_manifest=...)` întoarce `decision_manifest_drift`.
    Asta e diferența dintre „am măsurat" și „am măsurat CEVA, cândva".
    """
    return {
        "commit": _git_commit(),
        "retrieval_qrels_sha256": _sha256_file(args.retrieval_qrels),
        "quality_h3_sha256": _sha256_file(args.quality_h3) if args.quality_h3 else "missing",
        "policy_sha256": _sha256_file(args.policy) if args.policy else "missing",
        "thresholds": THRESHOLDS,
    }


def _evaluate(args: argparse.Namespace) -> int:
    blocking: list[str] = []
    metrics: dict[str, Any] = {}

    # --- 1. Readiness retrieval (NX-203/NX-209): qrels + splituri ---------------------------
    qrels_ready = False
    if args.retrieval_qrels.exists():
        qset = _load_qrels(args.retrieval_qrels)
        gates = {gate: gate_readiness(qset, gate) for gate in ("NX-207", "NX-209", "NX-210")}
        metrics["retrieval_gates"] = {g: r.as_dict() for g, r in gates.items()}
        qrels_ready = all(r.ready for r in gates.values())
        if not qrels_ready:
            blocking.append("retrieval_qrels_not_ready")
    else:
        blocking.append("retrieval_qrels_missing")

    # --- 2. Readiness calitate (NX-210 H3): dataset sigilat + policy înghețată ---------------
    quality = assess_h3_readiness(
        None,
        None,
        _load_qrels(args.retrieval_qrels) if args.retrieval_qrels.exists() else None,
        inventory_count=0,
        rewrite_count=0,
    )
    metrics["quality_h3"] = quality.model_dump(mode="json")
    if not quality.ready:
        blocking.extend(quality.blocking_codes)
        blocking.extend(quality.unavailable_codes)

    # --- 3. Verdict -------------------------------------------------------------------------
    # Fără dataset sigilat nu se poate pronunța NO_GO (n-am măsurat nimic ca să respingem) —
    # verdictul onest e NOT_READY. NO_GO e rezervat pentru „am măsurat și a picat un prag".
    if blocking:
        verdict = VERDICT_NOT_READY
    else:
        # Readiness verde → abia acum se pot compara baseline vs candidate pe H3. Instrumentul NU
        # emite GO nici aici: cel mult trimite pachetul spre decizia umană.
        verdict = VERDICT_NO_GO
        blocking.append("paired_run_and_human_decision_required")

    payload: dict[str, Any] = {
        "card": "NX-238",
        "verdict": verdict,
        "decided_at": args.decided_at or "",
        "decided_by": "",
        "manifest": _manifest(args),
        "blocking_codes": sorted(dict.fromkeys(blocking)),
        "metrics": metrics,
        "note": (
            "Software-ul nu poate emite GO. Promovarea cere dataset sigilat, rulare pereche "
            "blind pe H3, decizia semnată a lui Adi și semnarea amprentei cu "
            "RETRIEVAL_DECISION_KEY."
        ),
    }
    payload["fingerprint"] = compute_fingerprint(payload)
    payload["signature"] = ""

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nverdict: {verdict} · blockers: {len(payload['blocking_codes'])}", file=sys.stderr)
    print(f"artefact: {args.out}", file=sys.stderr)
    return 0 if verdict == VERDICT_GO else 2


def _sign(args: argparse.Namespace) -> int:
    """Semnează un artefact DUPĂ ce un om a decis. Refuză orice altceva decât un GO curat."""
    key = os.environ.get("RETRIEVAL_DECISION_KEY", "")
    if not key:
        print("RETRIEVAL_DECISION_KEY lipsește: fără cheie nu se poate semna", file=sys.stderr)
        return 2

    payload = _read_json(args.decision)
    if not isinstance(payload, dict):
        print("artefact invalid", file=sys.stderr)
        return 2
    if payload.get("verdict") != VERDICT_GO:
        print(
            f"verdictul din artefact e {payload.get('verdict')!r}, nu {VERDICT_GO}: "
            "semnarea nu poate SCHIMBA un verdict, doar atesta unul",
            file=sys.stderr,
        )
        return 2

    payload["decided_by"] = args.by
    payload["decided_at"] = args.decided_at or payload.get("decided_at") or ""
    payload["fingerprint"] = compute_fingerprint(payload)
    payload["signature"] = sign_fingerprint(payload["fingerprint"], key)
    args.decision.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    decision, block = load_decision(args.decision)
    if block or verify_decision(decision, key=key) is not None:
        print("semnătura nu se verifică după scriere — artefact respins", file=sys.stderr)
        return 2
    print(f"semnat de {args.by}: {payload['fingerprint']}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Ce ar spune runtime-ul despre acest artefact, cu cheia din mediu."""
    decision, block = load_decision(args.decision)
    if block:
        print(json.dumps({"promotable": False, "blocking_code": block}, indent=2))
        return 2
    block = verify_decision(decision, key=os.environ.get("RETRIEVAL_DECISION_KEY", ""))
    print(
        json.dumps(
            {
                "promotable": block is None,
                "verdict": decision.verdict if decision else None,
                "blocking_code": block,
                "blocking_codes": list(decision.blocking_codes) if decision else [],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if block is None else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    evaluate = commands.add_parser("evaluate", help="readiness real → artefact de decizie")
    evaluate.add_argument(
        "--retrieval-qrels", type=Path, default=Path("tests/golden/qrels_confirmed.json")
    )
    evaluate.add_argument("--quality-h3", type=Path)
    evaluate.add_argument("--policy", type=Path)
    evaluate.add_argument("--decided-at", default="")
    evaluate.add_argument("--out", type=Path, default=Path("reports/nx238/decision.json"))
    evaluate.set_defaults(handler=_evaluate)

    sign = commands.add_parser("sign", help="atestă un GO deja decis de om")
    sign.add_argument("--decision", type=Path, required=True)
    sign.add_argument("--by", required=True)
    sign.add_argument("--decided-at", default="")
    sign.set_defaults(handler=_sign)

    verify = commands.add_parser("verify", help="ce ar spune runtime-ul despre artefact")
    verify.add_argument("--decision", type=Path, default=Path("reports/nx238/decision.json"))
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
