"""NX-328 — snapshotul schemei `TurnInterpretation` (contractul `kernel.v1.0`, §„Enforcement").

Schema e ce scrie modelul, deci o schimbare a ei e o schimbare de contract. Snapshotul e generat pe
un set FIX de dimensiuni (enumul per tenant variază, forma nu) și ținut în repo. Testul
`tests/test_kernel_models.py` regenerează și compară: o diferență pică, iar mesajul cere fie un
bump de `KERNEL_CONTRACT_VERSION` în același diff (regula minor/major), fie revert.

    python scripts/kernel_schema_snapshot.py            # verifică (exit 1 la diferență)
    python scripts/kernel_schema_snapshot.py --write    # rescrie snapshotul
    python scripts/kernel_schema_snapshot.py --against origin/main   # regula de versiune (CI)

Egalitatea cu generatorul nu ajunge: cineva poate rula `--write` fără bump. `--against` compară cu
snapshotul de pe `main` și pică dacă schema s-a schimbat, dar versiunea nu.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.conversation.interpretation import (  # noqa: E402
    KERNEL_CONTRACT_VERSION,
    build_interpretation_schema,
)

SNAPSHOT = ROOT / "tests" / "kernel" / "schema" / "turn_interpretation.json"
#: Dimensiunile fixe ale snapshotului. Nu sunt ale niciunui tenant: doar dovedesc că enumul
#: pachetului se reunește cu cele universale, sortat.
SNAPSHOT_DIMENSIONS: tuple[str, ...] = ("size", "color", "brand")


def current() -> dict:
    return {
        "contract_version": KERNEL_CONTRACT_VERSION,
        "dimensions": list(SNAPSHOT_DIMENSIONS),
        "schema": build_interpretation_schema(SNAPSHOT_DIMENSIONS),
    }


def dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def bump_violation(base: dict, head: dict) -> str | None:
    """Regula din contract: schema scrisă de model s-a schimbat față de `base`, dar versiunea nu.
    PUR. `None` = conform (schema egală, sau schimbată împreună cu versiunea)."""
    if base.get("schema") == head.get("schema"):
        return None
    if base.get("contract_version") == head.get("contract_version"):
        return (
            f"schema TurnInterpretation s-a schimbat, dar versiunea a rămas "
            f"{head.get('contract_version')}: e o schimbare de contract (minor/major, vezi "
            f"docs/KERNEL-CONTRACT-v1.md §Status), deci KERNEL_CONTRACT_VERSION se schimbă în "
            f"același PR"
        )
    return None


def _snapshot_at(ref: str) -> dict | None:
    import subprocess  # noqa: PLC0415

    rel = SNAPSHOT.relative_to(ROOT).as_posix()
    out = subprocess.run(
        ["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return json.loads(out.stdout) if out.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rescrie snapshotul")
    parser.add_argument(
        "--against",
        metavar="REF",
        help="compară cu snapshotul de pe REF (ex. origin/main): schemă nouă ⇒ versiune nouă",
    )
    args = parser.parse_args(argv)
    if args.against:
        base = _snapshot_at(args.against)
        if base is None:
            # Snapshotul nu există pe REF: e PR-ul care îl introduce. Nimic de comparat, onest.
            print(f"snapshot absent pe {args.against}: primul snapshot, nimic de comparat")
            return 0
        problem = bump_violation(base, current())
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print(f"OK: schema conformă cu regula de versiune față de {args.against}")
        return 0
    text = dump(current())
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
        print(f"scris: {SNAPSHOT.relative_to(ROOT)}")
        return 0
    stored = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
    if stored.replace("\r\n", "\n") != text:
        print("schema TurnInterpretation a divergat de snapshot", file=sys.stderr)
        return 1
    print("OK: schema TurnInterpretation = snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
