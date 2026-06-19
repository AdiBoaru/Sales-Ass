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

    Race-safe (NX-87): două prime-mesaje strict simultane ale unui contact NOU nu mai pot crea
    două conversații deschise — `ON CONFLICT` pe indexul parțial `uq_conversations_one_open`
    (migrația 010) face al doilea INSERT no-op, iar pierzătorul re-citește conversația câștigătoare.
    """
    existing = await get_open_conversation(conn, business_id, contact_id, channel_id)
    if existing is not None:
        return existing

    row = await conn.fetchrow(
        f"""
        insert into conversations (business_id, contact_id, channel_id, locale)
        values ($1, $2, $3, $4)
        on conflict (business_id, contact_id, channel_id) where status = 'open'
        do nothing
        returning {_CONV_COLS}
        """,
        business_id,
        contact_id,
        channel_id,
        locale,
    )
    if row is not None:
        return _row_to_dict(row)
    # Am pierdut cursa: un INSERT simultan a câștigat (ON CONFLICT DO NOTHING → zero rânduri).
    # Re-citim conversația deschisă (a câștigătorului). Garantat să existe (indexul tocmai a prins).
    existing = await get_open_conversation(conn, business_id, contact_id, channel_id)
    if existing is not None:
        return existing
    raise RuntimeError(
        "get_or_create_conversation: conversație deschisă indisponibilă după conflict"
    )


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


async def set_conversation_locale(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    locale: str,
) -> None:
    """Persistă limba detectată (G5c) pe `conversations.locale` → limba „se lipește"
    (procesorul seedează `ctx.language` din ea la turul următor). NU atinge
    `state`/`state_version` → fără conflict cu patch-ul Sender-ului."""
    await conn.execute(
        """
        update conversations
           set locale = $3
         where business_id = $1 and id = $2
        """,
        business_id,
        conversation_id,
        locale,
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


async def is_in_24h_window(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
) -> bool:
    """Fereastra de 24h Meta pentru o conversație (proactiv, NX-71).

    Interogăm funcția SQL `in_24h_window(conversations)` (schema_v2) — sursa de
    adevăr (derivat din `last_inbound_at`, nu flag stocat — CLAUDE.md). `business_id`
    e în WHERE chiar dacă filtrăm pe PK: izolare multi-tenant fără excepție (P7).
    Conversație inexistentă / `last_inbound_at` NULL → `False` (cădem corect pe
    ramura de template, nu trimitem liber din greșeală)."""
    val = await conn.fetchval(
        """
        select in_24h_window(c) from conversations c
         where c.business_id = $1 and c.id = $2
        """,
        business_id,
        conversation_id,
    )
    return bool(val)
