"""NX-195 — proiecția faptelor comerciale în ce vede modelul: promisiunea de livrare + pragul de
transport gratuit, calculate DETERMINIST din config + ceasul magazinului.

De ce un modul separat și nu inline în tool: `delivery.promise()` e pur (primește `now`), iar
tool-urile au `TurnContext`. Aici se face exact o traducere — context → argumente — plus decizia
care contează la runtime: **dacă textul rezultat are componentă de ceas, răspunsul NU e cacheabil.**
Un hit de mâine ar servi „mai ai 2 ore" la ora 20:00; am mai avut fix bug-ul ăsta pe răspunsurile
de tip „n-am găsit".

Ceasul e al MAGAZINULUI (`businesses.timezone`), nu al serverului: un magazin din București cu
ora-limită 14:00 nu trebuie să promită altceva pentru că procesul rulează pe UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.commerce.config import CommerceConfig, load_commerce_config
from src.commerce.delivery import DeliveryPromise, free_shipping_gap, promise

#: fusuri uzuale fără dependență externă (zoneinfo lipsește pe unele imagini slim). Necunoscut →
#: UTC; promisiunea rămâne corectă ca ZI, ora-limită poate fi decalată cu o oră — acceptabil.
_TZ_OFFSETS = {
    "Europe/Bucharest": 3,
    "Europe/Chisinau": 3,
    "Europe/Berlin": 2,
    "Europe/London": 1,
    "UTC": 0,
}


def store_now(business: Any) -> datetime:
    """Ora curentă în fusul magazinului. Fără `zoneinfo` (imagini slim) → offset fix, suficient
    pentru o graniță pe oră/zi."""
    tz = getattr(business, "timezone", None) or "UTC"
    offset = _TZ_OFFSETS.get(tz, 0)
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=offset)


def commerce_config(business: Any) -> CommerceConfig:
    return load_commerce_config(getattr(business, "settings", None) or {})


def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def delivery_for(product: dict[str, Any], business: Any) -> DeliveryPromise:
    """Promisiunea de livrare pentru un produs, la ora magazinului. Produs fără `delivery_class`
    → promisiune goală (botul tace pe subiect, nu improvizează)."""
    cfg = commerce_config(business)
    return promise(
        delivery_class=product.get("delivery_class"),
        shipping=cfg.shipping,
        now=store_now(business),
        restock_date=_as_date(product.get("restock_date")),
    )


def free_shipping_hint(cart_total: float, business: Any) -> str | None:
    """„Mai adaugă X lei și ai transportul gratuit" — DOAR când pragul există și nu e atins.
    Un „mai adaugă 0 lei" e mai rău decât tăcerea."""
    cfg = commerce_config(business)
    gap = free_shipping_gap(cart_total, cfg.shipping)
    if gap is None:
        return None
    return f"mai adaugă {gap:.2f} lei și ai transportul gratuit"
