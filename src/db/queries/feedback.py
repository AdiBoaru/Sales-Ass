"""NX-246 (felia 2) — stratul SQL al feedbackului. Fiecare query cu `business_id = $1` (P7).

Regulile de business (autorizare, derivarea promptului, politica de revizie) stau în
`src/web/feedback.py`; aici sunt DOAR operațiile pe rând. Separarea e cea din NX-232
(`db/queries/web_turns.py` vs `web/turn_service.py`) și are un motiv practic: idempotența
trebuie să fie o proprietate a SCHEMEI, nu a unei secvențe de apeluri pe care cineva o poate
rescrie greșit. De aceea `upsert_feedback` e UN singur statement cu `ON CONFLICT`:

  • rândul nu există           → INSERT, `revision = 1`;
  • există, ACELAȘI action_id  → nicio schimbare (`revision` NEATINS) — retry de rețea;
  • există, ALT action_id      → UPDATE + `revision + 1` — corecție autorizată.

Ultimele două se disting ÎN SQL, nu în Python: două cereri concurente ar putea altfel să
citească amândouă „nu există" și să scrie amândouă. Aici pierde una pe unique și cade pe ramura
de update, determinist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

log = logging.getLogger(__name__)

_COLS = """
    id, business_id, conversation_id, turn_id, feedback_prompt_id, rating, reason_code,
    taxonomy_version, source, schema_version, release_sha, release_track, pipeline_version,
    last_action_id, revision, created_at, updated_at
"""


@dataclass(frozen=True)
class FeedbackRow:
    """Un vot, ca DATE. Fără text, fără identitate, fără token — vezi migrarea 042."""

    id: str
    business_id: str
    conversation_id: str
    turn_id: str
    feedback_prompt_id: str
    rating: str
    reason_code: str | None
    taxonomy_version: str
    source: str
    schema_version: str
    release_sha: str | None
    release_track: str
    pipeline_version: str | None
    last_action_id: str
    revision: int
    created_at: datetime
    updated_at: datetime


def _to_row(rec: asyncpg.Record | None) -> FeedbackRow | None:
    if rec is None:
        return None
    d = dict(rec)
    for key in ("id", "business_id", "conversation_id", "turn_id"):
        d[key] = str(d[key])
    return FeedbackRow(**d)


async def upsert_feedback(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    conversation_id: str,
    turn_id: str,
    feedback_prompt_id: str,
    rating: str,
    reason_code: str | None,
    taxonomy_version: str,
    schema_version: str,
    release_sha: str | None,
    release_track: str,
    pipeline_version: str | None,
    action_id: str,
    max_revisions: int,
) -> FeedbackRow | None:
    """Scrie sau CORECTEAZĂ votul, într-un singur statement. `None` = plafon de revizii atins.

    `where` de pe `do update` e locul în care trăiesc ambele reguli:
      • `last_action_id is distinct from $12` — un retry identic nu atinge rândul, deci nu
        incrementează `revision` și nu mișcă `updated_at`. Receipt-ul rămâne identic;
      • `revision < $13` — un flip-flop automat (👍👎👍👎…) nu poate scrie la infinit.

    Când `where` respinge update-ul, `RETURNING` nu întoarce nimic; apelantul recitește rândul și
    decide dacă e „același vot" (receipt identic) sau „plafon atins" (refuz onest).
    """
    rec = await conn.fetchrow(
        f"""
        insert into web_feedback (
            business_id, conversation_id, turn_id, feedback_prompt_id, rating, reason_code,
            taxonomy_version, schema_version, release_sha, release_track, pipeline_version,
            last_action_id
        )
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        on conflict (business_id, feedback_prompt_id) do update
           set rating = excluded.rating,
               reason_code = excluded.reason_code,
               last_action_id = excluded.last_action_id,
               revision = web_feedback.revision + 1,
               updated_at = now()
         where web_feedback.last_action_id is distinct from $12
           and web_feedback.revision < $13
        returning {_COLS}
        """,
        business_id,
        conversation_id,
        turn_id,
        feedback_prompt_id,
        rating,
        reason_code,
        taxonomy_version,
        schema_version,
        release_sha,
        release_track,
        pipeline_version,
        action_id,
        max_revisions,
    )
    return _to_row(rec)


async def get_feedback(
    conn: asyncpg.Connection, business_id: str, feedback_prompt_id: str
) -> FeedbackRow | None:
    """Votul curent al unui prompt. `business_id = $1` (P7)."""
    rec = await conn.fetchrow(
        f"select {_COLS} from web_feedback where business_id = $1 and feedback_prompt_id = $2",
        business_id,
        feedback_prompt_id,
    )
    return _to_row(rec)


@dataclass(frozen=True)
class FeedbackTally:
    """Agregat pe (rating, reason, track) — ce citește raportul. Zero rânduri individuale:
    un raport n-are nevoie de voturi, are nevoie de numere."""

    rating: str
    reason_code: str | None
    release_track: str
    n: int


async def tally_feedback(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    window_from: datetime,
    window_to: datetime,
) -> list[FeedbackTally]:
    """Numărătoarea ferestrei, AGREGATĂ ÎN SQL.

    Deliberat nu întoarce rânduri: un raport care aduce voturi individuale în memorie ajunge, mai
    devreme sau mai târziu, să le scrie într-un artefact. Aici nu are ce scrie — nu le vede.
    Fereastra e pe `created_at` (când s-a votat prima dată), nu pe `updated_at`: o corecție de
    mâine nu are voie să mute votul în fereastra de mâine și să dispară din cea de azi.
    """
    recs = await conn.fetch(
        """
        select rating, reason_code, release_track, count(*)::int as n
          from web_feedback
         where business_id = $1 and created_at >= $2 and created_at < $3
         group by rating, reason_code, release_track
         order by rating, reason_code nulls first, release_track
        """,
        business_id,
        window_from,
        window_to,
    )
    return [
        FeedbackTally(
            rating=r["rating"],
            reason_code=r["reason_code"],
            release_track=r["release_track"],
            n=int(r["n"]),
        )
        for r in recs
    ]
