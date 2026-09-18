"""Derivare badge de card (IZI) — generic, din SEMNALE de produs + reguli per-vertical.

Badge-ul de card („Top Favorit", „Super Preț") NU se inventează: se DERIVĂ determinist din date
reale — rating shrunk + nr. recenzii pentru „top", reducere reală (list_price vs price) pentru
„deal". Pragurile vin din `DomainPack.badge_rules` (config per-vertical, ne-hardcodat), cu
default-uri agnostice de vertical. Etichetele sunt per-locale (text UI, NU rutare). UN singur
badge per card (prioritate: deal > top — reducerea e semnalul de conversie cel mai tare).

Pur: fără I/O, fără LLM. Gated de `card_badges_enabled` la apelant (compose). Conservator prin
construcție (praguri sus → puține badge-uri) → nu „spamează" carduri cu etichete.
"""

from __future__ import annotations

from typing import Any

# Default-uri AGNOSTICE de vertical (override per-tenant în DomainPack.badge_rules).
_DEFAULT_RULES: dict[str, float] = {
    "top_rating": 4.7,  # rating shrunk minim pt „Top Favorit"
    "top_reviews": 50,  # nr. recenzii minim (evită 5★-cu-1-recenzie → badge fals)
    "deal_discount_pct": 20.0,  # reducere % minimă (list_price vs price) pt „Super Preț"
    # NX-299: reducerea % minimă a VOUCHERULUI (coupon_price vs price). Pragul e mai jos decât cel
    # de `deal` fiindcă semnalul e diferit: „Super Preț" spune că produsul E ieftin, voucherul
    # spune doar că mai poți scădea ceva la finalizare. Sub prag nu merită un badge.
    "coupon_discount_pct": 5.0,
}

# Etichete per-locale (UI). „top"/„deal" → textul afișat pe card.
_LABELS: dict[str, dict[str, str]] = {
    "ro": {"top": "Top Favorit", "deal": "Super Preț", "coupon": "Voucher -{pct}%"},
    "en": {"top": "Top Favorite", "deal": "Great Deal", "coupon": "Coupon -{pct}%"},
}

# Full-eMAG: tonul semantic al badge-ului (pt `badges:[{label,tone}]`). Reducere = accent tare
# (danger, ca „Super Preț" eMAG); top/curare = info. NEUTRU de locale (kind → tone).
BADGE_TONE: dict[str, str] = {"deal": "danger", "coupon": "danger", "top": "info"}


def derive_badge_kind(
    product: dict[str, Any],
    rules: dict[str, float] | None = None,
    *,
    coupon_enabled: bool = True,
) -> str | None:
    """KIND-ul semantic al badge-ului („deal"/„top") sau `None`, NEUTRU de locale. Prioritate:
    `deal` (reducere reală ≥ prag) > `top` (rating ≥ prag ȘI nr. recenzii ≥ prag). Forward-safe:
    câmpuri lipsă → None. `list_price`/`price`/`rating`/`review_count` din date."""
    r = {**_DEFAULT_RULES, **(rules or {})}

    price = product.get("price")
    list_price = product.get("list_price")  # populat DOAR la reducere reală (vezi _SELECT)
    if price and list_price:
        try:
            lp, pr = float(list_price), float(price)
        except (TypeError, ValueError):
            lp = pr = 0.0
        if lp > pr > 0 and (lp - pr) / lp * 100 >= r["deal_discount_pct"]:
            return "deal"

    # NX-299: voucherul, sub `deal` și peste `top`. Ordinea nu e de gust: o reducere pe care o ai
    # DEJA în preț bate una pe care o obții la finalizare, iar amândouă bat o etichetă de reputație.
    if coupon_enabled and coupon_discount_pct(product) is not None:
        return "coupon"

    rating = product.get("rating")
    review_count = product.get("review_count") or 0
    try:
        if (
            rating is not None
            and float(rating) >= r["top_rating"]
            and int(review_count) >= int(r["top_reviews"])
        ):
            return "top"
    except (TypeError, ValueError):
        return None
    return None


def coupon_discount_pct(product: dict[str, Any]) -> int | None:
    """Reducerea voucherului, în procente ÎNTREGI rotunjite în JOS, sau `None`.

    Rotunjirea în jos, ca la reducerile din `web/localization`: promisiunea afișată trebuie să
    rămână adevărată chiar dacă prețul se mișcă puțin între momentul randării și cel al plății.
    Un cupon fără preț, cu preț mai mare decât cel curent sau sub prag nu produce badge: coloana
    `coupon_without_discount` era deja o anomalie cunoscută la import (`catalog/sole_source.py`).
    """
    code = product.get("coupon_code")
    coupon = product.get("coupon_price")
    price = product.get("price")
    if not code or coupon is None or not price:
        return None
    try:
        cp, pr = float(coupon), float(price)
    except (TypeError, ValueError):
        return None
    if not (0 < cp < pr):
        return None
    pct = int((pr - cp) / pr * 100)
    return pct if pct >= _DEFAULT_RULES["coupon_discount_pct"] else None


def badge_label(
    kind: str | None, language: str | None, product: dict[str, Any] | None = None
) -> str | None:
    """Eticheta localizată a unui KIND de badge (sau None).

    NX-299: eticheta voucherului poartă CIFRA, iar cifra se calculează din două coloane reale
    (`coupon_price` vs `price`), exact ca procentul din spatele lui „Super Preț". De-aia trece
    pe lângă `_safe_badge`, care respinge etichetele cu cifre venite din catalog: acelea sunt
    text al furnizorului, pe care nimic nu-l poate confrunta cu prețul afișat."""
    if not kind:
        return None
    template = (_LABELS.get(language or "ro") or _LABELS["ro"]).get(kind)
    if template is None:
        return None
    if "{pct}" not in template:
        return template
    pct = coupon_discount_pct(product or {})
    return template.format(pct=pct) if pct is not None else None


def derive_badge(
    product: dict[str, Any], language: str | None, rules: dict[str, float] | None = None
) -> str | None:
    """Badge derivat (eticheta localizată) din semnalele produsului sau `None`. Wrapper peste
    `derive_badge_kind` + `badge_label` (back-compat — semnătură/retur neschimbate)."""
    return badge_label(derive_badge_kind(product, rules), language, product)
