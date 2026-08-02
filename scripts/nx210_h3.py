"""NX-210 H3 CLI: readiness, blind packet sealing, and post-rating evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.evals.nx210_blind import DecisionPolicy, evaluate_decision
from src.evals.nx210_h3 import (
    RatingsEnvelope,
    RevealEnvelope,
    RunArtifact,
    assess_h3_readiness,
    build_h3_packets,
    reveal_observations,
)
from src.evals.retrieval.schema import QrelsSet


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_model(path: Path, model: type[BaseModel]):
    return model.model_validate(_read_json(path))


def _load_qrels(path: Path) -> QrelsSet:
    raw = _read_json(path)
    return QrelsSet(**{key: value for key, value in raw.items() if not key.startswith("_")})


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _model_json(model: BaseModel) -> dict[str, object]:
    return model.model_dump(mode="json")


def _readiness(args: argparse.Namespace) -> int:
    qrels = _load_qrels(args.qrels)
    policy = _load_model(args.policy, DecisionPolicy) if args.policy else None
    report = assess_h3_readiness(qrels, policy)
    payload = {**_model_json(report), "ready": report.ready}
    if args.output:
        _write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.ready else 2


def _pack(args: argparse.Namespace) -> int:
    qrels = _load_qrels(args.qrels)
    policy = _load_model(args.policy, DecisionPolicy)
    baseline = _load_model(args.baseline, RunArtifact)
    candidate = _load_model(args.candidate, RunArtifact)
    bundle = build_h3_packets(qrels, baseline, candidate, policy, seed=args.seed)
    _write_json(args.packets_output, _model_json(bundle.packets))
    _write_json(args.reveal_output, _model_json(bundle.reveal))
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    policy = _load_model(args.policy, DecisionPolicy)
    ratings = _load_model(args.ratings, RatingsEnvelope)
    reveal = _load_model(args.reveal, RevealEnvelope)
    if policy.fingerprint != ratings.policy_fingerprint:
        raise ValueError("ratings policy fingerprint does not match policy")
    observations = reveal_observations(ratings, reveal)
    report = evaluate_decision(list(observations), policy)
    _write_json(args.output, _model_json(report))
    return 0 if report.decision == "candidate_for_adi_review" else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    readiness = commands.add_parser("readiness")
    readiness.add_argument("--qrels", type=Path, required=True)
    readiness.add_argument("--policy", type=Path)
    readiness.add_argument("--output", type=Path)
    readiness.set_defaults(handler=_readiness)

    pack = commands.add_parser("pack")
    pack.add_argument("--qrels", type=Path, required=True)
    pack.add_argument("--policy", type=Path, required=True)
    pack.add_argument("--baseline", type=Path, required=True)
    pack.add_argument("--candidate", type=Path, required=True)
    pack.add_argument("--seed", required=True)
    pack.add_argument("--packets-output", type=Path, required=True)
    pack.add_argument("--reveal-output", type=Path, required=True)
    pack.set_defaults(handler=_pack)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--policy", type=Path, required=True)
    evaluate.add_argument("--ratings", type=Path, required=True)
    evaluate.add_argument("--reveal", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.set_defaults(handler=_evaluate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
