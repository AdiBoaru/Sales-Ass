"""NX-203 — benchmark de retrieval (schelet). Vezi docs/NX-203-QRELS-SCHEMA.md.

Public: schema qrels, metrici, split-uri single-use, harness. Dataset-ul complet (200-500) NU e
aici — se populează din etichetele NX-202 validate de Adi.
"""

from src.evals.retrieval.harness import BenchmarkReport, RunConfig, run_benchmark
from src.evals.retrieval.schema import QrelsQuery, QrelsSet
from src.evals.retrieval.splits import Split, holdout_slice_for_gate, partition

__all__ = [
    "BenchmarkReport",
    "QrelsQuery",
    "QrelsSet",
    "RunConfig",
    "Split",
    "holdout_slice_for_gate",
    "partition",
    "run_benchmark",
]
