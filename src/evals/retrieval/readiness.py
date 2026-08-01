"""Readiness explicit for the Quality Overhaul retrieval gates."""

from __future__ import annotations

from dataclasses import dataclass

from src.evals.retrieval.schema import QrelsSet
from src.evals.retrieval.splits import Split, partition
from src.evals.retrieval.validation import (
    MIN_HOLDOUT_SLICE,
    TARGET_FAMILIES,
    ValidationReport,
    validate_integrity,
    verified_family_count,
)

_GATE_SPLITS = {
    "NX-207": Split.holdout_h1,
    "NX-209": Split.holdout_h2,
    "NX-210": Split.holdout_h3,
}


@dataclass(frozen=True)
class GateReadiness:
    gate: str
    split: str
    query_count: int
    verified_family_count: int
    blocking: tuple[str, ...]
    unavailable: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blocking and not self.unavailable

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "split": self.split,
            "query_count": self.query_count,
            "verified_family_count": self.verified_family_count,
            "ready": self.ready,
            "blocking": list(self.blocking),
            "unavailable": list(self.unavailable),
        }


def gate_readiness(qset: QrelsSet, gate: str) -> GateReadiness:
    """Evaluate a gate without opening or mutating its holdout slice."""
    try:
        split = _GATE_SPLITS[gate]
    except KeyError as exc:
        raise ValueError(f"unknown gate: {gate}") from exc

    all_report = validate_integrity(
        qset,
        min_families=TARGET_FAMILIES,
        require_human_verified=True,
        require_real_per_category=True,
        require_split_sizes=True,
    )
    selected = QrelsSet(
        schema_version=qset.schema_version,
        business_id=qset.business_id,
        queries=partition(qset)[split],
    )
    selected_report = validate_integrity(
        selected,
        min_queries=MIN_HOLDOUT_SLICE,
        require_human_verified=True,
    )
    report = ValidationReport(
        blocking=[*all_report.blocking, *selected_report.blocking],
        unavailable=[*all_report.unavailable, *selected_report.unavailable],
    )
    return GateReadiness(
        gate=gate,
        split=split.value,
        query_count=len(selected.queries),
        verified_family_count=verified_family_count(selected),
        blocking=tuple(dict.fromkeys(report.blocking)),
        unavailable=tuple(dict.fromkeys(report.unavailable)),
    )
