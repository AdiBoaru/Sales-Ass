"""Încărcarea configului de business în `BusinessConfig`.

Citit la intrarea în pipeline (după rezolvarea canalului), pe o conexiune
tenant-scoped — RLS pe `businesses` e `id = current_business_id()`.
"""

import json
from typing import Any

import asyncpg

from src.models import BusinessConfig


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value) if isinstance(value, str) else value


async def load_business(conn: asyncpg.Connection, business_id: str) -> BusinessConfig | None:
    """Întoarce `BusinessConfig` pentru business_id, sau None dacă lipsește.
    `conn` trebuie să fie tenant-scoped pe ACEST business_id."""
    row = await conn.fetchrow(
        """
        select
            id::text          as id,
            slug,
            name,
            vertical,
            default_locale,
            supported_locales,
            timezone,
            settings,
            daily_cost_cap_usd
        from businesses
        where id = $1
        """,
        business_id,
    )
    if row is None:
        return None
    return BusinessConfig(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        vertical=row["vertical"] or "ecommerce",
        default_locale=row["default_locale"] or "ro",
        supported_locales=list(row["supported_locales"] or ["ro"]),
        timezone=row["timezone"] or "Europe/Bucharest",
        settings=_loads(row["settings"]),
        daily_cost_cap_usd=(
            float(row["daily_cost_cap_usd"]) if row["daily_cost_cap_usd"] is not None else None
        ),
    )
