"""NX-236 — emiterea și autorizarea acțiunilor: dovada de emitere, legăturile, consumul one-shot.

Semnătura NU e autorizare. Un token valid criptografic spune doar „bytes-ăștia au ieșit de la
noi"; spune nimic despre CINE îi prezintă, dacă turul care i-a emis mai există, dacă acțiunea a
fost deja folosită sau dacă lumea s-a schimbat între timp. Aici se verifică restul, în ordinea în
care e ieftin să se verifice:

    crypto → audiență/expirare → tenant+sesiune (pseudonime) → turul-sursă (rând de ledger)
    → DOVADA DE EMITERE → disponibilitatea kind-ului → consum one-shot

**Dovada de emitere, fără tabel nou.** `action_id` e DERIVAT (HMAC peste turul-sursă + kind +
argumente canonice), iar planul acțiunilor se persistă în `response_json["actions"]` — adică
ÎN tranzacția terminală a turului-sursă (NX-232 scrie `response_json` sub fencing pe `lease_epoch`,
deci rezultatul și dovada se văd împreună sau deloc). La consum re-derivăm id-urile din planul
persistat: un token al cărui id nu apare acolo nu a fost emis, oricât de valid ar fi sigiliul.
Zero migrare, zero registru paralel în Redis, retenție moștenită de la ledger.

**Consumul one-shot, tot în ledger.** Cheia de consum e `request_fingerprint`-ul turului care
folosește acțiunea: pentru un input de tip acțiune, fingerprint-ul e HMAC peste `action_id` (NU
peste text și NU peste contextul de pagină — altfel același buton apăsat de pe două pagini ar
produce două chei și s-ar putea consuma de două ori). Consecințele cad singure din unicii care
există deja pe `web_turns`:

  • același token + același `client_turn_id` → rândul EXISTĂ (unique idempotency) → replay exact;
  • același token + ALT `client_turn_id` → găsim rândul primului consumator → `already_consumed`;
  • două consumări CONCURENTE → indexul parțial „un singur turn activ per conversație"
    serializează inserțiile: una câștigă, cealaltă recitește și primește `already_consumed`.

**Ce nu face modulul:** nu execută nimic (aia e `src/agent/action_kernel.py`), nu compune text și
nu atinge Redis. Singurele conexiuni pe care le ia sunt checkout-uri SCURTE, fără await extern
înăuntru (NX-231).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from src.config import get_settings
from src.db.provider import DbProvider
from src.db.queries.web_turns import WebTurnRow, find_turn_by_fingerprint, get_turn_by_id
from src.web.action_crypto import (
    ActionKey,
    KeyRing,
    OpenedToken,
    derive_action_id,
    open_token,
    pseudonym,
    seal,
)
from src.web.action_models import (
    EMITTABLE_KINDS,
    MAX_ACTIONS_PER_TURN,
    SINK_TURN,
    ActionCommand,
    ActionEnvelope,
    ActionPlan,
    parse_plans,
    spec_for,
)
from src.web.turn_service import TERMINAL_LEDGER_STATUSES, request_fingerprint, session_ref_hash

log = logging.getLogger(__name__)

# Scope-urile pseudonimelor. Constante, nu litere magice: schimbarea unui scope invalidează toate
# tokenurile emise anterior (pseudonimul se schimbă) — deci trebuie să fie evident ce se atinge.
SCOPE_TENANT = "tenant"
SCOPE_SESSION = "session"
SCOPE_CONVERSATION = "conversation"

# Cheia sub care planul acțiunilor stă în payload-ul terminal persistat. ADITIVĂ: calea v1
# (`/web/chat` → `render_web`) nu o scrie niciodată, deci contractul v1 rămâne byte-identic.
ACTIONS_PAYLOAD_KEY = "actions"


@dataclass(frozen=True)
class IssuedAction:
    """O acțiune emisă: planul + id-ul derivat + tokenul sigilat. Tokenul NU se persistă (se
    re-derivă determinist la fiecare proiecție), deci un DB scurs nu conține butoane valabile."""

    plan: ActionPlan
    action_id: str
    token: str
    expires_at: int


@dataclass(frozen=True)
class AuthorizedAction:
    """Verdictul POZITIV: comanda typed + rândul-sursă + cheia de consum (fingerprint)."""

    command: ActionCommand
    source: WebTurnRow
    fingerprint: str
    key_slot: str  # current | previous (rotație — metrici)
    age_s: float  # vechimea tokenului la consum


@dataclass(frozen=True)
class ActionRejected:
    """Verdictul NEGATIV. `code` e din vocabularul închis (ajunge la client), `reason` e pentru
    metrici (nu pleacă niciodată pe sârmă — ar fi un oracol)."""

    code: str
    reason: str = ""
    kind: str | None = None


AuthorizeOutcome = AuthorizedAction | ActionRejected


# ── Emitere ─────────────────────────────────────────────────────────────────────────────────
def issue_actions(
    row: WebTurnRow,
    plans: tuple[ActionPlan, ...],
    *,
    ring: KeyRing,
    ttl_s: int,
) -> tuple[IssuedAction, ...]:
    """Planurile persistate ale unui turn TERMINAL → acțiuni sigilate, legate de acel turn.

    DETERMINIST prin construcție: `issued_at` e `completed_at`-ul rândului (un fapt persistat, nu
    ceasul de acum), expirarea e `issued_at + ttl`, iar sigiliul e AES-SIV. Două proiecții ale
    aceluiași rând, cu aceeași cheie, produc EXACT aceiași bytes — deci `GET` repetat și SSE
    reconectat nu pot livra două tokenuri diferite pentru același buton.

    Emiterea presupune că rândul e terminal și are `completed_at`: un turn încă în lucru n-a emis
    nimic, iar a semna „ce va afișa" ar fi o promisiune pe care fencing-ul o poate anula."""
    if row.status not in TERMINAL_LEDGER_STATUSES or row.completed_at is None or not plans:
        return ()
    key = ring.current
    issued_at = int(row.completed_at.timestamp())
    expires_at = issued_at + max(1, ttl_s)
    tenant_ref = pseudonym(key, SCOPE_TENANT, row.business_id)
    session_ref = pseudonym(key, SCOPE_SESSION, row.session_ref_hash or "")
    conversation_ref = pseudonym(key, SCOPE_CONVERSATION, row.conversation_id)
    out: list[IssuedAction] = []
    for plan in plans[:MAX_ACTIONS_PER_TURN]:
        spec = spec_for(plan.kind)
        if spec is None or plan.kind not in EMITTABLE_KINDS:
            # Ce nu e în vocabularul de emitere nu se sigilează (pasul 1 din card). NX-237:
            # mutantele au handler la CONSUM, dar emiterea CTA-urilor de coș rămâne a NX-240.
            continue
        args = plan.args.to_canonical()
        action_id = derive_action_id(key, source_turn_id=row.id, kind=plan.kind, args=args)
        envelope = ActionEnvelope(
            version="a1",
            action_id=action_id,
            kind=plan.kind,
            args=plan.args,
            policy=spec.policy,
            audience="web-widget-v2",
            tenant_ref=tenant_ref,
            session_ref=session_ref,
            conversation_ref=conversation_ref,
            source_turn_id=row.id,
            source_revision=max(0, row.conversation_revision_at_accept or 0),
            issued_at=issued_at,
            expires_at=expires_at,
        )
        out.append(
            IssuedAction(
                plan=plan, action_id=action_id, token=seal(key, envelope), expires_at=expires_at
            )
        )
    return tuple(out)


def plans_from_row(row: WebTurnRow) -> tuple[ActionPlan, ...]:
    """Planul persistat al unui rând terminal (dovada de emitere), DEFENSIV."""
    payload = row.response_json if isinstance(row.response_json, dict) else {}
    return parse_plans(payload.get(ACTIONS_PAYLOAD_KEY))


def emitted_ids(row: WebTurnRow, key: ActionKey) -> dict[str, ActionPlan]:
    """`action_id → plan` RE-DERIVAT din planul persistat, cu cheia care a sigilat tokenul.

    Aici e dovada: mulțimea asta e singura sursă de acțiuni legitime pentru turul `row`. Se
    calculează cu cheia din TOKEN (nu cu cea curentă), altfel o rotație ar invalida retroactiv
    dovada pentru tokenuri emise legitim înainte."""
    out: dict[str, ActionPlan] = {}
    for plan in plans_from_row(row):
        action_id = derive_action_id(
            key, source_turn_id=row.id, kind=plan.kind, args=plan.args.to_canonical()
        )
        out[action_id] = plan
    return out


# ── Cheia de consum ─────────────────────────────────────────────────────────────────────────
def action_fingerprint(secret: str, *, business_id: str, channel_token: str, action_id: str) -> str:
    """Fingerprint-ul de request pentru un turn de tip ACȚIUNE = identitatea acțiunii.

    Deliberat FĂRĂ text și FĂRĂ contextul de pagină: pentru un mesaj, două pagini diferite sunt
    două requesturi diferite (NX-234); pentru o acțiune one-shot, „același buton, altă pagină" ar
    fi exact portița prin care s-ar consuma de două ori. Identitatea acțiunii e `action_id`, atât.
    """
    return request_fingerprint(
        secret,
        business_id=business_id,
        channel_token=channel_token,
        text="",
        content_type="action",
        action=action_id,
    )


# ── Verificare (PURĂ, partajată de toate rutele care consumă tokenuri) ──────────────────────
#
# NX-246 a adus a doua rută care consumă tokenuri (feedback). A copia secvența de verificări ar
# fi însemnat două locuri care trebuie să rămână sincronizate la fiecare schimbare de model de
# amenințare — exact patologia pe care NX-230 a consolidat-o (cinci regexuri de telefon, unul
# singur primea fixul). De aceea verificarea e SPARTĂ în două funcții pure, iar fiecare rută își
# face singură partea de DB (deci niciun checkout în plus pe calea fierbinte):
#
#   verify_envelope  — crypto, audiență/expirare, tenant, sesiune, SINK. Fără DB.
#   verify_source    — rândul-sursă: terminal, deținut, aceeași conversație, DOVADA de emitere.


@dataclass(frozen=True)
class VerifiedEnvelope:
    """Tokenul deschis și legat de requestul curent, ÎNAINTE de orice atingere de DB."""

    envelope: ActionEnvelope
    key: ActionKey
    slot: str
    spec: Any  # ActionSpec (import ciclic evitat: `action_models` nu importă serviciul)
    session_hash: str
    age_s: float


def verify_envelope(
    token: str,
    *,
    business_id: str,
    channel_token: str,
    visitor_id: str,
    ring: KeyRing,
    sink: str,
    now: datetime | None = None,
    skew_s: int = 0,
) -> VerifiedEnvelope | ActionRejected:
    """Pașii care NU ating DB-ul, în ordinea în care e ieftin să eșueze.

    `sink` e poarta STRUCTURALĂ: un token de feedback prezentat la `/web/v2/turns` (sau invers)
    e respins aici, nu într-un `if` uitat pe undeva mai jos. Fără ea, un click de „👍" ar porni un
    tur conversațional și ar arde slotul de single-flight al conversației.
    """
    moment = now or datetime.now(UTC)
    opened = open_token(token, ring, now=int(moment.timestamp()), skew_s=skew_s)
    if not isinstance(opened, OpenedToken):
        code = "action_expired" if opened.reason == "expired" else "action_invalid"
        return ActionRejected(code, opened.reason)

    envelope = opened.envelope
    key = opened.key
    spec = spec_for(envelope.kind)
    if spec is None:
        return ActionRejected("action_invalid", "unknown_kind")
    if spec.sink != sink:
        # Cod GENERIC: diferența dintre „nu există" și „e pentru altă rută" n-are voie să se vadă.
        return ActionRejected("action_not_found", "wrong_sink", envelope.kind)

    # Legăturile care nu ating DB-ul: tenant + sesiune. Se recalculează din valorile CURENTE ale
    # requestului — un token mutat pe alt tenant/vizitator nu se potrivește, fără niciun lookup.
    session_hash = session_ref_hash(channel_token, visitor_id)
    if envelope.tenant_ref != pseudonym(key, SCOPE_TENANT, business_id):
        return ActionRejected("action_not_found", "tenant_mismatch", envelope.kind)
    if envelope.session_ref != pseudonym(key, SCOPE_SESSION, session_hash):
        return ActionRejected("action_not_found", "session_mismatch", envelope.kind)
    return VerifiedEnvelope(
        envelope=envelope,
        key=key,
        slot=opened.slot,
        spec=spec,
        session_hash=session_hash,
        age_s=max(0.0, moment.timestamp() - envelope.issued_at),
    )


def verify_source(verified: VerifiedEnvelope, source: WebTurnRow | None) -> ActionRejected | None:
    """Pașii care cer rândul-sursă (deja citit de apelant). `None` = totul e în regulă.

    Ultimul pas e DOVADA de emitere: un sigiliu valid al cărui `action_id` nu se re-derivă din
    planul persistat înseamnă că acțiunea n-a fost niciodată oferită — oricât de corectă ar fi
    criptografia.
    """
    envelope, key = verified.envelope, verified.key
    if source is None:
        # Sursa a dispărut (retenție, GDPR erase) sau n-a existat niciodată pe tenantul ăsta.
        return ActionRejected("action_not_found", "source_missing", envelope.kind)
    if source.status not in TERMINAL_LEDGER_STATUSES:
        return ActionRejected("action_not_found", "source_not_terminal", envelope.kind)
    if source.session_ref_hash != verified.session_hash:
        # Defense-in-depth peste pseudonimul de sesiune: rândul însuși trebuie să fie al sesiunii.
        return ActionRejected("action_not_found", "source_not_owned", envelope.kind)
    if envelope.conversation_ref != pseudonym(key, SCOPE_CONVERSATION, source.conversation_id):
        return ActionRejected("action_not_found", "conversation_mismatch", envelope.kind)
    plan = emitted_ids(source, key).get(envelope.action_id)
    if plan is None or plan.kind != envelope.kind:
        # Sigiliu valid, dar acțiunea nu apare în ViewModel-ul terminal al sursei.
        return ActionRejected("action_not_found", "not_emitted", envelope.kind)
    return None


# ── Autorizare ──────────────────────────────────────────────────────────────────────────────
async def authorize_action(
    db: DbProvider,
    *,
    token: str,
    business_id: str,
    channel_token: str,
    visitor_id: str,
    client_turn_id: str,
    ring: KeyRing,
    fingerprint_secret: str,
    now: datetime | None = None,
    skew_s: int = 0,
) -> AuthorizeOutcome:
    """Token → `ActionCommand` autorizat, sau respingere typed. Zero efecte secundare.

    Ordinea e cea din docstringul modulului. Fiecare pas care eșuează întoarce un cod GENERIC
    către client (`action_invalid` / `action_not_found`) și un `reason` fin doar pentru metrici:
    diferența dintre „tokenul e stricat" și „tokenul e al altui tenant" nu are voie să se vadă din
    afară (failure matrix: „zero existence leak")."""
    verified = verify_envelope(
        token,
        business_id=business_id,
        channel_token=channel_token,
        visitor_id=visitor_id,
        ring=ring,
        sink=SINK_TURN,
        now=now,
        skew_s=skew_s,
    )
    if isinstance(verified, ActionRejected):
        return verified
    envelope, spec = verified.envelope, verified.spec

    async with db("web_action_authorize") as conn:
        source = await get_turn_by_id(conn, business_id, envelope.source_turn_id)
        consumer = None
        fingerprint = action_fingerprint(
            fingerprint_secret,
            business_id=business_id,
            channel_token=channel_token,
            action_id=envelope.action_id,
        )
        if source is not None:
            consumer = await find_turn_by_fingerprint(
                conn, business_id, source.conversation_id, fingerprint
            )

    rejected = verify_source(verified, source)
    if rejected is not None:
        return rejected
    assert source is not None  # `verify_source` a exclus None
    if not spec.available or (spec.mutating and not get_settings().conversation_cart_enabled):
        # NX-237: comerțul are handler (CartService + receipt), dar rămâne refuzat ONEST cât timp
        # serviciul e stins — refuzul e ÎNAINTE de consum, ca one-shot-ul să nu ardă degeaba.
        return ActionRejected("action_unavailable", "no_handler", envelope.kind)
    conflict = consumption_conflict(consumer, client_turn_id)
    if conflict is not None:
        # One-shot: ALT turn a folosit deja acțiunea. Retry-ul ACELUIAȘI `client_turn_id` trece de
        # aici și cade pe idempotency-ul ledgerului (replay exact, fără a doua execuție).
        return replace(conflict, kind=envelope.kind)

    command = ActionCommand(
        action_id=envelope.action_id,
        kind=envelope.kind,
        args=envelope.args,
        policy=envelope.policy,
        source_turn_id=source.id,
        source_revision=envelope.source_revision,
        conversation_id=source.conversation_id,
        option_text=_option_text(source, envelope),
    )
    return AuthorizedAction(
        command=command,
        source=source,
        fingerprint=fingerprint,
        key_slot=verified.slot,
        age_s=verified.age_s,
    )


def consumption_conflict(consumer: WebTurnRow | None, client_turn_id: str) -> ActionRejected | None:
    """Un rând care ține deja cheia de consum: e retry-ul ACELUIAȘI turn sau altcineva?

    Pur, ca ordinea verificărilor să fie testabilă. `None` = drum liber (turul curent poate
    consuma); `ActionRejected` = one-shot deja folosit de alt turn."""
    if consumer is None or consumer.client_turn_id == client_turn_id:
        return None
    return ActionRejected("action_already_consumed", "other_turn")


async def find_consumer(
    db: DbProvider, *, business_id: str, conversation_id: str, fingerprint: str
) -> WebTurnRow | None:
    """Rândul care a consumat deja acțiunea (dacă există). Checkout SCURT propriu — folosit pe
    drumul de arbitraj, după ce inserția a pierdut cursa de single-flight."""
    async with db("web_action_consumer") as conn:
        return await find_turn_by_fingerprint(conn, business_id, conversation_id, fingerprint)


def _option_text(source: WebTurnRow, envelope: ActionEnvelope) -> str | None:
    """Textul opțiunii alese, citit din ViewModel-ul PERSISTAT al turului-sursă.

    Browserul trimite un ordinal opac (`option_ref`), nu textul: eticheta rămâne display-only, iar
    ce a însemnat ea e treaba serverului, din propriul lui rând de ledger. Dacă lista nu mai are
    poziția cerută, întoarcem None — apelantul tratează cazul ca `stale`, nu alege altă opțiune."""
    if envelope.kind != "answer_clarification" or envelope.args.option_ref is None:
        return None
    payload = source.response_json if isinstance(source.response_json, dict) else {}
    options = payload.get("suggestions")
    if not isinstance(options, list) or envelope.args.option_ref >= len(options):
        return None
    value = options[envelope.args.option_ref]
    return str(value)[:200] if isinstance(value, str) and value.strip() else None


def merge_actions_into_view(view: dict[str, Any], plans: tuple[ActionPlan, ...]) -> dict[str, Any]:
    """Adaugă planul acțiunilor în payload-ul terminal, ÎNAINTE de persistare.

    Aditiv și numai pe calea v2: `render_web` (v1) rămâne neatins, iar un rând scris fără cheia
    asta pur și simplu nu emite butoane (comportamentul de dinainte de card). Ce se persistă e
    PLANUL, nu tokenul — dovada trebuie să supraviețuiască rotației de chei."""
    if not plans:
        return view
    view[ACTIONS_PAYLOAD_KEY] = [p.to_jsonb() for p in plans[:MAX_ACTIONS_PER_TURN]]
    return view


__all__ = [
    "ACTIONS_PAYLOAD_KEY",
    "SCOPE_CONVERSATION",
    "SCOPE_SESSION",
    "SCOPE_TENANT",
    "ActionRejected",
    "AuthorizeOutcome",
    "AuthorizedAction",
    "IssuedAction",
    "action_fingerprint",
    "authorize_action",
    "consumption_conflict",
    "emitted_ids",
    "find_consumer",
    "issue_actions",
    "merge_actions_into_view",
    "plans_from_row",
]
