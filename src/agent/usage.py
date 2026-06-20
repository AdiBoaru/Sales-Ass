"""Captarea usage-ului LLM per tur (tokeni + cost + cache hits) — observabilitate de cost.

Problema: răspunsurile OpenAI poartă `usage` (prompt/completion/cached tokens), dar adaptorul
nu le citea → `usage_daily.tokens_in/out/cost_usd` erau mereu 0, iar economia din prompt caching
(NX-78) invizibilă. Soluția respectă principiul 10 (stagiile nu știu că sunt măsurate):

  • adaptorul (`src.agent.llm`, singurul care vorbește OpenAI) raportează usage-ul fiecărui apel
    aici, prin `record_chat` / `record_embeddings`;
  • runner-ul deschide un acumulator per tur (`push`/`pop`) și emite UN event `llm_usage` la final;
  • processor-ul deschide un al doilea acumulator în jurul apelurilor POST-tur (summarizer / profil)
    → un al doilea `llm_usage` (phase=post_turn), ca nimic să nu scape rollup-ului (NX-103).

Izolare la concurență: acumulatorul stă într-un `ContextVar`. `asyncio.gather` (ex. tool-urile
rulate în paralel în `run_tool_loop`) copiază contextul, dar TOATE copiile văd ACEEAȘI instanță
(o mutăm, nu re-legăm var-ul) → tokenii sub-apelurilor concurente se adună corect, fără să se
amestece între tururi.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

from src.agent.pricing import cost_for


def _empty_model_row() -> dict[str, Any]:
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cached_tokens": 0, "cost_usd": 0.0}


@dataclass
class UsageAccumulator:
    """Totalurile unui tur. `tokens_in` = prompt (INCLUDE `cached_tokens`); `tokens_out` =
    completion. `cost_usd` = sumă pe apeluri, cu tokenii cached la tarif redus. `by_model` =
    defalcare per model (nano/mini/embeddings) pentru raportul de cost (NX-103)."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, model: str, prompt: int, completion: int, cached: int) -> None:
        cost = cost_for(model, prompt, cached, completion)
        self.calls += 1
        self.tokens_in += prompt
        self.tokens_out += completion
        self.cached_tokens += cached
        self.cost_usd += cost
        row = self.by_model.setdefault(model, _empty_model_row())
        row["calls"] += 1
        row["tokens_in"] += prompt
        row["tokens_out"] += completion
        row["cached_tokens"] += cached
        row["cost_usd"] += cost

    def snapshot(self) -> tuple[int, int, int, int, float]:
        """Stare curentă (calls, in, out, cached, cost) — runner-ul o diff-uiește per stagiu."""
        return (self.calls, self.tokens_in, self.tokens_out, self.cached_tokens, self.cost_usd)


_current: contextvars.ContextVar[UsageAccumulator | None] = contextvars.ContextVar(
    "llm_usage", default=None
)


def push() -> tuple[UsageAccumulator, contextvars.Token]:
    """Deschide un acumulator nou pentru turul curent. Întoarce (acc, token) → `pop(token)`."""
    acc = UsageAccumulator()
    return acc, _current.set(acc)


def pop(token: contextvars.Token) -> None:
    """Închide capturarea (restaurează valoarea anterioară a ContextVar-ului)."""
    _current.reset(token)


def _cached_from(usage: Any) -> int:
    """`prompt_tokens_details.cached_tokens` — tolerează obiect SDK SAU dict SAU lipsă."""
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is None:
        return 0
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def _field(usage: Any, name: str) -> int:
    val = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
    return int(val or 0)


def record_chat(resp: Any, model: str) -> None:
    """Raportează usage-ul unui apel chat (best-effort). Fără acumulator activ sau fără `usage`
    pe răspuns (ex. fake-uri din teste) → no-op, nu rupe turul."""
    acc = _current.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    acc.add(
        model,
        _field(usage, "prompt_tokens"),
        _field(usage, "completion_tokens"),
        _cached_from(usage),
    )


def record_embeddings(resp: Any, model: str) -> None:
    """Raportează usage-ul unui apel de embeddings (doar prompt tokens; fără cached/output)."""
    acc = _current.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    acc.add(model, _field(usage, "prompt_tokens"), 0, 0)
