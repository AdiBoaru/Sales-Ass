"""NX-234 — rehidratarea CANONICĂ a contextului de pagină: ID-uri opace → fapte tenant-scoped.

Aici se schimbă natura datelor. Ce intră sunt referințe pe care le-a afirmat un browser; ce iese
sunt fapte citite din catalog, fiecare cu sursă și vechime. Între cele două stă un singur query
(`load_context_entities`) și un set de reguli care nu depind de disciplină:

  • **Tenantul e poarta, nu un filtru.** Fiecare ramură a query-ului are `business_id = $1`. Un id
    care există la ALT tenant nu se întoarce — deci e indistinct de unul inexistent, iar
    „nu e al tău" nu devine un oracol de existență.
  • **Relația se verifică, nu se presupune.** Varianta trebuie să fie A produsului. Nu „o variantă
    validă" — a ACESTUI produs. Un `variant_id` de la alt produs nu e o nepotrivire minoră: e
    singura formă prin care un preț străin ar putea intra în răspuns, deci invalidează TOT
    contextul (zero preț, zero stoc folosit), nu doar varianta.
  • **`UNKNOWN` nu e `0` și nu e `MISMATCH`.** Un produs fără recenzii nu are „0 stele" — n-are
    rating. Un `delivery_class` absent nu e „livrare indisponibilă" — e o promisiune pe care nu o
    putem face. Downstream (NX-240) omite claimul și CTA-ul dependent; nici modelul, nici
    frontendul nu completează golul.
  • **Vechimea e un fapt despre fapt.** `Stale` nu ascunde valoarea (ar rupe pagini întregi de
    catalog care se sincronizează rar), o MARCHEAZĂ: politica de disclosure/refresh e a
    consumatorului, dar ca s-o poată aplica trebuie să știe.

Ce NU face modulul: nu citește coșul (nu există sursă canonică până la NX-237 — `resolve_cart`
întoarce onest `unavailable`, niciodată `ConversationState.cart` deghizat în coș de magazin), nu
compune text, nu atinge promptul și nu ține o conexiune peste nimic extern.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from src.config import get_settings
from src.db.provider import DbProvider
from src.db.queries.catalog import load_context_entities
from src.web.context import ContextRef, ContextStatus, NormalizedContext

log = logging.getLogger(__name__)

# Vechimea, în buckets LOW-CARDINALITY (P12: nicio dată exactă în labels de metrici).
AGE_BUCKETS: tuple[tuple[timedelta, str], ...] = (
    (timedelta(hours=1), "<1h"),
    (timedelta(days=1), "1-24h"),
    (timedelta(days=7), "1-7d"),
)
AGE_BUCKET_OLD = ">7d"
AGE_BUCKET_UNKNOWN = "unknown"

# Câmpuri care NU AU sursă canonică în sistemul de azi. Nu sunt „lipsă temporar": nu există feed.
# Sunt UNKNOWN prin construcție, ca să nu apară niciodată ca fapt derivat dintr-un `updated_at`
# generic (un produs actualizat ieri nu dovedește că voucherul e valabil azi).
STRUCTURALLY_UNKNOWN: frozenset[str] = frozenset({"promotion_eligibility", "delivery_promise"})


def age_bucket(observed_at: datetime | None, *, now: datetime) -> str:
    if observed_at is None:
        return AGE_BUCKET_UNKNOWN
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age = now - observed_at
    for limit, label in AGE_BUCKETS:
        if age < limit:
            return label
    return AGE_BUCKET_OLD


def _parse_ts(value: Any) -> datetime | None:
    """`timestamptz` din jsonb vine ca ISO string; dintr-un fake de test poate veni `datetime`."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True)
class Freshness:
    """Cât de proaspăt e faptul. `stale=True` NU înseamnă „aruncă-l", ci „spune-o"."""

    observed_at: datetime | None
    bucket: str
    stale: bool

    @classmethod
    def of(cls, observed_at: datetime | None, *, now: datetime, sla_s: int | None) -> Freshness:
        bucket = age_bucket(observed_at, now=now)
        if observed_at is None:
            # Fără timestamp nu putem susține prospețimea → tratat ca stale (conservator), nu ca
            # proaspăt prin lipsă de dovadă.
            return cls(None, bucket, True)
        ref = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
        if sla_s is None:
            # Tenantul și-a declarat catalogul static (`src/catalog/freshness.py`): faptul are
            # vârstă (rămâne în `bucket`), dar nu există prag față de care s-o judeci. Vechimea
            # se RAPORTEAZĂ, nu se sancționează.
            return cls(ref, bucket, False)
        return cls(ref, bucket, (now - ref) > timedelta(seconds=sla_s))


@dataclass(frozen=True)
class ProductEvidence:
    """Faptele CANONICE ale produsului de pe pagină. Toate câmpurile vin din catalog; niciunul
    din browser. `unknown` e explicit: consumatorul vede DE CE lipsește ceva, nu doar că e None."""

    product_id: str
    name: str
    url: str | None = None
    image: str | None = None
    brand: str | None = None
    currency: str | None = None
    price: float | None = None
    list_price: float | None = None
    price_source: str | None = None
    availability: str | None = None
    stock_total: int | None = None
    rating: float | None = None
    review_count: int | None = None
    review_summary: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    category_path: str | None = None
    delivery_class: str | None = None
    source: str = "catalog.products"
    freshness: Freshness | None = None
    unknown: frozenset[str] = frozenset()

    @property
    def on_sale(self) -> bool:
        if self.list_price is None or self.price is None:
            return False
        return self.list_price > self.price


@dataclass(frozen=True)
class VariantEvidence:
    variant_id: str
    product_id: str
    label: str | None = None
    sku: str | None = None
    price: float | None = None
    list_price: float | None = None
    stock: int | None = None
    source: str = "catalog.product_variants"
    freshness: Freshness | None = None
    unknown: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CategoryEvidence:
    category_id: str
    name: str | None = None
    slug: str | None = None
    path: str | None = None
    source: str = "catalog.categories"
    freshness: Freshness | None = None


@dataclass(frozen=True)
class CartSnapshot:
    """Coșul canonic e NX-237. Până atunci: `unavailable` cu motiv — un seam onest, nu
    `ConversationState.cart` (ce a pus botul în conversație) prezentat drept coșul magazinului."""

    status: str = "unavailable"
    reason: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class SurfaceContext:
    """Ce ȘTIM despre pagina pe care stă clientul, după rehidratare. Imutabil prin construcție:
    o dată calculat, nimeni nu-l „mai completează puțin" mai târziu (ar însemna două surse de
    adevăr în același turn, exact ce elimină cardul)."""

    surface: str = "other"
    status: ContextStatus = "absent"
    product: ProductEvidence | None = None
    variant: VariantEvidence | None = None
    category: CategoryEvidence | None = None
    cart: CartSnapshot = field(default_factory=CartSnapshot)
    reasons: tuple[str, ...] = ()
    relation_rejections: tuple[tuple[str, str], ...] = ()
    fetched_at: datetime | None = None
    hydration_ms: float = 0.0
    query_count: int = 0

    @property
    def has_evidence(self) -> bool:
        return self.product is not None

    @property
    def anchor_product_id(self) -> str | None:
        """Ancora canonică a paginii — singurul lucru din context pe care agentul îl poate folosi
        ca „produsul acesta". `None` pe orice status care nu poartă evidence."""
        return self.product.product_id if self.product is not None else None


# ── Rehidratare (UN round-trip, indiferent de câte referințe) ────────────────────────────────


def _split_refs(refs: list[ContextRef]) -> tuple[list[str], list[str]]:
    """UUID-uri canonice și chei de platformă, separate: un `::uuid[]` cu un string non-UUID ar
    ridica `DataError` în Postgres — deci separarea e o condiție de corectitudine, nu un stil."""
    uuids = [r.value for r in refs if r.kind == "uuid"]
    keys = [r.value for r in refs if r.kind == "external"]
    return uuids, keys


async def hydrate_refs(
    db: DbProvider,
    business_id: str,
    *,
    products: list[ContextRef] | None = None,
    variants: list[ContextRef] | None = None,
    categories: list[ContextRef] | None = None,
    operation: str = "web_context_hydrate",
) -> dict[str, dict[str, dict[str, Any]]]:
    """Un singur checkout SCURT + un singur query pentru TOATE referințele.

    Întoarce `{kind: {ref_cerut: date}}`. Cheia e referința AȘA CUM a fost cerută (UUID sau cheie
    de platformă) — apelantul nu trebuie să ghicească maparea inversă. Nicio referință nu produce
    query propriu: 1 ref sau 10, tot un round-trip (`tests/test_context_resolver_db.py` îl
    verifică cu 1/6/10 — un N+1 aici ar fi pe drumul sincron al fiecărui turn)."""
    p_uuid, p_key = _split_refs(products or [])
    v_uuid, v_key = _split_refs(variants or [])
    c_uuid, c_key = _split_refs(categories or [])
    if not any((p_uuid, p_key, v_uuid, v_key, c_uuid, c_key)):
        return {"product": {}, "variant": {}, "category": {}}
    async with db(operation) as conn:
        rows = await load_context_entities(
            conn,
            business_id,
            product_uuids=p_uuid,
            product_keys=p_key,
            variant_uuids=v_uuid,
            variant_keys=v_key,
            category_uuids=c_uuid,
            category_keys=c_key,
        )
    out: dict[str, dict[str, dict[str, Any]]] = {"product": {}, "variant": {}, "category": {}}
    for row in rows:
        kind = row.get("kind")
        ref = row.get("ref")
        if kind in out and ref:
            out[kind][str(ref)] = row.get("data") or {}
    return out


def _product_evidence(data: dict[str, Any], *, now: datetime, sla_s: int) -> ProductEvidence:
    """Rândul canonic → evidence, cu UNKNOWN-urile marcate EXPLICIT.

    Regulile care contează (și de ce):
      • `review_count == 0` ⇒ rating UNKNOWN. `products.rating` are `default 0`, deci un produs
        fără recenzii arată în DB exact ca unul evaluat cu zero. „0 stele" e o afirmație; absența
        recenziilor nu e.
      • `stock_total` NULL ⇒ UNKNOWN, dar `availability` rămâne cunoscut: sunt două fapte
        diferite, iar clasa de disponibilitate e cea pe care catalogul chiar o întreține.
      • promisiunea de livrare NU intră niciodată aici: depinde de CEAS (NX-191). Ce ținem e
        CLASA, un fapt stabil; promisiunea se calculează la compunere sau deloc."""
    unknown: set[str] = set(STRUCTURALLY_UNKNOWN)
    price = data.get("price")
    if price is None:
        unknown.add("price")
    review_count = data.get("review_count")
    rating = data.get("rating")
    if not review_count:
        unknown.add("rating")
        rating = None
        review_count = None if review_count is None else 0
    if data.get("stock_total") is None:
        unknown.add("stock_total")
    if not (data.get("review_summary") or "").strip():
        unknown.add("review_summary")
    if not data.get("delivery_class"):
        unknown.add("delivery_class")
    if not data.get("url"):
        unknown.add("url")
    if not data.get("image"):
        unknown.add("image")
    observed = _parse_ts(data.get("synced_at")) or _parse_ts(data.get("updated_at"))
    return ProductEvidence(
        product_id=str(data.get("id")),
        name=str(data.get("name") or ""),
        url=data.get("url"),
        image=data.get("image"),
        brand=data.get("brand"),
        currency=data.get("currency"),
        price=float(price) if price is not None else None,
        list_price=float(data["list_price"]) if data.get("list_price") is not None else None,
        price_source=data.get("price_source"),
        availability=data.get("availability"),
        stock_total=data.get("stock_total"),
        rating=float(rating) if rating is not None else None,
        review_count=review_count,
        review_summary=(data.get("review_summary") or None),
        category_id=data.get("category_id"),
        category_name=data.get("category_name"),
        category_path=data.get("category_path"),
        delivery_class=data.get("delivery_class"),
        freshness=Freshness.of(observed, now=now, sla_s=sla_s),
        unknown=frozenset(unknown),
    )


def _variant_evidence(data: dict[str, Any], *, now: datetime, sla_s: int) -> VariantEvidence:
    unknown: set[str] = set()
    if data.get("price") is None:
        unknown.add("price")
    if data.get("stock") is None:
        unknown.add("stock")
    return VariantEvidence(
        variant_id=str(data.get("id")),
        product_id=str(data.get("product_id")),
        label=data.get("label"),
        sku=data.get("sku"),
        price=float(data["price"]) if data.get("price") is not None else None,
        list_price=float(data["list_price"]) if data.get("list_price") is not None else None,
        stock=data.get("stock"),
        freshness=Freshness.of(_parse_ts(data.get("updated_at")), now=now, sla_s=sla_s),
        unknown=frozenset(unknown),
    )


def _category_evidence(data: dict[str, Any], *, now: datetime, sla_s: int) -> CategoryEvidence:
    return CategoryEvidence(
        category_id=str(data.get("id")),
        name=data.get("name"),
        slug=data.get("slug"),
        path=data.get("path"),
        freshness=Freshness.of(_parse_ts(data.get("updated_at")), now=now, sla_s=sla_s),
    )


def categories_compatible(product: ProductEvidence, category: CategoryEvidence) -> bool:
    """Categoria cerută e compatibilă cu produsul? Egală, strămoș sau descendent în
    `categories.path` (materialized path, ca `_category_clause` din NX-167).

    Compatibilitatea e o poartă de IGNORARE, nu un filtru dur: o categorie incompatibilă se scoate
    din context (failure matrix), nu restrânge căutarea și nu invalidează produsul. Fără `path` pe
    vreunul dintre capete rămâne doar egalitatea de id — fail-closed pe date lipsă, fiindcă aici
    „nu știu" nu are voie să devină „da"."""
    if product.category_id and category.category_id == product.category_id:
        return True
    p_path = (product.category_path or "").strip("/")
    c_path = (category.path or "").strip("/")
    if not p_path or not c_path:
        return False
    return p_path == c_path or p_path.startswith(c_path + "/") or c_path.startswith(p_path + "/")


def resolve_cart(normalized: NormalizedContext) -> CartSnapshot:
    """Coșul canonic e NX-237. Aici doar consemnăm onest că nu avem sursă.

    Tentația e să citim `ConversationState.cart` (există, e la îndemână) sau payload-ul din
    browser. Amândouă ar produce un „coș" care nu e coșul magazinului — adică exact tipul de fapt
    inventat pe care cardul îl interzice. Un `unavailable` cu motiv e mai util decât un total
    greșit: downstream omite CTA-ul dependent în loc să promită o comandă."""
    if not normalized.cart_ref:
        return CartSnapshot(status="unavailable", reason="no_cart_ref")
    return CartSnapshot(status="unavailable", reason="no_canonical_cart_source", ref=None)


async def resolve_surface_context(
    db: DbProvider,
    business_id: str,
    normalized: NormalizedContext | None,
    *,
    now: datetime | None = None,
) -> SurfaceContext:
    """Contextul afirmat → `SurfaceContext` canonic. Un checkout SCURT, un query, zero excepții
    propagate: orice eșec de catalog devine `unavailable` (terminal onest downstream, P6).

    Ordinea e deliberată: hidratare → validare de relație → proiecție. Validarea NU poate rula
    înaintea hidratării (n-ar avea ce compara), iar proiecția nu are voie să ruleze înaintea
    validării (ar expune fapte pe care tocmai le respingem)."""
    s = get_settings()
    now = now or datetime.now(UTC)
    if normalized is None or normalized.is_empty:
        return SurfaceContext(
            surface=normalized.surface if normalized else "other",
            status="absent",
            reasons=(normalized.reasons if normalized else ()) or ("context_absent",),
            cart=resolve_cart(normalized) if normalized else CartSnapshot(reason="no_cart_ref"),
            fetched_at=now,
        )

    reasons: list[str] = list(normalized.reasons)
    rejections: list[tuple[str, str]] = []
    started = perf_counter()
    try:
        entities = await asyncio.wait_for(
            hydrate_refs(
                db,
                business_id,
                products=[normalized.product] if normalized.product else [],
                variants=[normalized.variant] if normalized.variant else [],
                categories=[normalized.category] if normalized.category else [],
            ),
            timeout=s.web_context_hydration_timeout_ms / 1000.0,
        )
    except TimeoutError:
        log.warning(
            "web_context: rehidratare peste deadline (%sms)",
            s.web_context_hydration_timeout_ms,
        )
        return SurfaceContext(
            surface=normalized.surface,
            status="unavailable",
            reasons=(*reasons, "hydration_timeout"),
            cart=resolve_cart(normalized),
            fetched_at=now,
            hydration_ms=round((perf_counter() - started) * 1000.0, 1),
            query_count=1,
        )
    except Exception:  # noqa: BLE001 — catalogul jos nu are voie să omoare turul (P6)
        log.exception("web_context: rehidratare eșuată")
        return SurfaceContext(
            surface=normalized.surface,
            status="unavailable",
            reasons=(*reasons, "hydration_failed"),
            cart=resolve_cart(normalized),
            fetched_at=now,
            hydration_ms=round((perf_counter() - started) * 1000.0, 1),
            query_count=1,
        )
    hydration_ms = round((perf_counter() - started) * 1000.0, 1)
    sla = s.web_context_freshness_sla_s

    product: ProductEvidence | None = None
    if normalized.product is not None:
        raw = entities["product"].get(normalized.product.value)
        if raw:
            product = _product_evidence(raw, now=now, sla_s=sla)
        else:
            # Inexistent, nepublicat SAU al altui tenant — aceeași semantică externă, deliberat:
            # o distincție aici ar fi un oracol de existență cross-tenant.
            reasons.append("anchor_not_found")

    variant: VariantEvidence | None = None
    if normalized.variant is not None:
        raw_v = entities["variant"].get(normalized.variant.value)
        if not raw_v:
            reasons.append("variant_not_found")
        elif product is None:
            reasons.append("variant_without_resolved_product")
        else:
            candidate = _variant_evidence(raw_v, now=now, sla_s=sla)
            if candidate.product_id != product.product_id:
                # Tamper, nu neglijență: singura cale prin care un preț de la alt produs ar putea
                # intra în răspuns. Contextul CADE în întregime — zero preț, zero stoc folosit.
                rejections.append(("variant_product", "cross_product"))
                return SurfaceContext(
                    surface=normalized.surface,
                    status="invalid",
                    reasons=(*reasons, "variant_product_mismatch"),
                    relation_rejections=tuple(rejections),
                    cart=resolve_cart(normalized),
                    fetched_at=now,
                    hydration_ms=hydration_ms,
                    query_count=1,
                )
            variant = candidate

    category: CategoryEvidence | None = None
    if normalized.category is not None:
        raw_c = entities["category"].get(normalized.category.value)
        if not raw_c:
            reasons.append("category_not_found")
        else:
            candidate_c = _category_evidence(raw_c, now=now, sla_s=sla)
            if product is not None and not categories_compatible(product, candidate_c):
                rejections.append(("category_product", "incompatible"))
                reasons.append("category_incompatible")
            else:
                category = candidate_c

    status = _status_for(normalized, product=product, category=category, reasons=reasons)
    return SurfaceContext(
        surface=normalized.surface,
        status=status,
        product=product,
        variant=variant,
        category=category,
        cart=resolve_cart(normalized),
        reasons=tuple(reasons),
        relation_rejections=tuple(rejections),
        fetched_at=now,
        hydration_ms=hydration_ms,
        query_count=1,
    )


def _status_for(
    normalized: NormalizedContext,
    *,
    product: ProductEvidence | None,
    category: CategoryEvidence | None,
    reasons: list[str],
) -> ContextStatus:
    """Statusul rezultat, ca funcție PURĂ de ce s-a rezolvat — testabilă fără DB.

    `stale` e un status de sine stătător (nu un flag pe `resolved`) fiindcă e exact distincția
    pe care downstream o tratează diferit: `resolved` poate afirma, `stale` trebuie să spună de
    când. `partial` = am ancorat ceva, dar nu tot ce cerea pagina."""
    anchored = product is not None or category is not None
    if not anchored:
        return "partial" if normalized.surface != "other" else "absent"
    if product is not None and product.freshness is not None and product.freshness.stale:
        return "stale"
    wanted = sum(x is not None for x in (normalized.product, normalized.category))
    got = sum(x is not None for x in (product, category))
    if got < wanted or "category_incompatible" in reasons:
        return "partial"
    return "resolved"


__all__ = [
    "AGE_BUCKET_OLD",
    "AGE_BUCKET_UNKNOWN",
    "STRUCTURALLY_UNKNOWN",
    "CartSnapshot",
    "CategoryEvidence",
    "Freshness",
    "ProductEvidence",
    "SurfaceContext",
    "VariantEvidence",
    "age_bucket",
    "categories_compatible",
    "hydrate_refs",
    "resolve_cart",
    "resolve_surface_context",
]
