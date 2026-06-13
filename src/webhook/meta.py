"""Parser pentru payload-ul webhook Meta WhatsApp Cloud API.

Transformă JSON-ul brut Meta într-o listă plată de `InboundEvent` — un eveniment
per mesaj inbound. NU rezolvă business/contact/conversație și NU atinge DB: acela
e jobul worker-ului (unde trăiește accesul la DB). Webhook-ul rămâne subțire ca
să dea ACK în <50ms (Meta face retry agresiv la timeout).

Structura Meta (relevant):
    entry[].changes[].value.metadata.phone_number_id   → canalul (→ business)
    entry[].changes[].value.contacts[].profile.name    → nume afișat
    entry[].changes[].value.messages[]                 → mesajele inbound
        .from .id .timestamp .type + corpul specific tipului (text.body, image.id, ...)
    entry[].changes[].value.statuses[]                 → delivered/read/failed (IGNORATE aici)

Parsarea e defensivă: chei lipsă → sărim, nu crăpăm. Un payload doar cu statuses
sau malformat → listă goală.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Tipurile Meta care poartă media (id-ul de media stă sub cheia omonimă).
_MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")


@dataclass
class InboundEvent:
    """Un mesaj inbound normalizat, gata de pus pe stream (serializabil JSON).

    `content_type` e tipul BRUT Meta (text/image/audio/...); normalizarea la
    valorile permise de `messages.content_type` o face worker-ul. `payload`
    păstrează mesajul brut Meta pentru cazurile pe care nu le aplatizăm aici
    (interactive, location, butoane)."""

    phone_number_id: str
    wa_id: str
    provider_msg_id: str
    content_type: str
    timestamp: str | None = None
    body: str | None = None
    media_id: str | None = None
    profile_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "message", **asdict(self)}


@dataclass
class StatusEvent:
    """Un update de status (delivered/read/failed/sent) pentru un mesaj OUTBOUND.

    `provider_msg_id` = wamid-ul mesajului raportat (pe care l-am trimis noi).
    NU se deduplică la webhook: 'delivered' și 'read' au același wamid — dedupe
    pe wamid ar arunca statusuri legitime distincte."""

    phone_number_id: str
    provider_msg_id: str
    status: str
    timestamp: str | None = None
    recipient_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "status", **asdict(self)}


def _extract_body(msg: dict[str, Any], content_type: str) -> tuple[str | None, str | None]:
    """Întoarce (body, media_id) în funcție de tipul mesajului."""
    if content_type == "text":
        return (msg.get("text") or {}).get("body"), None
    if content_type in _MEDIA_TYPES:
        media = msg.get(content_type) or {}
        # documentele/imaginile pot avea caption (text util pt agent)
        return media.get("caption"), media.get("id")
    if content_type == "button":
        return (msg.get("button") or {}).get("text"), None
    if content_type == "interactive":
        inter = msg.get("interactive") or {}
        # reply pe buton sau pe listă → titlul ales
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title"), None
    return None, None


def parse_webhook(payload: dict[str, Any]) -> list[InboundEvent]:
    """Aplatizează un payload webhook Meta în mesajele inbound conținute.

    Ignoră `statuses` (tratate de status handler) și orice `change` care nu e pe
    câmpul `messages`. Robust la structuri parțiale."""
    events: list[InboundEvent] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages")
            if not messages:
                continue  # ex: doar statuses

            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
            # mapă wa_id → nume profil (un singur contact de regulă)
            names: dict[str, str] = {}
            for c in value.get("contacts") or []:
                wa_id = c.get("wa_id")
                name = (c.get("profile") or {}).get("name")
                if wa_id and name:
                    names[wa_id] = name

            for msg in messages:
                provider_msg_id = msg.get("id")
                wa_id = msg.get("from")
                content_type = msg.get("type", "unknown")
                if not provider_msg_id or not wa_id:
                    continue  # mesaj inutilizabil fără id/expeditor

                body, media_id = _extract_body(msg, content_type)
                events.append(
                    InboundEvent(
                        phone_number_id=phone_number_id,
                        wa_id=wa_id,
                        provider_msg_id=provider_msg_id,
                        content_type=content_type,
                        timestamp=msg.get("timestamp"),
                        body=body,
                        media_id=media_id,
                        profile_name=names.get(wa_id),
                        payload=msg,
                    )
                )

    return events


def parse_statuses(payload: dict[str, Any]) -> list[StatusEvent]:
    """Aplatizează update-urile de status (delivered/read/failed/sent) din payload.
    Robust la structuri parțiale; un payload doar cu mesaje → listă goală."""
    events: list[StatusEvent] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            statuses = value.get("statuses")
            if not statuses:
                continue

            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
            for st in statuses:
                provider_msg_id = st.get("id")
                status = st.get("status")
                if not provider_msg_id or not status:
                    continue
                events.append(
                    StatusEvent(
                        phone_number_id=phone_number_id,
                        provider_msg_id=provider_msg_id,
                        status=status,
                        timestamp=st.get("timestamp"),
                        recipient_id=st.get("recipient_id"),
                        payload=st,
                    )
                )

    return events
