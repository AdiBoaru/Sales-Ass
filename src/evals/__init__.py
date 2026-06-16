"""Evals — harness golden (G8-1) + (follow-up) LLM-as-judge nocturn.

`golden` livrează motorul de regresie: checker pur + încărcător de cazuri rulate
prin pipeline-ul real cu un LLM scriptat (zero OpenAI). Gate-ul CI = `tests/test_golden.py`.
"""

from src.evals.golden import (
    GoldenCase,
    GoldenExpect,
    GoldenResult,
    evaluate_reply,
    load_cases,
    run_case,
)

__all__ = [
    "GoldenCase",
    "GoldenExpect",
    "GoldenResult",
    "evaluate_reply",
    "load_cases",
    "run_case",
]
