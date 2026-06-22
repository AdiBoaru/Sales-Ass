"""Cost guard + rate limit (stagiul 2, G2c) — contoare Redis best-effort.

Protecție financiară + anti-abuz peste moderation (NX-15), urgentă acum că tool-calling-ul
(G7-1) face 2-4× apeluri mini/tur. Redis e guard-ul REALTIME; facturarea reală rămâne
`usage_daily` (rollup nocturn) — contoarele de aici sunt o estimare-plasă.

Toate cheile includ `business_id` (principiul 7). Funcțiile NU prind erorile de Redis —
caller-ul (gate / processor) le tratează fail-open (indisponibilitatea unui guard nu trebuie
să blocheze traficul).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg
    from redis.asyncio import Redis

    from src.models import Event

log = logging.getLogger(__name__)

# TTL al cheii de cost zilnice (zile) — mult peste o zi; rollup-ul nocturn e sursa de facturare.
_COST_KEY_TTL_S = 2 * 24 * 60 * 60
# Fereastra plafonului de cheltuială per-contact (NX-125): 24h fixe de la primul cost al ferestrei.
CONTACT_COST_WINDOW_S = 24 * 60 * 60


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


async def cost_add_and_total(redis: Redis, business_id: str, amount_usd: float) -> float:
    """NX-125: adaugă costul EXACT al turului (`ctx.usage.cost_usd`) și întoarce noul total
    al zilei — increment ATOMIC, comparat DUPĂ (elimină fereastra TOCTOU dintre `cost_over_budget`
    și `cost_add`). No-op la sumă ≤ 0: întoarce totalul curent fără să scrie. Caller-ul compară
    totalul cu plafonul → chiar dacă N tururi concurente scapă de pre-check, increment-ul atomic
    înregistrează totalul corect și turul URMĂTOR e blocat determinist."""
    key = f"cost:{business_id}:{_today()}"
    if amount_usd <= 0:
        val = await redis.get(key)
        return float(val) if val else 0.0
    total = await redis.incrbyfloat(key, amount_usd)
    await redis.expire(key, _COST_KEY_TTL_S)
    return float(total)


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


# --- plafon de cheltuială per-contact (NX-125) ------------------------------
# SOFT cap per-contact (canale identificate), ortogonal de plafonul per-business. O singură
# conversație în buclă nu mai poate arde plafonul întregului tenant. Web-ul anonim are deja
# plafon per-vizitor în calea sincronă (NX-120, `webcost:*`) → aici DOAR canale identificate,
# fără dublă-contorizare. Cheile includ `business_id` (P7); scope_key NU conține PII (contact_id
# = uuid intern, nu telefon — P12).


def contact_scope_key(business_id: str, contact_id: str) -> str:
    """Cheia de scope (fără prefixul `spend:`) pentru plafonul per-contact."""
    return f"{business_id}:contact:{contact_id}"


async def spend_capped(redis: Redis, scope_key: str, cap_usd: float) -> bool:
    """Pre-check IEFTIN (read-only): True dacă scope-ul a atins deja plafonul în fereastra
    curentă. `cap_usd` ≤ 0 / None → dezactivat (False). Mirror al `cost_over_budget` per-scope."""
    if not cap_usd or cap_usd <= 0:
        return False
    val = await redis.get(f"spend:{scope_key}")
    spent = float(val) if val else 0.0
    return spent >= cap_usd


async def spend_over_cap(
    redis: Redis, scope_key: str, amount_usd: float, cap_usd: float, window_s: int
) -> bool:
    """Adaugă ATOMIC `amount_usd` la `spend:{scope_key}` (EXPIRE la primul increment al ferestrei
    fixe) și întoarce True dacă noul total ≥ plafon — enforcement POST-increment (fără TOCTOU).
    `cap_usd` ≤ 0 / None → dezactivat (False, fără scriere). `amount_usd` ≤ 0 → compară doar
    totalul curent, fără să scrie."""
    if not cap_usd or cap_usd <= 0:
        return False
    key = f"spend:{scope_key}"
    if amount_usd <= 0:
        val = await redis.get(key)
        return (float(val) if val else 0.0) >= cap_usd
    total = await redis.incrbyfloat(key, amount_usd)
    if await redis.ttl(key) < 0:  # -1 = cheie fără expirare (primul increment al ferestrei)
        await redis.expire(key, window_s)
    return float(total) >= cap_usd


# --- reseed contor zilnic la pierderea Redis (NX-125, best-effort) -----------


async def seed_daily_cost(conn: asyncpg.Connection, redis: Redis, business_id: str) -> None:
    """Reseed LAZY al contorului zilnic din sursa durabilă, ca un FLUSHALL să nu reseteze plafonul
    la 0 pentru tot restul zilei. Santinelă `cost_seeded:{business}:{today}` → o singură dată/zi;
    seedăm DOAR dacă `cost:{business}:{today}` lipsește (nu clobberăm un contor viu). Sursă:
    `usage_daily.cost_usd` pe ziua curentă. Best-effort: orice eroare → log + continuă (nu blochează
    boot-ul / turul). `conn` = tenant_conn (`bot_runtime` are SELECT pe `usage_daily`)."""
    today = _today()
    sentinel = f"cost_seeded:{business_id}:{today}"
    cost_key = f"cost:{business_id}:{today}"
    try:
        if await redis.get(sentinel):
            return
        if await redis.get(cost_key) is None:
            row = await conn.fetchval(
                "select cost_usd from usage_daily where business_id = $1 and day = $2",
                business_id,
                datetime.now(UTC).date(),
            )
            if row:
                await redis.set(cost_key, float(row), ex=_COST_KEY_TTL_S)
        await redis.set(sentinel, "1", ex=_COST_KEY_TTL_S)
    except Exception as e:  # noqa: BLE001 — reseed best-effort, NU blochează (P6)
        log.warning("cost guard: reseed eșuat (%s) → continuă fără seed", type(e).__name__)


def estimate_turn_cost(
    events: Sequence[Event], *, cost_triage_usd: float, cost_agent_usd: float
) -> float:
    """DEPRECATED pentru contorizare (NX-125): contorul zilnic e alimentat acum cu costul EXACT
    din tokeni (`ctx.usage.cost_usd`, vezi `_record_turn_cost`). Rămâne ca estimare-ceiling
    pre-LLM (opțional, înainte de a apela LLM-ul) + pt costurile best-effort fără `ctx.usage`.

    Estimare grosieră a costului LLM al unui tur, din evenimente (sursa de FACTURARE rămâne
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
