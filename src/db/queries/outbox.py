"""Query-uri pe `outbox` — singurul punct de ieșire (stagiul 9).

Sender scrie aici TRANZACȚIONAL (în aceeași TX cu patch-ul de state); un
dispatcher separat citește, trimite la Meta și marchează rezultatul. Idempotența
e garantată de `unique(business_id, idempotency_key)`: a re-enqueua același tur
(ex. retry de pipeline) nu produce un al doilea mesaj.

Claim-ul dispatcher-ului folosește `FOR UPDATE SKIP LOCKED` ca mai mulți workeri
să poată trage din coadă în paralel fără să se calce. Claim-ul e tenant-scoped
(conn = tenant_conn): RLS filtrează `outbox` la business-ul curent, deci
dispatcher-ul iterează per tenant (bucla peste tenanți activi = treaba lui,
nu a acestui strat).

`conn` trebuie să fie deja tenant-scoped (tenant_conn).
"""

import json
from typing import Any

import asyncpg

# Backoff exponențial pentru retry-urile dispatcher-ului (secunde), cap la ~5 min.
_BACKOFF_SECONDS = [5, 30, 120, 300]
MAX_ATTEMPTS = 6  # după atâtea eșecuri → 'dead' (nu mai reîncearcă)

# Visibility timeout: la claim, next_attempt_at se împinge cu atât în viitor. Un
# rând rămas 'dispatching' (dispatcher mort între claim și mark) redevine scadent
# după acest interval și e re-revendicat — reaper implicit, fără coloană separată.
_VISIBILITY_TIMEOUT_S = 120

# Calificat cu `o.`/`due.`: în RETURNING-ul unui UPDATE ... FROM, coloanele comune
# (id) ar fi ambigue între tabelul țintă `o` și subquery-ul `due`. channel_kind +
# channel_account_id (canalul EXPEDITOR) vin din join-ul outbox→conversations→channels;
# dispatcher-ul alege transportul după channel_kind (NX-60).
_CLAIM_COLS = (
    "o.id::text, o.conversation_id::text, o.idempotency_key, o.kind, "
    "o.payload, o.attempts, due.channel_kind, due.channel_account_id"
)


def _claimed_row(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else d["payload"]
    return d


async def enqueue_outbox(
    conn: asyncpg.Connection,
    business_id: str,
    conversation_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    kind: str = "message",
) -> str | None:
    """Pune un mesaj în coada de ieșire, idempotent.

    Întoarce id-ul rândului nou, sau `None` dacă `idempotency_key` exista deja
    (turul a fost deja pus în coadă — nu dublăm). A se apela în tranzacția
    Sender-ului, împreună cu patch-ul de state.
    """
    new_id = await conn.fetchval(
        """
        insert into outbox (business_id, conversation_id, idempotency_key, kind, payload)
        values ($1, $2, $3, $4, $5::jsonb)
        on conflict (business_id, idempotency_key) do nothing
        returning id::text
        """,
        business_id,
        conversation_id,
        idempotency_key,
        kind,
        json.dumps(payload),
    )
    return new_id


async def claim_due(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    limit: int = 10,
    visibility_timeout_s: int = _VISIBILITY_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Revendică până la `limit` rânduri scadente și le trece în 'dispatching'.

    `FOR UPDATE SKIP LOCKED` sare peste rândurile pe care alți workeri le țin deja
    → zero dublu-trimitere între dispatcher-i paraleli. La claim, `next_attempt_at`
    e împins cu `visibility_timeout_s` în viitor: rândul nu e re-revendicat imediat,
    iar dacă dispatcher-ul moare înainte de mark_sent/mark_failed, redevine scadent
    după timeout (reaper implicit). `attempts` se incrementează la claim.

    Returnează și `channel_kind` + `channel_account_id` (canalul EXPEDITOR, din
    conversație) — dispatcher-ul alege transportul potrivit după channel_kind.
    """
    rows = await conn.fetch(
        f"""
        update outbox o
           set status = 'dispatching',
               attempts = o.attempts + 1,
               next_attempt_at = now() + make_interval(secs => $3)
          from (
            select o2.id, ch.kind as channel_kind,
                   ch.provider_account_id as channel_account_id
            from outbox o2
            join conversations c on c.id = o2.conversation_id
            join channels ch on ch.id = c.channel_id
            where o2.business_id = $1
              and o2.status in ('pending', 'failed', 'dispatching')
              and o2.next_attempt_at <= now()
            order by o2.next_attempt_at
            for update of o2 skip locked
            limit $2
          ) due
         where o.id = due.id
        returning {_CLAIM_COLS}
        """,
        business_id,
        limit,
        visibility_timeout_s,
    )
    return [_claimed_row(r) for r in rows]


async def business_ids_with_due_outbox(
    conn: asyncpg.Connection,
    *,
    limit: int = 100,
) -> list[str]:
    """Tenanții care au rânduri scadente în outbox — operație de CONTROL PLANE.

    A se rula pe `admin_conn` (cross-tenant): dispatcher-ul nu știe dinainte ce
    tenanți au de trimis, iar a deschide tenant_conn pentru fiecare business activ
    ar fi risipă. Întoarce doar id-urile; trimiterea efectivă se face per tenant,
    sub RLS. (`channels`/`conversations` nu sunt necesare aici — doar business_id.)
    """
    rows = await conn.fetch(
        """
        select distinct business_id::text as business_id
        from outbox
        where status in ('pending', 'failed', 'dispatching')
          and next_attempt_at <= now()
        limit $1
        """,
        limit,
    )
    return [r["business_id"] for r in rows]


async def mark_sent(
    conn: asyncpg.Connection,
    business_id: str,
    outbox_id: str,
    *,
    sent_message_id: str | None = None,
) -> None:
    """Marchează un rând ca trimis cu succes (după ACK de la Meta)."""
    await conn.execute(
        """
        update outbox
           set status = 'sent', last_error = null, sent_message_id = $3
         where business_id = $1 and id = $2
        """,
        business_id,
        outbox_id,
        sent_message_id,
    )


async def mark_failed(
    conn: asyncpg.Connection,
    business_id: str,
    outbox_id: str,
    attempts: int,
    error: str,
) -> str:
    """Marchează un eșec de trimitere și programează următoarea încercare.

    Sub `MAX_ATTEMPTS` → status='failed' cu `next_attempt_at` în viitor (backoff
    exponențial). La epuizarea încercărilor → 'dead' (nu mai reîncearcă; un mesaj
    de client care nu poate fi livrat e vizibil în coadă, nu pierdut tăcut —
    principiul 6). Întoarce statusul rezultat ('failed' | 'dead').
    """
    if attempts >= MAX_ATTEMPTS:
        await conn.execute(
            """
            update outbox set status = 'dead', last_error = $3
             where business_id = $1 and id = $2
            """,
            business_id,
            outbox_id,
            error,
        )
        return "dead"

    delay = _BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
    await conn.execute(
        f"""
        update outbox
           set status = 'failed',
               last_error = $3,
               next_attempt_at = now() + interval '{delay} seconds'
         where business_id = $1 and id = $2
        """,
        business_id,
        outbox_id,
        error,
    )
    return "failed"
