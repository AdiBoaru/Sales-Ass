"""NX-228 — Contractul `web-turn.v2` / `web-view.v2`: backendul livrează un ViewModel
**display-ready**, frontendul îl randează cu `switch(block.type)` și atât.

Pur: fără DB, fără LLM, fără I/O. Inert în acest card — nicio rută nu îl servește încă
(vine în NX-232/233). V1 (`src/channels/web/render.py` + `docs/FRONTEND-CONTRACT-IZI.md`)
rămâne NEATINS până la cutoverul NX-249.

**De ce există.** În v1 frontendul e un al doilea motor: își calculează procentul de reducere
(`ChatProductCard.jsx:266`), își ghicește tonul unui badge dintr-un regex peste cuvinte
românești (`inferBadgeTone`, `:64`), își parsează prețurile din string cu euristici de virgulă
(`chatClient.js:103`), alege comportamentul CTA-ului după `offer.kind` (`ChatOffer.jsx:29`),
compune mesaje din numele produsului (`:304`) și acumulează criterii de conversație (`:404`).
Fiecare dintre ele e o regulă comercială care trăiește în două locuri și diverge în tăcere.

**Regula pe care o impune tipul, nu disciplina.** Tot ce e afișabil e deja `str` localizat:
prețul e `"89,00 lei"`, nu `89.0`; reducerea e `"-18%"`, nu doi termeni de scăzut; stocul e
`"Ultimele 3 bucăți"`, nu `3`. Un `float` în contract e o invitație la aritmetică în browser,
deci nu există niciun `float` în ViewModel. Cifrele rămân în backend, unde există validator.

**Trei invarianți structurali (nu convenții):**
  1. `extra="forbid"` peste tot + union discriminat FINIT → un `block.type` necunoscut sau un
     câmp în plus respinge ÎNTREG payloadul. Fără skip/omit best-effort: un renderer care sare
     peste ce nu înțelege afișează un răspuns pe jumătate și îl numește succes.
  2. Terminalele `completed|failed|cancelled` au minimum un bloc randabil, impus de
     `model_validator`. P6: nicio cale terminală nu produce tăcere.
  3. Copy-ul de chrome/a11y e obligatoriu și non-blank. Un label lipsă nu e „FE-ul pune ceva
     implicit", e contract invalid — altfel microcopy-ul comercial se întoarce în browser pe
     ușa din dos.

**Untrusted by default.** `PageContextClaim` se numește *claim*, nu *context*: e ce AFIRMĂ
browserul. Niciun câmp din el nu e adevăr până la rehidratarea server-side din NX-234, iar
`business_id` nu apare în request deloc — e server-owned (P7), niciodată din browser.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ── Versiuni ────────────────────────────────────────────────────────────────────────────────
# Major-ul e în NUME, nu într-un câmp numeric: un client care nu cunoaște `web-view.v2` nu are
# ce „interpreta best-effort", ci refuză contractul (vezi `parse_view`).
TURN_SCHEMA_VERSION = "web-turn.v2"
VIEW_SCHEMA_VERSION = "web-view.v2"


# ── Limite (impuse în COD, nu în prompt — P4) ───────────────────────────────────────────────
MAX_MESSAGES = 4
MAX_BLOCKS_PER_MESSAGE = 12
MAX_PRODUCT_ITEMS = 6  # aceeași limită ca tool results (CLAUDE.md: max 6 produse)
MAX_COMPARISON_COLUMNS = 3
MAX_COMPARISON_ROWS = 12
MAX_ACTIONS_PER_ROW = 4  # `_MAX_WEB_CHIPS` din render.py v1 — widgetul nu trebuie să pară încărcat
MAX_ACTIONS_PER_ITEM = 3
MAX_KEY_VALUE_ROWS = 12
MAX_STATUS_ITEMS = 8
MAX_ROUTINE_STEPS = 8
MAX_MEMORY_CRITERIA = 8
MAX_CART_LINES = 20
MAX_BADGES = 3

MAX_TEXT_LEN = 2000
MAX_TITLE_LEN = 200
MAX_LABEL_LEN = 60
MAX_ACTION_LABEL_LEN = 40  # `_MAX_WEB_CHIP_LEN` v1: un chip e o etichetă, nu o propoziție
MAX_VALUE_LEN = 200
MAX_URL_LEN = 2048
MAX_TOKEN_LEN = 4096  # token opac semnat (NX-236); FE nu îl citește, doar îl retransmite
MAX_OPAQUE_ID_LEN = 128


# ── Primitive de string ─────────────────────────────────────────────────────────────────────
# `strip_whitespace` ÎNAINTE de `min_length` → `" "` e respins ca `""`. Un label „gol deghizat"
# ar trece de un `min_length=1` naiv și ar produce un buton fără nume.
def _bounded(max_length: int) -> Any:
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)
    ]


Text = _bounded(MAX_TEXT_LEN)
Title = _bounded(MAX_TITLE_LEN)
Label = _bounded(MAX_LABEL_LEN)
ActionLabel = _bounded(MAX_ACTION_LABEL_LEN)
Value = _bounded(MAX_VALUE_LEN)
OpaqueId = _bounded(MAX_OPAQUE_ID_LEN)
OpaqueToken = _bounded(MAX_TOKEN_LEN)


# ── Vocabulare ÎNCHISE ──────────────────────────────────────────────────────────────────────
# `tone` e semantic, nu cromatic: backendul spune CE ÎNSEAMNĂ, FE alege culoarea. FE-ul canonic
# cunoaște în plus `promo`; v2 nu îl emite — se adaugă printr-un minor cu schema hash negociat,
# nu prin „merge oricum, are fallback".
Tone = Literal["neutral", "info", "success", "warning", "danger"]

# `appearance` e rolul vizual al unui control, nu clasa lui CSS.
Appearance = Literal["primary", "secondary", "chip", "link", "danger"]

# Allowlist de iconuri = exact cele pe care rendererul canonic le are mapate
# (`ChatProductCard.jsx:73`). Backendul nu are voie să ceară un icon inexistent: ar produce un
# rând mut. Niciodată SVG sau URL — un icon e un NUME, nu conținut executabil.
Icon = Literal["truck", "tag", "percent", "shield", "clock", "gift", "info", "check", "alert"]

TextVariant = Literal["lead", "body", "caption", "disclosure"]
NoticeLevel = Literal["info", "success", "warning", "error"]

# Statusul de sârmă, exact cele șase. `turn.status`, NU `status` la top level: în v1 `status` e
# stocul unui produs, iar două înțelesuri pe același nume pe același canal e o capcană.
TurnStatus = Literal["accepted", "working", "validating", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Suprafața paginii gazdă — AFIRMATĂ de browser, adevăr abia după rehidratarea NX-234.
Surface = Literal["home", "category", "product", "cart", "checkout", "order", "other"]


# ── URL: allowlist de scheme ────────────────────────────────────────────────────────────────
# `https` absolut sau rută relativă („/p/ser-x"). Explicit INTERZISE: `javascript:`, `data:`,
# `file:`, `vbscript:` și `http` în clar. Verificarea e pe VALOAREA parsată, nu pe un regex de
# blocare: o listă de lucruri interzise se ocolește, o listă de lucruri permise nu.
_FORBIDDEN_URL_PREFIXES = ("javascript:", "data:", "file:", "vbscript:", "blob:", "about:")


def _validate_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("URL gol")
    if len(value) > MAX_URL_LEN:
        raise ValueError(f"URL peste {MAX_URL_LEN} caractere")
    lowered = value.lower()
    # `\` și whitespace intern sunt vectori clasici de bypass ("java\nscript:"); le respingem
    # înainte de orice comparație de prefix.
    if any(ch.isspace() for ch in value) or "\\" in value:
        raise ValueError("URL cu whitespace sau backslash")
    if lowered.startswith(_FORBIDDEN_URL_PREFIXES):
        raise ValueError(f"schemă de URL interzisă: {value[:24]!r}")
    if value.startswith("//"):
        # Protocol-relative: moștenește schema paginii gazdă, deci poate ajunge `http:`.
        raise ValueError("URL protocol-relativ (//) nu e permis")
    if value.startswith("/"):
        return value
    if lowered.startswith("https://"):
        return value
    raise ValueError("URL trebuie să fie https:// absolut sau rută relativă /…")


SafeUrl = Annotated[str, Field(max_length=MAX_URL_LEN)]


class _Base(BaseModel):
    """Bază comună: `extra="forbid"` peste tot. Un câmp în plus nu e ignorat, e eroare — altfel
    un backend mai nou poate „strecura" semantică pe care rendererul nu o afișează."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ── Request (browser → backend) ─────────────────────────────────────────────────────────────
class TextInput(_Base):
    type: Literal["text"]
    text: Text


class ActionInput(_Base):
    """Retrimiterea NESCHIMBATĂ a unui token opac. FE nu îl decodează, nu îl compune, nu îl
    completează — dacă ar putea, ar deveni din nou un motor de intenții (NX-236)."""

    type: Literal["action"]
    action_token: OpaqueToken


TurnInput = Annotated[TextInput | ActionInput, Field(discriminator="type")]


class PageContextClaim(_Base):
    """Ce AFIRMĂ pagina gazdă despre unde se află clientul. ID-uri opace, niciodată fapte:
    zero preț, zero nume, zero stoc, zero stare de conversație. NX-234 rehidratează server-side;
    până atunci fiecare câmp e neîncredere, inclusiv `locale`.

    `business_id` lipsește intenționat și nu se adaugă niciodată aici (P7)."""

    surface: Surface = "other"
    product_id: OpaqueId | None = None
    variant_id: OpaqueId | None = None
    category_id: OpaqueId | None = None
    cart_ref: OpaqueId | None = None
    locale: Label | None = None


class WebTurnRequestV2(_Base):
    """Un tur cerut de browser. `client_turn_id` e OBLIGATORIU: e cheia de idempotency pe care
    NX-232 întoarce exact același rezultat fără al doilea apel LLM. Fără el nu există replay,
    deci nu există recovery la refresh."""

    schema_version: Literal["web-turn.v2"]
    client_turn_id: UUID
    input: TurnInput
    context: PageContextClaim = PageContextClaim()
    id_token: OpaqueToken | None = None


# ── Bucăți de view ──────────────────────────────────────────────────────────────────────────
class ImageView(_Base):
    src: SafeUrl
    alt: Label  # obligatoriu: o poză fără alt e o poză invizibilă pentru cititorul de ecran

    @model_validator(mode="after")
    def _check_url(self) -> ImageView:
        _validate_url(self.src)
        return self


class PriceView(_Base):
    """Prețuri ca TEXT deja formatat și localizat. `discount` vine gata calculat de backend —
    în v1 frontendul îl deriva singur (`Math.round(((list-price)/list)*100)`), adică regula de
    reducere trăia în browser, fără validator și fără test."""

    current: Value
    previous: Value | None = None
    discount: Value | None = None


class BadgeView(_Base):
    label: Label
    tone: Tone = "neutral"


class NavigateActivation(_Base):
    """Navigare către un href DEJA validat de backend."""

    type: Literal["navigate"]
    href: SafeUrl
    target: Literal["_self", "_blank"] = "_self"

    @model_validator(mode="after")
    def _check_url(self) -> NavigateActivation:
        _validate_url(self.href)
        return self


# NX-236 e PRODUCĂTORUL tokenului (envelope sigilat AES-SIV, `src/web/action_crypto.py`, emis din
# planul persistat al turului-sursă). Aici rămâne DOAR forma: un string mărginit. Nota stă într-un
# COMENTARIU, nu în docstring, fiindcă docstringul intră ca `description` în JSON Schema — iar
# `schema_hash()` e moneda negocierii de capabilitate: nu se schimbă pentru o explicație.
class SubmitActivation(_Base):
    """Creează un tur nou trimițând înapoi tokenul opac. Ce înseamnă acțiunea știe DOAR
    backendul: labelul e pentru ochi, tokenul e pentru mașină. În v1 chips-urile își pierdeau
    `Chip.payload` (`render.py:178`) și rămânea doar textul — adică semantica se recupera
    ghicind din etichetă."""

    type: Literal["submit"]
    token: OpaqueToken


Activation = Annotated[NavigateActivation | SubmitActivation, Field(discriminator="type")]


class ActionView(_Base):
    """Ce vede și apasă utilizatorul. Separat de tokenul intern: FE știe eticheta, aspectul și
    faptul că e activabil — nu și ce va face serverul."""

    id: OpaqueId
    label: ActionLabel
    appearance: Appearance = "secondary"
    icon: Icon | None = None
    enabled: bool = True
    activation: Activation


class ProductItemView(_Base):
    """Un produs gata de afișat. `view_id` e identitatea în VIEW (pentru `key` și pentru
    referința „primul"), nu `product_id`: ID-ul de catalog nu are ce căuta în browser dacă
    singurul lucru pe care FE îl face cu el e să-l trimită înapoi — pentru asta există tokenul
    acțiunii."""

    view_id: OpaqueId
    title: Title
    subtitle: Value | None = None
    image: ImageView | None = None
    price: PriceView | None = None
    rating: Value | None = None  # „4,8 din 5 (120 recenzii)" — text, nu float + număr de derivat
    availability: Value | None = None  # „În stoc" / „Ultimele 3" — backendul interpretează cifra
    reason: Text | None = None
    badges: list[BadgeView] = Field(default_factory=list, max_length=MAX_BADGES)
    actions: list[ActionView] = Field(default_factory=list, max_length=MAX_ACTIONS_PER_ITEM)


class ComparisonCell(_Base):
    """Celulă deja localizată. `text=None` ⇒ backendul spune explicit «necunoscut», iar FE
    randează placeholderul lui — nu îl confundă cu „0" sau cu gol."""

    text: Value | None = None
    tone: Tone | None = None


class ComparisonRow(_Base):
    label: Label
    cells: list[ComparisonCell]


class KeyValueRow(_Base):
    label: Label
    value: Value


class StatusItem(_Base):
    label: Label
    detail: Value | None = None
    tone: Tone = "neutral"
    icon: Icon | None = None
    freshness: Value | None = None  # „verificat acum 2 minute" — text, nu timestamp de formatat


class RoutineStep(_Base):
    title: Title
    detail: Text | None = None


class CartLineView(_Base):
    view_id: OpaqueId
    title: Title
    quantity: Value  # „2 buc." — text; FE nu înmulțește nimic
    price: PriceView | None = None


# ── Blocuri (union discriminat FINIT) ───────────────────────────────────────────────────────
class TextBlock(_Base):
    id: OpaqueId
    type: Literal["text"]
    variant: TextVariant = "body"
    text: Text


class ProductListBlock(_Base):
    id: OpaqueId
    type: Literal["product_list"]
    items: list[ProductItemView] = Field(min_length=1, max_length=MAX_PRODUCT_ITEMS)


class ComparisonBlock(_Base):
    """Tabel gata calculat. FE nu decide câștigătorul, nu compară prețuri, nu evidențiază
    diferențe: `cells` sunt aliniate 1:1 cu `headers` și atât."""

    id: OpaqueId
    type: Literal["comparison"]
    headers: list[Label] = Field(min_length=2, max_length=MAX_COMPARISON_COLUMNS)
    rows: list[ComparisonRow] = Field(min_length=1, max_length=MAX_COMPARISON_ROWS)

    @model_validator(mode="after")
    def _cells_align(self) -> ComparisonBlock:
        for i, row in enumerate(self.rows):
            if len(row.cells) != len(self.headers):
                raise ValueError(
                    f"rows[{i}] are {len(row.cells)} celule, headers are {len(self.headers)}"
                )
        return self


class KeyValueBlock(_Base):
    id: OpaqueId
    type: Literal["key_value"]
    title: Title | None = None
    rows: list[KeyValueRow] = Field(min_length=1, max_length=MAX_KEY_VALUE_ROWS)


class StatusListBlock(_Base):
    id: OpaqueId
    type: Literal["status_list"]
    items: list[StatusItem] = Field(min_length=1, max_length=MAX_STATUS_ITEMS)


class RoutineBlock(_Base):
    id: OpaqueId
    type: Literal["routine"]
    title: Title | None = None
    steps: list[RoutineStep] = Field(min_length=1, max_length=MAX_ROUTINE_STEPS)


class NoticeBlock(_Base):
    """No-result, fallback, recovery, eroare de contract. Blocul care face P6 posibil: orice
    terminal are cel puțin ASTA de afișat."""

    id: OpaqueId
    type: Literal["notice"]
    level: NoticeLevel
    title: Title | None = None
    text: Text
    actions: list[ActionView] = Field(default_factory=list, max_length=MAX_ACTIONS_PER_ROW)


class MemoryBlock(_Base):
    """SNAPSHOT display-ready al criteriilor active, nu istoric append-only. În v1 widgetul își
    acumula singur criteriile (`ChatWidget.jsx:404`) — o a doua memorie, care diverge de a
    serverului la primul refresh."""

    id: OpaqueId
    type: Literal["memory"]
    title: Title | None = None
    criteria: list[Label] = Field(min_length=1, max_length=MAX_MEMORY_CRITERIA)


class CartSummaryBlock(_Base):
    """Snapshot server-owned. Se emite abia după NX-237: până există coș canonic + receipt,
    asistentul nu are voie să pretindă că a mutat ceva."""

    id: OpaqueId
    type: Literal["cart_summary"]
    title: Title | None = None
    lines: list[CartLineView] = Field(default_factory=list, max_length=MAX_CART_LINES)
    total: PriceView | None = None
    actions: list[ActionView] = Field(default_factory=list, max_length=MAX_ACTIONS_PER_ROW)


class ActionRowBlock(_Base):
    id: OpaqueId
    type: Literal["action_row"]
    actions: list[ActionView] = Field(min_length=1, max_length=MAX_ACTIONS_PER_ROW)


class DividerBlock(_Base):
    id: OpaqueId
    type: Literal["divider"]


Block = Annotated[
    TextBlock
    | ProductListBlock
    | ComparisonBlock
    | KeyValueBlock
    | StatusListBlock
    | RoutineBlock
    | NoticeBlock
    | MemoryBlock
    | CartSummaryBlock
    | ActionRowBlock
    | DividerBlock,
    Field(discriminator="type"),
]

# Blocurile care nu poartă conținut nu pot ține singure un terminal: un `divider` nu e un
# răspuns. Contează pentru invariantul P6, nu pentru randare.
_CONTENTLESS_BLOCK_TYPES: frozenset[str] = frozenset({"divider"})


# ── Envelope ────────────────────────────────────────────────────────────────────────────────
class MessageView(_Base):
    id: OpaqueId
    role: Literal["assistant", "user"]
    blocks: list[Block] = Field(min_length=1, max_length=MAX_BLOCKS_PER_MESSAGE)


class ConversationView(_Base):
    id: OpaqueId
    revision: int = Field(ge=0)


class TurnView(_Base):
    id: OpaqueId
    client_turn_id: UUID
    status: TurnStatus


class ProgressView(_Base):
    """Stare REALĂ, server-owned, pentru stările neterminale. Fără chain-of-thought și fără
    etape inventate: dacă serverul nu spune nimic, FE nu simulează progres cu un timer."""

    label: Label
    detail: Value | None = None


class ComposerView(_Base):
    """Copy-ul câmpului de input. `enabled=False` e single-flight-ul din NX-243/245: input
    inactiv până la terminal, decis de server, nu de un flag local."""

    enabled: bool
    label: Label
    placeholder: Label
    send_label: ActionLabel


class ChromeView(_Base):
    """Numele accesibile ale shell-ului. Obligatorii toate: un label lipsă ar însemna că FE
    compune text — exact ce interzice boundary-ul."""

    launcher_label: Label
    dialog_title: Title
    dialog_description: Value
    close_label: Label
    new_chat_label: Label


class AnnouncementsView(_Base):
    """Câte un anunț pentru FIECARE status de sârmă. Nu sunt opționale: live region-ul din
    NX-245 le citește direct, iar un status fără anunț ar fi tăcere pentru cine nu vede
    ecranul."""

    accepted: Value
    working: Value
    validating: Value
    completed: Value
    failed: Value
    cancelled: Value


class A11yView(_Base):
    announcements: AnnouncementsView


class ErrorView(_Base):
    """`code` e STABIL și low-cardinality (intră în metrici); `message` e pentru om.
    `retry_action` e tot un token opac — reîncercarea rămâne o acțiune server-owned."""

    code: Label
    message: Text
    retryable: bool = False
    retry_action: ActionView | None = None


class WebViewV2(_Base):
    """Envelope-ul terminal `web-view.v2`.

    Invariantul P6 e impus de tip: `completed|failed|cancelled` cer minimum un bloc cu
    conținut. Un terminal gol nu se poate construi, deci nu se poate livra."""

    schema_version: Literal["web-view.v2"]
    conversation: ConversationView
    turn: TurnView
    messages: list[MessageView] = Field(default_factory=list, max_length=MAX_MESSAGES)
    progress: ProgressView | None = None
    composer: ComposerView
    chrome: ChromeView
    a11y: A11yView
    error: ErrorView | None = None

    @model_validator(mode="after")
    def _terminal_is_renderable(self) -> WebViewV2:
        if self.turn.status in TERMINAL_STATUSES:
            renderable = sum(
                1 for m in self.messages for b in m.blocks if b.type not in _CONTENTLESS_BLOCK_TYPES
            )
            if renderable == 0:
                raise ValueError(
                    f"status {self.turn.status!r} e terminal, dar nu există niciun bloc "
                    "randabil (P6: nicio cale terminală nu produce tăcere)"
                )
        return self

    @model_validator(mode="after")
    def _progress_only_while_working(self) -> WebViewV2:
        if self.progress is not None and self.turn.status in TERMINAL_STATUSES:
            raise ValueError("`progress` nu are sens pe un status terminal")
        return self

    @model_validator(mode="after")
    def _error_only_on_failure(self) -> WebViewV2:
        if self.error is not None and self.turn.status != "failed":
            raise ValueError("`error` se emite doar cu status 'failed'")
        if self.error is None and self.turn.status == "failed":
            raise ValueError("status 'failed' cere `error` cu un cod stabil")
        return self


# ── JSON Schema + hash + negociere ──────────────────────────────────────────────────────────
def view_json_schema() -> dict[str, Any]:
    """JSON Schema al envelope-ului. Determinist pentru o definiție de model dată — testul de
    snapshot prinde orice schimbare accidentală de formă."""
    return WebViewV2.model_json_schema()


def turn_json_schema() -> dict[str, Any]:
    return WebTurnRequestV2.model_json_schema()


def _canonical(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_hash(schema: dict[str, Any] | None = None) -> str:
    """Hash stabil peste JSON Schema canonic. E moneda negocierii de capabilitate: serverul nu
    livrează un schema pe care clientul nu l-a acceptat, iar un rollout aditiv devine vizibil
    (hash-ul se schimbă) în loc să apară ca un câmp misterios într-un renderer vechi."""
    return hashlib.sha256(_canonical(schema or view_json_schema()).encode("utf-8")).hexdigest()


def negotiate_schema(accepted_hashes: list[str] | tuple[str, ...] | None) -> str:
    """Alege schema-ul de livrat pentru capabilitățile anunțate de client.

    Fără listă (client vechi/necunoscut) livrăm schema-ul curent — e singurul pe care îl
    cunoaștem. Cu listă care NU conține hash-ul curent, ridicăm: mai bine o eroare tehnică
    decât un payload pe care rendererul îl va respinge oricum, după ce l-am persistat ca livrat.
    """
    current = schema_hash()
    if not accepted_hashes:
        return current
    if current not in accepted_hashes:
        raise ValueError(
            f"niciun schema comun: serverul are {current[:12]}…, clientul acceptă "
            f"{[h[:12] for h in accepted_hashes]}"
        )
    return current


def parse_view(payload: dict[str, Any]) -> WebViewV2:
    """Punctul unic de validare al unui envelope. Un `schema_version` necunoscut e refuzat aici,
    ÎNAINTE de orice încercare de interpretare — „e aproape v2, hai să citim ce înțelegem" e
    exact modul în care un contract moare."""
    version = payload.get("schema_version")
    if version != VIEW_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version necunoscut: {version!r} (serverul vorbește {VIEW_SCHEMA_VERSION!r})"
        )
    return WebViewV2.model_validate(payload)


def parse_turn_request(payload: dict[str, Any]) -> WebTurnRequestV2:
    version = payload.get("schema_version")
    if version != TURN_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version necunoscut: {version!r} (serverul vorbește {TURN_SCHEMA_VERSION!r})"
        )
    return WebTurnRequestV2.model_validate(payload)
