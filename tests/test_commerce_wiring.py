"""NX-195 — wiring: promisiunea ajunge în răspuns, iar textul cu ceas NU se cachează.

Plasa de cache e testată pe `set_reply`, nu pe call-site-uri: e singurul punct prin care trec toate
răspunsurile, deci singurul loc unde regula nu poate fi uitată de cineva care adaugă o cale nouă.
"""

from __future__ import annotations

from datetime import datetime

from src.commerce.delivery import has_time_sensitive_text
from src.commerce.project import delivery_for, free_shipping_hint, store_now
from src.models import TurnContext


class _Biz:
    id = "biz"
    slug = "demo"
    name = "Demo"
    timezone = "Europe/Bucharest"
    settings = {
        "shipping": {
            "cutoff_hour": 14,
            "working_days": [1, 2, 3, 4, 5],
            "cost": 19.99,
            "free_threshold": 199.0,
            "class_days": {"standard": [2, 4], "supplier": [5, 7]},
        }
    }


def _ctx() -> TurnContext:
    return TurnContext.__new__(TurnContext)


def test_set_reply_forteaza_necacheabil_pe_text_cu_ceas():
    ctx = _ctx()
    ctx.set_reply("E în stoc și, dacă comanzi în următoarele 2 ore, ajunge mâine.")
    assert ctx.reply.cacheable is False


def test_set_reply_ramane_cacheabil_fara_ceas():
    ctx = _ctx()
    ctx.set_reply("Ajunge în 2-4 zile lucrătoare.")
    assert ctx.reply.cacheable is True


def test_set_reply_nu_reactiveaza_cache_ul_cand_apelantul_a_zis_false():
    ctx = _ctx()
    ctx.set_reply("Text banal.", cacheable=False)
    assert ctx.reply.cacheable is False


def test_predicatul_prinde_si_forma_fara_diacritice():
    assert has_time_sensitive_text("comanzi in urmatoarele 3 ore")
    assert has_time_sensitive_text("în următoarele 40 de minute")
    assert not has_time_sensitive_text("ajunge în 2-4 zile lucrătoare")
    assert not has_time_sensitive_text(None)
    assert not has_time_sensitive_text("")


def test_delivery_for_produs_next_day():
    p = {"delivery_class": "next_day", "restock_date": None}
    promise = delivery_for(p, _Biz())
    assert promise.text
    # ora reală decide dacă e countdown sau dată fixă; ambele sunt promisiuni valide
    assert "ajunge" in promise.text


def test_delivery_for_produs_epuizat_foloseste_restock():
    p = {"delivery_class": "next_day", "restock_date": "2099-08-05"}
    promise = delivery_for(p, _Biz())
    assert "revine în stoc" in promise.text
    assert promise.time_sensitive is False


def test_delivery_for_fara_clasa_tace():
    assert delivery_for({"delivery_class": None}, _Biz()).text is None


def test_delivery_for_accepta_data_ca_string_sau_date():
    from datetime import date

    a = delivery_for({"delivery_class": "standard", "restock_date": date(2099, 1, 2)}, _Biz())
    b = delivery_for({"delivery_class": "standard", "restock_date": "2099-01-02"}, _Biz())
    assert a.text == b.text


def test_prag_transport_gratuit_doar_cand_lipseste_ceva():
    assert free_shipping_hint(176.0, _Biz()).startswith("mai adaugă 23.00 lei")
    assert free_shipping_hint(199.0, _Biz()) is None
    assert free_shipping_hint(250.0, _Biz()) is None


def test_ora_magazinului_nu_e_ora_serverului():
    """Un magazin din București nu trebuie să promită altceva pentru că procesul rulează pe UTC."""
    now = store_now(_Biz())
    utc = datetime.utcnow()
    assert (now - utc).total_seconds() > 3000  # ≈ +1..3h, nu 0
