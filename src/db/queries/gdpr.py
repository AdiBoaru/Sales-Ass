"""Query-uri pentru stratul GDPR (NX-72) — gdpr_requests + audit_log + export reads.

FIECARE query are `business_id` în WHERE/VALUES (P7). Rulează pe `admin_conn` (control
plane) — GDPR e security definer + PII cross-tabel — dar filtrul în cod e mecanismul
primar: un export nu scapă niciodată un contact din alt tenant.

PII: export-ul CONȚINE `external_id` (e dreptul persoanei la datele ei) — dar NU se
loghează niciodată (logăm doar id-uri). `locale` nu e relevant aici (GDPR e agnostic de limbă).
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value) if isinstance(value, str) else value


def _jsonable(value: Any) -> Any:
    """datetime → ISO, Decimal → float (dict serializabil pt portabilitate)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [{k: _jsonable(v) for k, v in dict(r).items()} for r in records]


# --------------------------------------------------------------------------- #
# gdpr_requests — ciclul de viață al cererii
# --------------------------------------------------------------------------- #


async def create_request(
    conn: asyncpg.Connection, business_id: str, contact_id: str, kind: str, requested_by: str
) -> str:
    return await conn.fetchval(
        """
        insert into gdpr_requests (business_id, contact_id, kind, requested_by, status)
        values ($1, $2, $3, $4, 'pending')
        returning id::text
        """,
        business_id,
        contact_id,
        kind,
        requested_by,
    )


async def mark_processing(conn: asyncpg.Connection, business_id: str, req_id: str) -> None:
    await conn.execute(
        "update gdpr_requests set status = 'processing' where business_id = $1 and id = $2",
        business_id,
        req_id,
    )


async def mark_done(
    conn: asyncpg.Connection, business_id: str, req_id: str, *, result_ref: str | None
) -> None:
    await conn.execute(
        """
        update gdpr_requests set status = 'done', completed_at = now(), result_ref = $3
        where business_id = $1 and id = $2
        """,
        business_id,
        req_id,
        result_ref,
    )


async def mark_failed(conn: asyncpg.Connection, business_id: str, req_id: str) -> None:
    await conn.execute(
        """
        update gdpr_requests set status = 'failed', completed_at = now()
        where business_id = $1 and id = $2
        """,
        business_id,
        req_id,
    )


async def write_audit(
    conn: asyncpg.Connection,
    business_id: str,
    action: str,
    entity: str,
    entity_id: str,
    details: dict[str, Any],
) -> None:
    """Urmă autoritativă în `audit_log` (control plane, imutabil) — NU în analytics_events."""
    await conn.execute(
        """
        insert into audit_log (business_id, actor, action, entity, entity_id, details)
        values ($1, 'gdpr_svc', $2, $3, $4, $5::jsonb)
        """,
        business_id,
        action,
        entity,
        entity_id,
        json.dumps(details),
    )


# --------------------------------------------------------------------------- #
# Existență (izolare la erase) + export reads — toate cu business_id = $1 (P7)
# --------------------------------------------------------------------------- #


async def contact_in_business(conn: asyncpg.Connection, business_id: str, contact_id: str) -> bool:
    """Contactul aparține acestui tenant? Plasă de izolare ÎNAINTE de erase-ul global
    (gdpr_erase_contact nu cere business_id → nu atingem un contact din alt tenant)."""
    return bool(
        await conn.fetchval(
            "select 1 from contacts where business_id = $1 and id = $2",
            business_id,
            contact_id,
        )
    )


async def fetch_contact(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        select display_name, locale, profile, lead_score, lifecycle, consent
        from contacts where business_id = $1 and id = $2
        """,
        business_id,
        contact_id,
    )
    if row is None:
        return None
    d = {k: _jsonable(v) for k, v in dict(row).items()}
    d["profile"] = _loads(row["profile"])
    d["consent"] = _loads(row["consent"])
    return d


async def fetch_identities(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        select channel_kind, external_id, verified, created_at
        from channel_identities where business_id = $1 and contact_id = $2
        """,
        business_id,
        contact_id,
    )
    return _rows(rows)


async def fetch_conversations(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        select id::text, status, locale, created_at
        from conversations where business_id = $1 and contact_id = $2
        order by created_at
        """,
        business_id,
        contact_id,
    )
    return _rows(rows)


async def fetch_messages(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        select conversation_id::text, direction, author, content_type, body, created_at
        from messages where business_id = $1 and contact_id = $2
        order by created_at
        """,
        business_id,
        contact_id,
    )
    return _rows(rows)


async def count_messages(conn: asyncpg.Connection, business_id: str, contact_id: str) -> int:
    return int(
        await conn.fetchval(
            "select count(*) from messages where business_id = $1 and contact_id = $2",
            business_id,
            contact_id,
        )
    )


async def fetch_orders(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        # placed_at = momentul plasării (relevant pt persoană / portabilitate GDPR);
        # created_at = ingestia în sistem. Exportăm ambele; ordonăm pe placed_at (ca commerce.py).
        """
        select external_id, status, total, currency, placed_at, created_at
        from orders where business_id = $1 and contact_id = $2
        order by placed_at
        """,
        business_id,
        contact_id,
    )
    return _rows(rows)
