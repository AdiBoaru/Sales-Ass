"""Dedupe inbound layer 2 — claim durabil pe (business_id, provider_msg_id).

Plasa durabilă din NX-51: prinde retry-urile Meta care scapă de layer 1 (Redis),
ex. după FLUSHALL / restart / pierdere AOF. PK ne-partiționat → ON CONFLICT chiar
funcționează (spre deosebire de unique-ul de pe `messages`, care include cheia de
partiționare). Vezi docs/004_inbound_dedupe.sql.

`conn` trebuie să fie tenant-scoped (tenant_conn).
"""

import asyncpg


async def claim_inbound(
    conn: asyncpg.Connection,
    business_id: str,
    provider_msg_id: str,
) -> bool:
    """Revendică un mesaj pentru procesare. True dacă e NOU (procesează-l),
    False dacă a mai fost văzut (duplicat → skip).

    `INSERT ... ON CONFLICT DO NOTHING RETURNING` e atomic: doar primul apel
    pentru o pereche (business_id, provider_msg_id) primește rând."""
    won = await conn.fetchval(
        """
        insert into inbound_dedupe (business_id, provider_msg_id)
        values ($1, $2)
        on conflict (business_id, provider_msg_id) do nothing
        returning 1
        """,
        business_id,
        provider_msg_id,
    )
    return won is not None


async def cleanup_inbound_dedupe(
    conn: asyncpg.Connection,
    *,
    older_than_hours: int = 48,
) -> int:
    """Șterge markerele mai vechi decât fereastra de retry Meta. Întoarce câte.

    Operație de mentenanță CROSS-TENANT → a se rula pe `admin_conn` (nu tenant_conn):
    purjarea markerelor vechi nu e date de client, iar bot_runtime (RLS) ar șterge
    doar tenantul curent. Markerele non-PII pot fi purjate global."""
    result = await conn.execute(
        "delete from inbound_dedupe where first_seen < now() - make_interval(hours => $1)",
        older_than_hours,
    )
    # asyncpg întoarce "DELETE <n>"
    return int(result.split()[-1])
