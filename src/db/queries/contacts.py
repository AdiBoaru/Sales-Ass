"""Query-uri pe `contacts` + identity resolution prin `channel_identities`.

Identity resolution (Gates, stagiul 3): același user pe un canal = un singur
`contact`. PII-ul de canal (telefon E.164 / id canal) trăiește DOAR în
`channel_identities` (principiul 12) — niciodată în `contacts` și niciodată în
loguri.

Principiul 7: fiecare query are EXPLICIT `where business_id = $1` (mecanism
primar). RLS (rolul bot_runtime + app.business_id din tenant_conn) e plasa.

`conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

import json
from decimal import Decimal
from typing import Any

import asyncpg

from src.models import Contact

# Câmpurile non-PII din contacts pe care le încărcăm în Contact.
_CONTACT_COL_LIST = [
    "id::text",
    "business_id::text",
    "display_name",
    "locale",
    "profile",
    "lead_score",
    "lifecycle",
    "consent",
    "is_blocked",
]
# Variantă simplă (INSERT ... RETURNING, fără join).
_CONTACT_COLS = ", ".join(_CONTACT_COL_LIST)
# Variantă calificată cu aliasul `c` (SELECT cu join pe channel_identities, unde
# `id`/`business_id` ar fi altfel ambigue).
_CONTACT_COLS_C = ", ".join(f"c.{col}" for col in _CONTACT_COL_LIST)


class IdentityRaced(RuntimeError):
    """Intern: un alt tur a creat identitatea între SELECT și INSERT.
    Nu iese din modul — declanșează un re-SELECT."""


def _loads(value: Any) -> dict[str, Any]:
    """jsonb din asyncpg vine ca str → dict. None/'' → {}."""
    if not value:
        return {}
    return json.loads(value) if isinstance(value, str) else value


def _row_to_contact(row: asyncpg.Record) -> Contact:
    return Contact(
        id=row["id"],
        business_id=row["business_id"],
        display_name=row["display_name"],
        locale=row["locale"],
        profile=_loads(row["profile"]),
        lead_score=float(row["lead_score"]),
        lifecycle=row["lifecycle"],
        consent=_loads(row["consent"]),
        is_blocked=row["is_blocked"],
    )


async def _select_by_identity(
    conn: asyncpg.Connection,
    business_id: str,
    channel_kind: str,
    external_id: str,
) -> Contact | None:
    """Lookup contact prin (business_id, channel_kind, external_id)."""
    row = await conn.fetchrow(
        f"""
        select {_CONTACT_COLS_C}
        from contacts c
        join channel_identities ci on ci.contact_id = c.id
        where c.business_id = $1
          and ci.business_id = $1
          and ci.channel_kind = $2
          and ci.external_id = $3
        """,
        business_id,
        channel_kind,
        external_id,
    )
    return _row_to_contact(row) if row else None


async def get_contact_by_id(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> Contact | None:
    """Încarcă un contact după id (proactiv NX-70: avem nevoie de `consent` pentru poartă).

    `business_id = $1` explicit (P7). PII-ul de canal NU e aici (doar channel_identities)."""
    row = await conn.fetchrow(
        f"select {_CONTACT_COLS} from contacts where business_id = $1 and id = $2",
        business_id,
        contact_id,
    )
    return _row_to_contact(row) if row else None


async def block_contact(conn: asyncpg.Connection, business_id: str, contact_id: str) -> None:
    """Abuse blocklist (NX-15): marchează contactul ca blocat. Gate-ul tace contactele
    blocate la următoarele tururi. `conn` tenant-scoped; bot_runtime are UPDATE pe contacts."""
    await conn.execute(
        "update contacts set is_blocked = true where business_id = $1 and id = $2",
        business_id,
        contact_id,
    )


async def update_contact_profile_and_score(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    profile_patch: dict[str, Any],
    lead_score: float,
) -> None:
    """Post-tur (NX-88): MERGE `profile` (`||`, NU overwrite → cheile vechi rămân) + set
    `lead_score` (calculat în COD, nu de LLM) + `updated_at`. Owner EXCLUSIV la runtime pe aceste
    două câmpuri (P3). `business_id = $1` explicit (P7); `bot_runtime` are UPDATE pe contacts (003).

    `coalesce(profile, '{}')`: defensiv pe profile NULL (în schema_v2 e NOT NULL default '{}', dar
    nu plătim un crash pe state vechi). `lead_score` e numeric(5,2) → trimis ca Decimal."""
    await conn.execute(
        """
        update contacts
           set profile = coalesce(profile, '{}'::jsonb) || $3::jsonb,
               lead_score = $4,
               updated_at = now()
         where business_id = $1 and id = $2
        """,
        business_id,
        contact_id,
        json.dumps(profile_patch),
        Decimal(str(round(lead_score, 2))),
    )


async def get_or_create_contact(
    conn: asyncpg.Connection,
    business_id: str,
    channel_kind: str,
    external_id: str,
    *,
    display_name: str | None = None,
    locale: str | None = None,
    verified: bool = False,
) -> Contact:
    """Rezolvă contactul după identitatea de canal; îl creează dacă lipsește.

    Drumul rapid (cazul comun): contactul există → un singur SELECT. Drumul de
    creare e tranzacțional: insert contact + insert channel_identity împreună.
    `unique(business_id, channel_kind, external_id)` previne duplicatele sub
    concurență — dacă două tururi creează același contact nou simultan, cel care
    pierde conflictul face rollback la contactul orfan și re-citește câștigătorul.

    `verified` (NX-129): marchează identitatea ca VERIFICATĂ la creare (login passthrough web —
    `external_id` = customer_ref dovedit prin JWT). Anonim (visitor_id) = False (default). E creată
    DOAR cu un token valid → nu re-evaluăm flagul pe drumul rapid (rămâne stabil).
    """
    existing = await _select_by_identity(conn, business_id, channel_kind, external_id)
    if existing is not None:
        return existing

    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                insert into contacts (business_id, display_name, locale)
                values ($1, $2, $3)
                returning {_CONTACT_COLS}
                """,
                business_id,
                display_name,
                locale,
            )
            won = await conn.fetchval(
                """
                insert into channel_identities
                    (business_id, contact_id, channel_kind, external_id, verified)
                values ($1, $2, $3, $4, $5)
                on conflict (business_id, channel_kind, external_id) do nothing
                returning contact_id
                """,
                business_id,
                row["id"],
                channel_kind,
                external_id,
                verified,
            )
            if won is None:
                # alt tur a creat identitatea între timp → rollback orfanul
                raise IdentityRaced
            return _row_to_contact(row)
    except IdentityRaced:
        existing = await _select_by_identity(conn, business_id, channel_kind, external_id)
        if existing is None:  # pragma: no cover — imposibil după conflict
            raise RuntimeError("channel_identity conflict fără rând existent") from None
        return existing
