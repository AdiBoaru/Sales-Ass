"""NX-240 — `EvidenceBundle`: faptele comerciale ale turului, cu proveniență și prospețime.

**Ce e și ce NU e.** E snapshotul IMUABIL al lucrurilor pe care serverul le poate susține în
momentul în care se compune răspunsul: preț, monedă, stoc, variantă, rating, recenzii, URL,
verdicte de constrângere, coș. Nu e o a doua copie a catalogului și nu e un cache: se construiește
o dată per tur, din ce s-a retrievat oricum, se ÎNGHEAȚĂ în rândul terminal de ledger și de acolo
se proiectează la fiecare citire. De asta un preț schimbat în catalog după ce turul s-a încheiat
nu poate schimba răspunsul deja dat — nu pentru că am memorat un ecran, ci pentru că am memorat
FAPTELE pe care ecranul le afirmă.

**Trei stări, nu două (D8).** `known` / `unknown(reason)` / `stale(age, sla)` sunt distincte și
un câmp absent nu devine niciodată `false`/`0`:

  • preț fără monedă ⇒ preț UNKNOWN (o sumă fără monedă nu e o sumă);
  • `review_count == 0` ⇒ rating UNKNOWN — default-ul DB e 0, iar „0 stele" e o AFIRMAȚIE;
  • stoc NULL ⇒ UNKNOWN, niciodată „indisponibil" și niciodată „în stoc" prin lipsă de dovadă;
  • livrare/promoții/vouchere: fără adaptor canonic în mediul curent ⇒ `no_source`, adică nu
    apar deloc. Nu se derivă din `updated_at` și nu se inventează „poate ai un cod".

**`verified_at` nu e `updated_at`.** Un rând modificat nu e un fapt verificat: `updated_at` spune
când s-a atins rândul, `synced_at` spune când sincronizarea l-a confruntat cu sursa. Doar al
doilea e verificare, și numai el poate face un fapt `stale`. Consecința practică:

  • un fapt VERIFICAT care a depășit SLA-ul devine `stale` ⇒ nu se afișează și nu poartă CTA;
  • un fapt NEVERIFICAT nu poate deveni stale (n-avem de unde ști) ⇒ se afișează ca valoare de
    catalog, dar raportul de data-readiness îl numără separat, ca să se vadă că tenantul n-are
    pipeline de verificare.

Măsurat pe tenantul demo (`scripts/nx240_data_readiness.py`): `synced_at` e NULL pe 300/300
produse active — deci nimic nu e verificat, dar nimic nu e nici expirat. Vezi
`docs/WEB-VIEW-V2-DATA-READINESS.md` pentru ce blochează asta și ce nu.

**Pur.** Zero DB, zero HTTP, zero LLM, zero ceas implicit (`now` se pasează). Hidratarea e a
apelantului (`facts_provider.load_facts`, NX-237, un query per batch); aici intră numai rânduri
deja în memorie. Bugetul de query-uri se transportă în `query_count` ca să fie ASERTABIL.

**Omonim deliberat, sens diferit:** `src.retrieval.port.EvidenceBundle` e o listă de REFERINȚE
(`evidence_id → product_id`) pentru ranking. Asta de aici e mulțimea de FAPTE cu valoare și
sursă. Nu se importă niciodată amândouă în același modul fără alias.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from src.web.localization import to_decimal

# ── Vocabulare ÎNCHISE (intră în metrici ca labels — nu au voie să explodeze) ───────────────
FactStatus = Literal["known", "unknown", "stale"]

#: Câmpurile de fapt pe care ViewModel-ul le poate afirma. Un câmp care nu e aici nu poate fi
#: proiectat: adăugarea unuia e o decizie de contract (sursă + SLA + politică de unknown), nu o
#: cheie nouă strecurată într-un dict.
FACT_FIELDS: Final[tuple[str, ...]] = (
    "identity",
    "title",
    "brand",
    "url",
    "image",
    "currency",
    "price",
    "list_price",
    "availability",
    "stock",
    "variant",
    "rating",
    "review_count",
    "review_summary",
    "delivery_promise",
    "promotion",
    "voucher",
)

#: De ce lipsește un fapt. `no_source` (nu există adaptor în acest mediu) e RADICAL diferit de
#: `missing_value` (adaptorul există, rândul e NULL): primul e o limită de arhitectură care se
#: rezolvă cu un card, al doilea e o gaură de date care se rezolvă cu un import.
UnknownReason = Literal[
    "no_source",
    "missing_value",
    "zero_reviews",
    "invalid_value",
    "variant_mismatch",
    "not_retrieved",
]

#: Câmpurile care NU au sursă canonică în mediul curent (docs/CART-DATA-READINESS.md). Sunt
#: enumerate ca DATE, nu tăcute prin absență: raportul de coverage le arată ca `no_source`, deci
#: „widgetul nu afișează livrarea" e o afirmație verificabilă, nu o omisiune pe care s-o descoperi.
STRUCTURALLY_UNSOURCED: Final[frozenset[str]] = frozenset(
    {"delivery_promise", "promotion", "voucher"}
)


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """Politica UNUI câmp de fapt: cine îl deține, de unde vine, dacă i se aplică SLA-ul de
    prospețime, ce se întâmplă când lipsește și dacă absența lui blochează un CTA comercial.

    Matricea asta e cerută explicit de card („field → owner → source → freshness SLA → formatter →
    unknown behavior → CTA") și trăiește AICI, ca DATE, nu într-un document care poate diverge:
    testul de data-readiness o verifică față de implementare, iar documentul de readiness
    (`docs/WEB-VIEW-V2-DATA-READINESS.md`) se scrie din ea. Un câmp nou fără politică e prins de
    test înainte să apuce să ajungă în ViewModel fără o decizie de unknown."""

    field: str
    owner: str
    source: str | None  # `None` = nicio sursă canonică în acest mediu (adaptor inexistent)
    sla_applies: bool  # i se aplică pragul de prospețime? (doar faptele care se pot „strica")
    formatter: str  # funcția din `src/web/localization.py`, sau `-` dacă e text brut mărginit
    unknown_behavior: str  # ce vede clientul când lipsește
    blocks_commerce_cta: bool  # absența/nesiguranța lui oprește o promisiune de cumpărare?


FIELD_POLICY: Final[dict[str, FieldPolicy]] = {
    "identity": FieldPolicy("identity", "catalog", "products.id", False, "-", "produs omis", True),
    "title": FieldPolicy("title", "catalog", "products.name", False, "-", "produs omis", True),
    "brand": FieldPolicy(
        "brand", "catalog", "brands.name", False, "-", "subtitlu fără brand", False
    ),
    "url": FieldPolicy("url", "catalog", "products.product_url", False, "-", "fără buton", False),
    "image": FieldPolicy(
        "image", "catalog", "product_images.url", False, "-", "card fără poză", False
    ),
    "currency": FieldPolicy(
        "currency", "catalog", "products.currency", False, "-", "preț omis", True
    ),
    "price": FieldPolicy(
        "price",
        "catalog",
        "products.price/sale_price+variants",
        True,
        "format_money",
        "preț și reducere omise",
        True,
    ),
    "list_price": FieldPolicy(
        "list_price",
        "catalog",
        "products.price (fereastră de sale)",
        True,
        "format_discount",
        "fără preț tăiat și fără procent",
        False,
    ),
    "availability": FieldPolicy(
        "availability",
        "catalog",
        "products.availability",
        True,
        "format_availability",
        'fără etichetă de stoc (NU „indisponibil")',
        True,
    ),
    "stock": FieldPolicy(
        "stock",
        "catalog",
        "products.stock_total/variants.stock",
        True,
        "format_availability",
        'etichetă generică în loc de „ultimele N"',
        False,
    ),
    "variant": FieldPolicy(
        "variant", "catalog", "product_variants.label", False, "-", "subtitlu fără variantă", False
    ),
    "rating": FieldPolicy(
        "rating",
        "catalog",
        "products.rating (cu review_count > 0)",
        False,
        "format_rating",
        'fără rating (NU „0 stele")',
        False,
    ),
    "review_count": FieldPolicy(
        "review_count",
        "catalog",
        "products.review_count",
        False,
        "format_rating",
        "rating fără paranteza de recenzii",
        False,
    ),
    "review_summary": FieldPolicy(
        "review_summary",
        "content",
        "product_review_summaries.summary",
        False,
        "-",
        "fără rezumat de recenzii",
        False,
    ),
    # Fără adaptor canonic în mediul curent. Nu sunt „TODO în cod": sunt declarate ca lipsă, iar
    # guardul respinge ORICE afirmație despre ele. Un card separat aduce adaptorul, nu un prompt.
    "delivery_promise": FieldPolicy(
        "delivery_promise", "fulfillment", None, True, "-", "fără ETA, fără notice", False
    ),
    "promotion": FieldPolicy(
        "promotion", "promotions", None, True, "-", "fără promoție afișată", False
    ),
    "voucher": FieldPolicy("voucher", "promotions", None, True, "-", "fără voucher afișat", False),
}


#: Cap-uri (P4: bugetul e în cod). Aliniate cu `MAX_PRODUCT_ITEMS` din contractul NX-228.
MAX_BUNDLE_PRODUCTS: Final[int] = 6
MAX_BUNDLE_CART_LINES: Final[int] = 20
BUNDLE_SCHEMA_VERSION: Final[int] = 1


# ── Fapt ────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Fact:
    """Un fapt cu proveniență. `value` e `Decimal` pentru bani (niciodată `float`), `str`/`int`
    pentru restul. `verified_at`/`age_s` există DOAR când sursa chiar a verificat ceva."""

    field: str
    status: FactStatus
    value: Decimal | str | int | bool | None = None
    source: str = ""
    verified_at: datetime | None = None
    age_s: int | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Se poate AFIȘA? Cunoscut și nu expirat. `stale` nu se afișează: un preț despre care
        știm că e vechi peste SLA e o afirmație pe care nu o mai susținem."""
        return self.status == "known" and self.value is not None

    @property
    def verified(self) -> bool:
        """Se poate PROMITE? Cere verificare reală (`verified_at`), nu doar o valoare în rând.
        Poarta CTA-urilor mutante: fără ea, „adaugă în coș" ar fi o promisiune despre inventar
        făcută dintr-o coloană de catalog."""
        return self.usable and self.verified_at is not None

    @classmethod
    def known(
        cls,
        field_name: str,
        value: Any,
        *,
        source: str,
        verified_at: datetime | None = None,
        age_s: int | None = None,
        sla_s: int | None = None,
    ) -> Fact:
        """Fapt cunoscut; devine `stale` automat dacă e verificat și a depășit SLA-ul. Un fapt
        NEVERIFICAT (`verified_at is None`) nu poate deveni stale — n-avem de unde ști."""
        if value is None:
            return cls.unknown(field_name, "missing_value", source=source)
        if verified_at is not None and age_s is not None and sla_s is not None and age_s > sla_s:
            return cls(
                field=field_name,
                status="stale",
                value=value,
                source=source,
                verified_at=verified_at,
                age_s=age_s,
                reason="sla_exceeded",
            )
        return cls(
            field=field_name,
            status="known",
            value=value,
            source=source,
            verified_at=verified_at,
            age_s=age_s,
        )

    @classmethod
    def unknown(cls, field_name: str, reason: UnknownReason | str, *, source: str = "") -> Fact:
        return cls(field=field_name, status="unknown", value=None, source=source, reason=reason)

    def to_jsonb(self) -> dict[str, Any]:
        """Formă compactă pentru persistare. `Decimal` → `str` (exact), timp → ISO UTC."""
        out: dict[str, Any] = {"s": self.status}
        if self.value is not None:
            out["v"] = str(self.value) if isinstance(self.value, Decimal) else self.value
            if isinstance(self.value, Decimal):
                out["d"] = True  # marcaj de tip: „citește-l înapoi ca Decimal", nu ghici din text
        if self.source:
            out["src"] = self.source
        if self.verified_at is not None:
            out["at"] = self.verified_at.astimezone(UTC).isoformat()
        if self.age_s is not None:
            out["age"] = int(self.age_s)
        if self.reason:
            out["r"] = self.reason
        return out

    @classmethod
    def from_jsonb(cls, field_name: str, raw: Any) -> Fact:
        """Inversul, DEFENSIV: un payload stricat produce UNKNOWN, nu o excepție la randare."""
        if not isinstance(raw, Mapping):
            return cls.unknown(field_name, "missing_value")
        status = raw.get("s")
        if status not in ("known", "unknown", "stale"):
            return cls.unknown(field_name, "invalid_value")
        value = raw.get("v")
        if raw.get("d") is True:
            value = to_decimal(value)
        verified_at = _parse_iso(raw.get("at"))
        age = raw.get("age")
        return cls(
            field=field_name,
            status=status,
            value=value,
            source=str(raw.get("src") or ""),
            verified_at=verified_at,
            age_s=int(age) if isinstance(age, int) and not isinstance(age, bool) else None,
            reason=str(raw.get("r") or ""),
        )


def _sla_from_jsonb(raw: Any) -> int | None:
    """`sla_s` persistat → prag. `None` rămâne `None` (catalog declarat static); orice altceva
    neinterpretabil cade pe `0`, forma pe care o aveau bundle-urile scrise înainte de declarație."""
    if raw is None:
        return None
    if isinstance(raw, bool):  # `bool` e subclasă de `int` — l-ar citi ca prag de 1 secundă
        return 0
    return int(raw) if isinstance(raw, int) else 0


def _parse_iso(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _observed_at(row: Mapping[str, Any]) -> datetime | None:
    """Momentul VERIFICĂRII, nu al ultimei atingeri de rând. `synced_at` = sincronizarea a
    confruntat rândul cu sursa; `updated_at` = cineva a scris ceva. Doar primul e dovadă, iar
    confuzia lor e exact ce cere cardul să separăm."""
    return _parse_iso(row.get("synced_at"))


# ── Verdicte de constrângere ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ConstraintVerdictFact:
    """Verdictul unei constrângeri, ca fapt. `UNKNOWN` NU e `MISMATCH` (D7): primul e o
    nesiguranță de afișat, al doilea e o contrazicere care blochează produsul."""

    facet: str
    verdict: str  # MATCH | MISMATCH | UNKNOWN
    strength: str = "hard"  # hard | soft
    reason: str = ""

    @property
    def blocking(self) -> bool:
        return self.strength == "hard" and self.verdict == "MISMATCH"

    def to_jsonb(self) -> dict[str, Any]:
        out = {"f": self.facet, "v": self.verdict, "st": self.strength}
        if self.reason:
            out["r"] = self.reason
        return out

    @classmethod
    def from_jsonb(cls, raw: Any) -> ConstraintVerdictFact | None:
        if not isinstance(raw, Mapping):
            return None
        facet = raw.get("f")
        verdict = raw.get("v")
        if not isinstance(facet, str) or verdict not in ("MATCH", "MISMATCH", "UNKNOWN"):
            return None
        strength = raw.get("st")
        return cls(
            facet=facet,
            verdict=verdict,
            strength=strength if strength in ("hard", "soft") else "hard",
            reason=str(raw.get("r") or ""),
        )


# ── Evidence per produs ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ProductEvidence:
    """Faptele UNUI produs (eventual ale unei variante). `facts` acoperă exact `FACT_FIELDS`:
    fiecare câmp are o intrare, chiar și când e UNKNOWN — absența unei chei ar fi un al patrulea
    status nedeclarat."""

    product_id: str
    business_id: str
    variant_id: str | None = None
    match_class: str = "exact"  # exact | alternative | rejected
    facts: Mapping[str, Fact] = field(default_factory=dict)
    constraints: tuple[ConstraintVerdictFact, ...] = ()

    def fact(self, name: str) -> Fact:
        return self.facts.get(name) or Fact.unknown(name, "not_retrieved")

    @property
    def blocked(self) -> bool:
        """Produs pe care nu avem voie să-l arătăm: contrazis de o constrângere HARD sau clasificat
        `rejected` de retrieval. UNKNOWN nu blochează — se declară, nu se ascunde."""
        return self.match_class == "rejected" or any(c.blocking for c in self.constraints)

    @property
    def unknown_facets(self) -> tuple[str, ...]:
        return tuple(c.facet for c in self.constraints if c.verdict == "UNKNOWN")

    @property
    def sellable(self) -> str | None:
        """`None` = produsul poate purta un CTA de comerț; altfel codul de refuz.

        **Poarta e `usable`, nu `verified` — și diferența e o decizie, nu o scăpare.** Un buton
        „adaugă în coș" nu e o GARANȚIE de inventar: `CartService` (NX-237) rehidratează și
        revalidează preț/stoc/siguranță ÎNAINTE de fiecare mutație, deci un click pe un buton
        învechit produce un refuz onest, nu un coș greșit. Garanția trăiește la mutație, unde
        există tranzacție; butonul e doar o ofertă de a încerca.

        A cere `verified` aici ar însemna zero butoane pentru orice tenant fără pipeline de sync
        (măsurat: 0/300 pe demo) — adică am plăti cu toată funcționalitatea de comerț pentru o
        siguranță pe care o avem deja în altă parte. Ce NU se relaxează: `stale` rămâne
        neafișabil, deci un preț expirat scoate și prețul, și butonul."""
        if self.blocked:
            return "blocked"
        availability = self.fact("availability")
        if not availability.usable:
            return (
                "availability_stale" if availability.status == "stale" else "availability_unknown"
            )
        if availability.value in ("out_of_stock", "discontinued"):
            return "out_of_stock"
        price = self.fact("price")
        if not price.usable:
            return "price_stale" if price.status == "stale" else "price_unknown"
        return None

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "pid": self.product_id,
            "bid": self.business_id,
            "vid": self.variant_id,
            "mc": self.match_class,
            "f": {name: fact.to_jsonb() for name, fact in self.facts.items()},
            "c": [c.to_jsonb() for c in self.constraints],
        }

    @classmethod
    def from_jsonb(cls, raw: Any) -> ProductEvidence | None:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("pid"), str):
            return None
        raw_facts = raw.get("f")
        facts = (
            {name: Fact.from_jsonb(name, value) for name, value in raw_facts.items()}
            if isinstance(raw_facts, Mapping)
            else {}
        )
        constraints = tuple(
            c
            for c in (ConstraintVerdictFact.from_jsonb(item) for item in (raw.get("c") or []))
            if c is not None
        )
        variant = raw.get("vid")
        return cls(
            product_id=str(raw["pid"]),
            business_id=str(raw.get("bid") or ""),
            variant_id=str(variant) if variant else None,
            match_class=str(raw.get("mc") or "exact"),
            facts=facts,
            constraints=constraints,
        )


# ── Coșul, ca fapt ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CartLineEvidence:
    product_id: str
    variant_id: str | None
    name: str
    variant_label: str | None
    quantity: int
    unit_price: Decimal | None
    line_total: Decimal | None
    currency: str | None
    facts_status: str

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "pid": self.product_id,
            "vid": self.variant_id,
            "n": self.name,
            "vl": self.variant_label,
            "q": self.quantity,
            "u": str(self.unit_price) if self.unit_price is not None else None,
            "t": str(self.line_total) if self.line_total is not None else None,
            "cur": self.currency,
            "fs": self.facts_status,
        }

    @classmethod
    def from_jsonb(cls, raw: Any) -> CartLineEvidence | None:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("pid"), str):
            return None
        vid = raw.get("vid")
        return cls(
            product_id=str(raw["pid"]),
            variant_id=str(vid) if vid else None,
            name=str(raw.get("n") or ""),
            variant_label=str(raw["vl"]) if raw.get("vl") else None,
            quantity=int(raw.get("q") or 1),
            unit_price=to_decimal(raw.get("u")),
            line_total=to_decimal(raw.get("t")),
            currency=str(raw["cur"]) if raw.get("cur") else None,
            facts_status=str(raw.get("fs") or "unknown"),
        )


@dataclass(frozen=True, slots=True)
class CartEvidence:
    """Snapshotul coșului canonic (NX-237) ca FAPT, nu ca ecran: numere + monedă + eligibilitate.
    Formatarea rămâne a projectorului — altfel am avea două locuri care decid cum arată un total."""

    cart_id: str | None
    version: int
    status: str
    lines: tuple[CartLineEvidence, ...] = ()
    total: Decimal | None = None
    currency: str | None = None
    total_status: str = "empty"  # known | unknown | empty
    units: int = 0
    checkout_eligible: bool = False
    blocked_reasons: tuple[str, ...] = ()

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "id": self.cart_id,
            "ver": self.version,
            "st": self.status,
            "l": [line.to_jsonb() for line in self.lines[:MAX_BUNDLE_CART_LINES]],
            "t": str(self.total) if self.total is not None else None,
            "cur": self.currency,
            "ts": self.total_status,
            "u": self.units,
            "ok": self.checkout_eligible,
            "b": list(self.blocked_reasons),
        }

    @classmethod
    def from_jsonb(cls, raw: Any) -> CartEvidence | None:
        if not isinstance(raw, Mapping):
            return None
        lines = tuple(
            line
            for line in (CartLineEvidence.from_jsonb(item) for item in (raw.get("l") or []))
            if line is not None
        )
        return cls(
            cart_id=str(raw["id"]) if raw.get("id") else None,
            version=int(raw.get("ver") or 0),
            status=str(raw.get("st") or "empty"),
            lines=lines,
            total=to_decimal(raw.get("t")),
            currency=str(raw["cur"]) if raw.get("cur") else None,
            total_status=str(raw.get("ts") or "empty"),
            units=int(raw.get("u") or 0),
            checkout_eligible=bool(raw.get("ok")),
            blocked_reasons=tuple(str(r) for r in (raw.get("b") or []) if isinstance(r, str)),
        )


# ── Bundle ──────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Snapshotul complet, imuabil și mărginit. `as_of` e momentul construirii — projectorul îl
    folosește ca ceas, ca două proiecții ale aceluiași turn să dea aceiași bytes."""

    business_id: str
    locale: str
    as_of: datetime
    products: tuple[ProductEvidence, ...] = ()
    cart: CartEvidence | None = None
    query_count: int = 0
    # `None` = tenantul și-a declarat catalogul static (`src/catalog/freshness.py`), deci faptele
    # nu se judecă în timp. DISTINCT de `0`, care ar însemna „prag zero" ⇒ totul expirat.
    sla_s: int | None = 0
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def by_id(self, product_id: Any) -> ProductEvidence | None:
        key = str(product_id or "")
        for item in self.products:
            if item.product_id == key:
                return item
        return None

    @property
    def renderable(self) -> tuple[ProductEvidence, ...]:
        """Produsele care au voie să apară: identitate cunoscută și nicio contrazicere hard."""
        return tuple(p for p in self.products if not p.blocked and p.fact("title").usable)

    def coverage(self) -> dict[str, dict[str, int]]:
        """`field → {known, stale, unknown}` peste produsele bundle-ului. Baza raportului de
        data-readiness: un câmp cu 0 `known` pe tot catalogul nu e o preferință de design, e o
        sursă care lipsește."""
        out: dict[str, dict[str, int]] = {
            name: {"known": 0, "stale": 0, "unknown": 0} for name in FACT_FIELDS
        }
        for product in self.products:
            for name in FACT_FIELDS:
                out[name][product.fact(name).status] += 1
        return out

    def to_jsonb(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "business_id": self.business_id,
            "locale": self.locale,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "sla_s": self.sla_s,
            "query_count": self.query_count,
            "products": [p.to_jsonb() for p in self.products[:MAX_BUNDLE_PRODUCTS]],
        }
        if self.cart is not None:
            payload["cart"] = self.cart.to_jsonb()
        return payload

    @classmethod
    def from_jsonb(cls, raw: Any) -> EvidenceBundle | None:
        """Payload persistat → bundle. `None` (nu excepție) pe orice formă necunoscută: apelantul
        cade pe proiecția precedentă, nu pe un 500 (P6)."""
        if not isinstance(raw, Mapping):
            return None
        if raw.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            return None
        as_of = _parse_iso(raw.get("as_of"))
        business_id = raw.get("business_id")
        if as_of is None or not isinstance(business_id, str) or not business_id:
            return None
        products = tuple(
            p
            for p in (ProductEvidence.from_jsonb(item) for item in (raw.get("products") or []))
            if p is not None
        )
        return cls(
            business_id=business_id,
            locale=str(raw.get("locale") or "ro"),
            as_of=as_of,
            products=products[:MAX_BUNDLE_PRODUCTS],
            cart=CartEvidence.from_jsonb(raw.get("cart")),
            query_count=int(raw.get("query_count") or 0),
            # `None` persistat trebuie să se întoarcă `None`: citit ca `0` ar transforma
            # „nu se judecă în timp" în „prag zero", adică exact verdictul opus.
            sla_s=_sla_from_jsonb(raw.get("sla_s")),
        )


# ── Builder (PUR — hidratarea e a apelantului) ──────────────────────────────────────────────
def _price_facts(
    row: Mapping[str, Any],
    variant: Mapping[str, Any] | None,
    *,
    source: str,
    observed: datetime | None,
    age_s: int | None,
    sla_s: int | None,
) -> dict[str, Fact]:
    """Preț efectiv + preț tăiat + monedă. Trei reguli inseparabile:

    1. moneda lipsă ⇒ preț UNKNOWN (nu formatăm o sumă fără unitate);
    2. faptele VARIANTEI bat faptele produsului acolo unde varianta e mai specifică;
    3. `list_price` există DOAR ca reducere reală (`list > current`) — altfel e omis, nu zero.
    """
    facts: dict[str, Fact] = {}
    currency = row.get("currency")
    currency_fact = (
        Fact.known("currency", str(currency).upper(), source=source)
        if isinstance(currency, str) and currency.strip()
        else Fact.unknown("currency", "missing_value", source=source)
    )
    facts["currency"] = currency_fact

    price = to_decimal(row.get("price"))
    list_price = to_decimal(row.get("list_price"))
    if variant is not None and to_decimal(variant.get("price")) is not None:
        price = to_decimal(variant.get("price"))
        list_price = to_decimal(variant.get("list_price"))

    if price is None or price < 0:
        facts["price"] = Fact.unknown(
            "price", "missing_value" if price is None else "invalid_value", source=source
        )
    elif not currency_fact.usable:
        facts["price"] = Fact.unknown("price", "missing_value", source=source)
    else:
        facts["price"] = Fact.known(
            "price", price, source=source, verified_at=observed, age_s=age_s, sla_s=sla_s
        )

    if list_price is None or price is None or list_price <= price or not facts["price"].usable:
        facts["list_price"] = Fact.unknown("list_price", "missing_value", source=source)
    else:
        facts["list_price"] = Fact.known(
            "list_price", list_price, source=source, verified_at=observed, age_s=age_s, sla_s=sla_s
        )
    return facts


def _stock_facts(
    row: Mapping[str, Any],
    variant: Mapping[str, Any] | None,
    *,
    source: str,
    observed: datetime | None,
    age_s: int | None,
    sla_s: int | None,
) -> dict[str, Fact]:
    """Disponibilitate + stoc. Faptul mai SPECIFIC câștigă: o variantă cu stoc cunoscut 0 e
    `out_of_stock` pentru acea variantă, chiar dacă produsul e „in_stock" la nivel de clasă."""
    availability = row.get("availability")
    stock = row.get("stock")
    if stock is None:
        stock = row.get("stock_total")
    if variant is not None:
        variant_stock = variant.get("stock")
        if isinstance(variant_stock, int) and not isinstance(variant_stock, bool):
            stock = variant_stock
            if variant_stock <= 0:
                availability = "out_of_stock"
    facts = {
        "availability": (
            Fact.known(
                "availability",
                str(availability),
                source=source,
                verified_at=observed,
                age_s=age_s,
                sla_s=sla_s,
            )
            if isinstance(availability, str) and availability.strip()
            else Fact.unknown("availability", "missing_value", source=source)
        ),
        "stock": (
            Fact.known(
                "stock", int(stock), source=source, verified_at=observed, age_s=age_s, sla_s=sla_s
            )
            if isinstance(stock, int) and not isinstance(stock, bool) and stock >= 0
            else Fact.unknown("stock", "missing_value", source=source)
        ),
    }
    return facts


def _review_facts(row: Mapping[str, Any], *, source: str) -> dict[str, Fact]:
    """Rating + număr + rezumat. `review_count == 0` ⇒ rating UNKNOWN: default-ul DB e 0, iar
    a afișa „0 din 5" ar transforma absența recenziilor într-o notă proastă."""
    count = row.get("review_count")
    has_reviews = isinstance(count, int) and not isinstance(count, bool) and count > 0
    rating = to_decimal(row.get("rating"))
    facts = {
        "rating": (
            Fact.known("rating", rating, source=source)
            if has_reviews and rating is not None and 0 <= rating <= 5
            else Fact.unknown(
                "rating", "zero_reviews" if not has_reviews else "missing_value", source=source
            )
        ),
        "review_count": (
            Fact.known("review_count", int(count), source=source)
            if has_reviews
            else Fact.unknown("review_count", "zero_reviews", source=source)
        ),
    }
    summary = row.get("review_summary")
    facts["review_summary"] = (
        Fact.known("review_summary", str(summary).strip(), source="catalog.review_summaries")
        if isinstance(summary, str) and summary.strip()
        else Fact.unknown("review_summary", "missing_value", source="catalog.review_summaries")
    )
    return facts


def _variant_row(row: Mapping[str, Any], variant_id: str | None) -> Mapping[str, Any] | None:
    if not variant_id:
        return None
    for candidate in row.get("variants") or []:
        if isinstance(candidate, Mapping) and str(
            candidate.get("id") or candidate.get("variant_id") or ""
        ) == str(variant_id):
            return candidate
    return None


def build_product_evidence(
    row: Mapping[str, Any],
    *,
    business_id: str,
    now: datetime,
    sla_s: int | None,
    variant_id: str | None = None,
    match_class: str = "exact",
    constraints: Sequence[Any] = (),
) -> ProductEvidence | None:
    """Un rând de catalog (deja hidratat) → faptele lui. `None` doar fără identitate: un produs
    fără id nu poate fi nici afișat, nici acționat, nici verificat."""
    product_id = str(row.get("id") or row.get("product_id") or "").strip()
    if not product_id:
        return None
    source = "catalog.products"
    observed = _observed_at(row)
    age_s = max(0, int((now - observed).total_seconds())) if observed is not None else None
    variant = _variant_row(row, variant_id)
    if variant_id and variant is None:
        # Varianta cerută nu aparține produsului: nu inventăm faptele produsului în locul ei.
        return ProductEvidence(
            product_id=product_id,
            business_id=str(row.get("business_id") or business_id),
            variant_id=variant_id,
            match_class=match_class,
            facts={
                name: Fact.unknown(name, "variant_mismatch", source=source) for name in FACT_FIELDS
            },
            constraints=_verdicts(constraints),
        )

    facts: dict[str, Fact] = {}
    name = row.get("name")
    facts["identity"] = Fact.known("identity", product_id, source=source)
    facts["title"] = (
        Fact.known("title", str(name).strip(), source=source)
        if isinstance(name, str) and name.strip()
        else Fact.unknown("title", "missing_value", source=source)
    )
    brand = row.get("brand")
    facts["brand"] = (
        Fact.known("brand", str(brand).strip(), source="catalog.brands")
        if isinstance(brand, str) and brand.strip()
        else Fact.unknown("brand", "missing_value", source="catalog.brands")
    )
    url = row.get("url") or row.get("product_url")
    facts["url"] = (
        Fact.known("url", str(url).strip(), source=source)
        if isinstance(url, str) and url.strip()
        else Fact.unknown("url", "missing_value", source=source)
    )
    image = row.get("image") or row.get("image_url")
    facts["image"] = (
        Fact.known("image", str(image).strip(), source="catalog.product_images")
        if isinstance(image, str) and image.strip()
        else Fact.unknown("image", "missing_value", source="catalog.product_images")
    )
    variant_label = (variant or {}).get("label") if variant else None
    facts["variant"] = (
        Fact.known("variant", str(variant_label).strip(), source="catalog.product_variants")
        if isinstance(variant_label, str) and variant_label.strip()
        else Fact.unknown("variant", "missing_value", source="catalog.product_variants")
    )
    facts.update(
        _price_facts(row, variant, source=source, observed=observed, age_s=age_s, sla_s=sla_s)
    )
    facts.update(
        _stock_facts(row, variant, source=source, observed=observed, age_s=age_s, sla_s=sla_s)
    )
    facts.update(_review_facts(row, source=source))
    # Structural absente în mediul curent — declarate, nu tăcute (vezi STRUCTURALLY_UNSOURCED).
    for unsourced in STRUCTURALLY_UNSOURCED:
        facts[unsourced] = Fact.unknown(unsourced, "no_source")
    return ProductEvidence(
        product_id=product_id,
        business_id=str(row.get("business_id") or business_id),
        variant_id=variant_id,
        match_class=match_class,
        facts=facts,
        constraints=_verdicts(constraints),
    )


def _verdicts(raw: Sequence[Any]) -> tuple[ConstraintVerdictFact, ...]:
    """`ConstraintResult` (NX-187/238) sau dict → fapte de verdict. Tolerant la formă, strict la
    vocabular: un verdict necunoscut e DROPAT, nu tratat ca MATCH."""
    out: list[ConstraintVerdictFact] = []
    for item in raw or ():
        facet = getattr(item, "facet", None)
        verdict = getattr(item, "status", None) or getattr(item, "verdict", None)
        strength = getattr(item, "strength", "hard")
        reason = getattr(item, "reason", "") or ""
        if facet is None and isinstance(item, Mapping):
            facet = item.get("facet")
            verdict = item.get("verdict") or item.get("status")
            strength = item.get("strength", "hard")
            reason = item.get("reason") or ""
        if not isinstance(facet, str) or verdict not in ("MATCH", "MISMATCH", "UNKNOWN"):
            continue
        out.append(
            ConstraintVerdictFact(
                facet=facet,
                verdict=verdict,
                strength=strength if strength in ("hard", "soft") else "hard",
                reason=str(reason)[:64],
            )
        )
    return tuple(out)


def cart_evidence_from_snapshot(snapshot: Any) -> CartEvidence | None:
    """`CartSnapshot` (NX-237) → fapte. Numerele rămân numere: display-ul îl face projectorul,
    cu regulile de locale — un snapshot cu string-uri formatate deja ar fi al doilea formatter."""
    if snapshot is None:
        return None
    try:
        totals = snapshot.totals
        lines = tuple(
            CartLineEvidence(
                product_id=str(line.product_id),
                variant_id=str(line.variant_id) if line.variant_id else None,
                name=str(line.name or ""),
                variant_label=str(line.variant_label) if line.variant_label else None,
                quantity=int(line.quantity),
                unit_price=to_decimal(line.unit_price),
                line_total=to_decimal(line.line_total),
                currency=str(line.currency) if line.currency else None,
                facts_status=str(line.facts_status),
            )
            for line in snapshot.lines[:MAX_BUNDLE_CART_LINES]
        )
        return CartEvidence(
            cart_id=str(snapshot.cart_id) if snapshot.cart_id else None,
            version=int(snapshot.version),
            status=str(snapshot.status),
            lines=lines,
            total=to_decimal(totals.value),
            currency=str(totals.currency) if totals.currency else None,
            total_status=str(totals.status),
            units=int(totals.units),
            checkout_eligible=bool(snapshot.checkout_eligible),
            blocked_reasons=tuple(str(r) for r in snapshot.blocked_reasons),
        )
    except (AttributeError, TypeError, ValueError):
        # Un snapshot de altă formă nu blochează turul: coșul devine pur și simplu absent.
        return None


def build_evidence_bundle(
    *,
    business_id: str,
    locale: str,
    rows: Sequence[Mapping[str, Any]],
    now: datetime,
    sla_s: int | None,
    variant_by_product: Mapping[str, str | None] | None = None,
    match_class_by_product: Mapping[str, str] | None = None,
    constraints_by_product: Mapping[str, Sequence[Any]] | None = None,
    cart: Any = None,
    query_count: int = 0,
) -> EvidenceBundle:
    """Rândurile retrievate + verdictele + coșul → bundle imuabil. PUR și mărginit.

    Ordinea rândurilor se păstrează (e ordinea providerului de retrieval), duplicatele pe
    `product_id` se colapsează la prima apariție, iar capul e `MAX_BUNDLE_PRODUCTS`."""
    variants = variant_by_product or {}
    classes = match_class_by_product or {}
    constraints = constraints_by_product or {}
    seen: set[str] = set()
    products: list[ProductEvidence] = []
    for row in rows:
        if len(products) >= MAX_BUNDLE_PRODUCTS:
            break
        if not isinstance(row, Mapping):
            continue
        product_id = str(row.get("id") or row.get("product_id") or "").strip()
        if not product_id or product_id in seen:
            continue
        evidence = build_product_evidence(
            row,
            business_id=business_id,
            now=now,
            sla_s=sla_s,
            variant_id=variants.get(product_id),
            match_class=classes.get(product_id, "exact"),
            constraints=constraints.get(product_id, ()),
        )
        if evidence is None:
            continue
        seen.add(product_id)
        products.append(evidence)
    return EvidenceBundle(
        business_id=business_id,
        locale=locale,
        as_of=now.astimezone(UTC),
        products=tuple(products),
        cart=cart_evidence_from_snapshot(cart),
        query_count=query_count,
        sla_s=sla_s,
    )


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "FACT_FIELDS",
    "FIELD_POLICY",
    "MAX_BUNDLE_PRODUCTS",
    "STRUCTURALLY_UNSOURCED",
    "CartEvidence",
    "CartLineEvidence",
    "ConstraintVerdictFact",
    "EvidenceBundle",
    "Fact",
    "FieldPolicy",
    "FactStatus",
    "ProductEvidence",
    "build_evidence_bundle",
    "build_product_evidence",
    "cart_evidence_from_snapshot",
]
