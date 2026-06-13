"""Contractul de canal pentru TRIMITERE (marginea de outbound).

Pipeline-ul și dispatcher-ul sunt agnostice de canal: dispatcher-ul ia un rând din
`outbox`, citește `channel_kind` și cere registrului clientul potrivit. Adăugarea
unui canal nou = o clasă care implementează `ChannelSender` + o înregistrare în
registru — fără atingerea worker-ului sau a logicii de coadă/retry (NX-60).

Simetric, marginea de INTRARE (parser + verificare per canal) produce un envelope
neutru pe stream (vezi webhook/meta.py, channels/telegram/poller.py).
"""

from typing import Protocol, runtime_checkable


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
