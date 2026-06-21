"""Cost guard + rate limit (stagiul 2, G2c) — contoare Redis best-effort.

Protecție financiară + anti-abuz peste moderation (NX-15), urgentă acum că tool-calling-ul
(G7-1) face 2-4× apeluri mini/tur. Redis e guard-ul REALTIME; facturarea reală rămâne
`usage_daily` (rollup nocturn) — contoarele de aici sunt o estimare-plasă.

Toate cheile includ `business_id` (principiul 7). Funcțiile NU prind erorile de Redis —
caller-ul (gate / processor) le tratează fail-open (indisponibilitatea unui guard nu trebuie
să blocheze traficul).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from src.models import Event

# TTL al cheii de cost zilnice (zile) — mult peste o zi; rollup-ul nocturn e sursa de facturare.
_COST_KEY_TTL_S = 2 * 24 * 60 * 60


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


# --- rate limit per contact --------------------------------------------------


async def incr_window(redis: Redis, key: str, window_s: int) -> int:
    """INCR un contor cu fereastră FIXĂ: EXPIRE la primul increment, resetare după `window_s`.
    Primitivă generică de rate limit (refolosită de rate limit per contact + web NX-20, pe chei
    proprii). Întoarce noul count."""
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_s)
    return int(count)


async def rate_limit_count(redis: Redis, business_id: str, contact_id: str, window_s: int) -> int:
    """INCR contorul de mesaje al contactului; EXPIRE la primul (fereastră FIXĂ — resetează
    după `window_s` de la primul mesaj). Întoarce noul count."""
    return await incr_window(redis, f"rate:{business_id}:{contact_id}", window_s)


# --- cost guard per business (zilnic) ---------------------------------------


async def cost_over_budget(redis: Redis, business_id: str, cap_usd: float) -> bool:
    """True dacă cheltuiala estimată azi a businessului ≥ plafon."""
    val = await redis.get(f"cost:{business_id}:{_today()}")
    spent = float(val) if val else 0.0
    return spent >= cap_usd


async def cost_add(redis: Redis, business_id: str, amount_usd: float) -> None:
    """Adaugă o estimare de cost în contorul zilnic (+ EXPIRE). No-op la sumă ≤ 0."""
    if amount_usd <= 0:
        return
    key = f"cost:{business_id}:{_today()}"
    await redis.incrbyfloat(key, amount_usd)
    await redis.expire(key, _COST_KEY_TTL_S)


# --- cost guard per vizitator web (NX-120) ----------------------------------


async def web_cost_over_visitor_cap(
    redis: Redis, business_id: str, visitor_id: str, cap_usd: float
) -> bool:
    """NX-120: True dacă un SINGUR vizitator web a cheltuit azi ≥ plafonul per-vizitator. Plasă ca
    un token public furat să nu poată epuiza tot `daily_cost_cap_usd`-ul tenantului. Cheie fără PII
    în clar (visitor_id e id de canal, ca `webrl:*`)."""
    val = await redis.get(f"webcost:{business_id}:{visitor_id}:{_today()}")
    spent = float(val) if val else 0.0
    return spent >= cap_usd


async def web_cost_add_visitor(
    redis: Redis, business_id: str, visitor_id: str, amount_usd: float
) -> None:
    """Adaugă o estimare de cost în contorul per-vizitator (+ EXPIRE). No-op la sumă ≤ 0.
    Estimare-plasă de admitere (precizia per-tur = NX-125)."""
    if amount_usd <= 0:
        return
    key = f"webcost:{business_id}:{visitor_id}:{_today()}"
    await redis.incrbyfloat(key, amount_usd)
    await redis.expire(key, _COST_KEY_TTL_S)


def estimate_turn_cost(
    events: Sequence[Event], *, cost_triage_usd: float, cost_agent_usd: float
) -> float:
    """Estimare grosieră a costului LLM al unui tur, din evenimente (sursa de FACTURARE rămâne
    `usage_daily`). `intent_detected` = triajul (nano) a rulat; `agent_recommended`/`tool_call`
    = agentul (mini). Tool-calling: mini ≈ ×(1 + nr tool_call). Compatibil cu agentul RAG
    (zero `tool_call`) și cu G7-1."""
    types = [e.type for e in events]
    cost = 0.0
    if "intent_detected" in types:
        cost += cost_triage_usd
    tool_calls = types.count("tool_call")
    if tool_calls or "agent_recommended" in types:
        cost += cost_agent_usd * (1 + tool_calls)
    return cost
