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
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Capability(str, Enum):
    """NX-115 — ce poate randa un canal. Declarat per `ChannelSender`; dispatcher-ul rutează
    table-driven și degradează grațios la `send_text` (P6). Un canal nou = declară capabilități,
    nu editezi scara `if/elif`."""

    TEXT = "text"  # send_text — OBLIGATORIU pt orice sender
    RICH = "rich"  # send_rich — recomandare structurată (model iZi)
    CARDS = "cards"  # send_products — listă compactă cu butoane-link
    CAROUSEL = "carousel"  # send_carousel_card
    EDIT = "edit"  # edit_message_media — navigare carusel (R2)
    TYPING = "typing"  # mark_typing — semnal inbound (NX-90)
    MEDIA = "media"  # fetch_media — download inbound (informativ aici)
    OFFER = "offer"  # randare nativă Reply.offer (NX-114); fără ea → floor aplatizat în text
    # IZI-compare: randare nativă a tabelului `Reply.comparison` (web). Fără ea → floor aplatizat
    # (tabelul ca text) prin send_text. Randat tot prin `send_rich` (ca OFFER), nu metodă dedicată.
    COMPARISON = "comparison"


# Capabilitate → metoda reală pe sender. Sursa pt testul de consistență caps↔metode.
# OFFER/COMPARISON NU sunt aici: nu mapează la o metodă dedicată (randare în send_rich; floor text).
CAPABILITY_METHODS: dict[Capability, str] = {
    Capability.TEXT: "send_text",
    Capability.RICH: "send_rich",
    Capability.CARDS: "send_products",
    Capability.CAROUSEL: "send_carousel_card",
    Capability.EDIT: "edit_message_media",
    Capability.TYPING: "mark_typing",
    Capability.MEDIA: "fetch_media",
}


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


@dataclass
class CallbackEvent:
    """Apăsare pe un buton inline (Telegram callback_query) — UI deterministă,
    NU mesaj. Worker-ul o rutează la handler-ul de carusel (navigare), fără
    pipeline LLM. `card_message_id` = mesajul de editat; `data` = callback_data
    (ex. 'car:nav:2'). `provider_msg_id` = callback.id (idempotență)."""

    channel_kind: str
    channel_account_id: str
    sender_external_id: str
    provider_msg_id: str
    card_message_id: str
    data: str
    sender_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "callback", **asdict(self)}


@runtime_checkable
class ChannelSender(Protocol):
    """Un transport de mesaje outbound (WhatsApp, Telegram, ...).

    `account_id` = id-ul canalului EXPEDITOR (phone_number_id la WhatsApp, bot id
    la Telegram). `to` = id-ul destinatarului pe acel canal (wa_id / chat_id).
    Întoarce provider_msg_id-ul atribuit de platformă (wamid / message_id).
    Ridică la eroare de transport (dispatcher-ul prinde și programează retry)."""

    # NX-115: capabilități DECLARATE (matrice), nu deduse prin `hasattr`. Dispatcher-ul rutează
    # randarea pe baza lor și degradează grațios la send_text. `max_*_len` = clamp de transport
    # (None = fără limită). Fiecare implementare le setează ca atribute de clasă.
    capabilities: frozenset[Capability]
    max_text_len: int | None
    max_caption_len: int | None

    async def send_text(self, account_id: str, to: str, text: str) -> str: ...

    # Metode OPȚIONALE, gardate de CAPABILITY (nu `hasattr`): send_rich (RICH), send_products
    # (CARDS), send_carousel_card (CAROUSEL), edit_message_media (EDIT), mark_typing (TYPING),
    # fetch_media (MEDIA). Testul de consistență (test_dispatcher) verifică declarat ⇔ metodă reală.


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


@runtime_checkable
class MediaFetcher(Protocol):
    """Descarcă o media INBOUND (poză/voce) de la un canal — transport, la margine (NX-76).

    Simetric cu `ChannelSender` (outbound), dar pentru intrare: Gates îl folosește ca să aducă
    binarul unei poze/note vocale înainte de Vision/STT. `account_id` = id-ul canalului receptor;
    `media_id` = id-ul de media din envelope (`messages.media_ref`). Întoarce `(bytes, mime)`.
    `max_bytes` (opțional) = refuză media peste prag ÎNAINTE de a o descărca integral în memorie
    (din mărimea raportată de canal). Ridică la eroare de transport / prea mare — gate-ul prinde
    și degradează grațios (fail-soft, P6)."""

    async def fetch_media(
        self, account_id: str, media_id: str, *, max_bytes: int | None = None
    ) -> tuple[bytes, str]: ...


class MediaFetcherRegistry:
    """Mapează `channel_kind → MediaFetcher`. DOAR canalele care suportă download de media inbound
    (azi: WhatsApp). Un canal neînregistrat → `get` întoarce None → gate-ul degradează fail-soft."""

    def __init__(self) -> None:
        self._fetchers: dict[str, MediaFetcher] = {}

    def register(self, channel_kind: str, fetcher: MediaFetcher) -> None:
        self._fetchers[channel_kind] = fetcher

    def get(self, channel_kind: str) -> MediaFetcher | None:
        return self._fetchers.get(channel_kind)

    def kinds(self) -> list[str]:
        return list(self._fetchers)
