"""Dedupe inbound layer 2 — claim durabil pe (business_id, provider_msg_id).

Plasa durabilă din NX-51: prinde retry-urile Meta care scapă de layer 1 (Redis),
ex. după FLUSHALL / restart / pierdere AOF. PK ne-partiționat → ON CONFLICT chiar
funcționează (spre deosebire de unique-ul de pe `messages`, care include cheia de
partiționare). Vezi docs/004_inbound_dedupe.sql.

`conn` trebuie să fie tenant-scoped (tenant_conn).
"""

import asyncpg

# Cât timp un claim NEFINALIZAT e „în lucru" înainte să fie reclamabil ca orfan (NX-86).
# > latența realistă a unui tur (LLM + tool-uri + DB). Aliniat cu reaper-ul PEL (min_idle 60s).
CLAIM_TTL_S = 300


async def claim_inbound(
    conn: asyncpg.Connection,
    business_id: str,
    provider_msg_id: str,
    *,
    claim_ttl_s: int = CLAIM_TTL_S,
) -> bool:
    """Revendică un mesaj pentru procesare. True = PROCESEAZĂ-l (nou SAU orfan expirat reclamat);
    False = SKIP (duplicat FINALIZAT sau revendicat recent de alt worker).

    Claim-or-resume (NX-86): INSERT nou → claim. ON CONFLICT pe un rând NEFINALIZAT
    (`completed_at IS NULL`) cu claim EXPIRAT (`claimed_at < now()-ttl`) → re-claim (orfan dintr-un
    tur crăpat). Altfel (finalizat, sau revendicat recent) → zero rânduri → skip. Atomic: un singur
    worker câștigă reclaim-ul unui orfan."""
    won = await conn.fetchval(
        """
        insert into inbound_dedupe (business_id, provider_msg_id)
        values ($1, $2)
        on conflict (business_id, provider_msg_id) do update
            set claimed_at = now()
            where inbound_dedupe.completed_at is null
              and inbound_dedupe.claimed_at < now() - make_interval(secs => $3)
        returning 1
        """,
        business_id,
        provider_msg_id,
        claim_ttl_s,
    )
    return won is not None


async def mark_inbound_completed(
    conn: asyncpg.Connection,
    business_id: str,
    provider_msg_id: str,
) -> None:
    """Marchează turul FINALIZAT (`completed_at = now()`) → nu mai e reprocesat (NX-86). Apelat
    în TX-ul de outbox (atomic): crash ÎNAINTE de commit → `completed_at` rămâne NULL → orfan
    recuperabil de reaper; commit reușit → finalizat definitiv. `business_id = $1` (izolare)."""
    await conn.execute(
        "update inbound_dedupe set completed_at = now() "
        "where business_id = $1 and provider_msg_id = $2",
        business_id,
        provider_msg_id,
    )


async def cleanup_inbound_dedupe(
    conn: asyncpg.Connection,
    *,
    older_than_hours: int = 48,
    orphan_age_days: int = 7,
) -> int:
    """Purjă (NX-86), două criterii: (1) FINALIZATE mai vechi de `older_than_hours` (retenție peste
    fereastra de retry Meta); (2) ORFANI abandonați — `completed_at IS NULL` și `claimed_at` mai
    vechi de `orphan_age_days` (tur crăpat, nerecuperat → sigur de șters). Întoarce câte.

    Mentenanță CROSS-TENANT → `admin_conn` (markere non-PII, purjate global)."""
    result = await conn.execute(
        """
        delete from inbound_dedupe
        where (completed_at is not null and completed_at < now() - make_interval(hours => $1))
           or (completed_at is null and claimed_at < now() - make_interval(days => $2))
        """,
        older_than_hours,
        orphan_age_days,
    )
    # asyncpg întoarce "DELETE <n>"
    return int(result.split()[-1])
