"""NX-275 felia 7 (D2) — fast path exact: fapte servite fără niciun apel de model.

Testele importante NU sunt cele care arată că servește. Sunt cele care arată că REFUZĂ exact
acolo unde ar fi cel mai tentant să răspundă, fiindcă asta e singura cale din sistem care poate
răspunde greșit fără ca vreo poartă din aval s-o prindă: validatorul și `grounding_guard` verifică
dacă prețul EXISTĂ, nu dacă e prețul produsului despre care a întrebat clientul.

ZERO OpenAI, ZERO DB real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent import fast_path
from src.agent.fast_path import eligible, wanted_fact


class _Facts:
    """Un `CommerceFacts` suficient pentru compunere."""

    def __init__(self, **kw):
        self.name = kw.get("name", "Ser Hidratant")
        self.price = kw.get("price", 99.0)
        self.currency = kw.get("currency", "RON")
        self.availability = kw.get("availability", "in_stock")
        self.stock = kw.get("stock")
        self.unknown = kw.get("unknown", frozenset())
        self.raw = kw.get("raw", {"product_url": "https://x/p1"})
        self.freshness = SimpleNamespace(stale=kw.get("stale", False))

    @property
    def price_known(self) -> bool:
        return self.price is not None and self.currency is not None


def _ctx(query: str, *, displayed=None, page_id=None, action=None):
    return SimpleNamespace(
        message=SimpleNamespace(body=query),
        state=SimpleNamespace(
            pending_question=None,
            displayed_products=displayed or [],
        ),
        page_context=SimpleNamespace(product_id=page_id) if page_id else None,
        action=action,
        language="ro",
        business=SimpleNamespace(id="biz-1", settings={}),
        emit=lambda *a, **k: None,
        events=[],
    )


# ── Ce fapt cere mesajul ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cat costa?", "price"),
        ("ce pret are?", "price"),
        ("mai aveti pe stoc?", "stock"),
        ("e disponibil?", "stock"),
        ("imi dai linkul?", "link"),
        ("unde il gasesc?", "link"),
        # Cere DOUĂ lucruri: fast path-ul nu alege unul din două, fiindcă jumătatea lipsă n-ar
        # apărea nicăieri. Merge la creier, care le acoperă pe amândouă.
        ("cat costa si mai aveti pe stoc?", None),
        ("ce imi recomanzi pentru ten uscat?", None),
        ("", None),
    ],
)
def test_faptul_cerut_e_unul_singur_sau_niciunul(query, expected):
    assert wanted_fact(query) == expected


# ── Când REFUZĂ (partea care contează) ──────────────────────────────────────


def test_refuza_fara_ancora():
    """Fără produs afișat și fără pagină, „cât costă?" nu are subiect. A ghici înseamnă a răspunde
    despre alt produs, cu un preț perfect real."""
    reason, fact, pid = eligible(_ctx("cat costa?"), "cat costa?")
    assert reason == "no_anchor" and fact == "price" and pid is None


def test_refuza_pe_ancora_slaba():
    """Un singur produs afișat NU înseamnă că despre el întreabă clientul. `single` e plauzibil,
    iar plauzibil nu ajunge pe o cale fără poartă în aval."""
    displayed = [SimpleNamespace(product_id="p1", name="Ser Hidratant")]
    reason, _, pid = eligible(_ctx("cat costa?", displayed=displayed), "cat costa?")
    assert reason in {"weak_anchor", "no_anchor"} and pid is None


def test_refuza_mesajul_mixt():
    """Chiar dacă prima jumătate e un fapt simplu, restul mesajului rămâne nerăspuns. Un fast path
    care servește jumătate lasă cealaltă jumătate invizibilă."""
    q = "cat costa si ce imi mai recomanzi?"
    reason, _, _ = eligible(_ctx(q, page_id="p1"), q)
    assert reason in {"mixed", "not_exact", "no_single_fact"}


def test_refuza_cand_turul_porneste_dintr_o_actiune():
    q = "cat costa?"
    reason, _, _ = eligible(_ctx(q, page_id="p1", action=object()), q)
    assert reason == "action"


def test_accepta_ancora_de_pagina():
    """Cazul legitim: clientul E pe pagina produsului și întreabă prețul. Ancora vine de la server
    (NX-234), nu dintr-o potrivire de nume."""
    q = "cat costa?"
    reason, fact, pid = eligible(_ctx(q, page_id="p1"), q)
    assert reason is None and fact == "price" and pid == "p1"


# ── Compunerea textului ─────────────────────────────────────────────────────


def test_textul_vine_din_tabelul_de_copy_nu_din_cod():
    """P11: niciun număr formatat de mână, nicio monedă concatenată în cod. Dacă cineva scrie
    „99 lei" direct în modul, testul trebuie să pice."""
    text = fast_path._compose("price", _Facts(), "ro")
    assert text == "Ser Hidratant costă 99,00 lei."
    assert fast_path._compose("stock", _Facts(), "ro") == "Ser Hidratant: În stoc."
    assert fast_path._compose("link", _Facts(), "ro") == "Ser Hidratant: https://x/p1"


def test_un_fapt_necunoscut_nu_se_rosteste():
    """UNKNOWN nu e 0 și nu e „indisponibil" (D8). Fără fapt, turul merge la creier, care poate
    spune onest că nu știe."""
    assert fast_path._compose("price", _Facts(price=None), "ro") is None
    assert fast_path._compose("price", _Facts(unknown=frozenset({"price"})), "ro") is None
    assert fast_path._compose("stock", _Facts(availability=None), "ro") is None
    assert fast_path._compose("link", _Facts(raw={}), "ro") is None


def test_fiecare_locale_are_propriul_sablon():
    """Pilotul e `ro`, dar nucleul rămâne locale-aware (D3). Un șablon lipsă ar da KeyError la
    primul client pe alt locale."""
    from src.web.localization import copy_for

    for locale in ("ro", "en", "hu"):
        block = copy_for(locale)["fast_path"]
        assert set(block) == {"price", "stock", "link"}
        assert "{name}" in block["price"] and "{amount}" in block["price"]
    # Șabloanele NU sunt aceleași între limbi (o copiere ar trece neobservată).
    assert copy_for("ro")["fast_path"]["price"] != copy_for("en")["fast_path"]["price"]


# ── Poarta de flag + P6 ─────────────────────────────────────────────────────


async def test_stins_nu_atinge_nimic():
    """OFF = turul merge exact ca azi, iar `ctx.reply` rămâne nescris."""
    ctx = _ctx("cat costa?", page_id="p1")
    out = await fast_path.try_fast_path(ctx, SimpleNamespace())
    assert out.served is False and out.reason == "flag_off"


async def test_o_hidratare_esuata_lasa_turul_creierului(monkeypatch):
    """P6: o scurtătură nu are voie să transforme un tur care ar fi mers într-o eroare."""
    from src.config import get_settings

    monkeypatch.setenv("FAST_PATH_EXACT_ENABLED", "true")
    monkeypatch.setenv("SINGLE_BRAIN_ENABLED", "true")
    get_settings.cache_clear()

    class _Deps:
        def db(self, _op):
            raise RuntimeError("DB jos")

    try:
        ctx = _ctx("cat costa?", page_id="p1")
        out = await fast_path.try_fast_path(ctx, _Deps())
        assert out.served is False and out.reason == "facts_unavailable"
    finally:
        monkeypatch.delenv("FAST_PATH_EXACT_ENABLED", raising=False)
        monkeypatch.delenv("SINGLE_BRAIN_ENABLED", raising=False)
        get_settings.cache_clear()


def test_flagul_cere_creierul_unic():
    """Fast path-ul se declară acoperitor prin control plane-ul creierului unic. Aprins singur, ar
    lăsa obligația neacoperită și turul ar putea răspunde de două ori."""
    from pydantic import ValidationError

    from src.config import Settings

    base = dict(
        OPENAI_API_KEY="k",
        META_ACCESS_TOKEN="t",
        META_APP_SECRET="s",
        META_VERIFY_TOKEN="v",
        META_PHONE_NUMBER_ID="0",
        SUPABASE_DB_URL="postgresql://a:b@c/d",
        REDIS_URL="redis://x/0",
        ENV="test",
    )
    with pytest.raises((ValidationError, ValueError), match="SINGLE_BRAIN_ENABLED"):
        Settings(_env_file=None, FAST_PATH_EXACT_ENABLED="true", **base)
    Settings(_env_file=None, FAST_PATH_EXACT_ENABLED="true", SINGLE_BRAIN_ENABLED="true", **base)


# ── Ancorarea prin nume rostit (cea mai strânsă) ────────────────────────────


def test_garda_de_cost_evita_scanarea_cand_mesajul_nu_poate_contine_un_nume():
    """Decizie de COST, luată fără să atingem DB-ul: interogarea de ancorare e o scanare
    (indexurile sunt inerte sub RLS pe conexiunea de runtime), 130-235ms măsurat pe catalogul
    SOLE. Pe un „cât costă?" fără nume am plăti-o degeaba și tot am merge la creier."""
    assert fast_path.name_lookup_worth_it("cat costa?", "ro") is False
    assert fast_path.name_lookup_worth_it("ce pret are?", "ro") is False
    assert fast_path.name_lookup_worth_it("cat costa crema aia?", "ro") is False
    assert fast_path.name_lookup_worth_it("cat costa RIEMANN P20 Original SPF 50+?", "ro") is True


async def test_numele_ambiguu_nu_ancoreaza(monkeypatch):
    """Cazul care contează pe un catalog real: 35 de produse împart prefixul
    `TIRTIR Mask Fit Red Cushion` (nuanțe). A răspunde cu prețul uneia ar fi o ghicire cu preț
    perfect real — genul de greșeală pe care nicio poartă din aval n-o prinde."""
    from src.config import get_settings

    monkeypatch.setenv("FAST_PATH_EXACT_ENABLED", "true")
    monkeypatch.setenv("SINGLE_BRAIN_ENABLED", "true")
    get_settings.cache_clear()

    async def fake_anchor(ctx, deps, query):
        return None, "ambiguous"

    monkeypatch.setattr(fast_path, "_anchor_by_name", fake_anchor)
    try:
        q = "cat costa TIRTIR Mask Fit Red Cushion?"
        out = await fast_path.try_fast_path(_ctx(q), SimpleNamespace())
        assert out.served is False and out.reason == "ambiguous"
    finally:
        monkeypatch.delenv("FAST_PATH_EXACT_ENABLED", raising=False)
        monkeypatch.delenv("SINGLE_BRAIN_ENABLED", raising=False)
        get_settings.cache_clear()


async def test_ancora_aratata_de_server_are_intaietate_fata_de_nume(monkeypatch):
    """Pagina pe care E clientul bate un nume rostit: prima e un fapt al serverului, a doua o
    potrivire de text. Dacă ordinea s-ar inversa, o frază care conține din întâmplare alt nume ar
    răspunde despre alt produs decât cel de pe ecran."""
    from src.config import get_settings

    monkeypatch.setenv("FAST_PATH_EXACT_ENABLED", "true")
    monkeypatch.setenv("SINGLE_BRAIN_ENABLED", "true")
    get_settings.cache_clear()

    called = {"n": 0}

    async def fake_anchor(ctx, deps, query):
        called["n"] += 1
        return "ALT-PRODUS", "named_in_query"

    monkeypatch.setattr(fast_path, "_anchor_by_name", fake_anchor)
    try:
        q = "cat costa RIEMANN P20 Original SPF 50+ PA++++?"
        reason, fact, pid = fast_path.eligible(_ctx(q, page_id="p1"), q)
        assert pid == "p1" and reason is None and fact == "price"
        assert called["n"] == 0  # ancora de pagină a rezolvat deja: nicio scanare
    finally:
        monkeypatch.delenv("FAST_PATH_EXACT_ENABLED", raising=False)
        monkeypatch.delenv("SINGLE_BRAIN_ENABLED", raising=False)
        get_settings.cache_clear()
