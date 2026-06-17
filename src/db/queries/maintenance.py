"""Query-uri de mentenanță (NX-84) — drop partiții vechi + expire semantic_cache.

Operații CROSS-TENANT pe `admin_conn` (excepția documentată de la „business_id pe tot",
ca `cleanup_inbound_dedupe`/`rollup`): partițiile fizice și `expires_at` nu sunt date ale
unui singur client. Purjele PER-tenant (`purge_business`) rămân în `semantic_cache.py`.

Securitate DDL: numele de partiție e pus pe ALLOWLIST cu `_PART_RE` în AMBELE locuri
(caller + `drop_partition`) — niciun identificator liber nu ajunge în `DROP TABLE`.
"""

import re
from datetime import date

import asyncpg

# Pattern strict: DOAR partițiile lunare ale celor 2 tabele hot (NU *_default, NU altceva).
_PART_RE = re.compile(r"^(messages|analytics_events)_(\d{4})_(\d{2})$")


async def list_time_partitions(conn: asyncpg.Connection) -> list[tuple[str, date]]:
    """Partițiile lunare existente ale tabelelor hot + luna lor (parsată din nume).
    Sursă: pg_inherits (relația părinte-copil); numele e contractul de retenție.
    `*_default` și orice nu se potrivește `_PART_RE` sunt ignorate."""
    rows = await conn.fetch(
        """
        select c.relname as name
        from pg_inherits i
        join pg_class c on c.oid = i.inhrelid
        join pg_class p on p.oid = i.inhparent
        where p.relname in ('messages', 'analytics_events')
        """
    )
    out: list[tuple[str, date]] = []
    for r in rows:
        m = _PART_RE.match(r["name"])
        if not m:  # *_default sau orice altceva → skip (paranoia)
            continue
        out.append((r["name"], date(int(m.group(2)), int(m.group(3)), 1)))
    return out


async def drop_partition(conn: asyncpg.Connection, name: str) -> None:
    """DROP TABLE pe o partiție lunară. Numele e re-validat de `_PART_RE` aici (dublă plasă
    contra injection în identificator). `IF EXISTS` → idempotent."""
    if not _PART_RE.match(name):
        raise ValueError(f"refuz drop pe partiție ne-validată: {name!r}")
    await conn.execute(f'drop table if exists "{name}"')


async def expire_semantic_cache(conn: asyncpg.Connection) -> int:
    """Purjă bulk a entry-urilor expirate. CROSS-TENANT intenționat (admin_conn):
    `expires_at` e independent de `business_id`, iar RLS ar limita la un tenant.
    Întoarce nr. de rânduri șterse."""
    res = await conn.execute("delete from semantic_cache where expires_at < now()")
    # asyncpg întoarce „DELETE <n>"; gol/„DELETE 0" → 0 (nu crăpăm pe parsare)
    return int(res.split()[-1]) if res else 0
