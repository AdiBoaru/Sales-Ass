"""NX-191 — promisiunea de livrare e o afirmație despre calendar: se testează la ORA exactă.

Cazurile care contează sunt marginile (13:59 vs 14:01, vineri, weekend), nu media.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.commerce.config import ShippingConfig
from src.commerce.delivery import (
    add_working_days,
    free_shipping_gap,
    promise,
)

SHIP = ShippingConfig(
    cutoff_hour=14,
    working_days=(1, 2, 3, 4, 5),
    cost=19.99,
    free_threshold=199.0,
    class_days={"standard": (2, 4), "supplier": (5, 7), "preorder": (10, 14)},
)
# 2026-07-22 e miercuri; 2026-07-24 vineri; 2026-07-25 sâmbătă
MIERCURI_1120 = datetime(2026, 7, 22, 11, 20)
MIERCURI_1530 = datetime(2026, 7, 22, 15, 30)
VINERI_1300 = datetime(2026, 7, 24, 13, 0)
VINERI_1600 = datetime(2026, 7, 24, 16, 0)
SAMBATA_1000 = datetime(2026, 7, 25, 10, 0)


def test_next_day_inainte_de_ora_limita():
    p = promise(delivery_class="next_day", shipping=SHIP, now=MIERCURI_1120)
    assert p.text == "dacă comanzi în următoarele 2 ore, ajunge mâine"
    assert p.earliest == date(2026, 7, 23)
    assert p.time_sensitive is True  # → cacheable=False


def test_next_day_dupa_ora_limita_nu_mai_promite_maine():
    p = promise(delivery_class="next_day", shipping=SHIP, now=MIERCURI_1530)
    assert p.text == "ajunge poimâine"
    assert p.earliest == date(2026, 7, 24)
    assert p.time_sensitive is False  # fără ceas → cacheabil


def test_vineri_dupa_ora_limita_sare_weekendul():
    """Comandă vineri 16:00 → expediere luni → livrare marți. Fără regula de zile lucrătoare
    am promite sâmbătă, ceea ce e fals."""
    p = promise(delivery_class="next_day", shipping=SHIP, now=VINERI_1600)
    assert p.earliest == date(2026, 7, 28)  # marți
    assert p.earliest.isoweekday() == 2


def test_vineri_inainte_de_limita_promite_sambata_doar_daca_se_lucreaza():
    """Vineri 13:00, cu livrare doar luni-vineri: „mâine" ar fi sâmbătă → se împinge la luni."""
    p = promise(delivery_class="next_day", shipping=SHIP, now=VINERI_1300)
    assert p.earliest == date(2026, 7, 27)  # luni
    assert p.time_sensitive is True
    assert "următoarele" in p.text


def test_sambata_nu_promite_maine():
    p = promise(delivery_class="next_day", shipping=SHIP, now=SAMBATA_1000)
    assert p.earliest.isoweekday() in (1, 2)
    assert p.time_sensitive is False


def test_fara_ora_limita_nu_promitem_livrare_a_doua_zi():
    """Regula 2: fără config, tăcerea e mai ieftină decât o promisiune greșită."""
    ship = ShippingConfig(cutoff_hour=None, working_days=(1, 2, 3, 4, 5))
    p = promise(delivery_class="next_day", shipping=ship, now=MIERCURI_1120)
    assert p.time_sensitive is False
    assert "mâine" not in (p.text or "")
    assert p.text == "ajunge în 2-4 zile lucrătoare"  # degradare la standard


def test_clasa_standard_si_supplier():
    assert (
        promise(delivery_class="standard", shipping=SHIP, now=MIERCURI_1120).text
        == "ajunge în 2-4 zile lucrătoare"
    )
    assert (
        promise(delivery_class="supplier", shipping=SHIP, now=MIERCURI_1120).text
        == "ajunge în 5-7 zile lucrătoare"
    )


def test_clasa_necunoscuta_tace():
    for bad in (None, "", "teleportare"):
        p = promise(delivery_class=bad, shipping=SHIP, now=MIERCURI_1120)
        assert p.text is None
        assert not p


def test_produs_epuizat_pleaca_de_la_reaprovizionare():
    p = promise(
        delivery_class="next_day",
        shipping=SHIP,
        now=MIERCURI_1120,
        restock_date=date(2026, 8, 5),
    )
    assert "revine în stoc pe 5 august" in p.text
    assert p.earliest == date(2026, 8, 6)
    assert p.time_sensitive is False


def test_restock_in_trecut_se_ignora():
    """O dată de revenire deja trecută nu trebuie să blocheze promisiunea normală."""
    p = promise(
        delivery_class="next_day",
        shipping=SHIP,
        now=MIERCURI_1120,
        restock_date=date(2026, 7, 1),
    )
    assert "revine" not in p.text
    assert p.earliest == date(2026, 7, 23)


@pytest.mark.parametrize(
    "start,days,expected",
    [
        (date(2026, 7, 22), 0, date(2026, 7, 22)),  # miercuri, deja lucrătoare
        (date(2026, 7, 22), 2, date(2026, 7, 24)),  # → vineri
        (date(2026, 7, 22), 3, date(2026, 7, 27)),  # sare weekendul → luni
        (date(2026, 7, 25), 0, date(2026, 7, 27)),  # sâmbătă → luni
        (date(2026, 7, 24), 4, date(2026, 7, 30)),  # vineri + 4 lucrătoare → joi
    ],
)
def test_add_working_days(start, days, expected):
    assert add_working_days(start, days, (1, 2, 3, 4, 5)) == expected


def test_add_working_days_fara_zile_configurate_nu_intra_in_bucla():
    assert add_working_days(date(2026, 7, 22), 5, ()) == date(2026, 7, 22)


def test_prag_transport_gratuit():
    assert free_shipping_gap(176.0, SHIP) == 23.0
    assert free_shipping_gap(199.0, SHIP) is None  # atins → nu spunem nimic
    assert free_shipping_gap(250.0, SHIP) is None
    assert free_shipping_gap(50.0, ShippingConfig()) is None  # fără prag configurat
