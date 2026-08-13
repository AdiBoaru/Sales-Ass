"""NX-234 — contextul de pagină ca INPUT NEÎNCREZĂTOR: normalizare structurală și policy de
suprafață. Pur: fără DB, fără LLM, fără I/O. Rehidratarea canonică e în
`src/catalog/context_resolver.py`; snapshotul imuabil în `src/worker/turn_snapshot.py`.

**Ce rezolvă cardul.** Frontendul REAL are deja starea paginii și un coș local, deci are tot ce-i
trebuie ca să trimită „produsul curent" cu nume, preț și stoc. În clipa în care backendul ar crede
acele câmpuri, browserul devine al doilea source of truth pentru fapte comerciale — un motor fără
validator, fără test și fără tenant. `PageContextClaim` (NX-228) se numește *claim* exact din
motivul ăsta: e ce AFIRMĂ pagina, nu ce E adevărat.

**Ce trece de aici.** Doar tipul suprafeței și identificatori opaci. Un câmp comercial
(`price`, `stock`, `title`, `cart_items`) e respins cu 422 ÎNAINTE de fingerprint și de pipeline —
`extra="forbid"` din NX-228 îl prinde oricum, dar atunci codul de eroare ar fi generic
(`schema_invalid`) și metrica n-ar spune CE a încercat clientul. `reject_commercial_fields`
transformă exact acel caz într-un motiv stabil, low-cardinality.

**Trei decizii care nu sunt evidente:**

1. **Un ID e opac, dar nu e orice string.** Acceptăm două forme, amândouă unice pe tenant în
   schema reală: UUID-ul canonic (`products.id`) și `external_id`-ul platformei magazinului
   (`unique (business_id, external_id)`) — singurul pe care pagina gazdă chiar îl cunoaște.
   Slug-ul și URL-ul sunt REFUZATE: un URL nu e un identificator, e o cale de scraping DOM
   (explicit out of scope), iar acceptarea lui ar face din breadcrumb un input de ranking.
   Charsetul e restrâns (`[A-Za-z0-9_.:-]`): un ID cu ghilimele/spații nu e un ID.

2. **Suprafața decide ce ID are SENS.** Un `product_id` afirmat pe `home` nu e „context bonus",
   e o ancoră arbitrară pe care nimeni nu a navigat — adică fix mecanismul prin care un host
   (sau un tab compromis) ar putea ancora conversația pe orice produs. Policy: ID-urile care nu
   au sens pe suprafață se IGNORĂ cu reason code, nu se respinge requestul (un host neglijent nu
   e un atacator, iar un 422 aici ar fi fragil).

3. **`variant_id` fără `product_id` nu se rezolvă.** Varianta ar putea fi rezolvată singură (are
   `product_id` în DB), dar atunci relația „varianta aparține produsului paginii" n-ar mai avea
   ce verifica — s-ar deriva chiar din valoarea neîncrezătoare. Fără produs, varianta se ignoră.

Nimic de aici nu e adevăr: `NormalizedContext` conține REFERINȚE, iar faptele apar abia după
rehidratarea tenant-scoped. `business_id` nu apare în niciun câmp și nu se adaugă niciodată (P7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from src.web.contracts_v2 import MAX_OPAQUE_ID_LEN, PageContextClaim, Surface

# ── Câmpuri comerciale: 422 cu motiv, nu „schema invalidă" generică ─────────────────────────
# Lista e a lucrurilor pe care un frontend chiar e tentat să le trimită (are deja valorile în
# pagină). Nu e o listă de blocare exhaustivă — `extra="forbid"` e mecanismul; asta e eticheta.
COMMERCIAL_FIELDS: frozenset[str] = frozenset(
    {
        "price",
        "prices",
        "list_price",
        "sale_price",
        "currency",
        "discount",
        "stock",
        "stock_total",
        "availability",
        "title",
        "name",
        "product_name",
        "description",
        "image",
        "image_url",
        "rating",
        "review_count",
        "badge",
        "badges",
        "cart",
        "cart_items",
        "cart_total",
        "total",
        "criteria",
        "constraints",
        "category",
        "category_name",
        "breadcrumbs",
        "url",
        "page_url",
        "href",
        "html",
        "dom",
    }
)

# Suprafețele Stage 1. Sub-set al enumului NX-228 (`Surface`) — enumul de contract e mai larg
# decât ce știm să rehidratăm, iar diferența se vede aici, nu într-un `if` pierdut.
STAGE1_SURFACES: frozenset[str] = frozenset(
    {"home", "category", "product", "cart", "checkout", "order", "other"}
)

# Ce ancore au SENS pe fiecare suprafață. Un ID absent din set nu invalidează requestul: se
# ignoră cu reason code (vezi docstringul modulului, decizia 2).
SURFACE_ANCHORS: dict[str, frozenset[str]] = {
    "home": frozenset(),
    "category": frozenset({"category_id"}),
    "product": frozenset({"product_id", "variant_id", "category_id"}),
    "cart": frozenset({"cart_ref"}),
    "checkout": frozenset({"cart_ref"}),
    "order": frozenset(),
    "other": frozenset(),
}

# Suprafețele care CER o ancoră ca să fie complet rezolvabile. Lipsa ei nu e o eroare de client
# (o pagină de produs poate randa widgetul înainte să știe id-ul), ci un context `partial`.
SURFACE_REQUIRES_ANCHOR: dict[str, str | None] = {
    "product": "product_id",
    "category": "category_id",
    "cart": "cart_ref",
    "checkout": "cart_ref",
}

# Un ID opac: alfanumerice + separatorii uzuali de platformă. Fără spații, ghilimele, `/`, `%`.
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
# `external_id` din feed poate fi lung, dar nu nelimitat; capul rămâne cel de contract (NX-228).
MAX_CONTEXT_ID_LEN = MAX_OPAQUE_ID_LEN
# Un tag de limbă („ro", „ro-RO", „hu-HU"). Normalizat la subtagul primar, lowercase.
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
MAX_LOCALE_LEN = 35  # BCP-47 practic; capul de contract (`Label`, 60) e mai larg

IdKind = Literal["uuid", "external"]
ContextStatus = Literal["absent", "resolved", "partial", "stale", "invalid", "unavailable"]


class CommercialFieldRejected(ValueError):
    """Browserul a trimis un câmp comercial în `context`. 422 cu motiv stabil, înainte de accept."""

    def __init__(self, field_name: str) -> None:
        super().__init__(f"câmp comercial interzis în context: {field_name!r}")
        self.field_name = field_name


@dataclass(frozen=True)
class ContextRef:
    """O referință opacă NORMALIZATĂ — încă neîncrezătoare. `kind` spune pe ce coloană se va
    căuta (`products.id` vs `products.external_id`), nu că referința ar exista."""

    value: str
    kind: IdKind

    def as_uuid(self) -> str | None:
        return self.value if self.kind == "uuid" else None

    def as_external(self) -> str | None:
        return self.value if self.kind == "external" else None


@dataclass(frozen=True)
class NormalizedContext:
    """Contextul afirmat, curățat structural. ZERO fapte, ZERO lookup — doar forma.

    `reasons` acumulează motivele low-cardinality ale fiecărei decizii (ignorat/respins), în
    ordinea în care s-au luat: intră în evenimente, nu în răspuns. `rejected` e perechea
    (câmp, motiv) pentru tabelul `accepted / rejected / reason` din PR și pentru metrici."""

    surface: str = "other"
    product: ContextRef | None = None
    variant: ContextRef | None = None
    category: ContextRef | None = None
    cart_ref: str | None = None
    locale: str | None = None  # subtagul primar cerut de host; negocierea e server-side
    reasons: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        """Niciun anchor utilizabil — un context absent, nu unul greșit."""
        return not (self.product or self.variant or self.category or self.cart_ref)

    def with_reason(self, reason: str) -> NormalizedContext:
        from dataclasses import replace  # noqa: PLC0415 — helper local, fără cost la import

        return replace(self, reasons=(*self.reasons, reason))


@dataclass
class _Collector:
    """Acumulator mutabil folosit DOAR în interiorul normalizării (rezultatul e frozen)."""

    reasons: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def drop(self, field_name: str, reason: str) -> None:
        self.rejected.append((field_name, reason))
        self.reasons.append(reason)


def reject_commercial_fields(raw_context: Any) -> None:
    """Ridică `CommercialFieldRejected` dacă `context` conține o cheie comercială.

    Se cheamă pe payload-ul BRUT, înainte de `parse_turn_request`: după validarea Pydantic n-ar
    mai exista distincție între „un câmp în plus" și „browserul încearcă să dicteze prețul",
    iar exact a doua e cea pe care vrem s-o vedem în metrici."""
    if not isinstance(raw_context, dict):
        return
    for key in raw_context:
        if str(key).strip().lower() in COMMERCIAL_FIELDS:
            raise CommercialFieldRejected(str(key))


def normalize_id(
    raw: str | None, field_name: str = "id", sink: _Collector | None = None
) -> ContextRef | None:
    """Un ID opac → `ContextRef` sau None + reason. Fără lookup, fără DB, fără excepții.

    `sink` e opțional ca funcția să fie folosibilă și de una singură (teste, drumul de recitire
    din DB): motivul se pierde, decizia rămâne aceeași."""
    sink = sink if sink is not None else _Collector()
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        sink.drop(field_name, "id_empty")
        return None
    if len(value) > MAX_CONTEXT_ID_LEN:
        sink.drop(field_name, "id_too_long")
        return None
    if not _ID_RE.match(value):
        sink.drop(field_name, "id_charset")
        return None
    try:
        # `str(UUID(...))` canonicalizează (lowercase, cu cratime) → aceeași valoare produce
        # același fingerprint indiferent cum a scris-o hostul.
        return ContextRef(str(UUID(value)), "uuid")
    except ValueError:
        return ContextRef(value, "external")


def normalize_locale(raw: str | None, sink: _Collector) -> str | None:
    """`"ro-RO"` → `"ro"`. Doar FORMA: intersecția cu limbile suportate de business e
    server-side (`negotiate_locale`), fiindcă doar acolo se știe ce suportă tenantul."""
    if raw is None:
        return None
    value = raw.strip()
    if not value or len(value) > MAX_LOCALE_LEN or not _LOCALE_RE.match(value):
        sink.drop("locale", "locale_malformed")
        return None
    return value.replace("_", "-").split("-")[0].lower()


def normalize_context(
    claim: PageContextClaim | None,
    *,
    allowed_surfaces: frozenset[str] = STAGE1_SURFACES,
) -> NormalizedContext:
    """`PageContextClaim` (neîncrezător) → `NormalizedContext` (curățat structural).

    PUR și TOTAL: nu ridică niciodată pe un claim valid ca schemă. Orice valoare de care nu ne
    putem atinge fără lookup se ignoră cu reason code — decizia „e sau nu adevărat" e a
    rehidratării, nu a normalizării."""
    sink = _Collector()
    if claim is None:
        return NormalizedContext(surface="other", reasons=("context_absent",))

    surface = claim.surface if claim.surface in allowed_surfaces else "other"
    if surface != claim.surface:
        sink.drop("surface", "surface_not_supported")
    anchors = SURFACE_ANCHORS.get(surface, frozenset())

    def _for_surface(raw: str | None, name: str) -> ContextRef | None:
        if raw is None:
            return None
        if name not in anchors:
            sink.drop(name, "id_not_meaningful_for_surface")
            return None
        return normalize_id(raw, name, sink)

    product = _for_surface(claim.product_id, "product_id")
    variant = _for_surface(claim.variant_id, "variant_id")
    category = _for_surface(claim.category_id, "category_id")
    cart = _for_surface(claim.cart_ref, "cart_ref")

    if variant is not None and product is None:
        # Decizia 3 din docstring: fără produs nu există relația de verificat.
        sink.drop("variant_id", "variant_without_product")
        variant = None

    return NormalizedContext(
        surface=surface,
        product=product,
        variant=variant,
        category=category,
        cart_ref=cart.value if cart is not None else None,
        locale=normalize_locale(claim.locale, sink),
        reasons=tuple(sink.reasons),
        rejected=tuple(sink.rejected),
    )


def negotiate_locale(
    requested: str | None, *, supported: list[str] | tuple[str, ...], default: str
) -> tuple[str, str | None]:
    """Limba efectivă a turului + motivul, ca DECIZIE SERVER-OWNED (D3, P11).

    Intersecția dintre ce cere hostul și ce suportă tenantul. Necunoscut/nesuportat → default-ul
    businessului, DETERMINIST — niciodată copy ales de frontend și niciodată limba brută afirmată
    (un cache hit în limba greșită e un bug, nu un hit)."""
    fallback = (default or "ro").split("-")[0].lower()
    pool = {str(loc).split("-")[0].lower() for loc in (supported or ()) if loc}
    pool.add(fallback)
    if requested is None:
        return fallback, None
    if requested in pool:
        return requested, None
    return fallback, "locale_unsupported"


# ── Forma canonică: fingerprint (NX-232) și persistare (messages.payload) ────────────────────


def fingerprint_context(normalized: NormalizedContext) -> dict[str, Any]:
    """Forma canonică SAFE a contextului pentru HMAC-ul de idempotency (NX-232).

    Intră doar ce SCHIMBĂ înțelesul turului: suprafața și ancorele. Consecința e cerută explicit
    de failure matrix — „refresh pe altă pagină, același turn ID" trebuie să fie conflict de
    fingerprint (409), nu un turn vechi cu context nou lipit pe el. Motivele/rejecturile NU intră:
    sunt observabilitate, nu identitate de request."""
    out: dict[str, Any] = {"surface": normalized.surface}
    if normalized.product is not None:
        out["product"] = normalized.product.value
    if normalized.variant is not None:
        out["variant"] = normalized.variant.value
    if normalized.category is not None:
        out["category"] = normalized.category.value
    if normalized.cart_ref:
        out["cart_ref"] = normalized.cart_ref
    if normalized.locale:
        out["locale"] = normalized.locale
    return out


def to_payload(normalized: NormalizedContext) -> dict[str, Any] | None:
    """Forma PERSISTABILĂ (jsonb pe mesajul inbound al turului), sau None dacă nu e nimic de
    ținut minte. Durabilitatea e cerința de recovery a NX-233: executorul re-construiește turul
    EXCLUSIV din DB, deci un context care trăiește doar în requestul HTTP ar dispărea la primul
    restart. Zero PII, zero fapte — aceleași ID-uri opace pe care le-a trimis browserul.

    Motivele se persistă ca să nu se re-deducă la execuție (aceeași decizie, aceeași etichetă)."""
    if normalized.surface == "other" and normalized.is_empty and not normalized.locale:
        return None
    out: dict[str, Any] = {"v": 1, "surface": normalized.surface}
    for key, ref in (
        ("product", normalized.product),
        ("variant", normalized.variant),
        ("category", normalized.category),
    ):
        if ref is not None:
            out[key] = {"id": ref.value, "kind": ref.kind}
    if normalized.cart_ref:
        out["cart_ref"] = normalized.cart_ref
    if normalized.locale:
        out["locale"] = normalized.locale
    if normalized.reasons:
        out["reasons"] = list(normalized.reasons)
    return out


def from_payload(raw: Any) -> NormalizedContext | None:
    """Citirea inversă, DEFENSIVĂ (ca `ConversationState.from_jsonb`): un payload vechi, parțial
    sau stricat produce None sau un context redus — niciodată o excepție pe hot path. Ce se
    citește înapoi trece din nou prin `normalize_id`: DB-ul nostru e sursă de durabilitate, nu de
    încredere (un rând editat manual nu devine adevăr)."""
    if not isinstance(raw, dict):
        return None
    sink = _Collector()
    surface = raw.get("surface")
    surface = surface if isinstance(surface, str) and surface in STAGE1_SURFACES else "other"

    def _ref(key: str) -> ContextRef | None:
        node = raw.get(key)
        if not isinstance(node, dict):
            return None
        return normalize_id(str(node.get("id") or "") or None, key, sink)

    cart = raw.get("cart_ref")
    locale = raw.get("locale")
    reasons = tuple(str(r) for r in (raw.get("reasons") or []) if isinstance(r, str))
    ctx = NormalizedContext(
        surface=surface,
        product=_ref("product"),
        variant=_ref("variant"),
        category=_ref("category"),
        cart_ref=str(cart) if isinstance(cart, str) and cart.strip() else None,
        locale=normalize_locale(locale if isinstance(locale, str) else None, sink),
        reasons=(*reasons, *sink.reasons),
        rejected=tuple(sink.rejected),
    )
    if ctx.surface == "other" and ctx.is_empty and not ctx.locale:
        return None
    return ctx


def missing_anchor(normalized: NormalizedContext) -> str | None:
    """Ancora pe care suprafața o cere și requestul n-o are. None = nimic de reproșat."""
    required = SURFACE_REQUIRES_ANCHOR.get(normalized.surface)
    if required is None:
        return None
    present = {
        "product_id": normalized.product is not None,
        "category_id": normalized.category is not None,
        "cart_ref": bool(normalized.cart_ref),
    }
    return None if present.get(required) else required


__all__ = [
    "COMMERCIAL_FIELDS",
    "MAX_CONTEXT_ID_LEN",
    "SURFACE_ANCHORS",
    "SURFACE_REQUIRES_ANCHOR",
    "CommercialFieldRejected",
    "ContextRef",
    "ContextStatus",
    "NormalizedContext",
    "Surface",
    "fingerprint_context",
    "from_payload",
    "missing_anchor",
    "negotiate_locale",
    "normalize_context",
    "reject_commercial_fields",
    "to_payload",
]
