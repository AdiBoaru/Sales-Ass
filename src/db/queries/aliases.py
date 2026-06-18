"""Query-uri pe `intent_aliases` (+ răspuns FAQ după id) — stratul gratuit alias (NX-73).

Lookup EXACT pe `(business_id, phrase_norm)` printre aliasurile APROBATE — index B-tree
`idx_aliases_lookup`, zero token, ~0 latență. Popularea (candidați promovați din shadow mode)
e NX-93; aici DOAR consumăm.

Principiul 7: ambele query-uri filtrează EXPLICIT `where business_id = $1` (mecanism primar);
RLS pe `bot_runtime` e plasa. `conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

from __future__ import annotations

import asyncpg


async def lookup_alias(conn: asyncpg.Connection, business_id: str, phrase_norm: str) -> dict | None:
    """Match EXACT pe un alias APROBAT după fraza normalizată. Folosește `idx_aliases_lookup
    (business_id, phrase_norm)`, filtrând `status='approved'`. La empate (mai mulți aprobați pe
    aceeași frază) ia cel mai recent. `None` = miss. `business_id = $1` explicit (P7)."""
    row = await conn.fetchrow(
        """
        select id::text as id, target_kind, target_id::text as target_id, target_value
        from intent_aliases
        where business_id = $1
          and phrase_norm = $2
          and status = 'approved'
        order by created_at desc
        limit 1
        """,
        business_id,
        phrase_norm,
    )
    return dict(row) if row else None


async def get_faq_answer(
    conn: asyncpg.Connection, business_id: str, faq_id: str, locale: str
) -> str | None:
    """Răspunsul unui FAQ ACTIV pe `(business_id, id, locale)`. `locale` e parte din filtru
    (P11): un FAQ în limba greșită NU e un hit → `None` (miss grațios). `business_id = $1` (P7)."""
    row = await conn.fetchrow(
        """
        select answer
        from faqs
        where business_id = $1 and id = $2 and locale = $3 and is_active = true
        limit 1
        """,
        business_id,
        faq_id,
        locale,
    )
    return row["answer"] if row else None
