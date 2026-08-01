"""Report whether qrels can open H1/H2/H3 without changing their state.

PYTHONPATH=. python scripts/qrels_readiness.py tests/golden/qrels_confirmed.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.evals.retrieval.readiness import gate_readiness
from src.evals.retrieval.schema import QrelsSet


def load_qrels(path: Path) -> QrelsSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return QrelsSet(**{key: value for key, value in raw.items() if not key.startswith("_")})


def main() -> int:
    parser = argparse.ArgumentParser(description="Qrels readiness for H1/H2/H3")
    parser.add_argument("qrels", type=Path)
    args = parser.parse_args()
    qset = load_qrels(args.qrels)
    report = {
        "qrels": str(args.qrels),
        "business_id": qset.business_id,
        "gates": [gate_readiness(qset, gate).as_dict() for gate in ("NX-207", "NX-209", "NX-210")],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(gate["ready"] for gate in report["gates"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
