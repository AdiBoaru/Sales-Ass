"""G5b-1 — canonicalize + classify_volatility (pur, fără DB/LLM)."""

from src.cache.canonical import canonicalize, classify_volatility


def test_canonicalize_collapses_paraphrase():
    a, ha = canonicalize("Care e POLITICA de retur??")
    b, hb = canonicalize("care e politica   de retur")
    assert a == b == "care e politica de retur"
    assert ha == hb  # același hash → L1 exact prinde paraphrase-ul


def test_canonicalize_strips_diacritics():
    s, _ = canonicalize("Cât durează LIVRAREA?")
    assert s == "cat dureaza livrarea"


def test_canonicalize_empty():
    s, h = canonicalize("")
    assert s == ""
    assert len(h) == 64  # sha256 hexdigest


def test_classify_static():
    assert classify_volatility("care e politica de retur") == "static"
    assert classify_volatility("salut, ce faci") == "static"
    assert classify_volatility("") == "static"
    assert classify_volatility(None) == "static"


def test_classify_dynamic():
    assert classify_volatility("caut o cremă sub 80 lei") == "dynamic"  # buget + 'caut'
    assert classify_volatility("cât costă crema asta") == "dynamic"
    assert classify_volatility("aveți reducere la parfumuri") == "dynamic"
    assert classify_volatility("100 ron buget") == "dynamic"  # număr + monedă


def test_classify_realtime():
    assert classify_volatility("unde e comanda mea") == "realtime"
    assert classify_volatility("care e statusul comenzii") == "realtime"
    assert classify_volatility("vreau factura") == "realtime"
