"""Query-uri pe `faqs` — regulile magazinului (livrare, retur, plată, garanție).

Fără embeddings (decizie 2026-09-24): FAQ-ul nu se mai caută pe vectori. Unealta `faq_lookup`
aduce setul ACTIV al tenantului, pe locale, iar modelul alege răspunsul potrivit. Măsurat pe
30 de zile (`sole-ro`): căutarea pe vectori servise 0 răspunsuri din 102 încercări, iar setul
întreg are 20 de intrări, deci încape în context fără selecție prealabilă.

`conn` e DEJA tenant-scoped (tenant_conn). RLS pe `bot_runtime` (003) e plasa: un query fără
filtru de tenant → 0 rânduri, nu datele altui client (P7). `locale = $2` în WHERE: un răspuns în
limba greșită e un BUG (P11).
"""

from typing import Any

import asyncpg


async def list_active(
    conn: asyncpg.Connection, business_id: str, locale: str, *, limit: int
) -> list[dict[str, Any]]:
    """FAQ-urile ACTIVE ale tenantului pe `locale`, în ordine stabilă (după întrebare).

    Ordinea e deterministă ca același set să producă același text de unealtă de la un tur la
    altul: un răspuns de unealtă stabil e cache-uit la furnizor ca parte din prefix. `limit` e
    plafonul impus de apelant (P4), nu o sugestie."""
    rows = await conn.fetch(
        """
        select id::text as id, question, answer
        from faqs
        where business_id = $1
          and locale = $2
          and is_active = true
        order by question, id
        limit $3
        """,
        business_id,
        locale,
        limit,
    )
    return [dict(r) for r in rows]
