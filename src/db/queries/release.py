"""NX-249 — SQL-ul controllerului de release: policy (control plane) + cohorturi (tenant).

Două suprafețe, DELIBERAT separate, pe conexiuni diferite:

  • **`release_policies`** — control plane. Rândul poartă allowlistul de tenanți, deci nu are ce
    căuta pe o conexiune tenant-scoped: un tenant nu trebuie să afle cine altcineva e în canary.
    Se citește pe `admin_conn`, ca maparea canal→business. Migrarea 044 nici nu dă grant lui
    `bot_runtime` — deci nu e o convenție, e o imposibilitate.

  • **`web_turns`** — tenant. Captura asignării și faptele de cohort au `business_id = $1` peste
    tot (P7), iar `renderable` se calculează ÎN SQL (`response_json` nu iese din DB, ca la NX-246).

Nimic de aici nu ia decizii: SQL-ul întoarce fapte, `src/release/` decide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from src.db.queries.web_turn_slo import _RENDERABLE
from src.release.models import CapturedExecution

log = logging.getLogger(__name__)


class PolicyStoreUnavailable(RuntimeError):
    """Storeul de policy nu poate fi citit (tabel absent, conexiune moartă, timeout).

    Distinct de „nu există policy": primul e un incident de infrastructură care trebuie să ducă
    fail-closed ȘI să se vadă în metrici; al doilea e o stare normală de dinainte de primul apply.
    """


@dataclass(frozen=True, slots=True)
class PolicyRow:
    """Un rând de istoric: documentul + cine/când/de ce l-a aplicat."""

    environment: str
    revision: int
    policy_id: str
    policy: dict[str, Any]
    actor: str
    reason: str
    change_ticket: str | None
    applied_at: datetime


def _to_policy_row(rec: asyncpg.Record | None) -> PolicyRow | None:
    if rec is None:
        return None
    raw = rec["policy"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return PolicyRow(
        environment=rec["environment"],
        revision=int(rec["revision"]),
        policy_id=rec["policy_id"],
        policy=payload if isinstance(payload, dict) else {},
        actor=rec["actor"],
        reason=rec["reason"],
        change_ticket=rec["change_ticket"],
        applied_at=rec["applied_at"],
    )


async def _table_exists(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("select to_regclass('public.release_policies') is not null"))


async def current_policy(conn: asyncpg.Connection, environment: str) -> PolicyRow | None:
    """Ultima revizie a mediului. `None` = niciun apply încă (nu e o eroare).

    Ridică `PolicyStoreUnavailable` dacă tabelul lipsește: pe un deployment fără migrarea 044,
    controllerul trebuie să meargă fail-closed (control), nu să creadă că „nu există policy" —
    ambele duc la control azi, dar numai una se vede corect în raport.
    """
    if not await _table_exists(conn):
        raise PolicyStoreUnavailable("release_policies lipsește (migrarea 044 neaplicată)")
    rec = await conn.fetchrow(
        """
        select environment, revision, policy_id, policy, actor, reason, change_ticket, applied_at
          from release_policies
         where environment = $1
         order by revision desc
         limit 1
        """,
        environment,
    )
    return _to_policy_row(rec)


async def policy_history(
    conn: asyncpg.Connection, environment: str, *, limit: int = 50
) -> list[PolicyRow]:
    """Istoricul, cel mai recent primul. Bounded: un incident se citește pe ultimele revizii."""
    if not await _table_exists(conn):
        raise PolicyStoreUnavailable("release_policies lipsește (migrarea 044 neaplicată)")
    recs = await conn.fetch(
        """
        select environment, revision, policy_id, policy, actor, reason, change_ticket, applied_at
          from release_policies
         where environment = $1
         order by revision desc
         limit $2
        """,
        environment,
        limit,
    )
    return [row for row in (_to_policy_row(r) for r in recs) if row is not None]


async def insert_policy_revision(
    conn: asyncpg.Connection,
    *,
    environment: str,
    revision: int,
    policy_id: str,
    policy: dict[str, Any],
    actor: str,
    reason: str,
    change_ticket: str | None = None,
) -> bool:
    """CAS: `False` = revizia era deja luată (alt actor a aplicat între citire și scriere).

    Compare-and-set e în SCHEMĂ (`unique (environment, revision)`), nu într-o secvență
    citește-apoi-scrie: între cele două ar exista o fereastră în care doi operatori aplică
    simultan policy-uri diferite și ultimul câștigă tăcut. Aici al doilea PIERDE, explicit, și
    `scripts/release_control.py` îi spune să recitească.
    """
    if not await _table_exists(conn):
        raise PolicyStoreUnavailable("release_policies lipsește (migrarea 044 neaplicată)")
    try:
        await conn.execute(
            """
            insert into release_policies
                (environment, revision, policy_id, policy, actor, reason, change_ticket)
            values ($1, $2, $3, $4::jsonb, $5, $6, $7)
            """,
            environment,
            revision,
            policy_id,
            json.dumps(policy),
            actor,
            reason,
            change_ticket,
        )
    except asyncpg.UniqueViolationError:
        return False
    return True


# ── Captura de asignare (tenant) ────────────────────────────────────────────────────────────
async def latest_capture(
    conn: asyncpg.Connection, business_id: str, conversation_id: str
) -> CapturedExecution | None:
    """Asignarea celui mai recent turn CAPTURAT al conversației — sticky-ul, citit din ledger.

    `release_track is not null` în WHERE, nu în Python: turele de dinainte de migrarea 044 nu
    sunt „champion", sunt necunoscute, iar o conversație care are ȘI ture vechi, ȘI ture noi
    trebuie să-și amintească ultima decizie REALĂ, nu ultimul rând.

    `business_id = $1` (P7); indexul `idx_web_turns_conversation` acoperă ordonarea.
    """
    rec = await conn.fetchrow(
        """
        select release_track, release_policy_id, release_policy_revision, pipeline_version
          from web_turns
         where business_id = $1 and conversation_id = $2 and release_track is not null
         order by accepted_at desc
         limit 1
        """,
        business_id,
        conversation_id,
    )
    if rec is None:
        return None
    return CapturedExecution(
        track=rec["release_track"],
        policy_id=rec["release_policy_id"] or "",
        policy_revision=rec["release_policy_revision"],
        pipeline_version=rec["pipeline_version"],
    )


# ── Fapte de cohort (tenant) ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CohortTurnFact:
    """Un rând de ledger redus la ce trebuie pentru comparația candidate/control.

    Ca `TurnFact` (NX-246), plus cohortul. Fără `response_json`, fără text, fără `conversation_id`:
    raportul numără, nu citește conversații.
    """

    track: str
    status: str
    safe_error_code: str | None
    attempt: int
    accepted_at: datetime
    completed_at: datetime | None
    renderable: bool
    policy_id: str | None
    policy_revision: int | None
    has_action: bool


@dataclass(frozen=True, slots=True)
class CohortPage:
    facts: list[CohortTurnFact]
    truncated: bool
    total: int


async def load_cohort_facts(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    window_from: datetime,
    window_to: datetime,
    limit: int = 50_000,
) -> CohortPage:
    """Faptele ferestrei, cu cohortul pe fiecare rând. Un tenant, zero conținut.

    `has_action` se derivă din `safe_error_code`? Nu — din contract: un turn pornit dintr-un buton
    are `content_type='action'` pe mesajul inbound (migrarea 043). Îl luăm cu un lateral join
    mărginit, fiindcă „candidate e mai lent DOAR pe cohortul de acțiuni" e exact genul de regresie
    pe care o medie globală o ascunde (cardul o cere explicit în failure matrix).

    Fereastra se filtrează pe `accepted_at`, ca la NX-246: altfel turele lente ar dispărea din
    numitor exact când sistemul e lent.
    """
    if not await conn.fetchval("select to_regclass('public.web_turns') is not null"):
        log.warning("release: web_turns lipsește (migrarea 040 neaplicată) — raport fără date")
        return CohortPage([], False, 0)
    total = await conn.fetchval(
        "select count(*) from web_turns "
        "where business_id = $1 and accepted_at >= $2 and accepted_at < $3",
        business_id,
        window_from,
        window_to,
    )
    rows = await conn.fetch(
        f"""
        select coalesce(w.release_track, 'unknown') as track,
               w.status,
               w.safe_error_code,
               w.attempt,
               w.accepted_at,
               w.completed_at,
               w.release_policy_id,
               w.release_policy_revision,
               {_RENDERABLE} as renderable,
               coalesce(m.content_type = 'action', false) as has_action
          from web_turns w
          left join lateral (
                select content_type from messages
                 where business_id = w.business_id
                   and conversation_id = w.conversation_id
                   and provider_msg_id = w.client_turn_id::text
                   and direction = 'inbound'
                 order by created_at desc
                 limit 1
               ) m on true
         where w.business_id = $1
           and w.accepted_at >= $2
           and w.accepted_at < $3
         order by w.accepted_at
         limit $4
        """,
        business_id,
        window_from,
        window_to,
        limit,
    )
    facts = [
        CohortTurnFact(
            track=r["track"],
            status=r["status"],
            safe_error_code=r["safe_error_code"],
            attempt=int(r["attempt"]),
            accepted_at=r["accepted_at"],
            completed_at=r["completed_at"],
            renderable=bool(r["renderable"]),
            policy_id=r["release_policy_id"],
            policy_revision=r["release_policy_revision"],
            has_action=bool(r["has_action"]),
        )
        for r in rows
    ]
    return CohortPage(facts, truncated=int(total or 0) > len(facts), total=int(total or 0))


async def write_release_audit(
    conn: asyncpg.Connection,
    *,
    action: str,
    actor: str,
    entity_id: str,
    details: dict[str, Any],
) -> None:
    """Urma autoritativă a unei operații de release, în `audit_log` (control plane, imutabil).

    Nu în `analytics_events`: acolo sunt evenimente de produs, retenționate și partiționate; un
    „cine a apăsat kill-switchul" trebuie să supraviețuiască ștergerii unei partiții. `business_id`
    e NULL deliberat — un release e o decizie de mediu, nu de tenant, iar a-l lipi de un tenant
    arbitrar din allowlist ar fi o minciună convenabilă.

    `actor` vine de la operator (nu e hardcodat ca la GDPR): întreaga poantă a auditului de release
    e că spune CINE, iar CLI-ul îl cere explicit.
    """
    await conn.execute(
        """
        insert into audit_log (business_id, actor, action, entity, entity_id, details)
        values (null, $1, $2, 'release_policy', $3, $4::jsonb)
        """,
        actor,
        action,
        entity_id,
        json.dumps(details, sort_keys=True, ensure_ascii=False),
    )


async def count_active_turns(conn: asyncpg.Connection, business_id: str) -> dict[str, int]:
    """Ture ne-terminale, pe cohort. `unknown` = accept FĂRĂ captură de release.

    Cum recunoaștem un turn v1 „in-flight" la închiderea rutei: controllerul capturează un track
    la FIECARE accept v2; acceptul v1 (`/web/chat`) nu capturează niciodată. Deci un rând activ
    fără captură e, prin construcție, ori un turn v1, ori unul acceptat înainte ca controllerul să
    fie pornit — și amândouă trebuie să BLOCHEZE închiderea. E un criteriu structural, nu o
    euristică pe timp, și eșuează în direcția sigură: dacă nu știm, nu închidem.
    """
    recs = await conn.fetch(
        """
        select coalesce(release_track, 'unknown') as track, count(*)::int as n
          from web_turns
         where business_id = $1 and status in ('accepted', 'running')
         group by 1
        """,
        business_id,
    )
    return {r["track"]: int(r["n"]) for r in recs}
