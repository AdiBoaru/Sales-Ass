"""Validează qrels NX-203 fără DB sau OpenAI.

    python scripts/validate_retrieval_qrels.py tests/golden/retrieval_qrels_compound.json
    python scripts/validate_retrieval_qrels.py qrels.json --catalog db/seed/catalog_v2.json
    python scripts/validate_retrieval_qrels.py qrels.json --lax     # doar în dezvoltare

Poarta e STRICTĂ implicit. Înainte, `--strict` trebuia cerut manual și `--min-queries` avea
implicitul 1 — adică rularea normală trecea aproape orice set, inclusiv unul de 3 query-uri
sintetice nevalidate. O poartă pe care trebuie să-ți amintești s-o ceri nu e o poartă: trebuie să
fie greu de ocolit din greșeală și ușor de ocolit deliberat (`--lax` spune în clar ce face).
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

#: Pragul din ADR pentru un benchmark care poate decide un switch. Sub el, metricile sunt zgomot.
GATE_MIN_QUERIES = 200


def _catalog_product_ids(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products") if isinstance(data, dict) else data
    return [
        str(p.get("id") or p.get("slug"))
        for p in (products or [])
        if isinstance(p, dict) and (p.get("id") or p.get("slug"))
    ]


def validate(
    path: Path, *, strict: bool, min_queries: int, catalog: Path | None = None
) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    qset = QrelsSet(**{key: value for key, value in raw.items() if not key.startswith("_")})
    return integrity_issues(
        qset,
        min_queries=min_queries,
        require_human_verified=strict,
        require_real_per_category=strict,
        require_split_sizes=strict,
        catalog_product_ids=_catalog_product_ids(catalog),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validator qrels NX-203")
    parser.add_argument("qrels", type=Path)
    parser.add_argument(
        "--lax",
        action="store_true",
        help="renunță la cerințele de gate (verificare umană, query real/categorie, mărimea "
        "feliilor, pragul de volum). DOAR pentru dezvoltare — un set validat cu --lax nu are voie "
        "să susțină o decizie de switch.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="catalog contra căruia se verifică existența produselor judecate",
    )
    parser.add_argument("--min-queries", type=int, default=None)
    args = parser.parse_args()

    strict = not args.lax
    min_queries = (
        args.min_queries if args.min_queries is not None else (GATE_MIN_QUERIES if strict else 1)
    )
    try:
        issues = validate(args.qrels, strict=strict, min_queries=min_queries, catalog=args.catalog)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(f"Qrels invalid: {exc}", file=sys.stderr)
        return 2
    if issues:
        print(f"Qrels blocat ({'strict' if strict else 'lax'}):", file=sys.stderr)
        print("\n".join(f"- {issue}" for issue in issues[:40]), file=sys.stderr)
        if len(issues) > 40:
            print(f"- … încă {len(issues) - 40}", file=sys.stderr)
        return 1
    print(f"Qrels valid ({'strict' if strict else 'lax'}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
