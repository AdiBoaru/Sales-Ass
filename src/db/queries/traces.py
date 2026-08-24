"""NX-256 — captura FULL a turului (`conversation_traces`): insert + retenție + GDPR.

Tabelul e unealtă de DIAGNOZĂ (migrarea 045): un rând per tur cu clientul verbatim,
Reply-ul semantic complet, recomandările și intermediarele care altfel se pierd
(JSON-ul rich brut, id-urile picate la membership). Nimic de pe drumul turului nu
CITEȘTE de aici — singurul scriitor e aftercare-ul, singurii cititori sunt oamenii
și tooling-ul de test.

Convenții:
  • insert pe conexiune TENANT (bot_runtime + RLS) — scriitorul e în tur;
  • retenția + erase-ul GDPR pe conexiune ADMIN (cross-tenant / security definer path),
    ca la `web_turns` — bot_runtime nu are DELETE pe tabel, prin design.
"""

import json
from typing import Any

import asyncpg


async def insert_trace(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    conversation_id: str,
    contact_id: str,
    turn_id: str,
    channel_kind: str | None,
    language: str | None,
    model_route: str | None,
    client_text: str | None,
    bot_text: str | None,
    reply: dict[str, Any] | None,
    recommended: list[dict[str, Any]] | None,
    diagnostics: dict[str, Any] | None,
) -> None:
    """Un tur = un rând. `ON CONFLICT DO NOTHING` pe (business_id, turn_id): dacă un retry
    de aftercare ar reveni vreodată pe același tur, prima captură (cea mai apropiată de
    momentul turului) rămâne — o suprascriere ar putea „repara" retroactiv exact dovada
    pe care tabelul există s-o păstreze."""
    await conn.execute(
        """
        insert into conversation_traces
          (business_id, conversation_id, contact_id, turn_id, channel_kind,
           language, model_route, client_text, bot_text, reply, recommended, diagnostics)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12::jsonb)
        on conflict (business_id, turn_id) do nothing
        """,
        business_id,
        conversation_id,
        contact_id,
        turn_id,
        channel_kind,
        language,
        model_route,
        client_text,
        bot_text,
        json.dumps(reply, ensure_ascii=False, default=str) if reply is not None else None,
        json.dumps(recommended, ensure_ascii=False, default=str)
        if recommended is not None
        else None,
        json.dumps(diagnostics, ensure_ascii=False, default=str)
        if diagnostics is not None
        else None,
    )


async def cleanup_conversation_traces(
    conn: asyncpg.Connection,
    *,
    older_than_days: int = 30,
    batch_size: int = 5000,
) -> int:
    """Retenție BOUNDED (admin, cross-tenant, modelul `cleanup_web_turns`): captura e
    diagnoză, nu arhivă. Guard `to_regclass` → no-op tăcut pe o DB fără migrarea 045,
    deci jobul se poate înregistra necondiționat de flag (rândurile acumulate nu rămân
    pe disc dacă flagul se stinge)."""
    if not await conn.fetchval("select to_regclass('public.conversation_traces') is not null"):
        return 0
    result = await conn.execute(
        """
        delete from conversation_traces
         where id in (
            select id from conversation_traces
             where created_at < now() - make_interval(days => $1)
             limit $2
         )
        """,
        older_than_days,
        batch_size,
    )
    return int(result.split()[-1]) if isinstance(result, str) else 0


async def delete_traces_for_contact(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> int:
    """GDPR erase (modelul `delete_turns_for_contact`, NX-232): captura conține vocea
    clientului → se șterge în ACEEAȘI tranzacție cu `gdpr_erase_contact`. Guard
    `to_regclass` — erase-ul nu are voie să pice pe o DB fără migrarea 045."""
    if not await conn.fetchval("select to_regclass('public.conversation_traces') is not null"):
        return 0
    result = await conn.execute(
        "delete from conversation_traces where business_id = $1 and contact_id = $2",
        business_id,
        contact_id,
    )
    # asyncpg întoarce "DELETE <n>"; conexiunile fake din testele GDPR întorc None → 0.
    return int(result.split()[-1]) if isinstance(result, str) else 0
