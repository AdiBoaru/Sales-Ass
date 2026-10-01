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
FORMAT_VERSION = 1

#: Câmpurile contactului care intră. `display_name` nu: e PII și nimic din tur nu decide pe el.
_CONTACT_KEYS = ("id", "business_id", "locale", "profile", "lead_score", "lifecycle", "consent")


def capture_enabled() -> bool:
    s = get_settings()
    return bool(
        getattr(s, "conversation_trace_enabled", False)
        and getattr(s, "trace_model_io_enabled", False)
    )


def settings_overrides(settings: Any) -> dict[str, Any]:
    """Setările date EXPLICIT procesului (env), fără secrete, doar scalare. Replay-ul le aplică
    peste implicitele codului pe care rulează: la același release, profilul e exact cel de
    producție; pe codul unei reparații, implicitele noi rămân ale reparației (asta se testează).

    Doar cele setate explicit, nu toată configurația: ~400 de câmpuri pe fiecare tur ar fi zgomot,
    iar `config_revision` (în `release`) identifică oricum configurația întreagă."""
    names = getattr(settings, "model_fields_set", None) or set()
    out: dict[str, Any] = {}
    for name in sorted(names):
        if is_secret_field(name):
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


def turn_input(
    ctx: Any, snap: Any, event: dict[str, Any], business: Any, *, channel_id: str = ""
) -> dict[str, Any]:
    """Documentul `turn_input` (PUR pe argumente + setări). Produsele turului se citesc la final
    (retrieval + cardurile răspunsului), deci se cheamă după pipeline."""
    from src.ops.build_info import config_revision  # noqa: PLC0415

    s = get_settings()
    products: list[dict[str, Any]] = []
    if getattr(ctx, "retrieval", None) is not None:
        products.extend(ctx.retrieval.products or [])
    if getattr(ctx, "reply", None) is not None:
        products.extend(ctx.reply.products or [])
    contact = asdict(snap.contact) if snap.contact is not None else {}
    return {
        "v": FORMAT_VERSION,
        "snapshot": {
            "conversation_id": snap.conversation_id,
            "state": snap.state,
            "state_version": snap.state_version,
            "locale": snap.locale,
            "bot_active": snap.bot_active,
            "shadow_mode": snap.shadow_mode,
            "summary": snap.summary,
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
            "verified": bool(getattr(ctx, "verified_customer_ref", None)),
        },
        "env": {
            "release": getattr(s, "release_sha", "") or os.environ.get("RELEASE_SHA") or None,
            "config_revision": config_revision(s),
            "settings": settings_overrides(s),
            "pack_sha": pack_sha(business),
            "catalog": catalog_fingerprint(products),
        },
    }


def record_turn_input(
    ctx: Any, snap: Any, event: dict[str, Any], business: Any, *, channel_id: str = ""
) -> None:
    """Scrie `ctx.trace["turn_input"]`. Best-effort (P6): un eșec lasă turul nerejucabil."""
    if not capture_enabled():
        return
    try:
        ctx.trace["turn_input"] = turn_input(ctx, snap, event, business, channel_id=channel_id)
    except Exception:  # noqa: BLE001 — captura e diagnoză, nu are voie să rupă turul
        ctx.trace["turn_input"] = {"v": FORMAT_VERSION, "error": True}
