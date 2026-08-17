"""NX-246 (felia 3) — CLI-ul gate-ului de calitate „personal shopper".

    PYTHONPATH=. python scripts/web_quality_eval.py validate --suite tests/golden/web_journeys
    PYTHONPATH=. python scripts/web_quality_eval.py coverage --suite tests/golden/web_journeys
    PYTHONPATH=. python scripts/web_quality_eval.py seal    --content <dir>   # calculează SHA
    PYTHONPATH=. python scripts/web_quality_eval.py gate    --suite tests/golden/web_journeys \
        [--ratings ratings.json --keys keys.json --deterministic det.json] --json

Patru comenzi, în ordinea în care se folosesc:

  • `validate`  — schema + duplicate + coerența familie/conținut. Nu atinge nimic altceva.
  • `coverage`  — cât acoperă suita față de minimele cardului (60 dev / 40 holdout / 4 per familie
                  în holdout / ≥30% adversarial). Iese NON-ZERO când e incompletă.
  • `seal`      — SHA-256 peste amprentele holdoutului, ca să poată fi pus în manifest.
  • `gate`      — verdictul: PASS | FAIL | INSUFFICIENT | NOT-READY.

**Nu rulează modele.** Nici măcar `gate`: el consumă artefacte deja produse (ratings umane,
rezultate deterministe). Separarea e deliberată — un gate care ar chema el însuși modelul ar
amesteca „a măsura" cu „a decide", iar rularea ar costa bani de fiecare dată când cineva vrea doar
să recitească verdictul.

**Exit codes** (pentru CI/NX-247): `0` PASS · `1` FAIL · `2` INSUFFICIENT sau NOT-READY.
Lipsa datelor NU e zero. Un gate care trece pe „n-am găsit nimic" e fail-open.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.evals.pairwise import PairwiseResult, Rating, RevealKey, aggregate  # noqa: E402
from src.evals.quality_gate import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_PASS,
    DeterministicResult,
    GatePolicy,
    evaluate_gate,
)
from src.evals.web_journeys import (  # noqa: E402
    HoldoutManifest,
    coverage,
    load_journeys,
    seal_holdout,
    verify_holdout,
)

_EXIT = {VERDICT_PASS: 0, VERDICT_FAIL: 1}


def _dev_dir(suite: Path) -> Path:
    return suite / "dev"


def _manifest(suite: Path) -> HoldoutManifest:
    path = suite / "holdout_manifest.json"
    if not path.exists():
        # Manifest absent NU e „holdout gol": e o suită despre care nu putem afirma nimic.
        return HoldoutManifest(suite_id="absent", journey_count=0)
    return HoldoutManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def cmd_validate(args: argparse.Namespace) -> int:
    suite = Path(args.suite)
    journeys = load_journeys(_dev_dir(suite))
    manifest = _manifest(suite)
    print(f"schema:   web-journey.v1  ({len(journeys)} journey-uri de development valide)")
    print(f"manifest: {manifest.suite_id}  sigilat={manifest.sealed}")
    for j in journeys:
        print(f"  {j.journey_id:<40} {j.family:<22} ture={len(j.turns)} adv={j.adversarial}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    suite = Path(args.suite)
    dev = coverage(load_journeys(_dev_dir(suite)), holdout=False)
    manifest = _manifest(suite)
    print("DEVELOPMENT")
    _print_coverage(dev)
    print("\nHOLDOUT (din manifest — conținutul nu e în repo)")
    print(f"  suite_id: {manifest.suite_id}")
    print(f"  sigilat:  {manifest.sealed}")
    print(f"  journeys: {manifest.journey_count}")
    if manifest.by_family:
        print(f"  familii:  {json.dumps(manifest.by_family, sort_keys=True)}")
    if args.json:
        print(
            "\n" + json.dumps({"dev": dev.as_dict(), "holdout_sealed": manifest.sealed}, indent=2)
        )
    return 0 if dev.complete and manifest.sealed else 2


def _print_coverage(cov) -> None:
    print(f"  total:       {cov.total}")
    print(f"  adversarial: {cov.adversarial} ({cov.adversarial_ratio:.0%})")
    for family, n in sorted(cov.by_family.items()):
        print(f"    {family:<24} {n}")
    if cov.gaps:
        print("  LIPSURI:")
        for gap in cov.gaps:
            print(f"    - {gap}")
    else:
        print("  acoperire completă")


def cmd_seal(args: argparse.Namespace) -> int:
    """SHA-256 peste amprentele conținutului de holdout — de pus în manifest, manual."""
    journeys = load_journeys(Path(args.content))
    cov = coverage(journeys, holdout=True)
    digest = seal_holdout(journeys)
    print(f"content_sha256: {digest}")
    print(f"journey_count:  {len(journeys)}")
    print(f"adversarial:    {cov.adversarial}")
    print(f"by_family:      {json.dumps(cov.by_family, sort_keys=True)}")
    if cov.gaps:
        print("ATENȚIE — holdoutul nu îndeplinește minimele:")
        for gap in cov.gaps:
            print(f"  - {gap}")
    return 0 if cov.complete else 2


def cmd_gate(args: argparse.Namespace) -> int:
    suite = Path(args.suite)
    manifest = _manifest(suite)
    dev = coverage(load_journeys(_dev_dir(suite)), holdout=False)

    holdout_journeys = load_journeys(Path(args.holdout)) if args.holdout else None
    seal = verify_holdout(manifest, holdout_journeys)
    holdout_cov = coverage(holdout_journeys, holdout=True) if holdout_journeys else None

    deterministic = None
    if args.deterministic:
        raw = json.loads(Path(args.deterministic).read_text(encoding="utf-8"))
        deterministic = DeterministicResult(
            turns_checked=int(raw.get("turns_checked", 0)),
            by_check=dict(raw.get("by_check", {})),
            failing_journeys=tuple(raw.get("failing_journeys", ())),
        )

    pairwise: PairwiseResult | None = None
    if args.ratings and args.keys:
        ratings = [
            Rating.model_validate(r)
            for r in json.loads(Path(args.ratings).read_text(encoding="utf-8"))
        ]
        keys = [
            RevealKey.model_validate(k)
            for k in json.loads(Path(args.keys).read_text(encoding="utf-8"))
        ]
        families = {j.journey_id: j.family for j in (holdout_journeys or [])}
        pairwise = aggregate(ratings, keys, families)

    report = evaluate_gate(
        policy=GatePolicy(),
        seal=seal,
        dev_coverage=dev,
        holdout_coverage=holdout_cov,
        deterministic=deterministic,
        pairwise=pairwise,
        champion_release=args.champion_release,
        candidate_release=args.candidate_release,
    )
    payload = report.as_dict()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        # Artefactul conține DOAR agregate + coduri de motiv; niciun transcript (cardul:
        # „runnerul nu printează transcriptul în CI artifacts").
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"VERDICT: {report.verdict}")
        print(f"policy:  {report.policy_fingerprint[:16]}")
        for reason in report.reasons:
            print(f"  - {reason}")
    return _EXIT.get(report.verdict, 2)


def main() -> int:
    p = argparse.ArgumentParser(description="Gate de calitate conversațională web (NX-246)")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="schema + duplicate + coerență familie/conținut")
    v.add_argument("--suite", required=True)
    v.set_defaults(func=cmd_validate)

    c = sub.add_parser("coverage", help="acoperire față de minimele cardului")
    c.add_argument("--suite", required=True)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_coverage)

    s = sub.add_parser("seal", help="SHA-256 peste amprentele holdoutului")
    s.add_argument("--content", required=True, help="director cu journey-urile de holdout")
    s.set_defaults(func=cmd_seal)

    g = sub.add_parser("gate", help="verdictul (PASS|FAIL|INSUFFICIENT|NOT-READY)")
    g.add_argument("--suite", required=True)
    g.add_argument(
        "--holdout", default="", help="director cu conținutul de holdout (restricționat)"
    )
    g.add_argument("--ratings", default="", help="ratings umane (JSON)")
    g.add_argument("--keys", default="", help="cheia de dezvăluire (JSON)")
    g.add_argument("--deterministic", default="", help="rezultatul verificărilor deterministe")
    g.add_argument("--champion-release", default="")
    g.add_argument("--candidate-release", default="")
    g.add_argument("--out", default="")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_gate)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
