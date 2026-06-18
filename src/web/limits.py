"""Rate limit web (NX-20b) — DOUĂ chei: IP + visitor.

Web-ul e public anonim → abuz mai ușor decât WhatsApp (unde numărul e un cost real pt atacator).
Două contoare Redis peste primitiva `incr_window` (worker/limits.py): `webrl:ip:{token}:{ip}` și
`webrl:visitor:{visitor_id}`. IP-ul prinde un atacator care rotește `visitor_id`; `visitor_id`
prinde un client legit care spam-ează de pe un singur browser. Oricare depășește pragul → 429.

Best-effort: NU prindem erorile de Redis — caller-ul (endpointul) le tratează (503). Logurile NU
conțin token/IP/visitor_id (P12).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.worker.limits import incr_window

if TYPE_CHECKING:
    from redis.asyncio import Redis


async def web_rate_limited(
    redis: Redis,
    token: str,
    ip: str,
    visitor_id: str,
    *,
    max_ip: int,
    max_visitor: int,
    window_s: int,
) -> str | None:
    """Întoarce limiter-ul depășit (`"ip"` | `"visitor"`) sau `None` dacă requestul e sub praguri.

    Contorul de IP se verifică primul (prinde rotirea de visitor_id) — dacă a sărit, nu mai
    incrementăm contorul de visitor (requestul oricum se respinge). `ip` gol (necunoscut) →
    sărim cheia de IP și ne bazăm pe contorul de visitor."""
    if ip:
        if await incr_window(redis, f"webrl:ip:{token}:{ip}", window_s) > max_ip:
            return "ip"
    if await incr_window(redis, f"webrl:visitor:{visitor_id}", window_s) > max_visitor:
        return "visitor"
    return None
