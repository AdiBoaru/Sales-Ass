"""NX-232 — ledgerul durabil al turelor web (`web_turns`): accept, claim, fencing, replay.

Stratul SQL pur al ledgerului. Regulile de business (fingerprint, proiecția pe wire,
autorizarea de sesiune) stau în `src/web/turn_service.py`; aici sunt DOAR operațiile pe rând,
fiecare cu `business_id = $1` (P7) și fiecare tranziție ca UPDATE condiționat (compare-and-set):

  • `insert_turn`     — accept atomic: INSERT sau None la orice unique (idempotency SAU
                        single-flight); apelantul decide ce înseamnă None re-citind rândul.
  • `claim_turn`      — accepted→running, SAU reclaim pe un `running` cu lease EXPIRAT
                        (epoch+1). Un claim reușit e SINGURUL drum spre completed/failed.
  • `renew_lease`     — heartbeat: același owner + același epoch, altfel 0 rânduri.
  • `complete_turn` / `fail_turn` — terminal DOAR din `running` și DOAR cu epoch-ul curent
                        (fencing: un owner vechi trezit după reclaim face UPDATE pe 0 rânduri).
  • `cancel_turn`     — accepted|running → cancelled, cu error-view (P6: și cancelled e randabil).

Toate terminalele cer `response_json` NE-NULL — CHECK-ul din DB (040) e plasa, validarea
de conținut (bloc randabil) e în service. PII: niciun body/token/visitor_id brut pe niciun rând.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_ROW_COLS = """
    id, business_id, conversation_id, contact_id, session_ref_hash, client_turn_id,
    request_fingerprint, schema_version, status, attempt, lease_owner, lease_epoch,
    lease_expires_at, deadline_at, conversation_revision_at_accept, pipeline_version,
    response_json, safe_error_code, accepted_at, updated_at, completed_at
"""


@dataclass(frozen=True)
class WebTurnRow:
    """Un rând din ledger, ca DATE (fără handle de conexiune, ca `TurnLoadSnapshot`)."""

    id: str
    business_id: str
    conversation_id: str
    contact_id: str
    session_ref_hash: str | None
    client_turn_id: str
    request_fingerprint: str
    schema_version: str
    status: str
    attempt: int
    lease_owner: str | None
    lease_epoch: int
    lease_expires_at: datetime | None
    deadline_at: datetime | None
    conversation_revision_at_accept: int | None
    pipeline_version: str | None
    response_json: dict[str, Any] | None
    safe_error_code: str | None
    accepted_at: datetime
    updated_at: datetime
    completed_at: datetime | None


def _to_row(rec: asyncpg.Record | None) -> WebTurnRow | None:
    if rec is None:
        return None
    d = dict(rec)
    for key in ("id", "business_id", "conversation_id", "contact_id", "client_turn_id"):
        d[key] = str(d[key])
    raw = d.get("response_json")
    d["response_json"] = json.loads(raw) if isinstance(raw, str) else raw
    return WebTurnRow(**d)


async def insert_turn(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    contact_id: str,
    client_turn_id: str,
    request_fingerprint: str,
    *,
    session_ref_hash: str | None = None,
    conversation_revision: int | None = None,
    pipeline_version: str | None = None,
    deadline_at: datetime | None = None,
) -> WebTurnRow | None:
    """Acceptul atomic: INSERT sau None, fără fereastră de race.

    None acoperă AMBELE unique-uri (idempotency pe client_turn_id SAU single-flight pe
    conversație) — nu se pot distinge aici fără TOCTOU. Apelantul re-citește rândul cu
    `get_turn`: există → drumul de idempotency; nu există → alt turn activ (`active_turn`)."""
    try:
        rec = await conn.fetchrow(
            f"""
            insert into web_turns (
                business_id, conversation_id, contact_id, client_turn_id, request_fingerprint,
                session_ref_hash, conversation_revision_at_accept, pipeline_version, deadline_at
            )
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            returning {_ROW_COLS}
            """,
            business_id,
            conversation_id,
            contact_id,
            client_turn_id,
            request_fingerprint,
            session_ref_hash,
            conversation_revision,
            pipeline_version,
            deadline_at,
        )
    except asyncpg.UniqueViolationError:
        return None
    return _to_row(rec)


async def get_turn(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    client_turn_id: str,
) -> WebTurnRow | None:
    """Rândul unui turn după cheia de idempotency. `business_id = $1` (P7)."""
    rec = await conn.fetchrow(
        f"select {_ROW_COLS} from web_turns "
        "where business_id = $1 and conversation_id = $2 and client_turn_id = $3",
        business_id,
        conversation_id,
        client_turn_id,
    )
    return _to_row(rec)


async def get_turn_by_id(
    conn: asyncpg.Connection, business_id: str, turn_id: str
) -> WebTurnRow | None:
    rec = await conn.fetchrow(
        f"select {_ROW_COLS} from web_turns where business_id = $1 and id = $2",
        business_id,
        turn_id,
    )
    return _to_row(rec)


@dataclass(frozen=True)
class ClaimResult:
    """Rezultatul unui claim reușit. `reclaimed=True` = lease-ul anterior era expirat (un
    worker a murit cu turul în mână) — epoch-ul NOU îl deposedează definitiv (fencing)."""

    lease_epoch: int
    attempt: int
    reclaimed: bool


async def claim_turn(
    conn: asyncpg.Connection,
    business_id: str,
    turn_id: str,
    *,
    owner: str,
    lease_ttl_s: int,
) -> ClaimResult | None:
    """`accepted → running`, sau reclaim pe un lease EXPIRAT (`running` cu
    `lease_expires_at < now()`). Atomic: subselect-ul cu FOR UPDATE serializează doi
    revendicatori simultani — unul câștigă epoch-ul nou, celălalt primește 0 rânduri.

    None = nu se poate revendica: turn terminal, sau `running` cu lease încă viu
    (single-flight: procesarea e în altă parte)."""
    rec = await conn.fetchrow(
        """
        update web_turns w
           set status = 'running',
               lease_owner = $3,
               lease_epoch = w.lease_epoch + 1,
               lease_expires_at = now() + make_interval(secs => $4),
               attempt = w.attempt + 1,
               updated_at = now()
          from (
                select id, status as prev_status, lease_expires_at as prev_expiry
                  from web_turns
                 where business_id = $1 and id = $2
                   for update
               ) prev
         where w.id = prev.id
           and w.business_id = $1
           and (
                prev.prev_status = 'accepted'
                or (prev.prev_status = 'running' and prev.prev_expiry < now())
               )
        returning w.lease_epoch, w.attempt, prev.prev_status
        """,
        business_id,
        turn_id,
        owner,
        lease_ttl_s,
    )
    if rec is None:
        return None
    return ClaimResult(
        lease_epoch=rec["lease_epoch"],
        attempt=rec["attempt"],
        reclaimed=rec["prev_status"] == "running",
    )


async def renew_lease(
    conn: asyncpg.Connection,
    business_id: str,
    turn_id: str,
    *,
    owner: str,
    lease_epoch: int,
    lease_ttl_s: int,
) -> bool:
    """Heartbeat: prelungește lease-ul DOAR dacă owner-ul și epoch-ul sunt cele curente.
    False = ai fost deposedat (reclaim) sau turul nu mai e `running` — oprește lucrul."""
    result = await conn.execute(
        """
        update web_turns
           set lease_expires_at = now() + make_interval(secs => $5), updated_at = now()
         where business_id = $1 and id = $2
           and status = 'running' and lease_owner = $3 and lease_epoch = $4
        """,
        business_id,
        turn_id,
        owner,
        lease_epoch,
        lease_ttl_s,
    )
    return result.split()[-1] == "1"


async def complete_turn(
    conn: asyncpg.Connection,
    business_id: str,
    turn_id: str,
    *,
    lease_epoch: int,
    response_json: dict[str, Any],
) -> bool:
    """`running → completed`, cu fencing pe epoch. False = 0 rânduri: alt owner a
    reclamat între timp (sau turul nu mai e `running`) — REZULTATUL TREBUIE ARUNCAT,
    iar apelantul din tranzacția `TurnCommit` face rollback la tot (mesaj+stare+rezultat
    se văd împreună sau deloc)."""
    result = await conn.execute(
        """
        update web_turns
           set status = 'completed', response_json = $4::jsonb,
               completed_at = now(), updated_at = now()
         where business_id = $1 and id = $2
           and status = 'running' and lease_epoch = $3
        """,
        business_id,
        turn_id,
        lease_epoch,
        json.dumps(response_json),
    )
    return result.split()[-1] == "1"


async def fail_turn(
    conn: asyncpg.Connection,
    business_id: str,
    turn_id: str,
    *,
    lease_epoch: int,
    error_view: dict[str, Any],
    safe_error_code: str,
) -> bool:
    """`running → failed`, cu ACELAȘI fencing ca `complete_turn`. `error_view` e un payload
    RANDABIL (P6: și eșecul se afișează), `safe_error_code` e stabil și low-cardinality."""
    result = await conn.execute(
        """
        update web_turns
           set status = 'failed', response_json = $4::jsonb, safe_error_code = $5,
               completed_at = now(), updated_at = now()
         where business_id = $1 and id = $2
           and status = 'running' and lease_epoch = $3
        """,
        business_id,
        turn_id,
        lease_epoch,
        json.dumps(error_view),
        safe_error_code,
    )
    return result.split()[-1] == "1"


async def cancel_turn(
    conn: asyncpg.Connection,
    business_id: str,
    turn_id: str,
    *,
    error_view: dict[str, Any],
    safe_error_code: str = "cancelled",
    lease_epoch: int | None = None,
) -> bool:
    """`accepted|running → cancelled`. Din `accepted` nu există lease (nimeni nu lucrează),
    deci nu cere epoch; din `running` cere epoch-ul curent (nu anulezi turul altcuiva)."""
    result = await conn.execute(
        """
        update web_turns
           set status = 'cancelled', response_json = $3::jsonb, safe_error_code = $4,
               completed_at = now(), updated_at = now()
         where business_id = $1 and id = $2
           and (status = 'accepted' or (status = 'running' and lease_epoch = $5))
        """,
        business_id,
        turn_id,
        json.dumps(error_view),
        safe_error_code,
        lease_epoch if lease_epoch is not None else -1,
    )
    return result.split()[-1] == "1"


# ── NX-233: operațiile executorului async (scan, active-turn, refs de execuție) ─────────────


@dataclass(frozen=True)
class ClaimableTurn:
    """Referință MINIMĂ la un turn revendicabil (scanul executorului/sweeper-ului).
    Doar UUID-uri + timestamps + contoare — zero body, zero token, zero identitate (P12)."""

    business_id: str
    turn_id: str
    status: str
    attempt: int
    deadline_at: datetime | None
    accepted_at: datetime


async def claimable_turns(conn: asyncpg.Connection, *, limit: int) -> list[ClaimableTurn]:
    """Turele revendicabile: `accepted` (nimeni nu lucrează) sau `running` cu lease EXPIRAT
    (workerul a murit cu turul în mână). Ordonate fair (cel mai vechi accept primul), bounded.

    CONTROL PLANE (admin_conn), cross-tenant DELIBERAT — ca `cleanup_web_turns`: executorul
    servește toți tenanții și nu poate ști dinainte al cui e turul. Suprafața e non-PII
    (UUID-uri + timestamps); orice SCRIERE ulterioară e tenant-scoped prin `claim_turn`
    (`business_id = $1` + CAS). Scanul NU e autoritatea de claim — claim-ul CAS decide."""
    recs = await conn.fetch(
        """
        select business_id, id, status, attempt, deadline_at, accepted_at
          from web_turns
         where status = 'accepted'
            or (status = 'running' and lease_expires_at < now())
         order by accepted_at
         limit $1
        """,
        limit,
    )
    return [
        ClaimableTurn(
            business_id=str(r["business_id"]),
            turn_id=str(r["id"]),
            status=r["status"],
            attempt=r["attempt"],
            deadline_at=r["deadline_at"],
            accepted_at=r["accepted_at"],
        )
        for r in recs
    ]


async def get_active_turn(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> WebTurnRow | None:
    """Turnul ACTIV al conversației (cel mult unul — partial unique). Pentru payload-ul
    autorizat al conflictului `conversation_turn_in_progress` (referință, nu pornire)."""
    rec = await conn.fetchrow(
        f"select {_ROW_COLS} from web_turns "
        "where business_id = $1 and conversation_id = $2 and status in ('accepted','running')",
        business_id,
        conversation_id,
    )
    return _to_row(rec)


@dataclass(frozen=True)
class ExecutionRefs:
    """Referințele de care are nevoie executorul ca să RULEZE un turn revendicat, citite din
    DB (autoritatea de recovery): canalul conversației + identitatea de canal a contactului +
    inputul SAFE persistat la accept. Fără token de sesiune, fără body brut (P12: `safe_body`
    e forma deja trecută prin frontiera de privacy NX-230, singura care atinge discul)."""

    channel_id: str
    channel_kind: str
    channel_account_id: str
    sender_external_id: str | None
    inbound_msg_id: str | None
    safe_body: str | None
    content_type: str


async def load_execution_refs(
    conn: asyncpg.Connection, business_id: str, turn_id: str
) -> ExecutionRefs | None:
    """Refs de execuție pentru un turn: conversație → canal, contact → identitate de canal,
    mesajul inbound persistat la accept (`provider_msg_id = client_turn_id`).

    `business_id = $1` pe TOT joinul (P7). None = turnul nu există pe tenantul ăsta."""
    rec = await conn.fetchrow(
        """
        select c.channel_id,
               ch.kind as channel_kind,
               ch.provider_account_id as channel_account_id,
               ci.external_id as sender_external_id,
               m.id as inbound_msg_id,
               m.body as safe_body,
               m.content_type
          from web_turns w
          join conversations c
            on c.business_id = w.business_id and c.id = w.conversation_id
          join channels ch
            on ch.business_id = w.business_id and ch.id = c.channel_id
          left join lateral (
                select external_id from channel_identities
                 where business_id = w.business_id
                   and contact_id = w.contact_id
                   and channel_kind = ch.kind
                 order by created_at desc
                 limit 1
               ) ci on true
          left join lateral (
                select id, body, content_type from messages
                 where business_id = w.business_id
                   and conversation_id = w.conversation_id
                   and provider_msg_id = w.client_turn_id::text
                   and direction = 'inbound'
                 order by created_at desc
                 limit 1
               ) m on true
         where w.business_id = $1 and w.id = $2
        """,
        business_id,
        turn_id,
    )
    if rec is None:
        return None
    return ExecutionRefs(
        channel_id=str(rec["channel_id"]),
        channel_kind=rec["channel_kind"],
        channel_account_id=rec["channel_account_id"],
        sender_external_id=rec["sender_external_id"],
        inbound_msg_id=str(rec["inbound_msg_id"]) if rec["inbound_msg_id"] else None,
        safe_body=rec["safe_body"],
        content_type=rec["content_type"] or "text",
    )


async def delete_turns_for_contact(
    conn: asyncpg.Connection, business_id: str, contact_id: str
) -> int:
    """GDPR erase (rulat pe conexiunea de control plane, în aceeași tranzacție cu
    `gdpr_erase_contact`): ledgerul conține `response_json` — conținut de conversație —
    deci dispare odată cu contactul. `business_id = $1` (P7, ca tot drumul GDPR).

    TRANZITORIU (până la aplicarea migrării 040): pe o DB fără `web_turns`, un DELETE ar
    ABORTA tranzacția de erase (un error poisonează tx-ul chiar prins în Python) → probăm
    întâi cu `to_regclass` (nu ridică) și sărim cu warning. În prod poarta de boot
    (`assert_migrations_current`) garantează tabelul, deci ramura moare după migrare."""
    fetchval = getattr(conn, "fetchval", None)  # conexiunile fake din testele GDPR n-au fetchval
    if fetchval is not None:
        exists = await fetchval("select to_regclass('public.web_turns') is not null")
        if not exists:
            log.warning("gdpr erase: web_turns lipsește (migrarea 040 neaplicată) — skip ledger")
            return 0
    result = await conn.execute(
        "delete from web_turns where business_id = $1 and contact_id = $2",
        business_id,
        contact_id,
    )
    # asyncpg întoarce "DELETE <n>"; conexiunile fake din testele GDPR întorc None → 0.
    return int(result.split()[-1]) if isinstance(result, str) else 0


async def cleanup_web_turns(
    conn: asyncpg.Connection,
    *,
    terminal_older_than_hours: int = 168,
    stale_older_than_days: int = 7,
    batch_size: int = 5000,
) -> int:
    """Retenție BOUNDED (admin, cross-tenant, ca `cleanup_inbound_dedupe`): șterge
    (1) terminalele mai vechi decât fereastra de replay și (2) orfanii ne-terminali
    abandonați (accept fără follow-through / crash nerecuperat). `batch_size` limitează
    o rulare — jobul zilnic repetă până la 0, fără să țină un DELETE gigant.

    Jobul e înregistrat în scheduler NEcondiționat de flagul ledgerului, deci poate rula pe un
    deployment fără migrarea 040: acolo e no-op tăcut, nu o eroare la fiecare interval."""
    if not await conn.fetchval("select to_regclass('public.web_turns') is not null"):
        return 0
    result = await conn.execute(
        """
        delete from web_turns
         where id in (
            select id from web_turns
             where (completed_at is not null
                    and completed_at < now() - make_interval(hours => $1))
                or (status in ('accepted','running')
                    and accepted_at < now() - make_interval(days => $2))
             limit $3
         )
        """,
        terminal_older_than_hours,
        stale_older_than_days,
        batch_size,
    )
    return int(result.split()[-1])
