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

# Calificat cu `o.`: în RETURNING-ul unui UPDATE ... FROM, `id` ar fi ambiguu
# între tabelul țintă `o` și subquery-ul `due`.
_CLAIM_COLS = (
    "o.id::text, o.conversation_id::text, o.idempotency_key, o.kind, o.payload, o.attempts"
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
) -> list[dict[str, Any]]:
    """Revendică până la `limit` rânduri scadente și le trece în 'dispatching'.

    `FOR UPDATE SKIP LOCKED` sare peste rândurile pe care alți workeri le țin deja
    → zero dublu-trimitere între dispatcher-i paraleli. Rândurile revendicate ies
    din filtrul de scadență (status devine 'dispatching'), deci nu vor fi reluate
    până la mark_failed/reaper. `attempts` se incrementează la claim.
    """
    rows = await conn.fetch(
        f"""
        update outbox o
           set status = 'dispatching', attempts = o.attempts + 1
          from (
            select id
            from outbox
            where business_id = $1
              and status in ('pending', 'failed')
              and next_attempt_at <= now()
            order by next_attempt_at
            for update skip locked
            limit $2
          ) due
         where o.id = due.id
        returning {_CLAIM_COLS}
        """,
        business_id,
        limit,
    )
    return [_claimed_row(r) for r in rows]


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
