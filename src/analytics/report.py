"""NX-217 felia 3 — asamblarea raportului de cerere (contractul JSON spre frontend).

Leagă cele trei bucăți fără să adauge logică proprie: faptele (fereastra hibridă rollup + azi),
starea curentă a catalogului și regulile de acțiune. Plus trendul (aceeași măsură pe fereastra
precedentă) și bucla cerere × conversie.

Backend-ul NU compune text de UI. Întoarce dimensiuni, numere, dovadă și numărători separați;
fraza („Adaugă Bioderma: 73 de cereri, 0 în catalog") o scrie frontend-ul, care poate schimba
formularea fără redeploy de backend. Regula de aur rămâne: **fapte numărate, nu estimări** —
nicăieri `estimated_value`, nicăieri `confidence`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from src.analytics.actions import build_actions, health_indicators
from src.config import get_settings
from src.db.queries.demand import revenue_summary
from src.db.queries.demand_report import (
    brand_presence,
    category_funnel,
    product_state,
    window_facts,
)


def _dimension_values(facts: list[dict[str, Any]], kind: str) -> list[str]:
    """Valorile distincte ale unei dimensiuni — inputul pentru interogările de stare curentă."""
    seen: list[str] = []
    for f in facts:
        if f["dimension_kind"] == kind and f["dimension_key"] and f["dimension_key"] not in seen:
            seen.append(f["dimension_key"])
    return seen


async def build_demand_report(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    days: int | None = None,
    min_requests: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Raportul complet pe ultimele `days` zile (inclusiv ziua curentă, din evenimente live).

    Fereastra curentă = [today-days+1, today+1); cea precedentă = aceeași lungime, imediat
    înainte → `prev_count` per acțiune (trend). `conn` = admin (citește analytics_events)."""
    s = get_settings()
    days = days or s.demand_report_window_days
    min_requests = min_requests if min_requests is not None else s.demand_action_min_requests
    today = today or datetime.now(UTC).date()
    until = today + timedelta(days=1)
    since = until - timedelta(days=days)
    prev_since = since - timedelta(days=days)

    facts = await window_facts(conn, business_id, since, until, today=today)
    prev_facts = await window_facts(conn, business_id, prev_since, since, today=today)

    brands = _dimension_values(facts, "brand")
    products = _dimension_values(facts, "product")
    presence = await brand_presence(conn, business_id, brands)
    state = await product_state(conn, business_id, products)

    actions = build_actions(
        facts,
        prev_facts=prev_facts,
        brand_presence=presence,
        product_state=state,
        min_requests=min_requests,
    )
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat(), "days": days},
        "actions": actions,
        "health": health_indicators(facts),
        "category_funnel": await category_funnel(conn, business_id, since, until),
        # North-star (NX-162): bot-led și assisted rămân SEPARATE — însumate ar fi dublă numărare.
        "revenue": await revenue_summary(conn, business_id, since, until),
        "facts": facts,
    }
