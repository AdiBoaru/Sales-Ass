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
}

# Etichete per-locale (UI). „top"/„deal" → textul afișat pe card.
_LABELS: dict[str, dict[str, str]] = {
    "ro": {"top": "Top Favorit", "deal": "Super Preț"},
    "en": {"top": "Top Favorite", "deal": "Great Deal"},
    "hu": {"top": "Top kedvenc", "deal": "Szuper ár"},
}


def derive_badge(
    product: dict[str, Any], language: str | None, rules: dict[str, float] | None = None
) -> str | None:
    """Badge derivat din semnalele unui produs (dict de retrieval) sau `None`. Prioritate:
    `deal` (reducere reală ≥ prag) > `top` (rating ≥ prag ȘI nr. recenzii ≥ prag). Forward-safe:
    câmpuri lipsă → fără badge (nu crapă). `list_price`/`price`/`rating`/`review_count` din date."""
    lang = language or "ro"
    labels = _LABELS.get(lang) or _LABELS["ro"]
    r = {**_DEFAULT_RULES, **(rules or {})}

    price = product.get("price")
    list_price = product.get("list_price")  # populat DOAR la reducere reală (vezi _SELECT)
    if price and list_price:
        try:
            lp, pr = float(list_price), float(price)
        except (TypeError, ValueError):
            lp = pr = 0.0
        if lp > pr > 0:
            discount_pct = (lp - pr) / lp * 100
            if discount_pct >= r["deal_discount_pct"]:
                return labels["deal"]

    rating = product.get("rating")
    review_count = product.get("review_count") or 0
    try:
        if rating is not None and float(rating) >= r["top_rating"] and int(review_count) >= int(
            r["top_reviews"]
        ):
            return labels["top"]
    except (TypeError, ValueError):
        return None
    return None
