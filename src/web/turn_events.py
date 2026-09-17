"""Proiecția ledgerului către sârmă: status, SSE și copy-ul de progres.

PROIECȚIE, nu autoritate: tot ce iese de aici e derivat DETERMINIST din rândul `web_turns`
(autoritatea NX-232). Aceeași intrare → aceiași bytes, deci un GET repetat sau un SSE reconectat
nu pot „reconstrui" alt răspuns.

  • STATUS — `TurnStatusView` + payload-ul 202 (POST accept / GET pe un turn ne-terminal):
    statusul de sârmă (proiecția NX-232: `running` nu iese niciodată), `status_url`,
    `events_url` și `poll_after_ms` server-owned.
  • TERMINAL — `v1_terminal_view` (`turn_view_v1.py`). O singură vedere: `web-chat.v1`, adică
    exact payload-ul persistat de executor. Aici NU mai există un al doilea contract de vedere:
    envelope-ul de blocuri `web-view.v2` a fost ȘTERS, nu înghețat. Un contract pe care nu-l
    servește nimeni nu e gratis — e o ramură pe care n-o execută nimeni dar o citesc toți, plus
    un al doilea loc în care „ce vede clientul" poate diverge.
  • SSE — frame-uri cu `id:` MONOTONIC pe lifecycle (accepted=0 → working=1 → validating=2 →
    terminal=3): `Last-Event-ID` reia exact de unde a rămas, fără status inventat și fără
    rezultat dublu. Doar schimbări reale de status + rezultatul terminal deja comis — zero
    tokeni LLM, zero draft, zero chain-of-thought, prin construcție (nu există sursă).

Phase-ul (`working`/`validating`) e OBSERVABILITATE, nu stare: trăiește efemer în Redis
(best-effort, TTL), scris de executor prin hook-ul pur din runner. După restart/reclaim e
pur și simplu absent → proiecția întoarce `working` (contractul NX-232). Pierderea lui nu
pierde nimic — DB-ul rămâne singura autoritate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from src.db.queries.web_turns import WebTurnRow
from src.web.turn_service import VALIDATING_PHASE, project_wire_status
from src.web.turn_view_v1 import v1_terminal_view

log = logging.getLogger(__name__)

# Copy SERVER-OWNED al fazelor de lifecycle, localizat (D3: fallback pe pilot, fără KeyError).
# Cheile sunt EXACT statusurile de sârmă ale unui turn ne-terminal, deci clientul face un lookup,
# nu o traducere.
_PROGRESS_COPY: dict[str, dict[str, str]] = {
    "ro": {
        "accepted": "Am primit mesajul",
        "working": "Pregătesc răspunsul",
        "validating": "Verific răspunsul",
    },
    "en": {
        "accepted": "Message received",
        "working": "Preparing the reply",
        "validating": "Checking the reply",
    },
}


def progress_copy(language: str | None) -> dict[str, str]:
    """Etichetele fazelor, localizate — publicate la bootstrap (`async_turns.progress`).

    Copie defensivă: tabela e constantă de modul și nu are voie să iasă pe sârmă ca referință
    partajată.
    """
    table = _PROGRESS_COPY.get((language or "ro")[:2]) or _PROGRESS_COPY["ro"]
    return dict(table)


# ── Status pe sârmă ─────────────────────────────────────────────────────────────────────────
# Ordinal MONOTONIC pe lifecycle → id-ul de eveniment SSE. Terminalele împart același ordinal:
# un turn are UN singur terminal (state machine-ul NX-232 le face finale), deci nu există
# ambiguitate — doar garanția că un client cu Last-Event-ID=2 mai primește DOAR terminalul.
STATUS_ORDINAL: dict[str, int] = {
    "accepted": 0,
    "working": 1,
    "validating": 2,
    "completed": 3,
    "failed": 3,
    "cancelled": 3,
}

# Fazele allowlisted pe care hook-ul de runner are voie să le scrie (orice altceva se ignoră).
ALLOWED_PHASES: frozenset[str] = frozenset({"working", VALIDATING_PHASE})


@dataclass(frozen=True)
class TurnStatusView:
    """Statusul unui turn, ca DATE de sârmă (fără rând de DB, fără conexiune, fără PII)."""

    turn_id: str
    client_turn_id: str
    status: str  # statusul de CONTRACT (working/validating…), niciodată `running`

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.turn_id,
            "client_turn_id": self.client_turn_id,
            "status": self.status,
        }


def turn_status_view(row: WebTurnRow, phase: str | None = None) -> TurnStatusView:
    return TurnStatusView(
        turn_id=row.id,
        client_turn_id=row.client_turn_id,
        status=project_wire_status(row.status, phase),
    )


def status_url(turn_id: str) -> str:
    return f"/web/v2/turns/{turn_id}"


def events_url(turn_id: str) -> str:
    return f"/web/v2/turns/{turn_id}/events"


def status_payload(
    row: WebTurnRow,
    *,
    phase: str | None = None,
    poll_after_ms: int,
    sse_enabled: bool = False,
) -> dict[str, Any]:
    """Payload-ul 202 (POST accept / GET ne-terminal): lifecycle server-owned, fără text
    comercial parțial. `poll_after_ms` e decizia serverului, nu un backoff ghicit de client."""
    view = turn_status_view(row, phase)
    out: dict[str, Any] = {
        "schema_version": "web-turn-status.v2",
        "turn": view.as_payload(),
        "status_url": status_url(row.id),
        "poll_after_ms": poll_after_ms,
    }
    if sse_enabled:
        out["events_url"] = events_url(row.id)
    return out


# ── SSE (proiecție, nu transport de tokeni) ─────────────────────────────────────────────────
def sse_frame(event: str, event_id: int, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


def status_event(row: WebTurnRow, phase: str | None = None) -> tuple[int, str]:
    """(ordinal, frame) pentru statusul CURENT. Ordinalul e id-ul de eveniment — clientul cu
    `Last-Event-ID` reia strict crescător, fără status inventat și fără dubluri."""
    view = turn_status_view(row, phase)
    ordinal = STATUS_ORDINAL[view.status]
    return ordinal, sse_frame("status", ordinal, {"turn": view.as_payload()})


def result_event(row: WebTurnRow, language: str) -> tuple[int, str]:
    """Rezultatul TERMINAL deja comis (exact ce ar servi și GET — proiecția aceluiași rând)."""
    ordinal = STATUS_ORDINAL[project_wire_status(row.status)]
    return ordinal, sse_frame("result", ordinal, v1_terminal_view(row, language))


# ── Phase (observabilitate efemeră, Redis best-effort) ──────────────────────────────────────
def phase_key(business_id: str, turn_id: str) -> str:
    return f"webturn:phase:{business_id}:{turn_id}"


async def set_phase(redis, business_id: str, turn_id: str, phase: str, *, ttl_s: int) -> None:
    """Scrie faza curentă (allowlisted). Best-effort: Redis jos → faza lipsește → `working`.
    Nicio autoritate aici — după reclaim noul owner o suprascrie pur și simplu."""
    if phase not in ALLOWED_PHASES:
        return
    try:
        await redis.set(phase_key(business_id, turn_id), phase, ex=ttl_s)
    except Exception:  # noqa: BLE001 — observabilitate, nu corectitudine
        pass


async def get_phase(redis, business_id: str, turn_id: str) -> str | None:
    try:
        raw = await redis.get(phase_key(business_id, turn_id))
    except Exception:  # noqa: BLE001 — Redis jos → faza necunoscută → `working`
        return None
    if raw is None:
        return None
    phase = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    return phase if phase in ALLOWED_PHASES else None
