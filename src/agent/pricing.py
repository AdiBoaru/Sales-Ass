"""Tarife LLM (USD / 1M tokeni) + calcul de cost per apel — pentru observabilitatea de cost.

Sursa UNICĂ de prețuri. `cost_for` separă tokenii de prompt CACHED (preț redus prin prompt
caching OpenAI) de cei full price → arată direct economia adusă de prefixul static (NX-78).

Valorile implicite sunt cele PUBLICATE de OpenAI (NX-201 felia A, verificat 2026-07-31 pe
https://developers.openai.com/api/docs/pricing) — nu mai sunt estimări inventate. Rămân totuși
o PLASĂ, nu facturare: reconcilierea finală se face din factura reală (praguri de volum,
long-context, discounturi de cont nu se văd din listă). Două căi de editare:
  • implicit: editezi `PRICING` aici (sursa documentată);
  • prod, fără redeploy: `LLM_PRICING_JSON` în .env (override JSON parțial, merge peste implicit) —
    ex. {"gpt-5.6-terra": {"input": 2.50, "cached_input": 0.25, "output": 15.00}}.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from src.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRates:
    """USD / 1M tokeni. `cached_input` = tariful pentru tokenii de prompt serviți din cache
    (OpenAI: automat la prefix ≥1024 tokeni; tipic o fracție din `input`)."""

    input: float
    cached_input: float
    output: float


# Tarife per model — NX-201 felia A: reconciliate cu lista de prețuri PUBLICATĂ de OpenAI
# (https://developers.openai.com/api/docs/pricing, verificat 2026-07-31). NU mai sunt estimări
# inventate: valorile de dinainte erau subevaluate de 2,25x-4,00x (delta în docs/NX-201-PRICING.md).
# Modificarea lor schimbă când se declanșează plafoanele de cost — dar DOAR pe feliile alimentate
# din `cost_for` (pipeline). Aftercare și contorul web per-vizitator scriu încă o euristică fixă din
# config, deci NU se mișcă cu tarifele; cartografia celor trei alimentatori e în
# docs/NX-201-PRICING.md.
#
# NX-275 felia 1, re-verificat 2026-09-05 pe aceeași pagină. Două lucruri s-au schimbat:
#   • familia `gpt-5.6` a intrat în listă. `gpt-5.6-luna` — modelul de VÂNZARE de azi
#     (`MODEL_AGENT`) — lipsea de tot, deci cădea pe fallback-ul `mini` și fiecare cifră de cost
#     raportată de sistem era o supraestimare de 3,75x (0,75 vs 0,20 pe input, 4,50 vs 1,20 pe
#     output). Plafonul zilnic se declanșa pe bani care nu se cheltuiseră;
#   • `gpt-5.6-terra` s-a IEFTINIT (2,50/15,00 → 2,00/12,00), deci valoarea din iulie era stale.
# Tarifele de mai jos sunt cele STANDARD (API sincron). Batch/Flex sunt jumătate, Fast Mode e
# dublu — dacă vreodată rutăm un apel pe alt tier, tierul devine parte din cheie, nu o notă.
_DEFAULT_PRICING: dict[str, ModelRates] = {
    "gpt-6-astra": ModelRates(input=10.00, cached_input=1.00, output=50.00),
    "gpt-5.6-sol": ModelRates(input=4.00, cached_input=0.40, output=20.00),
    "gpt-5.6-terra": ModelRates(input=2.00, cached_input=0.20, output=12.00),
    "gpt-5.6-luna": ModelRates(input=0.20, cached_input=0.02, output=1.20),
    "gpt-5.4": ModelRates(input=2.50, cached_input=0.25, output=15.00),
    "gpt-5.4-mini": ModelRates(input=0.75, cached_input=0.075, output=4.50),
    "gpt-5.4-nano": ModelRates(input=0.20, cached_input=0.02, output=1.25),
    "text-embedding-3-small": ModelRates(input=0.02, cached_input=0.02, output=0.0),
    # Moderation e gratuit la OpenAI → tarife 0 (înregistrat pentru `calls`, cost 0).
    "omni-moderation-latest": ModelRates(input=0.0, cached_input=0.0, output=0.0),
}

# Fallback pentru un model necunoscut (nu vrem cost 0 silențios → estimare prudentă = mini).
# ⚠️ „Prudent = mini" e o convenție MOȘTENITĂ, păstrată deliberat aici ca să nu schimbăm două
# lucruri odată: un model necunoscut e mai probabil unul NOU (deci mai scump — `gpt-5.4` costă
# 3,3x cât mini), așa că fallback-ul tot subevaluează. Remediul corect nu e să ghicim mai bine,
# ci să facem fallback-ul DETECTABIL — vezi `has_rates()` (NX-204a).
_DEFAULT = ModelRates(input=0.75, cached_input=0.075, output=4.50)


def _apply_overrides(base: dict[str, ModelRates]) -> dict[str, ModelRates]:
    """Suprascrie tarifele implicite cu `LLM_PRICING_JSON` (parțial, per câmp). Best-effort:
    JSON invalid → WARNING + tarifele implicite (nu rupem botul pentru o setare greșită)."""
    raw = (get_settings().llm_pricing_json or "").strip()
    if not raw:
        return base
    # TOATĂ parsarea + coerciția numerică e sub try (nu doar json.loads): un override valid ca
    # JSON dar cu valoare ne-numerică ({"input": "cheap"} / null) ar arunca din float() pe HOT PATH
    # (cost_for rulează după FIECARE apel OpenAI). „Setare greșită → tarife implicite, NU bot rupt."
    try:
        overrides = json.loads(raw)
        if not isinstance(overrides, dict):
            raise ValueError("LLM_PRICING_JSON nu e un obiect JSON")
        merged = dict(base)
        for model, fields in overrides.items():
            if not isinstance(fields, dict):
                continue
            current = merged.get(model, _DEFAULT)
            merged[model] = replace(
                current,
                input=float(fields.get("input", current.input)),
                cached_input=float(fields.get("cached_input", current.cached_input)),
                output=float(fields.get("output", current.output)),
            )
        return merged
    except (ValueError, TypeError) as e:
        log.warning("LLM_PRICING_JSON ignorat (%s) → tarife implicite", e)
        return base


_PRICING_CACHE: dict[str, ModelRates] | None = None


def _pricing() -> dict[str, ModelRates]:
    """Tabelul de tarife efectiv (implicit + override), calculat o singură dată per proces."""
    global _PRICING_CACHE
    if _PRICING_CACHE is None:
        _PRICING_CACHE = _apply_overrides(_DEFAULT_PRICING)
    return _PRICING_CACHE


def _reset_pricing_cache() -> None:
    """Invalidează cache-ul de tarife (după ce s-a schimbat `LLM_PRICING_JSON`) — folosit de teste
    și de un eventual reload de config; în prod tarifele se citesc o dată la boot."""
    global _PRICING_CACHE
    _PRICING_CACHE = None


# Compat: cod/teste care citesc tabelul direct. Reflectă DOAR tarifele implicite (override-ul
# se aplică prin `rates_for`/`cost_for`, care sunt singurele căi de calcul).
PRICING = _DEFAULT_PRICING


def rates_for(model: str) -> ModelRates:
    return _pricing().get(model, _DEFAULT)


def has_rates(model: str) -> bool:
    """`True` dacă modelul are tarife EXPLICITE (implicit sau prin `LLM_PRICING_JSON`).

    `rates_for` cade grațios pe `_DEFAULT` (tarifele `mini`) — comportament corect în prod
    (nu rupem botul pentru un model nou), dar o MINCIUNĂ TĂCUTĂ într-un raport de cost:
    un model frontier raportat la tariful lui `mini`. Harness-urile de măsurare (NX-204)
    verifică asta ÎNAINTE de rulare și refuză să pornească fără tarife reale.
    """
    return model in _pricing()


def cost_for(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
    """Cost USD pentru un apel. `prompt_tokens` INCLUDE `cached_tokens` (convenția OpenAI);
    cei cached se taxează la `cached_input`, restul la `input`. Negativii sunt clampați."""
    r = rates_for(model)
    cached = max(min(cached_tokens, prompt_tokens), 0)
    full = max(prompt_tokens - cached, 0)
    return (
        full * r.input + cached * r.cached_input + max(completion_tokens, 0) * r.output
    ) / 1_000_000


def savings_for(model: str, cached_tokens: int) -> float:
    """Economia USD adusă de prompt caching: ce ar fi costat `cached_tokens` la tarif PLIN minus
    ce costă la tarif cached. Zero dacă modelul n-are discount de caching. Pozitiv = bani
    economisiți (NX-78: vizibilitatea directă a beneficiului prefixului static byte-identic)."""
    r = rates_for(model)
    cached = max(cached_tokens, 0)
    return cached * (r.input - r.cached_input) / 1_000_000
