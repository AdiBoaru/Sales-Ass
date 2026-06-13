"""Contractul de canal — envelope neutru (inbound) + ChannelSender (outbound).

Marginile sistemului (NX-60). Pipeline-ul și worker-ul sunt agnostice de canal:
  • INTRARE: fiecare canal (Meta webhook, Telegram poller, ...) parsează formatul
    lui și produce un `InboundEvent`/`StatusEvent` NEUTRU pe stream.
  • IEȘIRE: dispatcher-ul citește `channel_kind` din outbox și cere registrului
    `ChannelSender` potrivit. Adăugarea unui canal = o clasă + o înregistrare.

Câmpuri neutre: `channel_account_id` = id-ul canalului (phone_number_id la Meta,
bot id la Telegram); `sender_external_id` = id-ul userului pe canal (wa_id / chat_id).
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class InboundEvent:
    """Un mesaj inbound NORMALIZAT (envelope neutru de canal), serializabil JSON.

    `content_type` e tipul BRUT al canalului; normalizarea la valorile permise de
    `messages.content_type` o face worker-ul. `payload` păstrează mesajul brut."""

    channel_kind: str
    channel_account_id: str
    sender_external_id: str
    provider_msg_id: str
    content_type: str
    timestamp: str | None = None
    body: str | None = None
    media_id: str | None = None
    sender_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "message", **asdict(self)}


@dataclass
class StatusEvent:
    """Un update de status (delivered/read/failed/sent) pentru un mesaj OUTBOUND.

    `provider_msg_id` = id-ul mesajului raportat (pe care l-am trimis noi). NU se
    deduplică la intrare: 'delivered' și 'read' au același id."""

    channel_kind: str
    channel_account_id: str
    provider_msg_id: str
    status: str
    timestamp: str | None = None
    recipient_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "status", **asdict(self)}


@runtime_checkable
class ChannelSender(Protocol):
    """Un transport de mesaje outbound (WhatsApp, Telegram, ...).

    `account_id` = id-ul canalului EXPEDITOR (phone_number_id la WhatsApp, bot id
    la Telegram). `to` = id-ul destinatarului pe acel canal (wa_id / chat_id).
    Întoarce provider_msg_id-ul atribuit de platformă (wamid / message_id).
    Ridică la eroare de transport (dispatcher-ul prinde și programează retry)."""

    async def send_text(self, account_id: str, to: str, text: str) -> str: ...


class ChannelSenderRegistry:
    """Mapează `channel_kind → ChannelSender`. Populat la bootstrap-ul dispatcher-ului."""

    def __init__(self) -> None:
        self._senders: dict[str, ChannelSender] = {}

    def register(self, channel_kind: str, sender: ChannelSender) -> None:
        self._senders[channel_kind] = sender

    def get(self, channel_kind: str) -> ChannelSender | None:
        return self._senders.get(channel_kind)

    def kinds(self) -> list[str]:
        return list(self._senders)
