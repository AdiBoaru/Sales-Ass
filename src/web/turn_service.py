"""NX-232 — serviciul ledgerului de ture web: idempotency, replay exact, terminal non-empty.

Un turn web acceptat are un ID stabil (`client_turn_id`, generat și persistat de frontend) și
un rând durabil în `web_turns`. Pierderea răspunsului HTTP, refreshul, retry-ul, restartul și
două taburi întorc ACELAȘI rezultat terminal fără un al doilea apel LLM și fără o a doua
mutație. Frontendul NU decide dacă un retry e un turn nou și NU reconstruiește un răspuns
lipsă — backendul returnează exact același ViewModel sau statusul canonic.

Aici stau regulile de business; SQL-ul pur e în `src/db/queries/web_turns.py`:

  • STATE MACHINE — harta normativă a tranzițiilor (importată de teste; forma din
    docs/040_web_turns.sql e aceeași);
  • FINGERPRINT — HMAC peste inputul canonic safe + ID-urile de context relevante.
    Body-ul NU se stochează niciodată: fingerprint-ul e comparabil, nu inversabil;
  • ACCEPT — insert-or-inspect fără race: același ID + același request → același rând;
    același ID + ALT request → `IdempotencyConflict` (409); alt turn activ → `ActiveTurn`;
  • PROIECȚIA PE WIRE — enumul intern (`accepted|running|completed|failed|cancelled`)
    rămâne DISTINCT de statusul de contract NX-228 (`working|validating` etc.).
    `running` NU apare niciodată pe sârmă; `validating` e o proiecție server-owned a unui
    phase REAL, nu o stare DB și nu un timer cosmetic;
  • AUTORIZARE — orice read trece prin contactul rezolvat din sesiunea verificată
    (NX-229): alt tenant/contact primește None (404 fără existence leak).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.db.provider import DbProvider
from src.db.queries.contacts import get_or_create_contact
from src.db.queries.conversations import get_or_create_conversation, touch_last_inbound
from src.db.queries.messages import insert_message
from src.db.queries.release import latest_capture
from src.db.queries.web_turns import (
    ClaimResult,
    WebTurnRow,
    cancel_turn,
    claim_turn,
    complete_turn,
    fail_turn,
    get_active_turn,
    get_turn,
    get_turn_by_id,
    insert_turn,
)
from src.models import Author, Direction

log = logging.getLogger(__name__)

# ── State machine (normativă — docs/040_web_turns.sql are aceeași hartă) ────────────────────
LEDGER_STATUSES: frozenset[str] = frozenset(
    {"accepted", "running", "completed", "failed", "cancelled"}
)
TERMINAL_LEDGER_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# (de_la, la). `running → running` = renew (același epoch) SAU reclaim (epoch+1, lease expirat).
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("accepted", "running"),  # claim
        ("running", "running"),  # renew / reclaim
        ("running", "completed"),  # complete (CAS + epoch)
        ("running", "failed"),  # fail (CAS + epoch)
        ("accepted", "cancelled"),
        ("running", "cancelled"),
    }
)


def can_transition(from_status: str, to_status: str) -> bool:
    """Tranziție permisă? Terminalele sunt FINALE; `accepted → completed|failed` e interzis
    (fără claim nu există single-flight); nimic nu se „dez-revendică" înapoi în accepted."""
    if from_status not in LEDGER_STATUSES or to_status not in LEDGER_STATUSES:
        return False
    return (from_status, to_status) in ALLOWED_TRANSITIONS


# ── Proiecția ledger intern → status wire v2 (NX-228) ───────────────────────────────────────
# Ledgerul NU adaugă `working`/`validating` ca stări DB; contractul NU vede `running`.
_WIRE_PASSTHROUGH: frozenset[str] = frozenset({"accepted", "completed", "failed", "cancelled"})
VALIDATING_PHASE = "validating"


def project_wire_status(ledger_status: str, phase: str | None = None) -> str:
    """Statusul de contract pentru un status intern + phase-ul server-owned curent.

    `running` → `working` (execuție normală SAU phase indisponibil după recovery);
    `running` + phase `validating` REAL → `validating`. Orice alt status trece neschimbat.
    Frontendul nu deduce phase-ul din timp/text — mappingul de aici e singurul owner."""
    if ledger_status in _WIRE_PASSTHROUGH:
        return ledger_status
    if ledger_status == "running":
        return "validating" if phase == VALIDATING_PHASE else "working"
    raise ValueError(f"status de ledger necunoscut: {ledger_status!r}")


# ── Fingerprint (HMAC canonic, fără body stocat) ────────────────────────────────────────────
FINGERPRINT_VERSION = 1
# Contractul payload-ului persistat pe calea sincronă /web/chat (render_web) — NX-233
# proiectează de aici către web-view.v2.
RESPONSE_CONTRACT_SYNC_V1 = "web-chat.v1"


def request_fingerprint(
    secret: str,
    *,
    business_id: str,
    channel_token: str,
    text: str,
    content_type: str = "text",
    context: dict[str, Any] | None = None,
    action: str | None = None,
) -> str:
    """HMAC-SHA256 peste inputul CANONIC + ID-urile de context relevante.

    Body-ul intră în HMAC dar nu se stochează nicăieri — un DB scurs nu îl poate inversa,
    iar cu `secret` setat (WEB_TURN_FINGERPRINT_SECRET) nu poate nici confirma ghiciri pe
    mesaje scurte. `id_token`/semnăturile NU intră: rotația unui token nu transformă
    ACELAȘI turn logic într-un conflict fals.

    NX-234: `context` = forma canonică SAFE a contextului de pagină (`web.context
    .fingerprint_context` — doar suprafață + ID-uri opace). Intră fiindcă SCHIMBĂ înțelesul
    turului: „ce părere ai despre acesta?" pe două pagini diferite sunt două requesturi diferite,
    iar refolosirea aceluiași `client_turn_id` după navigare TREBUIE să fie 409, nu un turn vechi
    cu context nou lipit pe el. Absent/gol → cheia nu apare deloc în forma canonică, deci
    fingerprint-urile de dinainte de card rămân BYTE-IDENTICE.

    NX-236: `action` = `action_id`-ul unei acțiuni opace. Când e prezent, fingerprint-ul devine
    CHEIA DE CONSUM one-shot: același buton apăsat de două ori produce aceeași cheie, iar ledgerul
    arbitrează (`find_turn_by_fingerprint`). De aceea, pe drumul de acțiune, apelantul NU trimite
    nici text, nici context — „același buton, altă pagină" trebuie să rămână ACELAȘI consum."""
    payload: dict[str, Any] = {
        "v": FINGERPRINT_VERSION,
        "business_id": business_id,
        "channel_token": channel_token,
        "content_type": content_type,
        "text": text,
    }
    if context:
        payload["ctx"] = context
    if action:
        payload["act"] = action
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def session_ref_hash(token: str, sender_ref: str) -> str:
    """Referință de corelare NE-inversabilă (P12: visitor_id-ul brut nu atinge ledgerul)."""
    return hashlib.sha256(f"{token}:{sender_ref}".encode()).hexdigest()[:32]


# ── Rezultatele acceptului (contract intern, tipizat) ───────────────────────────────────────
@dataclass(frozen=True)
class Accepted:
    """Rând NOU: turul e al nostru de revendicat și rulat. `inbound_msg_id` (NX-233) = mesajul
    inbound persistat în același checkout, când acceptul e pe calea async (`persist_inbound`)."""

    row: WebTurnRow
    inbound_msg_id: str | None = None
    #: NX-249 — asignarea care tocmai a fost capturată pe rând. Marginea o folosește DOAR ca să
    #: emită metrica/evenimentul: autoritatea rămâne coloana din ledger, nu obiectul ăsta.
    assignment: Any | None = None


@dataclass(frozen=True)
class ExistingInProgress:
    """Același ID, procesare activă (alt tab / request paralel / crash nerecuperat încă).
    Răspunsul canonic e statusul + turn ID, nu un payload gol."""

    row: WebTurnRow


@dataclass(frozen=True)
class ExistingCompleted:
    """Același ID, rezultat terminal persistat → REPLAY exact (`row.response_json`),
    fără al doilea apel LLM și fără a doua mutație."""

    row: WebTurnRow


@dataclass(frozen=True)
class IdempotencyConflict:
    """Același ID cu ALT request fingerprint: clientul a refolosit ID-ul pentru alt input.
    409 — nu ghicim care dintre cele două requesturi e „cel adevărat"."""

    row: WebTurnRow


@dataclass(frozen=True)
class ReleaseDrained:
    """NX-249: conversația e DRENATĂ — a fost servită de candidate, kill-switchul e activ, iar
    compatibilitatea de rollback nu e dovedită.

    Nu acceptăm turul: a-l muta pe control i-ar schimba înțelesul (starea, referințele ordinale și
    acțiunile ei vin din candidate), iar a-l rula pe candidate ar încălca chiar kill-switchul.
    Singurul răspuns onest e un error-view care spune că e temporar. Turele deja acceptate nu sunt
    atinse de nimic din toate astea: ele se drenează normal, pe versiunea capturată."""

    assignment: Any


@dataclass(frozen=True)
class ActiveTurnConflict:
    """ALT turn e activ pe conversație (single-flight). Policy explicit: respingem, nu punem
    la coadă. `active` (NX-233) = rândul turnului activ, ca 409-ul `conversation_turn_in_progress`
    să poată indica AUTORIZAT ce așteaptă clientul (referință, nu pornire); None pe drumurile
    vechi care nu-l re-citesc."""

    active_client_turn_id: str | None = None
    active: WebTurnRow | None = None


AcceptOutcome = (
    Accepted
    | ExistingInProgress
    | ExistingCompleted
    | IdempotencyConflict
    | ActiveTurnConflict
    | ReleaseDrained
)


class FencedTurnCompletion(Exception):
    """Completarea a fost respinsă de fencing (alt owner a reclamat lease-ul). Rezultatul
    curent TREBUIE aruncat — ridicată din tranzacția `TurnCommit`, deci rollback la tot."""


class EmptyTerminalResult(Exception):
    """P6: un terminal fără conținut randabil nu se comite. Se persistă un error-view safe."""


def renderable(view: dict[str, Any] | None) -> bool:
    """Payload-ul v1 (`render_web`) are ceva de afișat? P6 la nivel de conținut, nu doar
    de NE-NULL (CHECK-ul din DB prinde NULL; asta prinde `{"content": "", ...}`)."""
    if not view:
        return False
    return bool(
        view.get("content") or view.get("products") or view.get("comparison") or view.get("offer")
    )


# Error-view terminal SAFE (P6): cod stabil + text randabil, localizat pe limba turului.
# Fallback pe limba pilotului doar când limba cerută nu există (nucleu locale-aware, D3).
_ERROR_TEXTS: dict[str, dict[str, str]] = {
    "ro": {
        "empty_result": "Nu am reușit să pregătesc un răspuns acum. Te rog să încerci din nou.",
        "processing_error": "A apărut o problemă la procesare. Te rog să încerci din nou.",
        "cancelled": "Cererea a fost anulată.",
        "deadline_exceeded": "Pregătirea răspunsului a durat prea mult. Te rog să încerci din nou.",
        "attempts_exhausted": "Nu am reușit să procesez cererea. Te rog să încerci din nou.",
        "release_draining": "Facem o actualizare chiar acum. Revino în câteva minute, te rog.",
    },
    "en": {
        "empty_result": "I could not prepare a reply right now. Please try again.",
        "processing_error": "Something went wrong while processing. Please try again.",
        "cancelled": "The request was cancelled.",
        "deadline_exceeded": "Preparing the reply took too long. Please try again.",
        "attempts_exhausted": "I could not process the request. Please try again.",
        "release_draining": "We are updating right now. Please come back in a few minutes.",
    },
    "hu": {
        "empty_result": "Most nem sikerült választ készítenem. Kérlek, próbáld újra.",
        "processing_error": "Hiba történt a feldolgozás során. Kérlek, próbáld újra.",
        "cancelled": "A kérés meg lett szakítva.",
        "deadline_exceeded": "A válasz elkészítése túl sokáig tartott. Kérlek, próbáld újra.",
        "attempts_exhausted": "Nem sikerült feldolgoznom a kérést. Kérlek, próbáld újra.",
        "release_draining": "Éppen frissítünk. Kérlek, térj vissza néhány perc múlva.",
    },
}


def error_view(code: str, language: str) -> dict[str, Any]:
    """Un payload de contract v1 RANDABIL pentru un terminal de eșec — niciodată gol,
    niciodată mesajul brut al excepției (safe: codul e stabil, textul e al nostru)."""
    texts = _ERROR_TEXTS.get(language) or _ERROR_TEXTS["ro"]
    text = texts.get(code) or texts["processing_error"]
    return {"content": text, "products": [], "suggestions": [], "error_code": code}


# ── Accept (insert-or-inspect, fără race) ───────────────────────────────────────────────────
async def accept_web_turn(
    db: DbProvider,
    *,
    business_id: str,
    channel_id: str,
    channel_kind: str,
    channel_token: str,
    sender_external_id: str,
    client_turn_id: str,
    fingerprint: str,
    verified: bool = False,
    display_name: str | None = None,
    locale: str | None = None,
    deadline_at: datetime | None = None,
    session_ref: str | None = None,
    persist_inbound: bool = False,
    safe_body: str | None = None,
    content_type: str = "text",
    page_context: dict[str, Any] | None = None,
    action_payload: dict[str, Any] | None = None,
    release: Any | None = None,
) -> AcceptOutcome:
    """Acceptul durabil al unui turn: UN checkout scurt care rezolvă contact + conversație
    (get_or_create, idempotent — aceleași rânduri pe care le va găsi și `load_turn`) și
    inserează rândul de ledger.

    INSERT eșuat pe unique ⇒ un rând comis există DEJA: re-citim și decidem —
    fingerprint diferit → conflict; terminal → replay; altfel → in-progress. Dacă rândul
    cu ID-ul nostru NU există, unique-ul lovit a fost single-flight-ul → alt turn activ
    (cu referința AUTORIZATĂ la turnul activ al ACESTEI conversații — NX-233).

    NX-233 (`persist_inbound=True`, calea async): mesajul inbound SAFE (deja trecut prin
    frontiera de privacy NX-230) se scrie ÎN ACELAȘI checkout cu rândul de ledger — recovery-ul
    după wake pierdut / restart / Redis mort re-construiește turul EXCLUSIV din DB. Executorul
    îl citește prin `load_execution_refs`; `load_turn` primește id-ul și NU îl mai inserează.
    `deadline_at` se fixează AICI și nu se prelungește niciodată la reclaim.

    NX-234 (`page_context`): contextul de pagină NORMALIZAT (suprafață + ID-uri opace, zero fapte)
    se persistă pe ACELAȘI rând de mesaj inbound. Nu e o comoditate, e cerința de recovery: un
    context care ar trăi doar în requestul HTTP ar dispărea la restart, iar executorul ar rula
    ACELAȘI turn cu altă ancoră. Ledgerul nu capătă coloană nouă (zero migrare) — inputul turului
    e mesajul, iar contextul e parte din input.

    NX-236 (`action_payload`): comanda TYPED deja autorizată (kind + argumente canonice + turul
    sursă), pe același rând de mesaj. Nu se re-verifică tokenul la execuție — el a fost consumat
    la accept, iar re-verificarea după o rotație de chei ar putea eșua pe un turn deja acceptat.
    Ce se persistă e VERDICTUL, nu tokenul (P12: niciun token pe disc).

    NX-249 (`release`): un `ReleaseContext` (sau `None` = controller stins → byte-identic).
    Asignarea se rezolvă AICI, nu la margine, dintr-un motiv structural: bucketul are nevoie de
    `conversation_id`, iar conversația se rezolvă în ACEST checkout. Sticky-ul (`latest_capture`)
    se citește pe aceeași conexiune, deci decizia și captura ei sunt în aceeași tranzacție cu
    rândul de ledger — nu există fereastră în care un turn să existe fără asignare."""
    async with db("web_turn_accept") as conn:
        contact = await get_or_create_contact(
            conn,
            business_id,
            channel_kind,
            sender_external_id,
            display_name=display_name,
            verified=verified,
        )
        conv = await get_or_create_conversation(
            conn, business_id, contact.id, channel_id, locale=locale
        )
        assignment = None
        if release is not None:
            prior = await latest_capture(conn, business_id, conv["id"])
            assignment = release.decide(business_id, conv["id"], prior)
            if assignment.is_drain:
                # ÎNAINTE de orice scriere: un turn drenat nu lasă rând de ledger, deci nu poate
                # fi confundat mai târziu cu un accept care „s-a pierdut".
                return ReleaseDrained(assignment)
        row = await insert_turn(
            conn,
            business_id,
            conv["id"],
            contact.id,
            client_turn_id,
            fingerprint,
            session_ref_hash=session_ref or session_ref_hash(channel_token, sender_external_id),
            conversation_revision=conv["state_version"],
            pipeline_version=RESPONSE_CONTRACT_SYNC_V1,
            deadline_at=deadline_at,
            release_track=assignment.track if assignment else None,
            release_policy_id=(assignment.policy_id or None) if assignment else None,
            release_policy_revision=assignment.policy_revision if assignment else None,
        )
        if row is not None:
            inbound_msg_id = None
            if persist_inbound:
                inbound_msg_id = await insert_message(
                    conn,
                    business_id,
                    conv["id"],
                    contact.id,
                    Direction.INBOUND,
                    Author.CONTACT,
                    body=safe_body,
                    content_type=content_type,
                    provider_msg_id=client_turn_id,
                    payload={
                        "turn_id": row.id,
                        "fragment_index": 0,
                        "web_turn_id": row.id,
                        **({"context": page_context} if page_context else {}),
                        **({"action": action_payload} if action_payload else {}),
                    },
                )
                await touch_last_inbound(conn, business_id, conv["id"])
            return Accepted(row, inbound_msg_id=inbound_msg_id, assignment=assignment)
        existing = await get_turn(conn, business_id, conv["id"], client_turn_id)
        if existing is None:
            active = await get_active_turn(conn, business_id, conv["id"])
            return ActiveTurnConflict(
                active_client_turn_id=active.client_turn_id if active else None,
                active=active,
            )
    return inspect_existing(existing, fingerprint)


def inspect_existing(existing: WebTurnRow, fingerprint: str) -> AcceptOutcome:
    """Decizia pe un rând deja comis — pură, ca ordinea verificărilor să fie testabilă:
    ÎNTÂI conflictul de fingerprint (același ID nu are voie să însemne două requesturi),
    ABIA APOI replay/status."""
    if existing.request_fingerprint != fingerprint:
        return IdempotencyConflict(existing)
    if existing.status in TERMINAL_LEDGER_STATUSES:
        return ExistingCompleted(existing)
    return ExistingInProgress(existing)


# ── Claim / terminal (folosite de calea sincronă; seams pt executorul NX-233) ───────────────
async def claim_web_turn(
    db: DbProvider, business_id: str, turn_id: str, *, owner: str, lease_ttl_s: int
) -> ClaimResult | None:
    async with db("web_turn_claim") as conn:
        return await claim_turn(conn, business_id, turn_id, owner=owner, lease_ttl_s=lease_ttl_s)


async def complete_web_turn_on_conn(
    conn: Any,
    business_id: str,
    turn_id: str,
    *,
    lease_epoch: int,
    view: dict[str, Any],
) -> None:
    """Finalizarea DIN tranzacția `TurnCommit` (mesaj + stare + rezultat împreună sau deloc).

    P6 înainte de scriere: un view ne-randabil NU se comite drept succes. Fencing după:
    0 rânduri = alt owner a reclamat → `FencedTurnCompletion` face rollback la TOT
    (mesajul nostru nu rămâne scris lângă rezultatul altcuiva)."""
    if not renderable(view):
        raise EmptyTerminalResult("terminal fără conținut randabil (P6)")
    ok = await complete_turn(
        conn, business_id, turn_id, lease_epoch=lease_epoch, response_json=view
    )
    if not ok:
        raise FencedTurnCompletion(f"web_turn {turn_id}: completare respinsă de fencing")


async def fail_web_turn(
    db: DbProvider,
    business_id: str,
    turn_id: str,
    *,
    lease_epoch: int,
    code: str,
    language: str,
) -> bool:
    """Terminal de eșec cu error-view safe (checkout scurt propriu — nu există commit de
    reply cu care să fie atomic). False = fenced (alt owner e autoritatea)."""
    async with db("web_turn_fail") as conn:
        return await fail_turn(
            conn,
            business_id,
            turn_id,
            lease_epoch=lease_epoch,
            error_view=error_view(code, language),
            safe_error_code=code,
        )


# ── Read autorizat (NX-229: tenant + contact pe TOATE reads) ────────────────────────────────
async def get_turn_authorized(
    db: DbProvider,
    *,
    business_id: str,
    channel_kind: str,
    sender_external_id: str,
    conversation_id: str,
    client_turn_id: str,
) -> WebTurnRow | None:
    """Rândul unui turn, DOAR dacă aparține contactului rezolvat din sesiunea verificată.

    `business_id` vine din sesiune (server-owned, P7); contactul se rezolvă din identitatea
    de canal — un alt tenant sau un alt vizitator primește None, indistinct de „nu există"
    (fără existence leak)."""
    async with db("web_turn_get") as conn:
        contact = await get_or_create_contact(conn, business_id, channel_kind, sender_external_id)
        row = await get_turn(conn, business_id, conversation_id, client_turn_id)
    if row is None or row.contact_id != contact.id:
        return None
    return row


async def get_turn_for_session(
    db: DbProvider,
    *,
    business_id: str,
    turn_id: str,
    channel_token: str,
    visitor_id: str,
) -> WebTurnRow | None:
    """Rândul unui turn după `turn_id` (id-ul de server), DOAR pentru sesiunea care l-a creat.

    Autorizarea (NX-233, GET/SSE): tenant din sesiunea verificată (server-owned, P7) +
    `session_ref_hash` — acceptul v2 îl calculează din (token, visitor_id), deci EXACT sesiunea
    de browser care a pornit turnul îl poate citi. Alt tenant, alt vizitator sau un id inexistent
    → None, indistinct (fără existence leak). Niciun PII nu intră în comparație: hash-ul e
    ne-inversabil, calculat pe loc din aceleași intrări."""
    async with db("web_turn_get") as conn:
        row = await get_turn_by_id(conn, business_id, turn_id)
    if row is None or row.session_ref_hash != session_ref_hash(channel_token, visitor_id):
        return None
    return row


async def cancel_web_turn(
    db: DbProvider,
    business_id: str,
    turn_id: str,
    *,
    code: str,
    language: str,
    lease_epoch: int | None = None,
) -> bool:
    """`accepted|running → cancelled` cu error-view RANDABIL (P6: și cancelled se afișează).
    Folosit de sweeper pe `accepted` peste deadline (fără lease → fără epoch). False = rândul
    nu mai era anulabil (alt drum l-a terminat/revendicat — DB-ul e autoritatea)."""
    async with db("web_turn_cancel") as conn:
        return await cancel_turn(
            conn,
            business_id,
            turn_id,
            error_view=error_view(code, language),
            safe_error_code=code,
            lease_epoch=lease_epoch,
        )


def attempt_bucket(attempt: int) -> str:
    """Eticheta low-cardinality pentru metrici (P12: fără UUID-uri în labels)."""
    if attempt <= 1:
        return "1"
    if attempt == 2:
        return "2"
    return "3+"
