"""Proiecția ledgerului către contractul `web-chat.v1` — transportul asincron, vederea v1.

De ce există modulul ăsta. NX-233 a construit transportul DURABIL (accept 202 → executor cu
lease/fencing → sweeper → SSE), dar l-a livrat cuplat cu o vedere NOUĂ (`web-view.v2`, blocuri).
Cele două sunt axe INDEPENDENTE, iar codul o spunea deja fără să o folosească: cererea e
`web-turn.v2`, statusul e `web-turn-status.v2`, vederea e `web-view.v2`. Trei contracte, trei
versiuni. Widgetul care rulează azi randează `web-chat.v1`, deci ca să câștige transportul nu
trebuie să schimbe vederea.

Ce proiectează: EXACT payload-ul persistat de executor (`render_web`, în tranzacția terminală),
plus plicul de tur pe care calea sincronă îl trimite deja (`_in_progress_response` are `turn`).
Nimic nu se recalculează, nimic nu se re-randează: dacă payload-ul persistat ar fi fost servit de
`/web/chat`, aceiași bytes se servesc și aici. Asta e și invarianta testată — `tests/
test_web_turn_view_v1.py::test_async_v1_body_equals_persisted_payload`.

Trei reguli care fac proiecția onestă:

  • **PURĂ.** Zero I/O, zero ceas, zero citire de catalog. Două GET-uri pe același rând dau
    aceiași bytes, iar un catalog schimbat după commit nu poate rescrie un răspuns deja dat
    (aceeași regulă ca projectorul NX-240).
  • **Cheile interne nu ajung pe sârmă.** `actions` (planul NX-236) și `grounded_v2` (verdictul
    NX-240) sunt dovezi server-side scrise în ACELAȘI payload; ele au consumatori interni, nu un
    randor. Filtrarea e pe ALLOWLIST (`_WIRE_KEYS`), nu pe blocklist: o cheie internă adăugată
    mâine nu se scurge pentru că a uitat cineva să o treacă pe listă.
  • **Niciun terminal mut (P6).** Un `failed`/`cancelled` primește text randabil localizat prin
    `error_view` (contractul v1 îl are deja) + un `error` typed. Un `completed` care ar ajunge
    aici fără nimic de afișat n-ar trebui să existe (`renderable()` e poarta de la commit), dar
    dacă apare, servim un terminal onest, nu un blank.
"""

from __future__ import annotations

from typing import Any

from src.db.queries.web_turns import WebTurnRow
from src.web.turn_service import (
    RESPONSE_CONTRACT_SYNC_V1,
    TERMINAL_LEDGER_STATUSES,
    error_view,
    project_wire_status,
    renderable,
)

#: Cheile pe care contractul v1 le pune pe sârmă, EXACT cele produse de `render_web`
#: (`src/channels/web/render.py`) plus `error_code`, scris de `error_view` pe terminalele de eșec.
#: Allowlist, nu blocklist: vezi docstringul modulului.
_WIRE_KEYS: tuple[str, ...] = (
    "content",
    "products",
    "suggestions",
    "comparison",
    "offer",
    "error_code",
)

#: Codurile după care clientul are voie să reîncearce ACELAȘI mesaj. Le ține aceeași listă ca
#: proiecția v2 — un „mai încearcă" care diferă între vederi ar fi două produse, nu unul.
_RETRYABLE_CODES: frozenset[str] = frozenset(
    {"empty_result", "processing_error", "deadline_exceeded"}
)


def wire_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Payload-ul persistat → doar cheile de contract v1, în ordine stabilă.

    Cheile absente rămân absente (contractul v1 e „degradare grațioasă": frontendul randează ce
    primește), cu o excepție — `content`/`products`/`suggestions` sunt scheletul pe care widgetul
    îl citește necondiționat, deci se emit mereu, ca la `render_web`.
    """
    source = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {
        "content": source.get("content") or "",
        "products": source.get("products") or [],
        "suggestions": source.get("suggestions") or [],
    }
    for key in _WIRE_KEYS:
        if key in out:
            continue
        value = source.get(key)
        if value:
            out[key] = value
    return out


def v1_terminal_view(row: WebTurnRow, language: str) -> dict[str, Any]:
    """Envelope-ul terminal pe contractul `web-chat.v1`: payload-ul persistat + plicul de tur.

    `language` se folosește DOAR pe drumul de eșec (textul de eroare localizat). Pe succes nu
    există nimic de localizat aici: tot ce e afișabil a fost compus deja, în limba turului, de
    pipeline — a-l re-atinge ar însemna un al doilea writer.
    """
    if row.status not in TERMINAL_LEDGER_STATUSES:
        raise ValueError(f"v1_terminal_view cere un status terminal, nu {row.status!r}")
    language = (language or "ro")[:2]
    persisted = row.response_json if isinstance(row.response_json, dict) else {}
    code = row.safe_error_code or persisted.get("error_code")

    if row.status == "completed" and renderable(persisted):
        body = wire_payload(persisted)
        error: dict[str, Any] | None = None
    else:
        # Eșec, anulare, SAU un `completed` fără nimic randabil (nu ar trebui să existe —
        # `renderable()` e poarta de la commit — dar tăcerea nu e o opțiune, P6).
        fallback_code = code or ("cancelled" if row.status == "cancelled" else "processing_error")
        shown = persisted if renderable(persisted) else error_view(fallback_code, language)
        body = wire_payload(shown)
        body["error_code"] = fallback_code
        error = {
            "code": fallback_code,
            "message": body["content"],
            "retryable": fallback_code in _RETRYABLE_CODES,
        }

    view: dict[str, Any] = {
        "schema_version": RESPONSE_CONTRACT_SYNC_V1,
        "conversation": {
            "id": row.conversation_id,
            "revision": max(0, row.conversation_revision_at_accept or 0),
        },
        "turn": {
            "id": row.id,
            "client_turn_id": row.client_turn_id,
            "status": project_wire_status(row.status),
        },
        **body,
    }
    if error is not None:
        view["error"] = error
    return view


__all__ = ["v1_terminal_view", "wire_payload"]
