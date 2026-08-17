"""NX-246 (felia 2) — feedback one-tap: server-owned, autorizat, idempotent, fără text liber.

Un „👍" pare banal până când te întrebi ce anume ar putea minți în el. Răspunsul e: aproape tot.
Un client poate trimite `{"rating": "positive"}` pentru turul altcuiva, de o mie de ori, cu un
`reason` inventat, dintr-o altă sesiune, pentru un turn care nu i-a cerut niciodată părerea. Dacă
oricare dintre astea reușește, semnalul pe care se ia decizia de rollout devine zgomot — și e cel
mai rău fel de zgomot, fiindcă arată exact ca date.

De aceea aici nu există „endpoint care primește un rating":

  • **Ratingul e în KIND, iar kind-ul e SIGILAT.** Browserul primește două tokenuri opace
    (`feedback_up` / `feedback_down`) și le retrimite neschimbate. Nu poate spune „positive" —
    poate doar prezenta ceva ce serverul a emis deja. Reason-ul, la fel: vocabular închis,
    validat la parse, prezent doar dacă serverul l-a pus în plan.
  • **Dovada de emitere e a NX-236.** `action_id` se re-derivă din planul persistat în
    `web_turns.response_json["actions"]`. Un token cu sigiliu perfect al cărui id nu apare acolo
    n-a fost niciodată oferit — deci nu poate vota.
  • **Legăturile sunt criptografice, nu declarate.** Tenant, sesiune, conversație și turul-sursă
    intră în plic ca pseudonime; un token mutat pe alt vizitator nu se potrivește, fără lookup.
  • **Unicitatea trăiește în DB**, nu într-o secvență de apeluri: un singur rând per prompt
    (unique), retry identic = același receipt, corecție = `revision + 1`, plafonat.

**`feedback_prompt_id` e DERIVAT, nu random** — abatere deliberată de la litera cardului, cu
motiv: proiecția v2 e o funcție PURĂ (NX-240) și tokenurile NX-236 sunt deterministe tocmai ca
două citiri ale aceluiași rând să dea aceiași bytes. Un id random ar rupe exact asta: un `GET`
repetat sau un SSE reconectat ar produce alt prompt pentru același turn, iar „un singur vot per
prompt" ar deveni „un vot per reîncărcare de pagină". HMAC peste `turn_id` păstrează proprietatea
care conta (clientul nu îl poate ghici, fiindcă cheia e server-owned) și o adaugă pe cea de care
avem nevoie.

**Ce NU face feedbackul** (cerință explicită): nu schimbă răspunsul curent, nu atinge rankingul,
nu intră în prompt, nu devine training data. Scrie un rând și întoarce un receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.db.provider import DbProvider
from src.db.queries.feedback import FeedbackRow, get_feedback, upsert_feedback
from src.db.queries.web_turns import WebTurnRow, get_turn_by_id
from src.observability import hooks
from src.web.action_crypto import KeyRing
from src.web.action_models import FEEDBACK_TAXONOMY_VERSION, RATING_BY_KIND, SINK_FEEDBACK
from src.web.action_service import ActionRejected, verify_envelope, verify_source
from src.web.localization import label

log = logging.getLogger(__name__)

#: Versiunea SCHEMEI de feedback. Intră în derivarea promptului: dacă schimbăm ce înseamnă un vot,
#: prompturile vechi nu se mai potrivesc, deci un token emis sub semantica veche nu poate scrie
#: peste un rând scris sub cea nouă.
FEEDBACK_SCHEMA_VERSION = "web-feedback.v1"

#: Câte corecții acceptăm per prompt. Un client care trimite 👍👎👍👎 automat nu are voie să scrie
#: la infinit (nici să umfle `updated_at`), dar o răzgândire onestă trebuie să încapă.
MAX_FEEDBACK_REVISIONS = 5

_PROMPT_DOMAIN = b"nx246.feedback:"


@dataclass(frozen=True)
class FeedbackReceipt:
    """Ce vede clientul: confirmare LOCALIZATĂ + starea curentă. Zero cifre pe sârmă în afară de
    `revision` (P: tot ce e afișabil e text server-owned, ca la NX-240)."""

    prompt_id: str
    rating: str
    reason_code: str | None
    revision: int
    message: str

    def as_view(self) -> dict[str, Any]:
        return {
            "type": "feedback_receipt",
            "prompt_id": self.prompt_id,
            "rating": self.rating,
            "reason_code": self.reason_code,
            "revision": self.revision,
            "message": self.message,
        }


@dataclass(frozen=True)
class FeedbackRejected:
    """Refuz typed. `code` pleacă pe sârmă (vocabular închis), `reason` rămâne pentru metrici —
    diferența dintre „token stricat" și „token al altui tenant" ar fi un oracol."""

    code: str
    reason: str = ""


FEEDBACK_ERROR_CODES: frozenset[str] = frozenset(
    {
        "feedback_disabled",  # kill-switch stins — refuz onest, fără scriere
        "feedback_invalid",  # crypto/format/kind: respingere GENERICĂ
        "feedback_expired",  # TTL depășit (ancorat în terminalul sursă)
        "feedback_not_found",  # sursa lipsește / nu e a sesiunii / n-a emis promptul
        "feedback_locked",  # plafon de revizii atins
        "feedback_unavailable",  # storage indisponibil — conversația rămâne neatinsă
    }
)

#: `ActionRejected.code` (NX-236) → codul de feedback. Mapare EXPLICITĂ, nu o transformare de
#: șiruri: vocabularele sunt separate fiindcă rutele sunt separate, iar un cod de acțiune scurs pe
#: ruta de feedback ar spune clientului lucruri despre mecanismul de tur.
_CODE_MAP: dict[str, str] = {
    "action_expired": "feedback_expired",
    "action_invalid": "feedback_invalid",
    "action_not_found": "feedback_not_found",
    "action_unavailable": "feedback_unavailable",
    "action_already_consumed": "feedback_invalid",
    "action_stale": "feedback_not_found",
    "action_disabled": "feedback_disabled",
}


def prompt_id_for(turn_id: str, secret: str, *, schema: str = FEEDBACK_SCHEMA_VERSION) -> str:
    """`turn_id` → id de prompt DETERMINIST și ne-ghicibil (vezi docstringul modulului)."""
    key = secret.encode() if secret else _PROMPT_DOMAIN
    digest = hmac.new(key, _PROMPT_DOMAIN + f"{schema}:{turn_id}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


async def submit_feedback(
    db: DbProvider,
    *,
    token: str,
    business_id: str,
    channel_token: str,
    visitor_id: str,
    ring: KeyRing,
    prompt_secret: str,
    locale: str,
    release_sha: str | None,
    release_track: str,
    now: datetime | None = None,
    skew_s: int = 0,
) -> FeedbackReceipt | FeedbackRejected:
    """Token opac → vot persistat + receipt. Singura cale prin care se naște un rând de feedback.

    Ordinea e cea din NX-236, cu ultimul pas înlocuit: în loc de „consumă în ledger", „scrie în
    `web_feedback`". Verificările nu sunt rescrise — sunt EXACT funcțiile pure pe care le
    folosește și ruta de tur (`verify_envelope` / `verify_source`), ca modelul de amenințare să
    aibă un singur loc.
    """
    verified = verify_envelope(
        token,
        business_id=business_id,
        channel_token=channel_token,
        visitor_id=visitor_id,
        ring=ring,
        sink=SINK_FEEDBACK,
        now=now,
        skew_s=skew_s,
    )
    if isinstance(verified, ActionRejected):
        return _reject(verified)

    envelope = verified.envelope
    async with db("web_feedback_source") as conn:
        source = await get_turn_by_id(conn, business_id, envelope.source_turn_id)
    rejected = verify_source(verified, source)
    if rejected is not None:
        return _reject(rejected)
    assert source is not None

    rating = RATING_BY_KIND.get(envelope.kind)
    if rating is None:  # imposibil prin `sink`, dar un registry editat greșit n-are voie să scrie
        return FeedbackRejected("feedback_invalid", "kind_without_rating")

    return await _record(
        db,
        source=source,
        business_id=business_id,
        prompt_secret=prompt_secret,
        rating=rating,
        reason_code=envelope.args.reason,
        action_id=envelope.action_id,
        locale=locale,
        release_sha=release_sha,
        release_track=release_track,
    )


async def _record(
    db: DbProvider,
    *,
    source: WebTurnRow,
    business_id: str,
    prompt_secret: str,
    rating: str,
    reason_code: str | None,
    action_id: str,
    locale: str,
    release_sha: str | None,
    release_track: str,
) -> FeedbackReceipt | FeedbackRejected:
    """Scrierea + receipt-ul. Trei rezultate, toate deterministe:

    • rând scris/actualizat  → receipt cu `revision` nou;
    • `None` din upsert      → ori retry IDENTIC (același `action_id`) ⇒ receipt IDENTIC, ori
                               plafon de revizii ⇒ `feedback_locked`. Recitim ca să distingem;
    • excepție de storage    → `feedback_unavailable`. Conversația NU e atinsă: cardul cere ca
                               un feedback care nu se poate scrie să nu strice turul.
    """
    prompt_id = prompt_id_for(source.id, prompt_secret)
    try:
        async with db("web_feedback_write") as conn:
            row = await upsert_feedback(
                conn,
                business_id,
                conversation_id=source.conversation_id,
                turn_id=source.id,
                feedback_prompt_id=prompt_id,
                rating=rating,
                reason_code=reason_code,
                taxonomy_version=FEEDBACK_TAXONOMY_VERSION,
                schema_version=FEEDBACK_SCHEMA_VERSION,
                release_sha=release_sha,
                release_track=release_track,
                pipeline_version=source.pipeline_version,
                action_id=action_id,
                max_revisions=MAX_FEEDBACK_REVISIONS,
            )
            if row is None:
                row = await get_feedback(conn, business_id, prompt_id)
    except Exception as e:  # noqa: BLE001 — storage jos ⇒ refuz onest, nu 500 și nu tur stricat
        log.warning("web_feedback: scriere eșuată (%s)", type(e).__name__)
        hooks.on_feedback("unknown", None, outcome="error", release_track=release_track)
        return FeedbackRejected("feedback_unavailable", "storage_error")

    if row is None:
        # `upsert` n-a scris ȘI rândul nu există: singura cale e o cursă în care altcineva l-a
        # șters între cele două operații (azi imposibil — nu există DELETE). Refuz onest.
        hooks.on_feedback(rating, reason_code, outcome="error", release_track=release_track)
        return FeedbackRejected("feedback_unavailable", "row_missing")
    if row.last_action_id != action_id and row.revision >= MAX_FEEDBACK_REVISIONS:
        hooks.on_feedback(rating, reason_code, outcome="rejected", release_track=release_track)
        return FeedbackRejected("feedback_locked", "max_revisions")

    outcome = "recorded" if row.last_action_id == action_id and row.revision == 1 else "updated"
    hooks.on_feedback(row.rating, row.reason_code, outcome=outcome, release_track=release_track)
    return FeedbackReceipt(
        prompt_id=prompt_id,
        rating=row.rating,
        reason_code=row.reason_code,
        revision=row.revision,
        message=receipt_message(row.rating, locale),
    )


def receipt_message(rating: str, locale: str) -> str:
    """Textul de confirmare, SERVER-OWNED și localizat (frontendul nu inventează „Mulțumim!")."""
    key = "feedback_thanks_positive" if rating == "positive" else "feedback_thanks_negative"
    return label(key, locale) or label(key, "ro") or "Mulțumim!"


def _reject(rejected: ActionRejected) -> FeedbackRejected:
    return FeedbackRejected(
        _CODE_MAP.get(rejected.code, "feedback_invalid"), rejected.reason or rejected.code
    )


def should_prompt(*, enabled: bool, row: WebTurnRow | None) -> bool:
    """Are turul ăsta voie să ceară feedback? Pur, ca poarta să fie testabilă.

    Doar turele COMPLETATE: a cere părerea despre un `failed`/`cancelled` ar strânge voturi despre
    un mesaj de eroare pe care noi l-am scris — semnal despre infrastructură, prezentat ca semnal
    despre calitatea răspunsului.
    """
    return bool(enabled and row is not None and row.status == "completed")


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "FEEDBACK_ERROR_CODES",
    "FEEDBACK_SCHEMA_VERSION",
    "MAX_FEEDBACK_REVISIONS",
    "FeedbackReceipt",
    "FeedbackRejected",
    "FeedbackRow",
    "prompt_id_for",
    "receipt_message",
    "should_prompt",
    "submit_feedback",
]
