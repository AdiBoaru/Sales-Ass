"""Contractul central — TurnContext + dataclass-urile care curg prin pipeline.

Un singur `TurnContext` traversează cele 9 stagii. Regula absolută (CLAUDE.md):
fiecare câmp are EXACT un stagiu care îl scrie. Proprietarii sunt notați în
docstring-ul fiecărui câmp.

Numele de câmpuri reflectă schema reală (schema_v2 / schema_reference.md):
`contacts`, `conversations.state`, `messages` (direction+author, body), etc.

Dataclass-uri (nu Pydantic) pentru obiectele interne — sunt lightweight și nu
trec granițe externe. Pydantic v2 se folosește la I/O LLM și webhook (validare
de input), nu aici.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Enum-uri (oglindesc CHECK-urile din schema_v2)
# ---------------------------------------------------------------------------


class Route(str, Enum):
    SIMPLE = "simple"
    SALES = "sales"
    ORDER = "order"
    HANDOFF = "handoff"
    CLARIFY = "clarify"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class Author(str, Enum):
    CONTACT = "contact"
    BOT = "bot"
    HUMAN_AGENT = "human_agent"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Entități citite din DB
# ---------------------------------------------------------------------------


@dataclass
class BusinessConfig:
    """Citit din `businesses`. Owner: încărcătorul de context la intrare."""

    id: str
    slug: str
    name: str
    vertical: str = "ecommerce"
    default_locale: str = "ro"
    supported_locales: list[str] = field(default_factory=lambda: ["ro"])
    timezone: str = "Europe/Bucharest"
    settings: dict[str, Any] = field(default_factory=dict)
    daily_cost_cap_usd: float | None = None


@dataclass
class Contact:
    """Citit din `contacts` (+ rezolvare prin `channel_identities`).
    PII-ul de canal NU stă aici — doar în channel_identities."""

    id: str
    business_id: str
    display_name: str | None = None
    locale: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    lead_score: float = 0.0
    lifecycle: str = "new"
    consent: dict[str, Any] = field(default_factory=dict)
    is_blocked: bool = False


@dataclass
class InboundMessage:
    """Mesajul brut primit. Owner: Webhook → pus pe stream → citit de runner."""

    provider_msg_id: str
    content_type: str = "text"
    body: str | None = None
    media_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """Un mesaj din istoric (`messages`). Folosit în TurnContext.history."""

    direction: Direction
    author: Author
    body: str | None
    content_type: str = "text"
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# State — conversations.state (jsonb, ≤8KB). REF-uri, nu obiecte (principiul 8)
# ---------------------------------------------------------------------------


@dataclass
class ProductRef:
    """Referință compactă în state — NU obiectul complet de produs."""

    product_id: str
    name: str
    price: float


@dataclass
class ConversationState:
    """`conversations.state` jsonb. Owner la scriere: Sender (patch tranzacțional).
    Bugetul de 8KB e impus în context builder + CHECK în DB (003)."""

    active_search: dict[str, Any] = field(default_factory=dict)
    displayed_products: list[ProductRef] = field(default_factory=list)
    pending_question: dict[str, Any] | None = None
    asked_intents: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    state_version: int = 0


# ---------------------------------------------------------------------------
# Rezultate scrise de stagii specifice
# ---------------------------------------------------------------------------


@dataclass
class RouteDecision:
    """Scris DOAR de stagiul Triaj."""

    route: Route
    category_key: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    missing_field: str | None = None


@dataclass
class RetrievalResult:
    """Scris DOAR de stagiul de Retrieval/tools. Produse = câmpuri minime."""

    products: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None


@dataclass
class Reply:
    """Orice stagiu poate seta → early exit la Sender."""

    text: str
    kind: str = "message"  # message | template | typing
    # Carduri de produs (W1): dacă setate, Sender-ul le trimite ca poză+preț+buton
    # după textul de lead-in. Câmpuri compacte (name, price, url, image), nu obiecte.
    products: list[dict[str, Any]] | None = None
    # G5b: răspuns reutilizabil pentru cache (False pe clarify/refuz/fallback —
    # specifice contextului, nu se cache-uiesc).
    cacheable: bool = True


@dataclass
class Event:
    """Acumulat pentru analytics_events. Owner: stagiile emit, runner-ul scrie."""

    type: str
    properties: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# TurnContext — obiectul care curge prin pipeline
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    turn_id: str
    business: BusinessConfig
    contact: Contact
    message: InboundMessage
    conversation_id: str
    history: list[Message] = field(default_factory=list)
    state: ConversationState = field(default_factory=ConversationState)
    language: str = "ro"
    bot_active: bool = True  # owner: processor (din conversations.bot_active)
    handoff_until: datetime | None = None  # owner: processor (conversations.handoff_until)
    route: RouteDecision | None = None  # owner: Triaj
    retrieval: RetrievalResult | None = None  # owner: Retrieval
    reply: Reply | None = None  # owner: orice stagiu (early exit)
    halt: bool = False  # owner: Gates (tăcere intenționată — early exit fără reply)
    from_cache: bool = False  # owner: Cache (G5b) — reply servit din cache
    events: list[Event] = field(default_factory=list)

    def emit(self, type_: str, **properties: Any) -> None:
        """Helper pentru stagii: adaugă un event fără să știe cum e scris."""
        self.events.append(Event(type=type_, properties=properties))

    def halt_silent(self, reason: str) -> None:
        """Tăcere INTENȚIONATĂ (Gates): oprește pipeline-ul FĂRĂ reply de bot —
        omul se ocupă (handoff activ / bot oprit). Singura excepție de la
        principiul 6 ('niciodată tăcere'). Emite `gate_halt` pentru observabilitate."""
        self.halt = True
        self.emit("gate_halt", reason=reason)

    def set_reply(
        self,
        text: str,
        kind: str = "message",
        products: list[dict[str, Any]] | None = None,
        *,
        cacheable: bool = True,
    ) -> None:
        """Setează reply → semnalează early exit la Sender. `products` (opțional) →
        Sender-ul le trimite ca carduri (poză+preț+buton) după text (W1). `cacheable`
        (G5b) → False pe clarify/refuz/fallback (nu se scriu în cache)."""
        self.reply = Reply(text=text, kind=kind, products=products, cacheable=cacheable)
