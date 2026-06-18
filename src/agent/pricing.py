"""Tarife LLM (USD / 1M tokeni) + calcul de cost per apel — pentru observabilitatea de cost.

Sursa UNICĂ de prețuri. `cost_for` separă tokenii de prompt CACHED (preț redus prin prompt
caching OpenAI) de cei full price → arată direct economia adusă de prefixul static (NX-78).

⚠️ Valorile sunt ESTIMĂRI configurabile (modelele sunt interne proiectului). Facturarea reală
se reconciliază din factura OpenAI; `usage_daily.cost_usd` rămâne o estimare-plasă, ca și
contoarele cost-guard. Editează DOAR aici când se schimbă tarifele.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRates:
    """USD / 1M tokeni. `cached_input` = tariful pentru tokenii de prompt serviți din cache
    (OpenAI: automat la prefix ≥1024 tokeni; tipic o fracție din `input`)."""

    input: float
    cached_input: float
    output: float


# Tarife per model (estimări — vezi nota din docstring-ul modulului). `cached_input` ≈ 10% din
# `input` (ordinul de mărime al discount-ului de prompt caching OpenAI pe modelele noi).
PRICING: dict[str, ModelRates] = {
    "gpt-5.4-mini": ModelRates(input=0.25, cached_input=0.025, output=2.00),
    "gpt-5.4-nano": ModelRates(input=0.05, cached_input=0.005, output=0.40),
    "text-embedding-3-small": ModelRates(input=0.02, cached_input=0.02, output=0.0),
}

# Fallback pentru un model necunoscut (nu vrem cost 0 silențios → estimare prudentă = mini).
_DEFAULT = ModelRates(input=0.25, cached_input=0.025, output=2.00)


def rates_for(model: str) -> ModelRates:
    return PRICING.get(model, _DEFAULT)


def cost_for(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
    """Cost USD pentru un apel. `prompt_tokens` INCLUDE `cached_tokens` (convenția OpenAI);
    cei cached se taxează la `cached_input`, restul la `input`. Negativii sunt clampați."""
    r = rates_for(model)
    cached = max(min(cached_tokens, prompt_tokens), 0)
    full = max(prompt_tokens - cached, 0)
    return (
        full * r.input + cached * r.cached_input + max(completion_tokens, 0) * r.output
    ) / 1_000_000
