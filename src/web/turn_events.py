"""NX-233 — proiecția ledgerului către sârmă: status, SSE și envelope-ul `web-view.v2`.

PROIECȚIE, nu autoritate: tot ce iese de aici e derivat DETERMINIST din rândul `web_turns`
(autoritatea NX-232) + copy-ul server-owned localizat. Aceeași intrare → aceiași bytes, deci
un GET repetat sau un SSE reconectat nu pot „reconstrui" alt răspuns.

Trei responsabilități, toate pure pe date (fără DB aici):

  • STATUS — `TurnStatusView` + payload-ul 202 (POST accept / GET pe un turn ne-terminal):
    statusul de sârmă (proiecția NX-232: `running` nu iese niciodată), `status_url`,
    `events_url` și `poll_after_ms` server-owned.
  • TERMINAL — `terminal_view()`: payload-ul PERSISTAT (contract `web-chat.v1`, scris de
    `render_web` în tranzacția de commit) → envelope `web-view.v2` (NX-228), validat cu
    `parse_view` ÎNAINTE de a fi servit. Reducerile/prețurile se calculează AICI (server-owned),
    exact regula pe care v1 o lăsa în browser. Chips-urile v1 NU se proiectează: un `submit`
    v2 cere token opac semnat (NX-236) — până atunci, mai bine absent decât un token fals.
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
from functools import lru_cache
from typing import Any

from src.agent.grounding_guard import GROUNDED_PAYLOAD_KEY, answer_from_jsonb
from src.channels.web.render_v2 import TurnIdentity, project
from src.config import get_settings
from src.db.queries.web_turns import WebTurnRow
from src.web.action_crypto import KeyRing, KeyRingError, parse_key_ring
from src.web.action_models import MAX_ACTIONS_PER_TURN, action_label
from src.web.action_service import IssuedAction, issue_actions, plans_from_row
from src.web.contracts_v2 import (
    MAX_ACTIONS_PER_ITEM,
    MAX_ACTIONS_PER_ROW,
    VIEW_SCHEMA_VERSION,
    parse_view,
)
from src.web.turn_service import (
    TERMINAL_LEDGER_STATUSES,
    VALIDATING_PHASE,
    project_wire_status,
)

log = logging.getLogger(__name__)

# ── Copy server-owned, localizat (D3: fallback pe pilot, fără KeyError) ─────────────────────
_COPY: dict[str, dict[str, Any]] = {
    "ro": {
        "chrome": {
            "launcher_label": "Deschide asistentul",
            "dialog_title": "Asistent de cumpărături",
            "dialog_description": "Întreabă despre produse, comenzi sau livrare.",
            "close_label": "Închide",
            "new_chat_label": "Conversație nouă",
        },
        "composer": {
            "label": "Mesajul tău",
            "placeholder": "Scrie un mesaj…",
            "send_label": "Trimite",
        },
        "announcements": {
            "accepted": "Mesajul a fost primit.",
            "working": "Asistentul pregătește răspunsul.",
            "validating": "Asistentul verifică răspunsul.",
            "completed": "Răspunsul este gata.",
            "failed": "A apărut o problemă la pregătirea răspunsului.",
            "cancelled": "Cererea a fost anulată.",
        },
        "progress": {
            "accepted": "Am primit mesajul",
            "working": "Pregătesc răspunsul",
            "validating": "Verific răspunsul",
        },
        "view_product": "Vezi produsul",
        "rating": "{rating} din 5",
        "reviews": "({n} recenzii)",
        "currency": "lei",
    },
    "en": {
        "chrome": {
            "launcher_label": "Open the assistant",
            "dialog_title": "Shopping assistant",
            "dialog_description": "Ask about products, orders or delivery.",
            "close_label": "Close",
            "new_chat_label": "New conversation",
        },
        "composer": {
            "label": "Your message",
            "placeholder": "Type a message…",
            "send_label": "Send",
        },
        "announcements": {
            "accepted": "Your message was received.",
            "working": "The assistant is preparing a reply.",
            "validating": "The assistant is checking the reply.",
            "completed": "The reply is ready.",
            "failed": "Something went wrong while preparing the reply.",
            "cancelled": "The request was cancelled.",
        },
        "progress": {
            "accepted": "Message received",
            "working": "Preparing the reply",
            "validating": "Checking the reply",
        },
        "view_product": "View product",
        "rating": "{rating} out of 5",
        "reviews": "({n} reviews)",
        "currency": "lei",
    },
    "hu": {
        "chrome": {
            "launcher_label": "Asszisztens megnyitása",
            "dialog_title": "Vásárlási asszisztens",
            "dialog_description": "Kérdezz termékekről, rendelésekről vagy szállításról.",
            "close_label": "Bezárás",
            "new_chat_label": "Új beszélgetés",
        },
        "composer": {
            "label": "Üzeneted",
            "placeholder": "Írj egy üzenetet…",
            "send_label": "Küldés",
        },
        "announcements": {
            "accepted": "Az üzenet megérkezett.",
            "working": "Az asszisztens készíti a választ.",
            "validating": "Az asszisztens ellenőrzi a választ.",
            "completed": "A válasz elkészült.",
            "failed": "Hiba történt a válasz elkészítése közben.",
            "cancelled": "A kérés meg lett szakítva.",
        },
        "progress": {
            "accepted": "Üzenet megérkezett",
            "working": "Válasz készítése",
            "validating": "Válasz ellenőrzése",
        },
        "view_product": "Termék megtekintése",
        "rating": "{rating} / 5",
        "reviews": "({n} értékelés)",
        "currency": "lei",
    },
}


def _copy(language: str | None) -> dict[str, Any]:
    return _COPY.get((language or "ro")[:2]) or _COPY["ro"]


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


# ── Formatare display-ready (server-owned; v1 lăsa exact calculele astea în browser) ────────
def _fmt_amount(value: float, language: str) -> str:
    """`89.0` → `89,00` (ro/hu) sau `89.00` (en). Determinist, fără locale de sistem."""
    text = f"{float(value):,.2f}"
    if (language or "ro")[:2] in ("ro", "hu"):
        text = text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return text


def _fmt_price(value: float, currency: str | None, language: str) -> str:
    return f"{_fmt_amount(value, language)} {currency or _copy(language)['currency']}"


def _fmt_rating(value: float, language: str) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    if (language or "ro")[:2] in ("ro", "hu"):
        text = text.replace(".", ",")
    return text


def _fmt_discount(price: float, list_price: float) -> str | None:
    if not list_price or list_price <= price:
        return None
    pct = round((list_price - price) / list_price * 100)
    return f"-{pct}%" if pct > 0 else None


def _price_view(card: dict[str, Any], language: str) -> dict[str, Any] | None:
    price = card.get("price")
    if not isinstance(price, (int, float)):
        return None
    currency = card.get("currency")
    view: dict[str, Any] = {"current": _fmt_price(float(price), currency, language)}
    list_price = card.get("list_price")
    if isinstance(list_price, (int, float)) and list_price > price:
        view["previous"] = _fmt_price(float(list_price), currency, language)
        discount = _fmt_discount(float(price), float(list_price))
        if discount:
            view["discount"] = discount
    return view


_SAFE_TONES = frozenset({"neutral", "info", "success", "warning", "danger"})


def _clip(text: Any, limit: int) -> str | None:
    s = str(text).strip() if text is not None else ""
    return s[:limit] if s else None


def _safe_url(raw: Any) -> str | None:
    """Doar https:// absolut sau rută relativă — aceeași regulă ca validatorul de contract.
    Un URL care ar pica validarea e OMIS (cardul rămâne randabil), nu servit."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or any(ch.isspace() for ch in value) or "\\" in value:
        return None
    if value.startswith("/") and not value.startswith("//"):
        return value
    if value.lower().startswith("https://"):
        return value
    return None


# ── NX-236: acțiuni opace (emise DIN planul persistat, la fiecare proiecție) ────────────────
@lru_cache(maxsize=4)
def _ring(spec: str) -> KeyRing:
    """Inelul de chei, cache-uit pe VALOAREA configurației: derivarea HKDF nu are ce căuta pe
    fiecare GET. O schimbare de config produce altă cheie de cache, deci alt inel."""
    return parse_key_ring(spec)


def issued_actions(row: WebTurnRow) -> tuple[IssuedAction, ...]:
    """Acțiunile sigilate ale unui turn terminal, re-derivate DETERMINIST din planul persistat.

    Kill-switch stins, inel invalid sau rând fără plan ⇒ `()`, adică exact ViewModel-ul de
    dinainte de card (butoanele lipsesc, restul e byte-identic). Un inel stricat NU rupe proiecția:
    P6 — mai bine un răspuns fără butoane decât niciun răspuns."""
    s = get_settings()
    if not s.web_actions_enabled:
        return ()
    try:
        ring = _ring(s.web_action_keys)
    except KeyRingError:
        log.warning("web_action: key ring invalid — proiecția livrează fără acțiuni")
        return ()
    return issue_actions(row, plans_from_row(row), ring=ring, ttl_s=s.web_action_ttl_s)


def _action_view(issued: IssuedAction, language: str, *, appearance: str) -> dict[str, Any] | None:
    """`IssuedAction` → `ActionView` (NX-228): etichetă + aspect + tokenul opac. Fără kind, fără
    argumente, fără id de produs — ce înseamnă butonul rămâne exclusiv pe server."""
    label = action_label(issued.plan.kind, language)
    if not label:
        return None
    return {
        "id": issued.action_id,
        "label": label[:40],
        "appearance": appearance,
        "activation": {"type": "submit", "token": issued.token},
    }


def _option_action_view(
    issued: IssuedAction, options: list[str], *, appearance: str = "chip"
) -> dict[str, Any] | None:
    """Acțiunea unei opțiuni de clarificare: eticheta e OPȚIUNEA persistată de noi, pe poziția
    din token. Frontendul o afișează; dacă o schimbă, se schimbă doar ce scrie pe buton — ordinalul
    din token rămâne cel care decide."""
    index = issued.plan.args.option_ref
    if index is None or index >= len(options):
        return None
    label = " ".join(str(options[index]).split()).strip()
    if not label:
        return None
    return {
        "id": issued.action_id,
        "label": label[:40],
        "appearance": appearance,
        "activation": {"type": "submit", "token": issued.token},
    }


def _product_item(
    card: dict[str, Any],
    turn_id: str,
    index: int,
    language: str,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    copy = _copy(language)
    item: dict[str, Any] = {
        "view_id": f"{turn_id}:p{index}",
        "title": _clip(card.get("name"), 200) or "—",
    }
    image = _safe_url(card.get("image_url"))
    if image:
        item["image"] = {"src": image, "alt": item["title"]}
    price = _price_view(card, language)
    if price:
        item["price"] = price
    rating = card.get("rating")
    if isinstance(rating, (int, float)):
        text = copy["rating"].format(rating=_fmt_rating(float(rating), language))
        count = card.get("review_count")
        if isinstance(count, int) and count > 0:
            text = f"{text} {copy['reviews'].format(n=count)}"
        item["rating"] = text[:200]
    reason = _clip(card.get("reason"), 2000)
    if reason:
        item["reason"] = reason
    badges = []
    for b in card.get("badges") or []:
        label = _clip((b or {}).get("label"), 60)
        if label:
            tone = b.get("tone") if b.get("tone") in _SAFE_TONES else "neutral"
            badges.append({"label": label, "tone": tone})
    if badges:
        item["badges"] = badges[:3]
    row_actions: list[dict[str, Any]] = []
    url = _safe_url(card.get("url"))
    if url:
        # `navigate` NU devine niciodată token: e un link deja validat de backend (https/relativ),
        # deci nu are ce comandă să poarte. Semnarea unui URL ar fi semnătură fără semantică.
        row_actions.append(
            {
                "id": f"{turn_id}:p{index}:view",
                "label": copy["view_product"][:40],
                "appearance": "secondary",
                "activation": {"type": "navigate", "href": url, "target": "_blank"},
            }
        )
    row_actions.extend(actions or [])
    if row_actions:
        item["actions"] = row_actions[:MAX_ACTIONS_PER_ITEM]
    return item


def _comparison_block(cmp: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    columns = [c for c in (cmp.get("columns") or []) if isinstance(c, dict)]
    rows = [r for r in (cmp.get("rows") or []) if isinstance(r, dict)]
    headers = [_clip(c.get("name"), 60) or "—" for c in columns][:3]
    if len(headers) < 2 or not rows:
        return None
    out_rows = []
    for r in rows[:12]:
        label = _clip(r.get("label"), 60)
        if not label:
            continue
        values = list(r.get("values") or [])[: len(headers)]
        values += [None] * (len(headers) - len(values))
        cells = [{"text": _clip(v, 200)} for v in values]
        out_rows.append({"label": label, "cells": cells})
    if not out_rows:
        return None
    return {"id": f"{turn_id}:cmp", "type": "comparison", "headers": headers, "rows": out_rows}


# ── Envelope-ul terminal `web-view.v2` ──────────────────────────────────────────────────────
def _blocks_from_payload(payload: dict[str, Any], row: WebTurnRow, language: str) -> list[dict]:
    """Payload-ul persistat `web-chat.v1` → blocurile v2, în ordinea de citire: text →
    comparație → carduri → rândul de acțiuni. Notice-ul de eșec se adaugă de apelant (are nevoie
    de status).

    NX-236: butoanele SEMANTICE se atașează aici, din acțiunile re-derivate. Un card primește
    acțiunile care îl NUMESC (`product_ref`), restul intră într-un `action_row` — legătura se face
    pe id-ul de catalog, care rămâne server-side (`ProductItemView` nu îl expune)."""
    blocks: list[dict[str, Any]] = []
    issued = issued_actions(row)
    per_product: dict[str, list[dict[str, Any]]] = {}
    row_actions: list[dict[str, Any]] = []
    option_actions: list[dict[str, Any]] = []
    options = [s for s in (payload.get("suggestions") or []) if isinstance(s, str)]
    for action in issued[:MAX_ACTIONS_PER_TURN]:
        if action.plan.kind == "answer_clarification":
            view = _option_action_view(action, options)
            if view:
                option_actions.append(view)
            continue
        ref = action.plan.args.product_ref
        if ref:
            view = _action_view(action, language, appearance="secondary")
            if view:
                per_product.setdefault(ref, []).append(view)
            continue
        view = _action_view(action, language, appearance="chip")
        if view:
            row_actions.append(view)

    content = _clip(payload.get("content"), 2000)
    if content and row.status == "completed":
        blocks.append({"id": f"{row.id}:t0", "type": "text", "variant": "body", "text": content})
    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        block = _comparison_block(comparison, row.id)
        if block:
            blocks.append(block)
    products = [p for p in (payload.get("products") or []) if isinstance(p, dict)][:6]
    if products:
        blocks.append(
            {
                "id": f"{row.id}:pl",
                "type": "product_list",
                "items": [
                    _product_item(
                        p, row.id, i, language, per_product.get(str(p.get("product_id") or ""))
                    )
                    for i, p in enumerate(products)
                ],
            }
        )
    # Opțiunile de clarificare primele: sunt răspunsul la ce tocmai am întrebat, deci pasul pe care
    # clientul chiar îl caută. Restul (compară, arată mai multe) vin după, în limita rândului.
    trailing = (option_actions + row_actions)[:MAX_ACTIONS_PER_ROW]
    if trailing:
        blocks.append({"id": f"{row.id}:ar", "type": "action_row", "actions": trailing})
    return blocks


_RETRYABLE_CODES = frozenset({"empty_result", "processing_error", "deadline_exceeded"})


def _envelope(row: WebTurnRow, language: str, blocks: list[dict]) -> dict[str, Any]:
    copy = _copy(language)
    status = project_wire_status(row.status)
    view: dict[str, Any] = {
        "schema_version": VIEW_SCHEMA_VERSION,
        "conversation": {
            "id": row.conversation_id,
            "revision": max(0, row.conversation_revision_at_accept or 0),
        },
        "turn": {"id": row.id, "client_turn_id": row.client_turn_id, "status": status},
        "messages": (
            [{"id": f"{row.id}:m0", "role": "assistant", "blocks": blocks}] if blocks else []
        ),
        "composer": {"enabled": True, **copy["composer"]},
        "chrome": dict(copy["chrome"]),
        "a11y": {"announcements": dict(copy["announcements"])},
    }
    return view


def grounded_view(row: WebTurnRow, payload: dict[str, Any], language: str) -> dict[str, Any] | None:
    """NX-240 — proiecția GROUNDED, când turul a înghețat un verdict (`grounded_v2`).

    `None` = nu se aplică (flag stins, rând vechi, payload nerecunoscut, proiecție eșuată) →
    apelantul cade pe proiecția din payload-ul v1. Degradarea e în ACEASTĂ direcție deliberat: un
    răspuns randat cu regulile de ieri e mai bun decât o eroare, iar diferența e observabilă
    (`web_view_projected{outcome}`), nu tăcută.

    Proiecția e PURĂ: `answer` vine din rând, acțiunile se re-derivă determinist din același rând
    (NX-236: `issued_at = completed_at`, sigiliu AES-SIV), iar ceasul e `as_of`-ul înghețat. Două
    GET-uri produc aceiași bytes; un catalog schimbat între ele nu produce nimic."""
    if not get_settings().web_view_v2_projector_enabled or row.status != "completed":
        return None
    raw = payload.get(GROUNDED_PAYLOAD_KEY)
    if not raw:
        return None
    try:
        answer = answer_from_jsonb(raw)
        if answer is None:
            return None
        identity = TurnIdentity(
            turn_id=row.id,
            client_turn_id=row.client_turn_id,
            conversation_id=row.conversation_id,
            conversation_revision=max(0, row.conversation_revision_at_accept or 0),
            status=project_wire_status(row.status),
        )
        view = project(
            answer,
            identity=identity,
            locale=language,
            issued_actions=issued_actions(row),
            now=answer.as_of or row.completed_at,
        )
        return view.model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001 — proiecția grounded nu are voie să scoată 500
        log.exception(
            "proiecția grounded (NX-240) a eșuat (web_turn %s) — cad pe payload-ul v1", row.id
        )
        return None


def terminal_view(row: WebTurnRow, language: str) -> dict[str, Any]:
    """Envelope-ul `web-view.v2` al unui turn TERMINAL, derivat determinist din payload-ul
    persistat. Validat cu `parse_view` înainte de a fi servit — un envelope care nu trece
    contractul NU pleacă pe sârmă; fallback-ul e un failed RANDABIL (P6), nu un 500."""
    if row.status not in TERMINAL_LEDGER_STATUSES:
        raise ValueError(f"terminal_view cere un status terminal, nu {row.status!r}")
    payload = row.response_json or {}
    language = (language or "ro")[:2]
    grounded = grounded_view(row, payload, language)
    if grounded is not None:
        return grounded
    try:
        blocks = _blocks_from_payload(payload, row, language)
        code = _clip(row.safe_error_code or payload.get("error_code"), 60)
        if row.status == "failed":
            text = _clip(payload.get("content"), 2000) or _copy(language)["announcements"]["failed"]
            blocks.insert(
                0, {"id": f"{row.id}:notice", "type": "notice", "level": "error", "text": text}
            )
            view = _envelope(row, language, blocks)
            view["error"] = {
                "code": code or "processing_error",
                "message": text,
                "retryable": (code or "processing_error") in _RETRYABLE_CODES,
            }
        elif row.status == "cancelled":
            text = (
                _clip(payload.get("content"), 2000) or _copy(language)["announcements"]["cancelled"]
            )
            blocks.insert(
                0, {"id": f"{row.id}:notice", "type": "notice", "level": "warning", "text": text}
            )
            view = _envelope(row, language, blocks)
        else:
            if not blocks:
                # P6 la nivel de proiecție: un completed persistat fără nimic randabil nu ar
                # trebui să existe (renderable() la commit), dar dacă apare, nu servim tăcere.
                raise ValueError("completed fără niciun bloc randabil")
            view = _envelope(row, language, blocks)
        parse_view(view)  # contractul e poarta: invalid → except → fallback failed randabil
        return view
    except Exception:  # noqa: BLE001 — proiecția nu are voie să scoată 500 pe un terminal
        log.exception("proiecția web-view.v2 a eșuat (web_turn %s) — fallback failed", row.id)
        return _projection_fallback(row, language)


def _projection_fallback(row: WebTurnRow, language: str) -> dict[str, Any]:
    copy = _copy(language)
    text = copy["announcements"]["failed"]
    view = _envelope(row, language, [])
    view["turn"]["status"] = "failed"
    view["messages"] = [
        {
            "id": f"{row.id}:m0",
            "role": "assistant",
            "blocks": [
                {"id": f"{row.id}:notice", "type": "notice", "level": "error", "text": text}
            ],
        }
    ]
    view["error"] = {"code": "projection_error", "message": text, "retryable": True}
    parse_view(view)  # fallback-ul e static — dacă pică, e bug de cod, nu de date
    return view


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
    return ordinal, sse_frame("result", ordinal, terminal_view(row, language))


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
