"""NX-233 — sweeper-ul de recovery: turele web abandonate se recuperează din DB, nu din noroc.

Rulează periodic (bounded) și acoperă exact golurile din failure matrix:

  • **wake pierdut / Redis mort** — un turn `accepted` pe care nu l-a trezit nimeni e găsit
    în Postgres și re-notificat (LPUSH best-effort); scanul executorului îl găsește oricum.
  • **worker mort după claim** — lease expirat: dacă turul mai are buget (deadline + attempts),
    rămâne revendicabil (executorul îl reclamă cu epoch nou); dacă NU, sweeperul îl
    TERMINALIZEAZĂ onest: claim (epoch+1, deposedează definitiv orice zombie) → `failed` cu
    error-view randabil (P6 — niciodată un turn agățat pe veci în `running`).
  • **deadline depășit în `accepted`** — nimeni n-a apucat să-l ruleze la timp → `cancelled`
    cu error-view randabil (`accepted → cancelled` nu cere epoch: nu există lease).

`deadline_at` NU se prelungește niciodată aici — e bugetul fixat la accept (NX-241 îl strânge).

Un singur sweeper activ per flotă: advisory lock Postgres (try-lock, fără așteptare) — mai
multe replici pot rula bucla, doar una mătură la un moment dat. Scanul e pe `admin_conn`
(control plane, cross-tenant, non-PII: UUID-uri + timestamps — ca `cleanup_web_turns`);
orice SCRIERE e tenant-scoped (`business_id = $1` prin `tenant_db`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.config import get_settings
from src.db.connection import admin_conn, get_pool
from src.db.provider import tenant_db
from src.db.queries.businesses import load_business
from src.db.queries.web_turns import ClaimableTurn, claimable_turns
from src.web.turn_executor import wake_executor
from src.web.turn_service import cancel_web_turn, claim_web_turn, fail_web_turn

log = logging.getLogger(__name__)

# Cheia advisory lock-ului (arbitrară, stabilă, un singur loc). pg_try_advisory_lock e
# session-scoped: îl ținem DOAR pe durata unui sweep (același checkout), apoi unlock explicit.
_SWEEPER_LOCK_KEY = 0x4E583233  # "NX233"


@dataclass
class SweepReport:
    """Ce a făcut o trecere — pentru log/telemetrie (count-uri, zero PII)."""

    scanned: int = 0
    rewoken: int = 0
    cancelled: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)  # doar coduri, fără id-uri de client


async def _business_locale(business_id: str) -> str:
    try:
        db = tenant_db(business_id)
        async with db("load_business") as conn:
            business = await load_business(conn, business_id)
        return (business.default_locale if business else None) or "ro"
    except Exception:  # noqa: BLE001 — limba nu are voie să blocheze terminalizarea (P6)
        return "ro"


async def _terminalize(turn: ClaimableTurn, *, code: str, report: SweepReport) -> None:
    """Terminal ONEST pentru un turn care nu mai are buget. Toate scrierile sunt CAS —
    dacă între scan și scriere altcineva a terminat/revendicat turul, facem 0 rânduri și
    ne retragem (DB-ul e autoritatea, sweeperul nu forțează nimic)."""
    db = tenant_db(turn.business_id)
    lang = await _business_locale(turn.business_id)
    if turn.status == "accepted":
        # Nimeni nu lucrează (fără lease) → `accepted → cancelled`, cu payload randabil.
        if await cancel_web_turn(db, turn.business_id, turn.turn_id, code=code, language=lang):
            report.cancelled += 1
            report.details.append(f"cancelled:{code}")
        else:
            report.skipped += 1
        return
    # `running` cu lease expirat: claim (epoch+1 — deposedează definitiv zombie-ul), apoi
    # failed cu epoch-ul NOU. Zombie-ul care s-ar trezi după asta scrie pe 0 rânduri.
    settings = get_settings()
    claim = await claim_web_turn(
        db,
        turn.business_id,
        turn.turn_id,
        owner="sweeper",
        lease_ttl_s=settings.web_turn_lease_ttl_s,
    )
    if claim is None:
        report.skipped += 1
        return
    ok = await fail_web_turn(
        db,
        turn.business_id,
        turn.turn_id,
        lease_epoch=claim.lease_epoch,
        code=code,
        language=lang,
    )
    if ok:
        report.failed += 1
        report.details.append(f"failed:{code}")
    else:
        report.skipped += 1


async def sweep_once(redis, *, limit: int | None = None) -> SweepReport:
    """O trecere bounded. Advisory lock: dacă alt sweeper mătură deja, ieșim imediat."""
    settings = get_settings()
    report = SweepReport()
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        got = await conn.fetchval("select pg_try_advisory_lock($1)", _SWEEPER_LOCK_KEY)
        if not got:
            return report
        try:
            turns = await claimable_turns(conn, limit=limit or settings.web_turn_claim_batch * 4)
        finally:
            await conn.execute("select pg_advisory_unlock($1)", _SWEEPER_LOCK_KEY)
    report.scanned = len(turns)
    now = datetime.now(UTC)
    for turn in turns:
        overdue = turn.deadline_at is not None and turn.deadline_at <= now
        # Attempts: `attempt` = câte claim-uri au ars deja. Un `running` expirat cu attempt la
        # plafon a crăpat de N ori — încă o execuție ar fi a N+1-a; terminalizăm.
        exhausted = turn.status == "running" and turn.attempt >= settings.web_turn_max_attempts
        if overdue or exhausted:
            code = "deadline_exceeded" if overdue else "attempts_exhausted"
            try:
                await _terminalize(turn, code=code, report=report)
            except Exception:  # noqa: BLE001 — un turn stricat nu oprește trecerea
                log.exception("sweeper: terminalizare eșuată (web_turn %s)", turn.turn_id)
                report.skipped += 1
            continue
        # Turn sănătos, dar neconsumat (wake pierdut / executori ocupați) → re-notificare.
        await wake_executor(redis, turn.business_id, turn.turn_id)
        report.rewoken += 1
    if report.scanned:
        log.info(
            "sweeper web_turns: scanned=%d rewoken=%d cancelled=%d failed=%d skipped=%d",
            report.scanned,
            report.rewoken,
            report.cancelled,
            report.failed,
            report.skipped,
        )
    return report


async def run_recovery_loop(redis) -> None:
    """Bucla sweeper-ului (rulează până la anulare, în procesul worker)."""
    log.info("web_turn recovery sweeper pornit")
    while True:
        try:
            await sweep_once(redis)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — o trecere eșuată nu omoară bucla
            log.exception("sweeper web_turns: trecere eșuată — reîncerc la următorul interval")
        await asyncio.sleep(get_settings().web_turn_sweep_interval_s)
