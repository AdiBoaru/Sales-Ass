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


async def get_data_version(conn: asyncpg.Connection, business_id: str) -> int:
    """Versiunea de date a businessului (G5b-2) — citită o dată per tur dynamic ca să
    invalideze în bloc cache-ul vechi. 1 dacă lipsește (default schema)."""
    val = await conn.fetchval(
        "select data_version from businesses where id = $1",
        business_id,
    )
    return int(val) if val is not None else 1


async def bump_data_version(conn: asyncpg.Connection, business_id: str) -> int:
    """Incrementează `businesses.data_version` → toate entry-urile cache dynamic vechi
    devin instant inaccesibile la următorul lookup. Apelat de jobul de sync de catalog
    la final (când va exista) sau manual (scripts/bump_cache_version.py). Întoarce noua
    versiune. Entry-urile `static` IGNORĂ data_version (nu sunt afectate)."""
    return await conn.fetchval(
        """
        update businesses set data_version = data_version + 1
         where id = $1
        returning data_version
        """,
        business_id,
    )
