"""NX-233 — executorul web asincron serial: claim, lease heartbeat, fencing, finalizare.

Requestul HTTP acceptă durabil (NX-232) și pleacă; AICI se lucrează. Bucla executorului:

    wake (Redis, best-effort) SAU poll → scan DB (autoritatea) → per turn:
    admission → claim CAS (epoch+1) → refs de execuție din DB → pipeline (handle_turn,
    conn-per-op) sub LeaseGuard + deadline → finalizare ÎN tranzacția TurnCommit
    (rezultat + mesaj + stare împreună sau deloc) → aftercare POST-terminal.

Invariantele pe care le impune codul, nu disciplina:

  • UN owner fenced per turn: claim/renew/complete sunt compare-and-set pe
    (business_id, turn_id, status, lease_epoch) — un worker vechi trezit după reclaim face
    UPDATE pe 0 rânduri și își ARUNCĂ rezultatul (`FencedTurnCompletion` → rollback la tot).
  • Redis e scheduling, DB e adevărul: wake-ul pierdut nu pierde turul — scanul (+ sweeperul
    NX-233 din `turn_recovery.py`) îl găsește `accepted` în Postgres.
  • `deadline_at` e fixat la accept și NU se prelungește la reclaim: heartbeat-ul ține lease-ul
    (dovadă de viață), nu bugetul de timp al clientului.
  • Zero conexiune ținută peste pipeline/LLM/backoff: executorul primește PROVIDERUL
    (`tenant_db`), fiecare operație (claim, heartbeat, refs, fail) e checkout scurt etichetat.
  • Niciun terminal mut (P6): eșec/deadline/no-reply → `fail_web_turn` cu error-view RANDABIL;
    `deduped` (claim durabil ținut de un tur crăpat FOARTE recent) e singurul „mai târziu"
    onest — lease-ul expiră (aliniat cu CLAIM_TTL_S) și reclaim-ul rulează ambele.

Shutdown ordonat: `request_stop()` oprește claims noi; turul curent termină sau e anulat de
apelant — un tur anulat rămâne `running` cu lease, îl reclamă alt worker după expiry. Nimic
nu se marchează fals `completed`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from src.channels.web.render import render_web
from src.config import get_settings
from src.db.connection import admin_conn, get_pool
from src.db.provider import DbProvider, tenant_db
from src.db.queries.businesses import load_business
from src.db.queries.web_turns import (
    claimable_turns,
    get_turn_by_id,
    load_execution_refs,
    renew_lease,
)
from src.models import Event
from src.web.turn_events import set_phase
from src.web.turn_service import (
    EmptyTerminalResult,
    FencedTurnCompletion,
    attempt_bucket,
    claim_web_turn,
    complete_web_turn_on_conn,
    fail_web_turn,
)
from src.worker.admission import get_admission
from src.worker.aftercare import persist_events, run_aftercare
from src.worker.processor import handle_turn

log = logging.getLogger(__name__)

# Coada de wake (listă Redis, best-effort). Capată — un backlog uriaș nu are sens: scanul DB
# găsește oricum tot ce e revendicabil; lista doar SCURTEAZĂ așteptarea.
WAKE_KEY = "webturns:wake"
_WAKE_CAP = 1000


async def wake_executor(redis, business_id: str, turn_id: str) -> None:
    """Trezește executorii (best-effort, DUPĂ commitul acceptului — niciodată înainte).
    Eșecul e irelevant pentru corectitudine: DB-ul rămâne autoritatea de recovery."""
    try:
        pipe = redis.pipeline()
        pipe.lpush(WAKE_KEY, f"{business_id}:{turn_id}")
        pipe.ltrim(WAKE_KEY, 0, _WAKE_CAP - 1)
        await pipe.execute()
    except Exception as e:  # noqa: BLE001 — wake-ul pierdut e acoperit de scan/sweeper
        log.warning("web_turn wake eșuat (%s) — sweeperul acoperă", type(e).__name__)


# ── Tipuri (fără raw body, fără token, fără conexiune) ──────────────────────────────────────
@dataclass(frozen=True)
class AcceptedTurn:
    """Referință minimă la un turn de rulat (ce întoarce scanul)."""

    business_id: str
    turn_id: str


@dataclass(frozen=True)
class TurnClaim:
    """Claim-ul reușit al ACESTUI owner: epoch-ul e cheia de fencing a oricărei scrieri."""

    business_id: str
    turn_id: str
    lease_epoch: int
    attempt: int
    reclaimed: bool


@dataclass(frozen=True)
class TerminalCommit:
    """Ce s-a întâmplat cu un turn procesat — pentru log/telemetrie, nu control de flux.
    `outcome`: completed | failed | fenced (rezultat aruncat, alt owner e autoritatea) |
    deferred (lăsat pe loc, îl reia lease expiry/sweeperul)."""

    turn_id: str
    outcome: str
    code: str | None = None


class LeaseGuard:
    """Heartbeat-ul lease-ului: renew periodic cu checkout SCURT; se oprește garantat la stop.

    `lost` se aprinde când renew-ul întoarce 0 rânduri — am fost deposedați (reclaim) sau
    turul nu mai e `running` → apelantul ANULEAZĂ pipeline-ul și aruncă rezultatul local.
    Eșecul de rețea NU e pierdere: reîncercăm la următorul tick, plasa e TTL-ul lease-ului."""

    def __init__(
        self,
        db: DbProvider,
        claim: TurnClaim,
        *,
        owner: str,
        ttl_s: int,
        interval_s: float,
    ) -> None:
        self._db = db
        self._claim = claim
        self._owner = owner
        self._ttl_s = ttl_s
        self._interval_s = interval_s
        self.lost = asyncio.Event()
        self.renewals = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — oprire garantată
                pass
            self._task = None

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                async with self._db("web_turn_heartbeat") as conn:
                    ok = await renew_lease(
                        conn,
                        self._claim.business_id,
                        self._claim.turn_id,
                        owner=self._owner,
                        lease_epoch=self._claim.lease_epoch,
                        lease_ttl_s=self._ttl_s,
                    )
            except Exception as e:  # noqa: BLE001 — tranzient: mai încercăm; TTL-ul e plasa
                log.warning("web_turn heartbeat eșuat (%s) — reîncerc", type(e).__name__)
                continue
            if not ok:
                self.lost.set()
                return
            self.renewals += 1


class WebTurnExecutor:
    """Bucla de execuție. O instanță per proces worker; mai multe procese coexistă — claim-ul
    CAS decide cine ia turul, epoch-ul decide al cui rezultat contează."""

    def __init__(self, redis, *, owner: str | None = None) -> None:
        self._redis = redis
        self.owner = owner or f"exec-{uuid4().hex[:12]}"
        self._stopping = False

    def request_stop(self) -> None:
        """Shutdown ordonat: nu mai revendicăm ture noi; turul curent termină (sau apelantul
        anulează task-ul — lease-ul rămâne și e reclamat de alt worker, nimic fals completed)."""
        self._stopping = True

    async def run(self) -> None:
        log.info("web_turn executor %s pornit", self.owner)
        while not self._stopping:
            try:
                refs = await self._next_refs()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — o iterație stricată nu omoară bucla
                log.exception("web_turn executor: scan eșuat — reîncerc")
                await asyncio.sleep(get_settings().web_turn_executor_poll_s)
                continue
            for ref in refs:
                if self._stopping:
                    break
                try:
                    await self.process_turn(ref)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — turul rămâne pe lease/sweeper (P6)
                    log.exception(
                        "web_turn %s: procesare eșuată — recuperat de lease/sweeper", ref.turn_id
                    )
        log.info("web_turn executor %s oprit", self.owner)

    async def _next_refs(self) -> list[AcceptedTurn]:
        """Așteaptă un wake (Redis, best-effort) cel mult `poll_s`, apoi scanează DB-ul —
        AUTORITATEA. Wake-ul doar scurtează somnul; pierderea lui nu pierde nimic."""
        s = get_settings()
        try:
            await self._redis.brpop(WAKE_KEY, timeout=max(1, int(s.web_turn_executor_poll_s)))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — Redis jos → poll simplu (DB rămâne autoritatea)
            await asyncio.sleep(s.web_turn_executor_poll_s)
        if self._stopping:
            return []
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            rows = await claimable_turns(conn, limit=s.web_turn_claim_batch)
        return [AcceptedTurn(r.business_id, r.turn_id) for r in rows]

    # ── procesarea unui turn ────────────────────────────────────────────────────────────
    async def process_turn(self, ref: AcceptedTurn) -> TerminalCommit | None:
        """Un turn, cap-coadă. None = nu l-am luat noi (admission plină / claim pierdut) —
        rămâne în DB și îl ia altcineva; nu e o eroare."""
        s = get_settings()
        admission = get_admission(self._redis)
        slot = await admission.acquire(ref.business_id, s.admission_acquire_timeout_ms / 1000.0)
        if not slot.admitted:
            # Peste capacitate: turul RĂMÂNE accepted în DB (echivalentul re-queue-ului P6);
            # următorul scan îl reia. Nu ardem un attempt pe o frână de sistem.
            log.info(
                "web_turn %s: admission respins (%s) — reîncercat la următorul scan",
                ref.turn_id,
                slot.reason,
            )
            return None
        try:
            return await self._process_admitted(ref, s)
        finally:
            await admission.release(slot)

    async def _process_admitted(self, ref: AcceptedTurn, s) -> TerminalCommit | None:
        db = tenant_db(ref.business_id)
        claimed = await claim_web_turn(
            db, ref.business_id, ref.turn_id, owner=self.owner, lease_ttl_s=s.web_turn_lease_ttl_s
        )
        if claimed is None:
            return None  # alt worker l-a luat / e terminal — DB-ul a decis
        claim = TurnClaim(
            business_id=ref.business_id,
            turn_id=ref.turn_id,
            lease_epoch=claimed.lease_epoch,
            attempt=claimed.attempt,
            reclaimed=claimed.reclaimed,
        )
        async with db("web_turn_get") as conn:
            row = await get_turn_by_id(conn, ref.business_id, ref.turn_id)
        if row is None:
            return None
        async with db("load_business") as conn:
            business = await load_business(conn, ref.business_id)
        lang = (business.default_locale if business else None) or "ro"
        events = [
            Event(
                "web_turn_claimed",
                {
                    "attempt_bucket": attempt_bucket(claim.attempt),
                    "web_turn_id": row.id,
                    "queue_wait_ms": round(
                        max(0.0, (datetime.now(UTC) - row.accepted_at).total_seconds() * 1000.0)
                    ),
                },
            )
        ]
        if claim.reclaimed:
            events.append(Event("web_turn_reclaimed", {"web_turn_id": row.id}))
        await self._emit(db, row, *events)

        async def _fail(code: str) -> TerminalCommit:
            ok = await fail_web_turn(
                db,
                ref.business_id,
                ref.turn_id,
                lease_epoch=claim.lease_epoch,
                code=code,
                language=lang,
            )
            outcome = "failed" if ok else "fenced"
            await self._emit(
                db,
                row,
                Event(
                    "web_turn_executed", {"outcome": outcome, "code": code, "web_turn_id": row.id}
                ),
            )
            return TerminalCommit(row.id, outcome, code)

        now = datetime.now(UTC)
        if business is None:
            return await _fail("processing_error")
        # Attempts: claim-ul tocmai a incrementat. Peste plafon = crash-loop — terminal ONEST,
        # nu încă o execuție. (`deadline_at` NU se prelungește la reclaim — e cel de la accept.)
        if claim.attempt > s.web_turn_max_attempts:
            return await _fail("attempts_exhausted")
        if row.deadline_at is not None and row.deadline_at <= now:
            return await _fail("deadline_exceeded")
        async with db("web_turn_refs") as conn:
            refs = await load_execution_refs(conn, ref.business_id, ref.turn_id)
        if refs is None or refs.sender_external_id is None or refs.inbound_msg_id is None:
            # Fără identitate/input durabil nu putem re-construi turul — terminal onest (P6).
            log.warning("web_turn %s: refs de execuție incomplete — fail onest", row.id)
            return await _fail("processing_error")

        event = {
            "channel_kind": refs.channel_kind,
            "channel_account_id": refs.channel_account_id,
            "sender_external_id": refs.sender_external_id,
            "provider_msg_id": row.client_turn_id,
            "content_type": refs.content_type,
            "body": refs.safe_body or "",
            # NX-234: contextul de pagină vine din DB (persistat la accept), nu din requestul
            # HTTP care de mult s-a închis. Un reclaim după restart rulează cu ACEEAȘI ancoră.
            **({"page_context": refs.page_context} if refs.page_context else {}),
        }
        persisted: dict = {}

        async def _commit(conn, reply, language):
            view = render_web(reply, language or lang)
            await complete_web_turn_on_conn(
                conn,
                ref.business_id,
                ref.turn_id,
                lease_epoch=claim.lease_epoch,
                view=view,
            )
            persisted["view"] = view

        def _stage_hook(stage_name: str) -> None:
            # Phase de OBSERVABILITATE (efemer, best-effort): `validating` DOAR când un stagiu
            # de validare chiar rulează — zero statusuri cosmetice, zero CoT.
            if "validat" in stage_name:
                asyncio.get_running_loop().create_task(
                    set_phase(
                        self._redis,
                        ref.business_id,
                        ref.turn_id,
                        "validating",
                        ttl_s=s.web_turn_lease_ttl_s,
                    )
                )

        await set_phase(
            self._redis, ref.business_id, ref.turn_id, "working", ttl_s=s.web_turn_lease_ttl_s
        )
        guard = LeaseGuard(
            db,
            claim,
            owner=self.owner,
            ttl_s=s.web_turn_lease_ttl_s,
            interval_s=s.web_turn_heartbeat_s,
        )
        guard.start()
        started = perf_counter()
        deadline_left = (
            (row.deadline_at - now).total_seconds() if row.deadline_at else s.web_turn_deadline_s
        )
        pipeline = asyncio.create_task(
            handle_turn(
                db,
                business,
                refs.channel_id,
                event,
                redis=self._redis,
                deliver=False,
                defer_aftercare=True,
                commit_hook=_commit,
                turn_id=ref.turn_id,
                preinserted_inbound_msg_id=refs.inbound_msg_id,
                stage_hook=_stage_hook,
            )
        )
        lost = asyncio.create_task(guard.lost.wait())
        try:
            try:
                done, _ = await asyncio.wait(
                    {pipeline, lost},
                    timeout=max(0.1, deadline_left),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # Kill/shutdown în plin zbor: anulăm pipeline-ul CURAT (nimic fals completed;
                # lease-ul rămâne și e reclamat de alt worker) și propagăm anularea.
                pipeline.cancel()
                await asyncio.gather(pipeline, return_exceptions=True)
                raise
            if pipeline not in done:
                # Deadline atins SAU lease pierdut cât timp pipeline-ul rula → anulăm curat.
                pipeline.cancel()
                await asyncio.gather(pipeline, return_exceptions=True)
                if lost.done():
                    await self._emit(
                        db,
                        row,
                        Event(
                            "web_turn_fenced_write_rejected",
                            {"phase": "pipeline", "web_turn_id": row.id},
                        ),
                    )
                    return TerminalCommit(row.id, "fenced")
                return await _fail("deadline_exceeded")
            try:
                result = pipeline.result()
            except FencedTurnCompletion:
                # Commit respins de epoch: rollback complet a avut loc, al LUI e autoritatea.
                await self._emit(
                    db,
                    row,
                    Event(
                        "web_turn_fenced_write_rejected",
                        {"phase": "commit", "web_turn_id": row.id},
                    ),
                )
                return TerminalCommit(row.id, "fenced")
            except EmptyTerminalResult:
                return await _fail("empty_result")
            except Exception:  # noqa: BLE001 — orice eșec de pipeline → terminal onest (P6)
                log.exception("web_turn %s: pipeline eșuat", row.id)
                return await _fail("processing_error")
        finally:
            lost.cancel()
            await guard.stop()

        execution_ms = round((perf_counter() - started) * 1000.0)
        if "view" in persisted:
            await self._emit(
                db,
                row,
                Event(
                    "web_turn_executed",
                    {"outcome": "completed", "web_turn_id": row.id, "execution_ms": execution_ms},
                ),
            )
            # Aftercare STRICT post-terminal: rezultatul e deja comis; un eșec aici nu-l atinge.
            if result.aftercare is not None:
                try:
                    await run_aftercare(db, self._redis, result.aftercare)
                except Exception:  # noqa: BLE001 — best-effort prin contract
                    log.exception("web_turn %s: aftercare eșuat (rezultatul rămâne)", row.id)
            return TerminalCommit(row.id, "completed")
        if result.deduped:
            # Claim-ul durabil (inbound_dedupe) e ținut de o execuție crăpată FOARTE recent.
            # Lease-ul e aliniat cu CLAIM_TTL_S: la expirare, reclaim-ul le ia pe amândouă.
            log.info("web_turn %s: dedupe activ — reluat după expirarea lease-ului", row.id)
            await self._emit(db, row, Event("web_turn_deferred_dedupe", {"web_turn_id": row.id}))
            return TerminalCommit(row.id, "deferred")
        if result.reply is not None:
            # Reply comis fără view persistat: hook-ul n-a rulat (n-ar trebui). Zgomotos + onest.
            log.warning("web_turn %s: reply fără view persistat — fail onest", row.id)
        return await _fail("empty_result")

    async def _emit(self, db: DbProvider, row, *events: Event) -> None:
        """Evenimente pe traiectoria conversației (best-effort, P12: doar id-uri + etichete)."""
        await persist_events(
            db,
            row.business_id,
            row.conversation_id,
            row.contact_id,
            list(events),
            operation="web_turn_events",
        )
