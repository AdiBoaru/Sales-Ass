"""Rezolvarea canalului — control plane (NU date de tenant).

Problema de bootstrap: un mesaj inbound vine cu `phone_number_id` (canalul Meta),
dar pentru a deschide o conexiune tenant-scoped avem nevoie de `business_id` —
exact ce încercăm să aflăm. Lookup-ul phone_number_id → business e deci o
operație de CONTROL PLANE, rulată pe o conexiune admin (`admin_conn`), nu pe una
de tenant. E singura excepție de la „business_id pe tot": aici îl DERIVĂM.

`channels` e o tabelă de infrastructură (mapare canal→business), nu date de
client. Lookup-ul e parametrizat (zero injection) și întoarce strict id-urile.
"""

import asyncpg


async def resolve_channel(
    conn: asyncpg.Connection,
    channel_kind: str,
    provider_account_id: str,
) -> dict[str, str] | None:
    """(channel_kind, provider_account_id) → {business_id, channel_id}, sau None.

    Generic pe canal (NX-60): phone_number_id la WhatsApp, bot id la Telegram, ...
    A se rula pe `admin_conn` (cross-tenant): la momentul apelului încă nu avem
    un tenant scope. Filtrăm pe canal activ — un canal dezactivat nu primește
    procesare (mesajele lui se ignoră, nu crapă worker-ul).
    """
    row = await conn.fetchrow(
        """
        select id::text as channel_id, business_id::text as business_id
        from channels
        where kind = $1
          and provider_account_id = $2
          and status = 'active'
        """,
        channel_kind,
        provider_account_id,
    )
    return dict(row) if row else None


async def resolve_channel_by_phone(
    conn: asyncpg.Connection,
    phone_number_id: str,
) -> dict[str, str] | None:
    """Wrapper WhatsApp peste `resolve_channel` (compat)."""
    return await resolve_channel(conn, "whatsapp", phone_number_id)


async def upsert_channel(
    conn: asyncpg.Connection,
    business_id: str,
    kind: str,
    provider_account_id: str,
    *,
    display_name: str | None = None,
) -> dict:
    """Creează/actualizează un canal (idempotent pe unique(kind, provider_account_id)).

    Operație de ONBOARDING — a se rula cu rol ADMIN (postgres), NU bot_runtime:
    `channels` e read-only pentru bot. Întoarce {id, created} (created=False dacă
    rândul exista deja și a fost reactivat). Folosit de scripturile de seed."""
    row = await conn.fetchrow(
        """
        insert into channels (business_id, kind, provider_account_id, display_name, status)
        values ($1, $2, $3, $4, 'active')
        on conflict (kind, provider_account_id) do update
            set status = 'active',
                business_id = excluded.business_id,
                display_name = coalesce(excluded.display_name, channels.display_name)
        returning id::text as id, (xmax = 0) as created
        """,
        business_id,
        kind,
        provider_account_id,
        display_name,
    )
    return {"id": row["id"], "created": row["created"]}
