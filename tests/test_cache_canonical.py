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


def test_classify_contextual_cheaper():
    # Refinare RELATIVĂ la setul afișat → bypass (niciodată din cache-ul partajat).
    assert classify_volatility("ceva mai ieftin") == "contextual"
    assert classify_volatility("Aș vrea varianta cea mai ieftină") == "contextual"
    assert classify_volatility("ai ceva mai accesibil") == "contextual"
    assert classify_volatility("e prea scump") == "contextual"
    assert classify_volatility("cheaper please") == "contextual"
    # `contextual` ÎNAINTE de `dynamic`: „caut ceva mai ieftin" rămâne bypass, nu dynamic.
    assert classify_volatility("caut ceva mai ieftin") == "contextual"


def test_classify_bare_cheap_stays_dynamic():
    # „ieftin" simplu (fără comparativul „mai") = query de produs, NU refinare relativă.
    assert classify_volatility("caut o cremă ieftină") == "dynamic"
