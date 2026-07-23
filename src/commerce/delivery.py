"""NX-191 — promisiunea de livrare, calculată DETERMINIST (P2: LLM-ul nu socotește date).

„Dacă comanzi în următoarele 2 ore, ajunge mâine" e o afirmație despre calendar și ceas. Modelul
n-are voie s-o producă: ar inventa ore și ar promite în weekend. Aici se calculează, iar modelul
primește doar textul gata făcut (sau nimic, dacă nu putem promite nimic onest).

Trei reguli care se plătesc scump dacă lipsesc:
  1. **Zile lucrătoare.** „Comandă vineri la 13:00 → ajunge mâine" e fals dacă sâmbăta nu se
     livrează. Decizia userului: doar zile lucrătoare, FĂRĂ calendar de sărbători (pe demo l-am
     întreține degeaba) — asumat explicit, nu uitat.
  2. **Fără config, fără promisiune.** Lipsește ora-limită → nu spunem „mâine". Tăcerea e mai
     ieftină decât o promisiune greșită.
  3. **Textul cu ceas NU se cachează.** `DeliveryPromise.time_sensitive` spune apelantului că
     răspunsul care conține fraza asta trebuie marcat `cacheable=False`: un hit de mâine ar servi
     „mai ai 2 ore" la ora 20:00. Am mai avut exact bug-ul ăsta pe răspunsurile „n-am găsit".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.commerce.config import ShippingConfig

# zile de reaprovizionare adăugate peste termenul normal, când produsul e epuizat dar are dată de
# revenire: livrarea pleacă de la restock, nu de azi.
_MAX_LOOKAHEAD_DAYS = 60


@dataclass(frozen=True)
class DeliveryPromise:
    """Ce poate spune botul despre livrare. `text` e None când nu avem temei pentru nicio afirmație
    (config lipsă / clasă necunoscută) — atunci botul tace pe subiectul livrare, nu improvizează."""

    text: str | None
    earliest: date | None = None
    latest: date | None = None
    #: conține o componentă de ceas („în următoarele 2 ore") → răspunsul NU e cacheabil
    time_sensitive: bool = False

    def __bool__(self) -> bool:
        return self.text is not None


def _is_working(d: date, working_days: tuple[int, ...]) -> bool:
    return d.isoweekday() in working_days


def add_working_days(start: date, days: int, working_days: tuple[int, ...]) -> date:
    """`start` + N zile LUCRĂTOARE. `days=0` → prima zi lucrătoare de la `start` inclusiv.
    Fără zile lucrătoare configurate → întoarce `start` (nu intrăm în buclă infinită)."""
    if not working_days:
        return start
    cur = start
    while not _is_working(cur, working_days):
        cur += timedelta(days=1)
    remaining = days
    guard = 0
    while remaining > 0 and guard < _MAX_LOOKAHEAD_DAYS:
        cur += timedelta(days=1)
        guard += 1
        if _is_working(cur, working_days):
            remaining -= 1
    return cur


def _fmt_ro(d: date, today: date) -> str:
    """Data în limbaj natural RO. Relativ pentru primele două zile (așa vorbește un vânzător),
    absolut după. Fără nume de lună declinat greșit — folosim forma uzuală."""
    delta = (d - today).days
    if delta <= 0:
        return "azi"
    if delta == 1:
        return "mâine"
    if delta == 2:
        return "poimâine"
    luni = (
        "ianuarie",
        "februarie",
        "martie",
        "aprilie",
        "mai",
        "iunie",
        "iulie",
        "august",
        "septembrie",
        "octombrie",
        "noiembrie",
        "decembrie",
    )
    return f"{d.day} {luni[d.month - 1]}"


def _hours_left(now: datetime, cutoff_hour: int) -> float:
    cutoff = now.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
    return (cutoff - now).total_seconds() / 3600


def _fmt_countdown(hours: float) -> str:
    if hours >= 2:
        return f"în următoarele {int(hours)} ore"
    minutes = max(1, int(hours * 60))
    return f"în următoarele {minutes} de minute" if minutes != 1 else "în următorul minut"


def promise(
    *,
    delivery_class: str | None,
    shipping: ShippingConfig,
    now: datetime,
    restock_date: date | None = None,
) -> DeliveryPromise:
    """Promisiunea de livrare pentru un produs, la momentul `now`.

    `now` e INJECTAT, nu citit din ceasul global: funcția trebuie să fie testabilă la 13:59 vineri
    fără să aștepți vineri. Apelantul îl dă din fusul magazinului (`businesses.timezone`).
    """
    today = now.date()
    working = shipping.working_days

    # Produs epuizat cu dată de revenire: livrarea pleacă de la reaprovizionare, nu de azi.
    if restock_date is not None:
        if restock_date <= today:
            base = today
        else:
            eta = add_working_days(restock_date, 1, working)
            return DeliveryPromise(
                text=f"revine în stoc pe {_fmt_ro(restock_date, today)}, "
                f"iar livrarea durează încă o zi lucrătoare",
                earliest=eta,
                latest=eta,
            )
    else:
        base = today

    if delivery_class == "next_day":
        if not shipping.promises_next_day:
            # fără oră-limită nu putem promite „mâine" → degradăm la termenul standard
            return _range_promise("standard", shipping, base, today, working)
        hours = _hours_left(now, shipping.cutoff_hour or 0)
        target = add_working_days(base + timedelta(days=1), 0, working)
        if hours > 0 and _is_working(today, working):
            return DeliveryPromise(
                text=f"dacă comanzi {_fmt_countdown(hours)}, ajunge {_fmt_ro(target, today)}",
                earliest=target,
                latest=target,
                time_sensitive=True,  # → răspunsul NU se cachează
            )
        # după ora-limită (sau în weekend): prima expediere e ziua lucrătoare următoare
        ship_day = add_working_days(base + timedelta(days=1), 0, working)
        target = add_working_days(ship_day + timedelta(days=1), 0, working)
        return DeliveryPromise(
            text=f"ajunge {_fmt_ro(target, today)}", earliest=target, latest=target
        )

    return _range_promise(delivery_class, shipping, base, today, working)


def _range_promise(
    delivery_class: str | None,
    shipping: ShippingConfig,
    base: date,
    today: date,
    working: tuple[int, ...],
) -> DeliveryPromise:
    span = shipping.class_days.get(delivery_class or "")
    if not span:
        return DeliveryPromise(text=None)  # clasă necunoscută → tăcere, nu improvizație
    lo, hi = span
    earliest = add_working_days(base, lo, working)
    latest = add_working_days(base, hi, working)
    if lo == hi:
        text = f"ajunge în {lo} zile lucrătoare"
    else:
        text = f"ajunge în {lo}-{hi} zile lucrătoare"
    return DeliveryPromise(text=text, earliest=earliest, latest=latest)


def free_shipping_gap(cart_total: float, shipping: ShippingConfig) -> float | None:
    """Cât mai lipsește până la transportul gratuit. None dacă nu există prag sau e deja atins →
    apelantul nu spune nimic (un „mai adaugă 0 lei" e mai rău decât tăcerea)."""
    threshold = shipping.free_threshold
    if threshold is None or cart_total >= threshold:
        return None
    return round(threshold - cart_total, 2)


def has_time_sensitive_text(text: str | None) -> bool:
    """Textul conține o promisiune raportată la CEASUL de acum („în următoarele 2 ore")?

    Plasă la scrierea în cache, independentă de cine a compus fraza — inclusiv modelul, dacă a
    preluat formularea din contextul primit. Un hit de mâine ar servi „mai ai 2 ore" seara.
    """
    if not text:
        return False
    t = text.lower()
    return "urmatoarele" in t.replace("ă", "a") and ("ore" in t or "minute" in t)
