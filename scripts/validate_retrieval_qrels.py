"""Validează qrels NX-203 fără DB sau OpenAI.

python scripts/validate_retrieval_qrels.py tests/golden/retrieval_qrels_compound.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evals.retrieval.schema import QrelsSet  # noqa: E402
from src.evals.retrieval.validation import integrity_issues  # noqa: E402


def validate(path: Path, *, strict: bool, min_queries: int) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    qset = QrelsSet(**{key: value for key, value in raw.items() if not key.startswith("_")})
    return integrity_issues(
        qset,
        min_queries=min_queries,
        require_human_verified=strict,
        require_real_per_category=strict,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validator qrels NX-203")
    parser.add_argument("qrels", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="cere verificare umană și query real/categorie",
    )
    parser.add_argument("--min-queries", type=int, default=1)
    args = parser.parse_args()
    try:
        issues = validate(args.qrels, strict=args.strict, min_queries=args.min_queries)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(f"Qrels invalid: {exc}", file=sys.stderr)
        return 2
    if issues:
        print("Qrels blocat:", file=sys.stderr)
        print("\n".join(f"- {issue}" for issue in issues), file=sys.stderr)
        return 1
    print("Qrels valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
