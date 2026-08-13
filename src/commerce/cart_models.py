"""NX-237 — coșul canonic al conversației: comenzi typed, fapte cu sursă, snapshot versionat.

Până acum coșul trăia în `ConversationState.cart` ca listă de linii cu PREȚ COPIAT la momentul
adăugării. Consecința nu e teoretică: un preț schimbat în catalog după `cart_add` rămânea în stare
și ajungea în checkout; un retry putea dubla o linie; iar frontendul avea propriul cart în
localStorage — trei „adevăruri" care nu se puteau valida reciproc.

Aici forma repară exact asta, prin tip, nu prin disciplină:

  • **`CartCommand`** — inputul UNIC al oricărei mutații (action click SAU tool LLM): referințe și
    cantități mărginite, NICIODATĂ preț/nume/discount. Ce nu se normalizează curat nu intră.
  • **`CommerceFacts`** — faptele comerciale ale unei linii, rehidratate din catalog LA MOMENTUL
    mutației/citirii, cu sursă și prospețime. `UNKNOWN ≠ 0` (D8): un stoc necunoscut nu e stoc
    zero, un rating fără recenzii nu e „0 stele", o promoție fără regulă canonică NU există.
  • **`CartSnapshot`** — proiecția display-ready, calculată server-side (totaluri, display strings).
    Un preț necunoscut pe orice linie ⇒ total UNKNOWN și checkout omis, nu o sumă inventată.
  • **`MutationReceipt`** — dovada unei mutații: idempotency key, versiune înainte/după, cod de
    rezultat. Retry-ul cu aceeași cheie primește ACELAȘI receipt (replay), nu o a doua mutație.

Modulul e PUR: fără DB, fără LLM, fără ceas (primește `now`), fără random. Persistența e în
`db/queries/carts.py`, orchestrarea în `cart_service.py`, hidratarea în `facts_provider.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from src.catalog.context_resolver import Freshness

# ── Caps (P4 — bugetul e impus în cod, nu în prompturi) ─────────────────────────────────────
CART_MAX_LINES = 10  # aliniat cu CheckoutArgs.max_length (legacy) și cu plafonul de checkout
CART_MAX_LINE_QUANTITY = 10  # aliniat cu MAX_QUANTITY din action_models (o comandă de chat,
#                              nu un B2B bulk order; peste asta e operator uman, nu buton)
MAX_REF_LEN = 64  # id opac de produs/variantă (uuid canonic sau cheia platformei)

CartOperation = Literal["add", "set_quantity", "remove", "clear", "checkout"]
CART_OPERATIONS: frozenset[str] = frozenset({"add", "set_quantity", "remove", "clear", "checkout"})

# Vocabular ÎNCHIS de rezultate (intră în receipts + metrici; low-cardinality, P10/P12).
CART_ERROR_CODES: frozenset[str] = frozenset(
    {
        "cart_disabled",  # kill-switch OFF — refuz onest, zero scriere
        "product_not_found",  # inexistent / inactiv / al altui tenant (aceeași semantică — P7)
        "variant_not_found",  # varianta nu există pe ACEST produs (membership verificat)
        "safety_excluded",  # NX-173: poarta de mutație a refuzat ÎNAINTE de scriere
        "out_of_stock",  # availability='out_of_stock'/'discontinued' — nu adăugăm ce nu există
        "availability_unknown",  # sursa de stoc nu susține un „în stoc" — nu presupunem
        "insufficient_stock",  # stoc CUNOSCUT < cantitatea cerută (bounded reject, explicat)
        "quantity_invalid",  # în afara [1, CART_MAX_LINE_QUANTITY]
        "cart_full",  # CART_MAX_LINES atins — linie nouă refuzată, nu tăiată tăcut
        "cart_not_active",  # checked_out/expirat — nu se reînvie tăcut (failure matrix)
        "line_not_found",  # set_quantity/remove pe o linie care nu există
        "cart_empty",  # checkout fără linii
        "price_unknown",  # preț fără sursă → niciun total; checkout blocat (suma e necesară)
        "currency_mismatch",  # monede diferite în același coș → refuz, nu o sumă greșită
        "version_conflict",  # expected_version stale → conflict + snapshot fresh
        "receipt_pending",  # o mutație anterioară e încă nefinalizată (extern) → reconcile întâi
        "checkout_unavailable",  # fără URL de checkout configurat (nu inventăm domenii)
        "internal_error",  # excepție neprevăzută — receipt failed, cart neschimbat
    }
)

FactsStatus = Literal["known", "stale", "unknown"]


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """Amprentă DETERMINISTĂ a unui payload (chei sortate, fără spații) — aceeași convenție ca
    `action_models.canonical_json`/fingerprint-ul de request NX-232. Intră în idempotency keys:
    aceleași argumente ⇒ aceeași cheie, indiferent de ordinea construcției dict-ului."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _ref(value: object) -> str | None:
    """Un id opac mărginit — aceeași regulă ca `action_models._ref` / `web.context.normalize_id`:
    fără spații, fără ghilimele, fără URL. Un id cu spații nu e id."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_REF_LEN:
        return None
    if any(ch.isspace() for ch in text):
        return None
    if not all(ch.isalnum() or ch in "_.:-" for ch in text):
        return None
    return text


@dataclass(frozen=True)
class CartCommand:
    """Comanda UNICĂ de mutație. Refs + cantitate mărginită; NICIODATĂ preț/nume/discount —
    faptele comerciale se rehidratează server-side la execuție, nu se primesc de la apelant."""

    operation: CartOperation
    product_ref: str | None = None
    variant_ref: str | None = None
    quantity: int | None = None
    # Optimistic concurrency: None = fără verificare (calea LLM nu urmărește versiuni; lockul de
    # rând serializează oricum). Un int = versiunea pe care apelantul CREDE că o are coșul
    # (calea de acțiuni NX-240 o va purta) — mismatch ⇒ conflict + snapshot fresh, fără merge.
    expected_version: int | None = None

    @classmethod
    def parse(cls, operation: str, raw: Mapping[str, Any] | None = None) -> CartCommand | None:
        """Input brut → comandă validată, sau None. STRICT: o operație necunoscută, un ref
        malformat sau o cantitate în afara capului nu se „repară" — se resping."""
        if operation not in CART_OPERATIONS:
            return None
        raw = raw or {}
        product_ref = _ref(raw.get("product_id") or raw.get("product_ref"))
        variant_raw = raw.get("variant_id") or raw.get("variant_ref")
        variant_ref = _ref(variant_raw) if variant_raw is not None else None
        if variant_raw is not None and variant_ref is None:
            return None
        quantity: int | None = None
        if raw.get("quantity") is not None:
            q = raw.get("quantity")
            if isinstance(q, bool) or not isinstance(q, int):
                return None
            if not 1 <= q <= CART_MAX_LINE_QUANTITY:
                return None
            quantity = q
        expected = raw.get("expected_version")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            return None
        if operation in ("add", "set_quantity", "remove") and product_ref is None:
            return None
        if operation == "set_quantity" and quantity is None:
            return None
        if operation == "add" and quantity is None:
            quantity = 1
        return cls(
            operation=operation,  # type: ignore[arg-type]
            product_ref=product_ref,
            variant_ref=variant_ref,
            quantity=quantity,
            expected_version=expected,
        )

    def fingerprint(self) -> str:
        """Amprenta canonică a comenzii — parte din idempotency key pe calea LLM (turn+op+args)."""
        payload: dict[str, Any] = {"op": self.operation}
        if self.product_ref:
            payload["p"] = self.product_ref
        if self.variant_ref:
            payload["v"] = self.variant_ref
        if self.quantity is not None:
            payload["q"] = self.quantity
        return canonical_fingerprint(payload)


# ── Fapte comerciale (rehidratate, cu sursă + prospețime) ───────────────────────────────────


@dataclass(frozen=True)
class CommerceFacts:
    """Faptele unei perechi (produs, variantă) LA MOMENTUL citirii. Toate din catalog (sursa
    canonică a acestui mediu — vezi docs/CART-DATA-READINESS.md), niciunul de la apelant.

    Semantica UNKNOWN (D8), moștenită din `context_resolver`:
      • `price is None` ⇒ `price` în `unknown` — nu există „preț 0";
      • `review_count == 0` ⇒ rating UNKNOWN (default-ul DB e 0, dar „0 stele" e o afirmație);
      • `stock is None` ⇒ cap necunoscut — `availability` rămâne faptul întreținut de catalog;
      • promoția există DOAR ca `list_price > price` în fereastra de sale (regula canonică din
        `_SALE_ACTIVE`); vouchere/ETA nu au sursă → nu apar deloc aici (structural unknown).
    """

    product_id: str
    name: str
    currency: str | None = None
    price: float | None = None  # prețul EFECTIV (fereastra de sale + min-variantă, ca validatorul)
    list_price: float | None = None  # prețul tăiat, DOAR la reducere reală; altfel None
    availability: str | None = None  # None = necunoscut (nu „in_stock" prin lipsă de dovadă)
    stock: int | None = None  # cap numai dacă sursa îl dă; None = necunoscut
    variant_id: str | None = None
    variant_label: str | None = None
    rating: float | None = None
    review_count: int | None = None
    review_summary: str | None = None
    freshness: Freshness | None = None
    unknown: frozenset[str] = frozenset()
    source: str = "catalog.products"
    # Rândul COMPLET de catalog (products + variants hidratate) — pentru poarta de siguranță
    # (NX-173 citește attributes/ingredients) și pentru validator (ToolResult.products). Nu se
    # persistă în stare (P8) și nu intră în receipt.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def price_known(self) -> bool:
        return self.price is not None and self.currency is not None

    @property
    def sellable(self) -> str | None:
        """None = vandabil; altfel codul de refuz. `availability` necunoscut NU devine „în stoc"
        (failure matrix: stock provider timeout/stale ⇒ UNKNOWN, nu presupunere)."""
        if self.availability is None:
            return "availability_unknown"
        if self.availability in ("out_of_stock", "discontinued"):
            return "out_of_stock"
        return None

    @property
    def facts_status(self) -> FactsStatus:
        if not self.price_known:
            return "unknown"
        if self.freshness is not None and self.freshness.stale:
            return "stale"
        return "known"


# ── Snapshot display-ready (server-owned; FE = renderer pasiv) ──────────────────────────────

# RON se afișează „lei" (limbaj natural RO); alte monede rămân cod ISO — nu inventăm simboluri.
_CURRENCY_WORDS = {"RON": "lei"}


def format_amount(value: float, currency: str | None, language: str | None = "ro") -> str:
    """`89.0` → `89,00 lei` (ro/hu) / `89.00 RON` (en). Determinist, fără locale de sistem —
    aceeași convenție ca proiecția v2 (`turn_events`)."""
    lang = (language or "ro")[:2]
    text = f"{value:.2f}"
    if lang != "en":
        text = text.replace(".", ",")
    word = currency or ""
    if lang != "en":  # „lei" e limbaj natural RO/HU; în engleză rămâne codul ISO
        word = _CURRENCY_WORDS.get(word, word)
    return f"{text} {word}".strip()


@dataclass(frozen=True)
class CartLineView:
    """O linie de coș, display-ready. Prețurile vin din fapte rehidratate ACUM, nu din ce s-a
    copiat la add — snapshotul vechi nu e niciodată truth pentru următoarea citire."""

    product_id: str
    variant_id: str | None
    name: str
    variant_label: str | None
    quantity: int
    unit_price: float | None
    line_total: float | None
    currency: str | None
    unit_price_display: str | None
    line_total_display: str | None
    availability: str | None
    facts_status: FactsStatus
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class CartTotals:
    """Totalul coșului. `status='unknown'` când ORICE linie are preț necunoscut — un total
    parțial prezentat ca total ar fi o sumă greșită cu UI frumos."""

    status: Literal["known", "unknown", "empty"]
    value: float | None = None
    currency: str | None = None
    display: str | None = None
    lines: int = 0
    units: int = 0


@dataclass(frozen=True)
class CartSnapshot:
    """Proiecția versionată a coșului canonic. TOT ce e afișabil (totaluri, display strings)
    e calculat aici, server-side; frontendul nu calculează linii, totaluri sau eligibilitate."""

    cart_id: str | None
    version: int
    status: str  # active | checked_out | expired | empty (empty = nu există rând încă)
    lines: tuple[CartLineView, ...] = ()
    totals: CartTotals = field(default_factory=lambda: CartTotals(status="empty"))
    facts_status: FactsStatus = "known"
    checkout_eligible: bool = False
    blocked_reasons: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.lines

    def to_state_ref(self) -> dict[str, Any]:
        """Referința care intră în stare (P8): id + versiune + un contor. NICIODATĂ linii,
        prețuri sau totaluri — starea nu mai e o a doua copie a coșului."""
        return {"id": self.cart_id, "version": self.version, "lines": len(self.lines)}

    def command_lines(self) -> list[dict[str, Any]]:
        """Liniile ca REFS pentru comenzi downstream (checkout fallback, cross-sell exclude)."""
        return [
            {"product_id": ln.product_id, "variant_id": ln.variant_id, "quantity": ln.quantity}
            for ln in self.lines
        ]


def build_snapshot(
    *,
    cart_id: str | None,
    version: int,
    status: str,
    items: list[dict[str, Any]],
    facts: Mapping[tuple[str, str | None], CommerceFacts],
    language: str | None = "ro",
) -> CartSnapshot:
    """Items persistate (refs + qty) + fapte rehidratate → snapshot display-ready. PUR.

    Reguli (aprobate în docs/CART-DATA-READINESS.md):
      • linie fără fapte (produs dispărut/inactiv între timp) ⇒ facts UNKNOWN pe linie, total
        UNKNOWN — linia rămâne VIZIBILĂ (clientul a pus-o acolo), dar nu susținem un preț;
      • orice preț necunoscut ⇒ total UNKNOWN + checkout blocat (`price_unknown`);
      • monede diferite între linii ⇒ total UNKNOWN + checkout blocat (`currency_mismatch`);
      • linie nevandabilă (OOS/unknown availability) ⇒ checkout blocat cu motivul liniei;
      • `facts_status` global = cel mai slab dintre linii (unknown > stale > known)."""
    lines: list[CartLineView] = []
    blocked: list[str] = []
    total = 0.0
    units = 0
    currencies: set[str] = set()
    worst: FactsStatus = "known"
    total_known = bool(items)
    for it in items:
        pid = str(it["product_id"])
        vid = it.get("variant_id")
        vid = str(vid) if vid else None
        qty = int(it.get("quantity") or 1)
        units += qty
        f = facts.get((pid, vid))
        if f is None:
            lines.append(
                CartLineView(
                    product_id=pid,
                    variant_id=vid,
                    name=str(it.get("name") or "(produs indisponibil)"),
                    variant_label=None,
                    quantity=qty,
                    unit_price=None,
                    line_total=None,
                    currency=None,
                    unit_price_display=None,
                    line_total_display=None,
                    availability=None,
                    facts_status="unknown",
                    unknown=("price", "availability"),
                )
            )
            worst = "unknown"
            total_known = False
            if "price_unknown" not in blocked:
                blocked.append("price_unknown")
            continue
        line_status = f.facts_status
        if line_status == "unknown":
            worst = "unknown"
        elif line_status == "stale" and worst == "known":
            worst = "stale"
        unit = f.price if f.price_known else None
        line_total = round(unit * qty, 2) if unit is not None else None
        if unit is None:
            total_known = False
            if "price_unknown" not in blocked:
                blocked.append("price_unknown")
        else:
            total += unit * qty
            currencies.add(f.currency or "")
        sell = f.sellable
        if sell and sell not in blocked:
            blocked.append(sell)
        lines.append(
            CartLineView(
                product_id=pid,
                variant_id=vid,
                name=f.name,
                variant_label=f.variant_label,
                quantity=qty,
                unit_price=unit,
                line_total=line_total,
                currency=f.currency,
                unit_price_display=(
                    format_amount(unit, f.currency, language) if unit is not None else None
                ),
                line_total_display=(
                    format_amount(line_total, f.currency, language)
                    if line_total is not None
                    else None
                ),
                availability=f.availability,
                facts_status=line_status,
                unknown=tuple(sorted(f.unknown)),
            )
        )
    if len(currencies) > 1:
        total_known = False
        if "currency_mismatch" not in blocked:
            blocked.append("currency_mismatch")
    if not items:
        totals = CartTotals(status="empty")
    elif total_known:
        currency = next(iter(currencies), None) or None
        totals = CartTotals(
            status="known",
            value=round(total, 2),
            currency=currency,
            display=format_amount(round(total, 2), currency, language),
            lines=len(lines),
            units=units,
        )
    else:
        totals = CartTotals(status="unknown", lines=len(lines), units=units)
    eligible = status == "active" and bool(items) and not blocked
    if status != "active" and items:
        blocked.append("cart_not_active")
    if not items:
        blocked.append("cart_empty")
    return CartSnapshot(
        cart_id=cart_id,
        version=version,
        status=status if cart_id else "empty",
        lines=tuple(lines),
        totals=totals,
        facts_status=worst,
        checkout_eligible=eligible,
        blocked_reasons=tuple(blocked),
    )


# ── Receipts (dovada mutației) ──────────────────────────────────────────────────────────────

ReceiptStatus = Literal["pending", "succeeded", "failed", "unknown_reconcile"]


@dataclass(frozen=True)
class MutationReceipt:
    """Dovada idempotentă a unei mutații. `replayed=True` = cheia a mai fost consumată, iar
    rezultatul întors e cel ORIGINAL — zero a doua mutație (failure matrix: retry după response
    loss). Câmpurile de checkout (`url`/`ref_code`) sunt setate doar pe `operation='checkout'`."""

    receipt_id: str
    operation: CartOperation
    status: ReceiptStatus
    idempotency_key: str
    before_version: int
    after_version: int | None = None
    result_code: str | None = None
    external_ref: str | None = None
    url: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class MutationOutcome:
    """Rezultatul UNIC al oricărei operații de serviciu: receipt (dacă s-a scris unul) +
    snapshotul PROASPĂT + eventualul cod de eroare din vocabularul închis."""

    snapshot: CartSnapshot
    receipt: MutationReceipt | None = None
    error: str | None = None  # din CART_ERROR_CODES; None = succes (sau replay de succes)
    conflict: bool = False  # expected_version stale — snapshotul e cel fresh, server-owned
    # Rândurile de catalog ale liniilor atinse (pentru ToolResult.products → validator). Nu se
    # persistă; e transportul in-memory dintre serviciu și tool în ACELAȘI tur.
    products: tuple[Mapping[str, Any], ...] = ()
    # DOAR pe checkout: liniile VALIDATE (name/price/quantity, prețuri rehidratate) — sursa
    # vederii pentru model și a totalului grounded. Tot in-memory, nu se persistă.
    lines: tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None and not self.conflict


__all__ = [
    "CART_ERROR_CODES",
    "CART_MAX_LINES",
    "CART_MAX_LINE_QUANTITY",
    "CART_OPERATIONS",
    "CartCommand",
    "CartLineView",
    "CartOperation",
    "CartSnapshot",
    "CartTotals",
    "CommerceFacts",
    "FactsStatus",
    "MutationOutcome",
    "MutationReceipt",
    "ReceiptStatus",
    "build_snapshot",
    "canonical_fingerprint",
    "format_amount",
]
