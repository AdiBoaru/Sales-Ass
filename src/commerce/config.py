"""NX-191 — config comercial per magazin, din `businesses.settings` (P9: din DB, nu hardcodat).

De ce tipizat și nu `settings.get(...)` împrăștiat prin cod: fiecare cifră de aici ajunge într-o
frază pe care botul o spune clientului („transport gratuit de la 199 lei", „ajunge mâine"). Un
`None` scăpat devine „transport gratuit de la None lei". Parserul e FAIL-SAFE: chei lipsă/stricate
→ valorile implicite de mai jos, niciodată excepție pe calea caldă.

Forma în `businesses.settings`:

    {
      "prices_include_vat": true,
      "shipping": {"cutoff_hour": 14, "working_days": [1,2,3,4,5],
                   "cost": 19.99, "free_threshold": 199.0, "courier": "Cargus",
                   "class_days": {"standard": [2,4], "supplier": [5,7]}},
      "returns":  {"days": 14, "from": "delivery"},
      "payment":  {"methods": ["card", "ramburs"]}
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Implicite prudente: dacă un tenant n-are config, botul NU inventează termene scurte.
_DEFAULT_CLASS_DAYS: dict[str, tuple[int, int]] = {
    # clasa → (min_zile_lucrătoare, max_zile_lucrătoare). `next_day` e tratat separat
    # (depinde de ora-limită), de aceea nu apare aici cu 1-1 orb.
    "standard": (2, 4),
    "supplier": (5, 7),
    "preorder": (10, 14),
}
_DEFAULT_WORKING_DAYS = (1, 2, 3, 4, 5)  # ISO: 1=luni … 7=duminică


def _as_float(v: Any, default: float | None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f >= 0 else default


def _as_int(v: Any, default: int | None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ShippingConfig:
    """Politica de livrare a magazinului. `cutoff_hour` + `working_days` sunt singurele
    ingrediente de care are nevoie promisiunea „comandă până la X → ajunge mâine"."""

    cutoff_hour: int | None = None  # None → nu promitem niciodată livrare a doua zi
    working_days: tuple[int, ...] = _DEFAULT_WORKING_DAYS
    cost: float | None = None
    free_threshold: float | None = None
    courier: str | None = None
    class_days: dict[str, tuple[int, int]] = field(
        default_factory=lambda: dict(_DEFAULT_CLASS_DAYS)
    )

    @property
    def promises_next_day(self) -> bool:
        """Fără oră-limită NU există promisiune de livrare a doua zi (nu o inventăm)."""
        return self.cutoff_hour is not None


@dataclass(frozen=True)
class ReturnsConfig:
    days: int | None = None
    # 'delivery' = termenul curge de la primirea coletului (decizia userului: la nivel de COMANDĂ,
    # nu de produs); 'order' = de la plasarea comenzii.
    from_event: str = "delivery"


@dataclass(frozen=True)
class PaymentConfig:
    methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommerceConfig:
    prices_include_vat: bool = True
    shipping: ShippingConfig = field(default_factory=ShippingConfig)
    returns: ReturnsConfig = field(default_factory=ReturnsConfig)
    payment: PaymentConfig = field(default_factory=PaymentConfig)


def load_commerce_config(settings: dict[str, Any] | None) -> CommerceConfig:
    """`businesses.settings` → CommerceConfig. Fail-safe: orice cheie stricată cade pe implicit."""
    s = settings if isinstance(settings, dict) else {}

    raw_ship = s.get("shipping")
    raw_ship = raw_ship if isinstance(raw_ship, dict) else {}

    days = raw_ship.get("working_days")
    working = (
        tuple(
            d
            for d in (days if isinstance(days, list) else [])
            if isinstance(d, int) and 1 <= d <= 7
        )
        or _DEFAULT_WORKING_DAYS
    )

    cutoff = _as_int(raw_ship.get("cutoff_hour"), None)
    if cutoff is not None and not (0 <= cutoff <= 23):
        cutoff = None

    class_days = dict(_DEFAULT_CLASS_DAYS)
    raw_cd = raw_ship.get("class_days")
    if isinstance(raw_cd, dict):
        for k, v in raw_cd.items():
            if isinstance(v, list) and len(v) == 2:
                lo, hi = _as_int(v[0], None), _as_int(v[1], None)
                if lo is not None and hi is not None and 0 < lo <= hi:
                    class_days[str(k)] = (lo, hi)

    raw_ret = s.get("returns")
    raw_ret = raw_ret if isinstance(raw_ret, dict) else {}
    from_event = raw_ret.get("from")
    from_event = from_event if from_event in ("delivery", "order") else "delivery"

    raw_pay = s.get("payment")
    raw_pay = raw_pay if isinstance(raw_pay, dict) else {}
    methods = tuple(
        str(m) for m in (raw_pay.get("methods") or []) if isinstance(m, str) and m.strip()
    )

    vat = s.get("prices_include_vat")

    return CommerceConfig(
        prices_include_vat=bool(vat) if isinstance(vat, bool) else True,
        shipping=ShippingConfig(
            cutoff_hour=cutoff,
            working_days=working,
            cost=_as_float(raw_ship.get("cost"), None),
            free_threshold=_as_float(raw_ship.get("free_threshold"), None),
            courier=(raw_ship.get("courier") or None),
            class_days=class_days,
        ),
        returns=ReturnsConfig(days=_as_int(raw_ret.get("days"), None), from_event=from_event),
        payment=PaymentConfig(methods=methods),
    )
