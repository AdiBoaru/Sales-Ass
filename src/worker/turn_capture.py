"""NX-366 — cu ce a pornit turul, înregistrat ca turul să poată fi rejucat fără model.

Perechea lui `src.agent.model_io`: acolo e ce a IEȘIT din model, aici e ce a INTRAT în tur
(`TurnLoadSnapshot`, care e deja DATE pure, plus amprentele mediului). Replay-ul
(`src/evals/trace_replay.py`) reconstruiește din ele exact contextul pe care l-a avut producția.

Proprietar unic al lui `ctx.trace["turn_input"]`: processor-ul, prin `record_turn_input` (P3).

Confidențialitate (P12, NX-230): mesajul clientului NU se înregistrează aici. Replay-ul folosește
`client_text` din rândul de trace, adică forma SAFE. Pe un mesaj cu PII (un telefon), replay-ul vede
deci textul redactat, nu cel brut: o pierdere de fidelitate declarată, preferată unei a doua copii a
vocii clientului pe disc. Identitatea verificată (NX-129) intră doar ca bit, nu ca valoare.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from typing import Any

from src.config import get_settings
from src.ops.build_info import is_secret_field

#: Versiunea formei. Replay-ul refuză o formă pe care n-o cunoaște (`not_replayable`).
FORMAT_VERSION = 2

#: Plafonul documentului (P4). Măsurat: setările ~11 KB, starea ≤ 8 KB, istoricul de 8 mesaje
#: ~12 KB pe `sole-ro`. Peste plafon turul e nerejucabil, cu motiv, nu trunchiat.
MAX_INPUT_BYTES = 128 * 1024

#: Setări care nu sunt secrete după NUME (`is_secret_field`), dar sunt chei după ROST: salt-ul de
#: asignare NX-249 e cheia HMAC care face bucketul necalculabil din afară. Gol azi în producție;
#: setat mâine, ar ajunge în traceuri și în cazurile exportate în repo.
_NEVER_RECORD = frozenset({"release_assignment_salt"})

#: Câmpurile contactului care intră. `display_name` nu: e PII și nimic din tur nu decide pe el.
_CONTACT_KEYS = ("id", "business_id", "locale", "profile", "lead_score", "lifecycle", "consent")


def capture_enabled() -> bool:
    s = get_settings()
    return bool(
        getattr(s, "conversation_trace_enabled", False)
        and getattr(s, "trace_model_io_enabled", False)
    )


def settings_profile(settings: Any) -> dict[str, Any]:
    """TOATE setările scalare care nu sunt secrete (aceeași mulțime pe care o amprentează
    `config_revision`). Replay-ul pornește din ele, nu din `.env`-ul mașinii pe care rulează:
    doar valorile date explicit procesului ar fi lăsat restul pe ce are local cine rulează
    replay-ul (recenzia: un `.env` local cu creierul unic aprins rula altă cale decât producția)."""
    try:
        names = sorted(type(settings).model_fields)
    except AttributeError:
        names = sorted(k for k in vars(settings) if not k.startswith("_"))
    out: dict[str, Any] = {}
    for name in names:
        if is_secret_field(name) or name in _NEVER_RECORD:
            continue
        value = getattr(settings, name, None)
        if isinstance(value, (bool, int, float, str)):
            out[name] = value
    return out


def pack_sha(business: Any) -> str | None:
    """Amprenta pachetului de domeniu cu care a rulat turul (din `businesses.settings`). Replay-ul
    o compară cu pachetul de acum și raportează driftul."""
    pack = (getattr(business, "settings", None) or {}).get("domain_pack")
    if not pack:
        return None
    raw = json.dumps(pack, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def catalog_fingerprint(products: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """`{product_id: [price, sale_price, availability, synced_at]}` pentru produsele turului.
    Replay-ul citește catalogul de ACUM; amprenta spune dacă s-a schimbat ceva între timp, ca o
    diferență să fie atribuită driftului, nu codului."""
    out: dict[str, list[Any]] = {}
    for p in products:
        pid = p.get("product_id") or p.get("id")
        if not pid:
            continue
        out[str(pid)] = [
            p.get("price"),
            p.get("sale_price"),
            p.get("availability") or p.get("stock"),
            p.get("synced_at"),
        ]
    return out


def _message(m: Any) -> dict[str, Any]:
    return {
        "direction": getattr(getattr(m, "direction", None), "value", None),
        "author": getattr(getattr(m, "author", None), "value", None),
        "body": m.body,
        "content_type": m.content_type,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "id": m.id,
        "payload": m.payload,
    }


def begin_turn_input(
    snap: Any, event: dict[str, Any], business: Any, *, channel_id: str, verified: bool
) -> dict[str, Any] | None:
    """Partea de INTRARE a documentului, luată ÎNAINTEA pipeline-ului și înghețată imediat (JSON).
    `ConversationState.from_jsonb` partajează dicturi cu `snap.state`, iar stagiile le mută pe loc
    (clarificarea scrie în `constraints`): luată după pipeline, „starea inițială" ar conține deja
    răspunsul turului. `None` cu captura stinsă."""
    if not capture_enabled():
        return None
    from src.ops.build_info import config_revision  # noqa: PLC0415

    try:
        s = get_settings()
        contact = asdict(snap.contact) if snap.contact is not None else {}
        doc = {
            "v": FORMAT_VERSION,
            "snapshot": {
                "conversation_id": snap.conversation_id,
                "state": snap.state,
                "state_version": snap.state_version,
                "locale": snap.locale,
                "bot_active": snap.bot_active,
                "shadow_mode": snap.shadow_mode,
                "facts": snap.facts,
                "history": [_message(m) for m in snap.history],
                "contact": {k: contact.get(k) for k in _CONTACT_KEYS},
                "inbound_msg_id": snap.inbound_msg_id,
            },
            "event": {
                "channel_id": channel_id,
                "content_type": event.get("content_type", "text"),
                "channel_kind": event.get("channel_kind", "webchat"),
                "channel_account_id": event.get("channel_account_id", ""),
                "media_id": event.get("media_id"),
                # Exact cheile pe care le citește `processor.prepare_turn_context`: acțiunea opacă
                # (NX-236, deja autorizată la margine) și contextul de pagină ID-only (NX-234).
                "action": event.get("action"),
                "page_context": event.get("page_context"),
                "verified": verified,
            },
            "env": {
                "release": getattr(s, "release_sha", "") or os.environ.get("RELEASE_SHA") or None,
                "config_revision": config_revision(s),
                "settings": settings_profile(s),
                "pack_sha": pack_sha(business),
            },
        }
        frozen = json.loads(json.dumps(doc, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 — captura e diagnoză, nu are voie să rupă turul
        return {"v": FORMAT_VERSION, "error": "capture_failed"}
    if len(json.dumps(frozen, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_BYTES:
        return {"v": FORMAT_VERSION, "error": "oversize"}
    return frozen


def finish_turn_input(ctx: Any, pending: dict[str, Any] | None) -> None:
    """Adaugă amprenta catalogului (produsele turului se știu abia la final) și scrie
    `ctx.trace["turn_input"]` (proprietar unic: processor-ul, prin funcția asta)."""
    if pending is None:
        return
    if "error" not in pending:
        try:
            products: list[dict[str, Any]] = []
            if getattr(ctx, "retrieval", None) is not None:
                products.extend(ctx.retrieval.products or [])
            if getattr(ctx, "reply", None) is not None:
                products.extend(ctx.reply.products or [])
            pending["env"]["catalog"] = json.loads(
                json.dumps(catalog_fingerprint(products), default=str)
            )
        except Exception:  # noqa: BLE001
            pending = {"v": FORMAT_VERSION, "error": "capture_failed"}
    ctx.trace["turn_input"] = pending
