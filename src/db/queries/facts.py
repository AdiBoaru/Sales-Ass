"""NX-148 — conversation_facts: memorie structurată per contact.

Facts stabile despre client (buget, tip de piele, mărime, brand, restricții), extrase post-tur
și injectate țintit în prompt. `select_whitelisted_facts` e PUR (whitelist + dedupe + cap,
testabil fără DB); `upsert_facts` / `fetch_relevant_facts` sunt tenant-scoped (P7). PII: nimic
din `fact_value` nu conține telefon/id canal (whitelist de tipuri + extractorul aruncă restul).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

# Cap dur de facts injectate/persistate per contact (P4 — buget de context în cod).
MAX_FACTS = 10


def select_whitelisted_facts(
    facts: list[dict[str, Any]], whitelist: frozenset[str] | set[str], *, cap: int = MAX_FACTS
) -> list[dict[str, Any]]:
    """PUR: păstrează doar `fact_type` din whitelist, dedupe per tip (ține confidence maxim),
    ordonează pe confidence desc și taie la `cap`. Un `fact_type` inventat de model (în afara
    whitelist-ului per vertical) e ARUNCAT — plasa anti-halucinație de memorie (P12)."""
    best: dict[str, dict[str, Any]] = {}
    for f in facts:
        ftype = f.get("fact_type")
        if not ftype or (whitelist and ftype not in whitelist):
            continue
        if f.get("fact_value") in (None, "", {}, []):
            continue
        conf = float(f.get("confidence") or 0.0)
        prev = best.get(ftype)
        if prev is None or conf >= float(prev.get("confidence") or 0.0):
            best[ftype] = {**f, "confidence": conf}
    ordered = sorted(best.values(), key=lambda f: float(f.get("confidence") or 0.0), reverse=True)
    return ordered[:cap]


async def upsert_facts(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    conversation_id: str | None,
    facts: list[dict[str, Any]],
) -> int:
    """Upsert per (business_id, contact_id, fact_type): un fact re-menționat bump-uie
    `last_seen_at` + `max(confidence)`, nu duplică. Întoarce câte au fost scrise. `WHERE`-ul
    implicit e `business_id` (P7; RLS ca plasă). `fact_value` serializat în jsonb."""
    if not facts:
        return 0
    rows = [
        (
            business_id,
            contact_id,
            conversation_id,
            f["fact_type"],
            json.dumps(f.get("fact_value")),
            float(f.get("confidence") or 0.5),
            f.get("source_message_id"),
            f.get("expires_at"),
        )
        for f in facts
    ]
    await conn.executemany(
        """
        insert into conversation_facts
            (business_id, contact_id, conversation_id, fact_type, fact_value,
             confidence, source_message_id, expires_at)
        values ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
        on conflict (business_id, contact_id, fact_type) do update set
            fact_value        = excluded.fact_value,
            confidence        = greatest(conversation_facts.confidence, excluded.confidence),
            conversation_id   = excluded.conversation_id,
            source_message_id = excluded.source_message_id,
            expires_at        = excluded.expires_at,
            last_seen_at      = now()
        """,
        rows,
    )
    return len(rows)


async def fetch_relevant_facts(
    conn: asyncpg.Connection, business_id: str, contact_id: str, *, limit: int = MAX_FACTS
) -> list[dict[str, Any]]:
    """Facts ne-expirate ale unui contact (tenant-scoped), ordonate pe confidence desc →
    last_seen desc. Pentru injectarea bugetată din `facts_block` (NX-148 felia 2)."""
    rows = await conn.fetch(
        """
        select fact_type, fact_value, confidence, last_seen_at, expires_at
        from conversation_facts
        where business_id = $1 and contact_id = $2
          and (expires_at is null or expires_at > now())
        order by confidence desc, last_seen_at desc
        limit $3
        """,
        business_id,
        contact_id,
        min(limit, MAX_FACTS),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        value = r["fact_value"]
        if isinstance(value, str):
            value = json.loads(value) if value else None
        out.append(
            {
                "fact_type": r["fact_type"],
                "fact_value": value,
                "confidence": r["confidence"],
                "last_seen_at": r["last_seen_at"],
                "expires_at": r["expires_at"],
            }
        )
    return out
