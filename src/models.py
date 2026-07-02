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
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # NX-114: DomainPack trăiește în src/domain/pack.py; import doar pt type-hint (fără ciclu
    # la runtime — `from __future__ import annotations` face adnotările lazy).
    from src.domain.pack import DomainPack

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
    # NX-114: config per-(business, vertical) — politică/taxonomie din DB+seed (P9). Owner UNIC:
    # `load_business` (apelează load_domain_pack). None dacă DOMAIN_PACK_ENABLED=false (fail-safe).
    domain_pack: DomainPack | None = None


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
    """Mesajul brut primit. Owner: Webhook → pus pe stream → citit de runner.

    `channel_kind`/`channel_account_id` = contextul de canal (id-ul canalului RECEPTOR), umplute
    de processor din envelope. Gates le folosește ca să ceară fetcher-ul de media corect (NX-76),
    fără cod de canal în pipeline."""

    provider_msg_id: str
    content_type: str = "text"
    body: str | None = None
    media_ref: str | None = None
    channel_kind: str = "whatsapp"
    channel_account_id: str = ""
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


# NX-119: cap dur al pool-ului de sesiune (≈24×uuid(36) ≈ 0.9KB → sub bugetul de 8KB, P4/P8).
MAX_SEARCH_POOL = 24


def _safe_int(value: Any) -> int:
    """Întreg defensiv: orice gunoi (string ne-numeric, None, listă) → 0, niciodată ridică."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hydrate_active_search(raw: Any) -> dict[str, Any] | None:
    """Hidratează `active_search` DEFENSIV (NX-119): chei lipsă/ne-listă → sesiune absentă.
    Păstrează DOAR ref-uri (P8): `pool` id-uri (cap MAX_SEARCH_POOL), `cursor`/`page` int-uri
    defensive, `fp` str, `filters` dict mic. from_jsonb rulează la FIECARE tur pe hot path → un
    cursor ne-numeric (drift/edit manual) NU trebuie să crape turul (ar bloca conversația, P6)."""
    if not isinstance(raw, dict):
        return None
    pool = raw.get("pool")
    if not isinstance(pool, list) or not pool:
        return None
    return {
        "filters": raw.get("filters") if isinstance(raw.get("filters"), dict) else {},
        "pool": [str(x) for x in pool][:MAX_SEARCH_POOL],
        "cursor": _safe_int(raw.get("cursor")),
        "fp": str(raw.get("fp") or ""),
        "page": _safe_int(raw.get("page")),
    }


@dataclass
class ConversationState:
    """`conversations.state` jsonb. Owner la scriere: Sender (patch tranzacțional).
    Bugetul de 8KB e impus în context builder + CHECK în DB (003)."""

    displayed_products: list[ProductRef] = field(default_factory=list)
    # NX-119: sesiune de căutare persistentă peste tururi — DOAR ref-uri (`pool` id-uri + `cursor` +
    # `fp` + `filters` mici), NU obiecte de produs (P8, cap MAX_SEARCH_POOL). Owner la scriere:
    # processor (din ctx.state_patch produs de search_products_tool). None = nicio sesiune activă.
    active_search: dict[str, Any] | None = None
    pending_question: dict[str, Any] | None = None
    # NX-112: semnal ieftin anti-loop — sloturile deja întrebate (citit de context_blocks; NX-116).
    # Cap 8 (P4), populat de clarify_resume_stage. NU e vocabular hardcodat (field-uri dinamice).
    asked_intents: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    # NX-133: STIVA de constrângeri de căutare multi-tur (buget/concerns/brand/suitable_for +
    # category_key pt reset). Distinct de `constraints` (acela = slot-fill din clarify, NX-112).
    # Owner la scriere: stagiul agent (`merge_constraints`, după triaj); persistat de processor.
    # Ref-uri scalare + listă scurtă de termeni (P8); cap 6 chei / concerns ≤5 (P4).
    search_constraints: dict[str, Any] = field(default_factory=dict)
    # NX-79: coșul acumulat de `cart_add` (ref-uri, NU obiecte de produs — P8). Top-level în jsonb;
    # owner la scriere: Sender (processor, din `ctx.state_patch`). Cap 10 linii (impus în cart_add).
    cart: list[dict[str, Any]] = field(default_factory=list)
    state_version: int = 0

    @classmethod
    def from_jsonb(cls, raw: dict[str, Any] | None) -> ConversationState:
        """Hidratează din `conversations.state` (jsonb). DEFENSIV: chei lipsă → default,
        produse fără id/name/price → sărite (nu crapă pe state vechi/parțial). `displayed_products`
        e scris ca {product_id|id, name, price, ...} de Sender — luăm doar cele 3 ref-uri (P8)."""
        raw = raw or {}
        products: list[ProductRef] = []
        for p in raw.get("displayed_products") or []:
            pid = p.get("product_id") or p.get("id")
            if pid is None or p.get("name") is None or p.get("price") is None:
                continue
            products.append(ProductRef(product_id=pid, name=p["name"], price=float(p["price"])))
        # Coșul (NX-79): linii incomplete sărite (la fel de defensiv ca displayed_products).
        cart: list[dict[str, Any]] = []
        for line in raw.get("cart") or []:
            pid = line.get("product_id")
            if pid is None or line.get("name") is None or line.get("price") is None:
                continue
            cart.append(
                {
                    "product_id": pid,
                    "variant_id": line.get("variant_id"),
                    "name": line["name"],
                    "price": float(line["price"]),
                    "quantity": int(line.get("quantity") or 1),
                }
            )
        return cls(
            displayed_products=products,
            active_search=_hydrate_active_search(raw.get("active_search")),
            pending_question=raw.get("pending_question"),
            # NX-112: cap 8 la hidratare (plasă peste clarify; state vechi cu >8 intrări se taie).
            asked_intents=(raw.get("asked_intents") or [])[-8:],
            constraints=raw.get("constraints") or {},
            # NX-133: back-compat — state vechi fără cheia asta / corupt (non-dict) → stivă goală.
            search_constraints=(
                raw.get("search_constraints")
                if isinstance(raw.get("search_constraints"), dict)
                else {}
            ),
            cart=cart,
            state_version=int(raw.get("state_version") or 0),
        )


# ---------------------------------------------------------------------------
# Rezultate scrise de stagii specifice
# ---------------------------------------------------------------------------


@dataclass
class RouteDecision:
    """Scris DOAR de stagiul Triaj."""

    route: Route
    category_key: str | None = None
    # NX-116: sloturi structurate normalizate (budget_max/concerns/suitable_for/brand). Owner UNIC
    # = Triaj (din `slots` validate); citit de agent ca seed pentru search_products (hint, P3).
    filters: dict[str, Any] = field(default_factory=dict)
    missing_field: str | None = None
    # A2 (Val1): clientul vrea să CUMPERE ACUM un produs deja discutat („îl iau", „cumpăr",
    # „adaugă în coș"). Triajul (nano, model+context — NU wordlist) îl setează; agentul îl
    # citește ca semnal să onoreze cumpărarea (checkout_link + confirmă stocul), nu să re-recomande.
    purchase_intent: bool = False


@dataclass
class Relevance:
    """Semnal STRUCTURAT de relevanță al retrievalului (izi-parity, hardening). Scris de
    `search_products_tool`, citit de `compose.assemble` ca să SUPRIME „👉 Recomandarea mea" pe
    un rezultat OFF-CATEGORY (produs din categoria greșită) + să pună un mesaj onest de redirect.

    Două semnale independente, deterministe:
      • `category_dropped` — filtrul de CATEGORIE a fost renunțat în scara de relaxare (modelul a
        cerut o categorie inexistentă → search a scos-o ca să iasă ceva). Boolean, robust.
      • `top_cosine` — cea mai mică distanță cosine (cel mai apropiat vector). MARE = semantic
        departe → prinde căutarea free-text FĂRĂ filtru de categorie („fond de ten" pe catalog
        skincare), unde `category_dropped` e False. `None` pe calea lexical-only (fără embeddings).

    Fail-open: absent (`None` pe `RetrievalResult`) ⇒ tratat ca potrivire exactă (fără suprimare) —
    paginare / „mai ieftin" / re-hidratare din state nu-l setează, deci nu declanșează gate-ul."""

    relaxed: bool = False
    category_dropped: bool = False
    top_cosine: float | None = None


@dataclass
class RetrievalResult:
    """Scris DOAR de stagiul de Retrieval/tools. Produse = câmpuri minime."""

    products: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None
    # izi-parity hardening: semnal de relevanță (off-category) → compose suprimă pick-ul + pune
    # mesaj onest de redirect. Fail-open: None ⇒ potrivire exactă (comportament vechi).
    relevance: Relevance | None = None


# ---------------------------------------------------------------------------
# RichReply — recomandare structurată „model iZi" (NX-richreply)
# Separă faptele (din retrieval, hidratate de cod) de raționament (proza LLM).
# LLM-ul emite DOAR cuvinte + referințe `product_id`; codul pune prețuri/rating/
# linkuri. Zero preț/produs inventat PRIN CONSTRUCȚIE. Câmpurile factuale vin
# din `ctx.retrieval`; singurul text LLM per card e `reason` (ancorat pe un pro real).
# ---------------------------------------------------------------------------


@dataclass
class RichItem:
    """Un card îmbogățit. TOATE câmpurile vin din retrieval, EXCEPT `reason`."""

    product_id: str
    name: str
    price: float  # prețul CURENT (efectiv) — pe ăsta îl validează validatorul (nu-i schimba sensul)
    reason: str | None = None  # SINGURUL text LLM per card (fit scurt + pro real), scrubuit
    url: str | None = None
    image: str | None = None
    rating: float | None = None
    review_count: int | None = None  # randat doar dacă > 0 (data-gated)
    badge: str | None = None  # data-gated + guard pe tag de discount (vezi compose)
    # IZI-anchor: prețul ORIGINAL (de listă), randat tăiat pe card DOAR la reducere reală (on_sale).
    # `price` rămâne CURENTUL; `list_price > price` ⇒ reducere. Din date (compose), nu LLM.
    list_price: float | None = None
    # Full-eMAG (contract FE extins): tonul semantic al badge-ului (info/danger/...) pentru
    # `badges:[{label,tone}]`; moneda (DomainPack); `details` = descriere extinsă („Spune-mi mai
    # multe"), din ai_summary (catalog, medical-guarded). Toate din DATE, nu LLM per-tur.
    badge_tone: str | None = None
    currency: str | None = None
    details: str | None = None


@dataclass
class Chip:
    """Sugestie de follow-up tappabilă (Telegram reply-keyboard → trimite `label` ca mesaj nou)."""

    label: str
    payload: str  # token rutat: "chip:cheaper" | "chip:nofrag" | "chip:cmp:<idA>:<idB>"


@dataclass
class RichReply:
    """Recomandarea structurată, NEUTRĂ de canal. Sender-ul o aplatizează în text
    (floor) + o trimite bogat pe canalele care suportă (Telegram `send_rich`)."""

    intro: str | None  # framing LLM (fără cifre/linkuri), scrubuit
    items: list[RichItem]  # asamblate de COD din retrieval, cap 6
    pick: tuple[str, str] | None  # (product_id, justificare) — recomandarea decisivă
    education: str | None  # „ce contează la categoria asta" (LLM), scrubuit
    chips: list[Chip]  # derivate DETERMINIST din retrieval
    disclaimer: str  # constant per-locale


@dataclass
class Offer:
    """NX-114 — ofertă/CTA NEUTRĂ de canal. Emitentul (agent/checkout) setează intenția
    semantică; CUM se randează e exclusiv la margine (NX-60): buton (web), CTA interactiv
    (WhatsApp), buton inline (Telegram). Floor pe canale fără randare bogată = url append-uit
    în text. Owner: stagiul care emite oferta."""

    kind: str  # "checkout" | "open_url" | "quick_reply" | "book"
    label: str  # textul afișat pe buton/CTA (per-locale, vine de la emitent)
    url: str | None = None  # pt checkout/open_url (din catalog/checkout_links, NU inventat)
    payload: str | None = None  # pt quick_reply: token rutat (ex. "offer:reorder") — neutru


# ---------------------------------------------------------------------------
# Comparison — tabel comparativ structurat (model iZi), NEUTRU de canal.
# Construit DETERMINIST din retrieval (compose.build_comparison): fiecare celulă e
# un fapt real (preț/rating/avantaje din recenzii), NICIUN text LLM → zero halucinație
# prin construcție (aceeași garanție ca RichReply). Web îl randează ca tabel; canalele
# fără randare de tabel îl primesc ca floor aplatizat (compose.flatten_comparison).
# ---------------------------------------------------------------------------


@dataclass
class ComparisonColumn:
    """O coloană = un produs comparat. TOATE câmpurile vin din retrieval (P: fapte din date)."""

    product_id: str
    name: str
    price: float  # prețul CURENT (efectiv, ce plătește clientul)
    list_price: float | None = None  # preț original (tăiat) — DOAR la reducere reală (on_sale)
    image: str | None = None
    url: str | None = None
    rating: float | None = None


@dataclass
class ComparisonRow:
    """Un rând = o dimensiune (Preț, Rating, Avantaje, ...) + valoarea per coloană (aliniat 1:1
    cu `columns`). `None` = lipsă (randat „—"), NU zero — un fapt absent nu e inventat."""

    label: str
    values: list[str | None]


@dataclass
class Comparison:
    """Tabel comparativ structurat (2-3 produse), neutru de canal. Owner: stagiul Agent."""

    columns: list[ComparisonColumn]
    rows: list[ComparisonRow]
    # Lead-ul conversațional (framing scurt + verdict derivat din date: cel mai ieftin / cel mai
    # bine cotat). Determinist — fără proză LLM → randat ca text de lead pe web (tabelul îl fac
    # `rows`); pe canalele text intră în `Reply.text` (floor) împreună cu tabelul aplatizat.
    intro: str | None = None


@dataclass
class Reply:
    """Orice stagiu poate seta → early exit la Sender."""

    text: str
    kind: str = "message"  # message | template | typing
    # Carduri de produs (W1): dacă setate, Sender-ul le trimite ca poză+preț+buton
    # după textul de lead-in. Câmpuri compacte (name, price, url, image), nu obiecte.
    products: list[dict[str, Any]] | None = None
    # NX-richreply: recomandare structurată (model iZi). Dacă setată, Sender-ul o
    # randează bogat (Telegram); `text` rămâne aplatizarea ei (floor pt WhatsApp/cache).
    rich: RichReply | None = None
    # IZI-compare: tabel comparativ structurat (2-3 produse). Dacă setat, marginile care îl
    # suportă (web, Capability.COMPARISON) îl randează ca tabel; restul primesc `text` (floor
    # aplatizat). Coexistă cu `products` (cardurile produselor comparate). Owner: stagiul Agent.
    comparison: Comparison | None = None
    # NX-130: slot de clarificare. Dacă setat (reply de tip CLARIFY), processor-ul îl
    # persistă în `conversations.state.pending_question`; orice alt reply îl curăță (None).
    # Owner: stagiul care cere clarificarea (triaj azi). Ref-uri compacte (P8), nu obiecte.
    pending_question: dict[str, Any] | None = None
    # G5b: răspuns reutilizabil pentru cache (False pe clarify/refuz/fallback —
    # specifice contextului, nu se cache-uiesc).
    cacheable: bool = True
    # NX-114: ofertă/CTA neutră de canal (seam channel-aware). Randată bogat la margine
    # (NX-115/127); floor pe canale text = url append-uit la text de `set_offer`. Owner: emitent.
    offer: Offer | None = None
    # Chips de sugestie pe un reply NON-rich (ex. clarify): opțiuni scurte pe care clientul le poate
    # apăsa (voce de client → reintră ca tur nou). Rich-ul are propriile chips (rich.chips).
    suggestions: list[str] = field(default_factory=list)


@dataclass
class Event:
    """Acumulat pentru analytics_events. Owner: stagiile emit, runner-ul scrie."""

    type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnUsage:
    """Consumul LLM al turului (tokeni + cost + defalcări) — observabilitate de cost (NX-103).
    Owner UNIC la scriere: runner-ul (din UsageAccumulator, după pipeline). Citit de processor
    (atașat pe rândul `messages` outbound) și expus de harness-ul de simulare per mesaj.

    `tokens_in` INCLUDE `cached_tokens` (convenția OpenAI). `latency_ms` = wall-clock-ul
    pipeline-ului. `by_stage`/`by_model` = defalcări {nume: {calls, tokens, cost}}."""

    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    savings_usd: float = 0.0  # bani economisiți de prompt caching (tarif plin − cached)
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)  # modelele folosite (pt messages.model_route)
    by_stage: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_event_props(self) -> dict[str, Any]:
        """Forma pentru event-ul `llm_usage` (analytics_events): coloane dedicate tokens_in/out +
        cost_usd extrase de insert_events; restul (cached/savings/defalcări) în properties jsonb."""
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cached_tokens": self.cached_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "savings_usd": round(self.savings_usd, 6),
            "llm_calls": self.calls,
            "by_stage": self.by_stage,
            "by_model": self.by_model,
        }


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
    # seed: processor (din conv.locale); owner refinare per-tur: language_stage (G5c)
    language: str = "ro"
    bot_active: bool = True  # owner: processor (din conversations.bot_active)
    handoff_until: datetime | None = None  # owner: processor (conversations.handoff_until)
    # NX-129: customer_ref verificat (login passthrough web) — id stabil de client din eshop,
    # stabilit la marginea de canal și rezolvat de processor în contact verificat. None = anonim.
    # Owner: processor. Citit de order_gate (poarta de comandă) și de check_order (NX-130: lookup).
    verified_customer_ref: str | None = None
    route: RouteDecision | None = None  # owner: Triaj
    retrieval: RetrievalResult | None = None  # owner: Retrieval
    reply: Reply | None = None  # owner: orice stagiu (early exit)
    halt: bool = False  # owner: Gates (tăcere intenționată — early exit fără reply)
    from_cache: bool = False  # owner: Cache (G5b) — reply servit din cache
    # owner: processor (seed din conversation_summaries, G6-2 felia 2). Rezumatul rolling al
    # conversației lungi (acoperă mesajele de dinaintea ultimelor 8). Citit de context_blocks.
    summary: str | None = None
    # NX-79: mutații de state cerute de tool-uri (ex. cart_add → {"cart": [...]}). Owner UNIC:
    # stagiul Agent (acumulat din `ToolResult.state_patch` în `execute`); processor-ul îl
    # merge-uiește în `new_state` la scriere (P3 — nu se scrie din două locuri).
    state_patch: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    # NX-103: consumul LLM al turului (tokeni/cost/defalcări). Owner: runner-ul (post-pipeline);
    # processor-ul îl atașează pe mesajul outbound. None până rulează pipeline-ul / fără apel LLM.
    usage: TurnUsage | None = None

    def emit(self, type_: str, **properties: Any) -> None:
        """Helper pentru stagii: adaugă un event fără să știe cum e scris.

        NX-122: injectează `turn_id`-ul turului în orice event, fără ca stagiul să
        știe (P10 — observabilitate din runner/context). `setdefault` → nu suprascrie
        un `turn_id` explicit pasat de apelant (P3 — un singur scriitor de turn_id)."""
        properties.setdefault("turn_id", self.turn_id)
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

    def set_rich_reply(
        self,
        rich: RichReply,
        *,
        text: str,
        products: list[dict[str, Any]] | None = None,
        cacheable: bool = False,
    ) -> None:
        """Setează un reply BOGAT (model iZi) → early exit la Sender. `text` = aplatizarea
        deterministă a lui `rich` (floor pt canale fără rich + messages.body + log). `products`
        = cardurile compacte (pt cache signature). `cacheable=False` implicit: răspunsul bogat
        se regenerează (cache-ul ar servi doar textul aplatizat). Owner: stagiul agent."""
        self.reply = Reply(
            text=text, kind="message", products=products, rich=rich, cacheable=cacheable
        )

    def set_comparison_reply(
        self,
        comparison: Comparison,
        *,
        text: str,
        products: list[dict[str, Any]] | None = None,
        chips: list[str] | None = None,
    ) -> None:
        """IZI-compare — setează un reply de COMPARAȚIE (tabel structurat) → early exit la Sender.
        `text` = aplatizarea deterministă a tabelului (floor pt canale fără randare de tabel +
        messages.body + cache). `products` = cardurile compacte ale produselor comparate (→
        `displayed_products`, ca un follow-up „adaugă prima" să le regăsească). `chips` =
        follow-up-uri din partea clientului (voce de client → fără scrub). Non-cacheabil (relativ
        la setul afișat al ACESTUI client). Owner: stagiul agent."""
        self.reply = Reply(
            text=text,
            kind="message",
            products=products,
            comparison=comparison,
            suggestions=chips or [],
            cacheable=False,
        )

    def set_offer(self, offer: Offer) -> None:
        """NX-114 — atașează o ofertă NEUTRĂ de canal pe reply-ul curent. Marginile bogate
        (NX-115/127) o randează ca buton/CTA/inline; un Sender vechi vede FLOOR-ul: dacă
        `offer.url` există, e append-uit la `text` (comportamentul de azi, dar din câmp tipizat,
        nu „scuipat" de LLM). Necesită un reply setat (creează unul gol defensiv altfel)."""
        if self.reply is None:
            self.reply = Reply(text="")
        self.reply.offer = offer
        if offer.url and offer.url not in self.reply.text:
            self.reply.text = f"{self.reply.text}\n{offer.url}".strip()

    def set_clarify(
        self, text: str, *, field: str, resume_route: str, suggestions: list[str] | None = None
    ) -> None:
        """NX-130 — pune o întrebare de clarificare ȘI memorează slotul de umplut la turul
        următor (`pending_question`). Reply NON-cacheabil (specific contextului). `attempts`
        crește dacă re-întrebăm ACELAȘI slot consecutiv (semnal anti-buclă). `suggestions` =
        chips opționale pe care clientul le poate apăsa (ex. idei de cadou). Owner al
        scrierii în DB rămâne Sender (processor propagă `reply.pending_question` în state)."""
        prev = (
            self.state.pending_question if isinstance(self.state.pending_question, dict) else None
        )
        attempts = int(prev.get("attempts") or 0) + 1 if prev and prev.get("field") == field else 1
        pq: dict[str, Any] = {
            "field": field,
            "resume_route": resume_route,
            "asked_at": datetime.now(UTC).isoformat(),
            "attempts": attempts,
        }
        self.reply = Reply(
            text=text, cacheable=False, pending_question=pq, suggestions=suggestions or []
        )
        self.emit("clarify_asked", field=field, attempts=attempts)
