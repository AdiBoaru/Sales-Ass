"""Contractul de canal — envelope neutru (inbound) + ChannelSender (outbound).

Marginile sistemului (NX-60). Pipeline-ul și worker-ul sunt agnostice de canal:
  • INTRARE: fiecare canal parsează formatul lui și produce un `InboundEvent`
    NEUTRU pe stream.
  • IEȘIRE: dispatcher-ul citește `channel_kind` din outbox și cere registrului
    `ChannelSender` potrivit. Adăugarea unui canal = o clasă + o înregistrare.

NX-289: singurul canal implementat e `webchat`. Contractul rămâne generic (nu e o
dependență de un canal anume — e motivul pentru care stagiile 3-9 nu știu de niciunul),
dar tot ce era specific WhatsApp/Telegram a fost ȘTERS, nu doar dezactivat: statusuri
de livrare de provider, callback-uri de butoane inline, template-uri aprobate.

Câmpuri neutre: `channel_account_id` = id-ul canalului RECEPTOR (public_token la web);
`sender_external_id` = id-ul userului pe canal (visitor_id la web).
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
    Capability.MEDIA: "fetch_media",
}


# Canale cu identitate „de facto" stabilă: id-ul de canal ESTE userul (telefon E.164, chat id).
# NX-289: după scoaterea WhatsApp/Telegram, mulțimea e GOALĂ — `webchat` e ANONIM by design
# (src/web/session.py). Constanta rămâne fiindcă `identity_is_stable` trebuie să se poată
# reactiva la un canal viitor fără să se rescrie consumatorii.
IDENTIFIED_CHANNELS: tuple[str, ...] = ()


def identity_is_stable(channel_kind: str, verified_customer_ref: str | None) -> bool:
    """True dacă turul aparține unui CLIENT identificabil peste sesiuni, nu unui vizitator anonim.

    Un singur loc de adevăr pentru două întrebări care păreau diferite dar sunt aceeași:
      • poate ajunge turul la comenzi/retururi? (NX-128, `src/worker/order_gate.py`)
      • are sens un plafon de cost PER CONTACT? (NX-125, `src/worker/processor.py`)
    Ambele cer ca „contactul" să fie o persoană stabilă, nu un `visitor_id` care se schimbă la
    golirea cookie-urilor.

    NX-289: înainte răspunsul era numele canalului (`whatsapp`/`telegram`). Odată cu ele,
    testul pe canal ar fi întors MEREU False, deci plafonul per-contact ar fi murit tăcut, nu
    prin decizie. Sursa de identitate care a rămas e login passthrough-ul verificat (NX-129) —
    aceeași proprietate, exprimată pe canalul care există. Plafonul e oricum opt-in
    (`CONTACT_DAILY_COST_CAP_USD=0` implicit), deci reconectarea nu schimbă nimic în producție
    până când cineva îl pornește."""
    return channel_kind in IDENTIFIED_CHANNELS or bool(verified_customer_ref)


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
    # NX-129: identitate STABILĂ verificată a sender-ului, stabilită la marginea de canal (login
    # passthrough web → customer_ref din JWT). None = anonim. Neutru de canal: orice canal care
    # poate dovedi un client o pune aici; worker-ul rezolvă contactul pe ea (verified=true).
    verified_customer_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "message", **asdict(self)}


@runtime_checkable
class ChannelSender(Protocol):
    """Un transport de mesaje outbound (azi: `webchat`).

    `account_id` = id-ul canalului EXPEDITOR (public_token la web). `to` = id-ul
    destinatarului pe acel canal (visitor_id). Întoarce provider_msg_id-ul atribuit de
    transport. Ridică la eroare (dispatcher-ul prinde și programează retry)."""

    # NX-115: capabilități DECLARATE (matrice), nu deduse prin `hasattr`. Dispatcher-ul rutează
    # randarea pe baza lor și degradează grațios la send_text. `max_*_len` = clamp de transport
    # (None = fără limită). Fiecare implementare le setează ca atribute de clasă.
    capabilities: frozenset[Capability]
    max_text_len: int | None
    max_caption_len: int | None

    async def send_text(self, account_id: str, to: str, text: str) -> str: ...

    # Metode OPȚIONALE, gardate de CAPABILITY (nu `hasattr`): send_rich (RICH), send_products
    # (CARDS), fetch_media (MEDIA). Testul de consistență (test_channel_caps) verifică
    # declarat ⇔ metodă reală.


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
    """Mapează `channel_kind → MediaFetcher`. DOAR canalele care suportă download de media inbound.
    Un canal neînregistrat → `get` întoarce None → gate-ul degradează fail-soft.

    NX-289: azi registrul e GOL — `webchat` nu trimite media inbound, iar singurul fetcher
    implementat era al WhatsApp-ului. Calea Vision (NX-76) rămâne în cod și degradează pe
    `no_downloader`; seam-ul se reactivează când un canal aduce binar, fără `if` de rescris."""

    def __init__(self) -> None:
        self._fetchers: dict[str, MediaFetcher] = {}

    def register(self, channel_kind: str, fetcher: MediaFetcher) -> None:
        self._fetchers[channel_kind] = fetcher

    def get(self, channel_kind: str) -> MediaFetcher | None:
        return self._fetchers.get(channel_kind)

    def kinds(self) -> list[str]:
        return list(self._fetchers)
