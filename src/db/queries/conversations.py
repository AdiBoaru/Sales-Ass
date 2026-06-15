"""Query-uri pe `conversations` — load/create + patch tranzacțional al state-ului.

`conversations.state` (jsonb ≤8KB) e starea compactă a agentului. Owner la
scriere: Sender, care face patch-ul ÎN ACEEAȘI tranzacție cu scrierea în outbox
(stagiul 9). Patch-ul folosește `state_version` ca optimistic lock: dacă altă
scriere a intervenit între citire și patch, UPDATE-ul nu prinde niciun rând →
`StateConflict` (turul reia, nu suprascrie orbește).

Bugetul de 8KB e impus în context builder + CHECK în DB (003) ca plasă: un patch
peste 8KB ridică eroare din DB, nu trece tăcut (principiul 4).

`state` se întoarce ca dict brut (json) + `state_version` separat. Maparea în
`ConversationState` (dataclass) e treaba context builder-ului (G5), nu a stratului
de date. `conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

import json
from typing import Any

import asyncpg

_CONV_COLS = (
    "id::text, status, bot_active, handoff_until, last_inbound_at, "
    "last_outbound_at, last_message_at, locale, state, state_version, "
    "risk_flags, shadow_mode"
)


class StateConflict(RuntimeError):
    """Optimistic lock pierdut: `state_version` din DB ≠ versiunea așteptată.
    Altă scriere a modificat state-ul între citire și patch."""


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    d["state"] = json.loads(d["state"]) if isinstance(d["state"], str) else (d["state"] or {})
    return d


async def get_open_conversation(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    channel_id: str,
) -> dict[str, Any] | None:
    """Conversația deschisă (cea mai recentă) pentru contact pe acest canal."""
    row = await conn.fetchrow(
        f"""
        select {_CONV_COLS}
        from conversations
        where business_id = $1
          and contact_id = $2
          and channel_id = $3
          and status = 'open'
        order by last_message_at desc nulls last, created_at desc
        limit 1
        """,
        business_id,
        contact_id,
        channel_id,
    )
    return _row_to_dict(row) if row else None


async def get_or_create_conversation(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    channel_id: str,
    *,
    locale: str | None = None,
) -> dict[str, Any]:
    """Întoarce conversația deschisă a contactului pe canal; o creează dacă lipsește.

    Nota: nu există unique pe (contact, channel, open), deci două primele-mesaje
    strict simultane pentru un contact NOU ar putea crea două conversații. În
    practică debounce-ul (stagiul 2) le coalesce într-un singur lot, iar lock-ul
    per-conversație serializează restul. Hardening (advisory lock) → follow-up.
    """
    existing = await get_open_conversation(conn, business_id, contact_id, channel_id)
    if existing is not None:
        return existing

    row = await conn.fetchrow(
        f"""
        insert into conversations (business_id, contact_id, channel_id, locale)
        values ($1, $2, $3, $4)
        returning {_CONV_COLS}
        """,
        business_id,
        contact_id,
        channel_id,
        locale,
    )
    return _row_to_dict(row)


async def patch_conversation_state(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    new_state: dict[str, Any],
    expected_version: int,
    *,
    touch_outbound: bool = False,
) -> int:
    """Scrie `state` cu optimistic lock; întoarce noua `state_version`.

    UPDATE prinde rândul DOAR dacă `state_version = expected_version`. La match,
    incrementează versiunea. Fără match (altă scriere a intervenit) → `StateConflict`.
    `touch_outbound=True` actualizează și `last_outbound_at`/`last_message_at` în
    aceeași tranzacție (Sender: state + outbox atomic).

    A se apela ÎN tranzacția caller-ului (Sender), nu deschide una proprie.
    """
    set_outbound = ", last_outbound_at = now(), last_message_at = now()" if touch_outbound else ""
    new_version = await conn.fetchval(
        f"""
        update conversations
           set state = $4::jsonb,
               state_version = state_version + 1{set_outbound}
         where business_id = $1
           and id = $2
           and state_version = $3
        returning state_version
        """,
        business_id,
        conversation_id,
        expected_version,
        json.dumps(new_state),
    )
    if new_version is None:
        raise StateConflict(
            f"state_version != {expected_version} pentru conversation {conversation_id}"
        )
    return new_version


async def set_handoff(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    *,
    window_minutes: int,
    risk_flag: str,
    assigned_user_id: str | None = None,
) -> None:
    """Escaladează conversația la un om (Gates, G5a): împinge `handoff_until` cu
    `window_minutes` în viitor (botul tace în fereastra asta), adaugă `risk_flag`
    și — opțional — asignează un agent. `assigned_user_id` rămâne cârlig (consola
    de agent îl umple). NU atinge `state`/`state_version` → nu intră în conflict
    cu patch-ul Sender-ului din același tur."""
    await conn.execute(
        """
        update conversations
           set handoff_until = now() + make_interval(mins => $3),
               risk_flags = array_append(risk_flags, $4),
               assigned_user_id = coalesce($5::uuid, assigned_user_id)
         where business_id = $1 and id = $2
        """,
        business_id,
        conversation_id,
        window_minutes,
        risk_flag,
        assigned_user_id,
    )


async def touch_last_inbound(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
) -> None:
    """Marchează `last_inbound_at = now()` (alimentează fereastra 24h Meta).
    Apelat de webhook la primirea unui mesaj inbound."""
    await conn.execute(
        """
        update conversations
           set last_inbound_at = now(), last_message_at = now()
         where business_id = $1 and id = $2
        """,
        business_id,
        conversation_id,
    )
