"""Query-uri pe `wa_templates` — citire de template-uri aprobate (proactiv, NX-71).

Proactivul în afara ferestrei de 24h Meta poate trimite DOAR un template cu
`status='approved'` (vezi CLAUDE.md „PROACTIV"). Aici doar CITIM template-ul;
submit-ul/aprobarea la Meta (ciclul de viață al `status`) e task de margine.

Principiul 11 (limba e parte din cheie): lookup-ul filtrează pe `language = locale`;
un template în limba greșită NU e un fallback, e un bug — întoarcem `None`.

`conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

import json
from typing import Any

import asyncpg


def _loads_variables(value: Any) -> list[str]:
    """`wa_templates.variables` (jsonb) → lista de nume așteptate. None/'' → []."""
    if not value:
        return []
    return json.loads(value) if isinstance(value, str) else value


async def get_approved_template(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    channel_id: str,
    name: str,
    locale: str,
) -> dict[str, Any] | None:
    """Cel mai nou template APROBAT pentru (business, canal, nume, limbă).

    Filtrează pe `business_id` (P7) + `language = locale` (P11) + `status='approved'`.
    `order by version desc limit 1` = ultima versiune aprobată. Lipsă în `locale` →
    `None` (NU cădem pe altă limbă). Întoarce dict cu `variables` deja deserializat.
    """
    row = await conn.fetchrow(
        """
        select id::text, name, language, body, variables, provider_template_id
        from wa_templates
        where business_id = $1
          and channel_id = $2
          and name = $3
          and language = $4
          and status = 'approved'
        order by version desc
        limit 1
        """,
        business_id,
        channel_id,
        name,
        locale,
    )
    if row is None:
        return None
    d = dict(row)
    d["variables"] = _loads_variables(d["variables"])
    return d
