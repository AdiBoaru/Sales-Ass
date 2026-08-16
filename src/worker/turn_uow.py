"""NX-231 — Unit of Work al unui tur: `load → compute (fără conexiune) → commit → aftercare`.

Înainte, `handle_turn` primea o conexiune vie și o ținea de la primul query până la ultimul, cu
triajul, agentul, tool-urile și modelul la mijloc. Conexiunea aparținea TURULUI. Sub concurență
poolul se epuiza pe timp de rețea OpenAI, nu pe muncă de DB — iar frâna reală a sistemului
devenea, accidental, `bot_pool.max_size`.

Aici e granița pusă explicit:

  1. **load** — un singur checkout scurt care rezolvă contactul/conversația, revendică dedupe-ul,
     scrie mesajul inbound și citește tot ce-i trebuie turului. Rezultatul e un `TurnLoadSnapshot`
     IMUTABIL: date, nu un handle. Nimeni nu poate „mai trage un query" din el mai târziu.
  2. **compute** — pipeline-ul rulează pe snapshot + provider. Stagiile iau conexiuni scurte
     pentru operațiile lor; între ele (LLM, embed, HTTP) nu se ține NIMIC.
  3. **commit** — un singur checkout + O tranzacție: mesajele outbound, rândurile de outbox,
     versiunea de stare și finalizarea claim-ului de dedupe. Atomic sau deloc.
  4. **aftercare** — primește ID-uri și un snapshot, niciodată o conexiune (vezi `aftercare.py`).

De ce contează separarea la nivel de tip: un `TurnLoadSnapshot` nu poate fi folosit „încă puțin"
peste un await extern, fiindcă nu are ce să folosească. Regula nu depinde de disciplină.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.db.provider import DbProvider
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import (
    StateConflict,
    get_or_create_conversation,
    get_state_and_version,
    patch_conversation_state,
    touch_last_inbound,
)
from src.db.queries.facts import fetch_relevant_facts
from src.db.queries.inbound_dedupe import claim_inbound, mark_inbound_completed
from src.db.queries.messages import get_recent_messages, insert_message
from src.db.queries.outbox import enqueue_outbox
from src.db.queries.summaries import get_summary_for_context
from src.models import Author, BusinessConfig, Contact, Direction, Message
from src.observability import turn_latency

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnLoadSnapshot:
    """Tot ce a citit turul din DB, ca DATE. Fără connection handle, fără lazy loading.

    `deduped=True` e singurul caz în care restul câmpurilor nu contează: mesajul a mai fost
    procesat (retry de provider) și turul se oprește înainte de orice scriere."""

    deduped: bool
    contact: Contact | None = None
    conversation_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    state_version: int = 0
    locale: str | None = None
    bot_active: bool = True
    handoff_until: datetime | None = None
    shadow_mode: bool = False
    history: list[Message] = field(default_factory=list)
    summary: str | None = None
    facts: list[Any] = field(default_factory=list)
    inbound_msg_id: str | None = None


async def load_turn(
    db: DbProvider,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
    *,
    turn_id: str,
    safe_body: str | None,
    identity_external_id: str,
    verified_customer_ref: str | None,
    load_facts: bool,
    preinserted_msg_id: str | None = None,
) -> TurnLoadSnapshot:
    """Faza 1: UN checkout scurt, zero await extern înăuntru.

    Ordinea e cea de dinainte de NX-231, deliberat: claim de dedupe ÎNAINTE de orice scriere
    (un duplicat nu produce mesaj și nici outbox), apoi contact → conversație → mesaj inbound →
    `last_inbound_at`, apoi citirile de context. `get_recent_messages` rămâne DUPĂ insert (istoricul
    include mesajul curent, ca înainte).

    `preinserted_msg_id` (NX-233): mesajul inbound a fost DEJA persistat la acceptul durabil
    (același checkout cu rândul `web_turns`) → nu-l inserăm a doua oară și nu re-atingem
    `last_inbound_at` (acceptul a făcut-o). Claim-ul de dedupe RĂMÂNE: el e single-execution
    per attempt (aliniat cu lease-ul de ledger, CLAIM_TTL_S)."""
    provider_msg_id = event.get("provider_msg_id")
    with turn_latency.span("load"):  # NX-241: faza `load` = ACEST checkout, măsurat unde se face
        return await _load_turn(
            db,
            business,
            channel_id,
            event,
            turn_id=turn_id,
            safe_body=safe_body,
            identity_external_id=identity_external_id,
            verified_customer_ref=verified_customer_ref,
            load_facts=load_facts,
            preinserted_msg_id=preinserted_msg_id,
            provider_msg_id=provider_msg_id,
        )


async def _load_turn(
    db: DbProvider,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
    *,
    turn_id: str,
    safe_body: str | None,
    identity_external_id: str,
    verified_customer_ref: str | None,
    load_facts: bool,
    preinserted_msg_id: str | None,
    provider_msg_id: str | None,
) -> TurnLoadSnapshot:
    async with db("turn_load") as conn:
        # Dedupe layer 2 (durabil): retry de provider care a scăpat de Redis (FLUSHALL/restart).
        # NX-86 claim-or-resume: un crash lasă `completed_at` NULL → orfanul e reclamat după TTL.
        if provider_msg_id and not await claim_inbound(conn, business.id, provider_msg_id):
            return TurnLoadSnapshot(deduped=True)

        contact = await get_or_create_contact(
            conn,
            business.id,
            event.get("channel_kind", "whatsapp"),
            identity_external_id,
            display_name=event.get("sender_name"),
            verified=bool(verified_customer_ref),
        )
        conv = await get_or_create_conversation(
            conn,
            business.id,
            contact.id,
            channel_id,
            locale=business.default_locale,
        )
        if preinserted_msg_id is not None:
            inbound_msg_id = preinserted_msg_id
        else:
            inbound_msg_id = await insert_message(
                conn,
                business.id,
                conv["id"],
                contact.id,
                Direction.INBOUND,
                Author.CONTACT,
                body=safe_body,
                content_type=event.get("content_type", "text"),
                provider_msg_id=provider_msg_id,
                media_ref=event.get("media_id"),
                # NX-146: turn_id în payload → Turn Replay citește EXACT mesajele turului.
                payload={"turn_id": turn_id, "fragment_index": 0},
            )
            await touch_last_inbound(conn, business.id, conv["id"])
        history = await get_recent_messages(conn, business.id, conv["id"])
        summary = await get_summary_for_context(conn, business.id, conv["id"])
        # NX-148: memoria structurată. BEST-EFFORT (ca summary/cache): un fail de citire NU
        # blochează turul — degradare la history+state (P6).
        facts: list[Any] = []
        if load_facts:
            try:
                facts = await fetch_relevant_facts(conn, business.id, contact.id)
            except Exception:  # noqa: BLE001 — memoria e opțională, turul răspunde oricum
                log.exception("încărcarea facts a eșuat (turul continuă)")

    return TurnLoadSnapshot(
        deduped=False,
        contact=contact,
        conversation_id=conv["id"],
        state=conv["state"] or {},
        state_version=conv["state_version"],
        locale=conv["locale"],
        bot_active=conv["bot_active"],
        handoff_until=conv["handoff_until"],
        shadow_mode=bool(conv.get("shadow_mode")),
        history=history,
        summary=summary,
        facts=facts,
        inbound_msg_id=inbound_msg_id,
    )


@dataclass(frozen=True)
class OutboundFragment:
    """Un fragment de răspuns gata de persistat. `outbox_payload=None` = livrare sincronă (răspunsul
    HTTP e transportul) → mesajul se scrie `sent`, fără rând de outbox."""

    body: str
    message_payload: dict[str, Any]
    usage_kwargs: dict[str, Any]
    outbox_payload: dict[str, Any] | None
    idempotency_key: str


@dataclass(frozen=True)
class TurnCommit:
    """Faza 3, descrisă ca date. Tot ce e aici intră într-o SINGURĂ tranzacție."""

    business_id: str
    conversation_id: str
    contact_id: str
    fragments: list[OutboundFragment]
    new_state: dict[str, Any]
    expected_version: int
    provider_msg_id: str | None
    deliver: bool


@dataclass
class CommitResult:
    """Ce s-a întâmplat la commit — pentru log/telemetrie, nu pentru control de flux."""

    first_outbox_id: str | None = None
    state_conflict: str | None = None  # None | "retried" | "dropped"


async def commit_turn(
    db: DbProvider,
    commit: TurnCommit,
    *,
    rebuild_state: Callable[[dict[str, Any]], dict[str, Any]],
    on_commit: Callable[[Any], Any] | None = None,
) -> CommitResult:
    """Faza 3: UN checkout, O tranzacție, zero await extern.

    Atomicitatea e contractul: mesajele outbound, rândurile de outbox, patch-ul de stare și
    `mark_inbound_completed` ori intră toate, ori niciunul. Un crash înainte de COMMIT lasă
    `completed_at` NULL → mesajul e recuperabil; după COMMIT e finalizat și nu se reprocesează.

    `on_commit` (NX-232) = seam-ul prin care MARGINEA atașează o scriere suplimentară la
    ACEEAȘI tranzacție (ex. finalizarea rândului din ledgerul web: rezultat + mesaj + stare se
    văd împreună sau deloc). Primește conexiunea tranzacției; dacă ridică, TOT commit-ul face
    rollback — exact ce cere fencing-ul (un rezultat respins nu lasă mesajul lui în urmă).
    Pipeline-ul rămâne agnostic de canal: seam-ul e generic, conținutul îl decide marginea.

    Singura excepție de la „totul sau nimic" e conflictul de versiune de stare (NX-221): acolo
    pierdem patch-ul de stare, NU răspunsul (P6) — vezi `_patch_state`."""
    result = CommitResult()
    async with db("turn_commit") as conn:
        async with conn.transaction():
            for frag in commit.fragments:
                out_msg_id = await insert_message(
                    conn,
                    commit.business_id,
                    commit.conversation_id,
                    commit.contact_id,
                    Direction.OUTBOUND,
                    Author.BOT,
                    body=frag.body,
                    content_type="text",
                    # Sync: livrarea E răspunsul HTTP → mesajul e deja `sent` (n-are dispatcher
                    # care să-l ducă din `queued`). Async: `queued` până trece dispatcher-ul.
                    status="queued" if commit.deliver else "sent",
                    **frag.usage_kwargs,
                    payload=frag.message_payload,
                )
                if frag.outbox_payload is None:
                    continue
                outbox_id = await enqueue_outbox(
                    conn,
                    commit.business_id,
                    commit.conversation_id,
                    frag.idempotency_key,
                    {**frag.outbox_payload, "message_id": out_msg_id},
                )
                if result.first_outbox_id is None:
                    result.first_outbox_id = outbox_id
            result.state_conflict = await _patch_state(conn, commit, rebuild_state)
            # NX-86: claim finalizat ÎN aceeași TX cu outbox → atomic.
            if commit.provider_msg_id:
                await mark_inbound_completed(conn, commit.business_id, commit.provider_msg_id)
            # NX-232: scrierea marginii (ex. ledgerul web) intră în ACEEAȘI tranzacție.
            if on_commit is not None:
                await on_commit(conn)
    return result


async def _patch_state(
    conn: Any,
    commit: TurnCommit,
    rebuild_state: Callable[[dict[str, Any]], dict[str, Any]],
) -> str | None:
    """NX-221: `StateConflict` nu omoară reply-ul. Conflictul (altă scriere între citirea de la
    începutul turului și patch) → re-citim `(state, state_version)` PROASPETE și re-aplicăm
    deltele turului O SINGURĂ DATĂ, pe starea proaspătă (last-writer-wins per cheie), nu pe
    snapshotul stale. Al doilea eșec → pierdem patch-ul de stare, NU răspunsul (P6): insert-urile
    din aceeași TX rămân comise.

    `StateConflict` e ridicat din Python după un UPDATE care n-a prins niciun rând (nu o eroare de
    DB) → tranzacția rămâne utilizabilă."""
    try:
        await patch_conversation_state(
            conn,
            commit.business_id,
            commit.conversation_id,
            commit.new_state,
            commit.expected_version,
            touch_outbound=True,
        )
        return None
    except StateConflict:
        pass
    fresh = await get_state_and_version(conn, commit.business_id, commit.conversation_id)
    if fresh is not None:
        fresh_state, fresh_version = fresh
        try:
            await patch_conversation_state(
                conn,
                commit.business_id,
                commit.conversation_id,
                rebuild_state(fresh_state),
                fresh_version,
                touch_outbound=True,
            )
            return "retried"
        except StateConflict:
            pass
    log.warning(
        "state conflict: patch de stare pierdut, reply livrat (conv=%s)", commit.conversation_id
    )
    return "dropped"


async def complete_without_reply(
    db: DbProvider, business_id: str, provider_msg_id: str | None
) -> None:
    """Tur DONE fără reply (halt/no-reply): finalizează claim-ul, altfel reaper-ul l-ar reprocesa.
    Checkout scurt propriu — nu există nimic altceva de scris."""
    if not provider_msg_id:
        return
    async with db("turn_complete_no_reply") as conn:
        await mark_inbound_completed(conn, business_id, provider_msg_id)
