"""Testele regulilor de citire a sursei SOLE (`src/catalog/sole_source.py`).

Valorile din teste sunt REALE, copiate din `sole_data.db`, nu inventate. Un test scris pe
exemple plauzibile ar fi trecut și pentru parsere care pică pe date adevărate — exact cazul
PAO-ului de mai jos, care arată perfect parsabil și nu trebuie parsat.

Testul de acoperire pe vocabularul complet (toate cele 111 badge-uri, toate cele 17 chei de
secțiune) rulează doar dacă baza sursă e prezentă local; în CI se sare, dar clasificatorul
rămâne acoperit de cazurile inline.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from src.catalog import sole_source as ss

SOLE_DB = os.environ.get("SOLE_SOURCE_DB", r"D:\Work\SOLE SCRIPT\sole_data.db")
needs_source = pytest.mark.skipif(
    not os.path.exists(SOLE_DB), reason=f"baza sursa SOLE lipseste ({SOLE_DB})"
)


# ============================================================================
# Preț: capcana cuponului
# ============================================================================


def test_cupon_nu_devine_sale_price():
    """2.131 din 2.767 de produse au reducere CONDIȚIONATĂ de `WELCOME15`.

    Dacă ajunge în `sale_price`, widgetul o afișează, `grounding_guard` o confirmă fiindcă e
    în baza noastră, iar clientul aude un preț pe care nu-l poate obține.
    """
    p = ss.parse_price(30.0, 25.5, "WELCOME15")
    assert p is not None
    assert p.price == Decimal("30")
    assert p.sale_price is None, "pretul de cupon NU e sale_price"
    assert p.coupon_code == "WELCOME15"
    assert p.coupon_price == Decimal("25.5")


def test_reducere_reala_devine_sale_price():
    """O reducere necondiționată CHIAR e `sale_price`.

    Sintetic deliberat: catalogul SOLE n-are niciuna (vezi
    `test_sursa_nu_are_nicio_reducere_neconditionata`). Regula trebuie să existe și să fie
    corectă pentru ziua în care sursa are una, altfel primul preț redus real ar fi tratat ca
    preț de cupon și n-ar fi afișat niciodată.
    """
    p = ss.parse_price(100.0, 80.0, None)
    assert p is not None and p.sale_price == Decimal("80") and p.coupon_code is None


def test_promo_egal_sau_mai_mare_nu_e_reducere():
    """395 de produse au `price_promo == price_regular`. Nu e reducere, e aceeași cifră."""
    assert ss.parse_price(50.0, 50.0, "WELCOME15").sale_price is None
    assert ss.parse_price(50.0, 50.0, "WELCOME15").coupon_price is None
    assert ss.parse_price(50.0, 60.0, None).sale_price is None


def test_produs_fara_pret_nu_se_importa():
    """Cele 9 scrape-uri esuate au name/price/brand NULL."""
    assert ss.parse_price(None, None, None) is None
    assert ss.parse_price(0, None, None) is None


# ============================================================================
# Depozitare: PAO e capcana
# ============================================================================

# Textul REAL, identic pe toate cele 2.251 de produse care au secțiunea.
STORAGE_REAL = (
    "Data minimă de durabilitate a lotului disponibil\n"
    ": 06.01.2029\n"
    "Perioada de utilizare după deschidere\n"
    ": conform simbolului PAO înscris pe ambalaj — de exemplu, 12M, 24M sau 36M. "
    "Perioada se calculează de la prima deschidere, cu respectarea condițiilor de păstrare "
    "indicate de producător.\n"
    "Condiții de păstrare\n"
    ": între 5°C și 25°C"
)


def test_pao_nu_se_extrage_niciodata():
    """Linia PAO conține „12M, 24M sau 36M" DE EXEMPLU, identic pe 2.251 de produse.

    Un parser lacom ar produce `pao_months=12` pentru toate, iar botul ar spune „se folosește
    12 luni după deschidere" despre oricare. Cifra ar fi în baza noastră, deci validatorul ar
    confirma-o. E cea mai periculoasă clasă de eroare: una care trece toate porțile.
    """
    assert ss.parse_storage(STORAGE_REAL).pao_months is None


def test_data_durabilitatii_si_temperatura_se_extrag():
    f = ss.parse_storage(STORAGE_REAL)
    assert f.min_durability_date == date(2029, 1, 6), "format dd.mm.yyyy, nu mm.dd"
    assert (f.temp_min_c, f.temp_max_c) == (Decimal("5"), Decimal("25"))


def test_temperatura_stricata_ramane_necunoscuta():
    """44 de produse au „între °C și °C", fără cifre. UNKNOWN, nu zero."""
    f = ss.parse_storage("Condiții de păstrare\n: între °C și °C")
    assert f.temp_min_c is None and f.temp_max_c is None


def test_data_imposibila_ramane_necunoscuta():
    assert ss.parse_storage(": 31.02.2029").min_durability_date is None


def test_sectiune_lipsa():
    f = ss.parse_storage(None)
    assert (f.min_durability_date, f.temp_min_c, f.pao_months) == (None, None, None)


# ============================================================================
# Volum
# ============================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("50 ml", (Decimal("50"), "ml")),
        ("4.5 gr", (Decimal("4.5"), "g")),
        ("18 gr", (Decimal("18"), "g")),
        ("1 l", (Decimal("1"), "l")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_volum_parsabil(raw, expected):
    assert ss.parse_volume(raw) == expected


@pytest.mark.parametrize(
    "raw", ["4 gr x 40 gr", "28 gr + 2 ml", "6 X 1 gr", "1.5 gr + 33 gr", "4x15ml"]
)
def test_volum_compus_ramane_necunoscut(raw):
    """41 de valori compuse. Primul număr NU e cantitatea netă.

    `price_per_unit` e coloană generated: o cantitate greșită produce un preț per unitate fals
    fără ca nimeni sa fi scris o cifră greșită.
    """
    assert ss.parse_volume(raw) == (None, None)


# ============================================================================
# Badge-uri
# ============================================================================


@pytest.mark.parametrize(
    "label,kind",
    [
        ("CPNP", "compliance"),
        ("AM PM dimineata si seara", "fact"),
        ("AM dimineata", "fact"),
        ("PM seara", "fact"),
        ("Protectie UV Daily", "fact"),
        ("Protectie UV Outdoor", "fact"),
        ("Aprobat pentru copii", "fact"),
        ("Eficienta demonstrata stintific", "claim"),
        ("SOLE Exclusiv", "merchant_marketing"),
        ("Cadou", "merchant_marketing"),
        ("SOLE.ro este magazin oficial al brandului TIRTIR în România", "merchant_marketing"),
    ],
)
def test_clasificare_badge(label, kind):
    assert ss.classify_badge(label) == kind


def test_afirmatia_de_eficacitate_nu_e_fapt():
    """„Eficienta demonstrata stintific" (331 de produse) n-are studiu citabil.

    Ca `fact` ar deveni argument de vânzare pe care validatorul nu-l poate verifica.
    """
    assert ss.classify_badge("Eficienta demonstrata stintific") != "fact"


# ============================================================================
# Secțiuni: cele două familii
# ============================================================================


def test_familia_f1_e_citabila():
    c = ss.classify_section("Descriere")
    assert c is not None
    assert (c.source, c.voice, c.evidence_role) == ("merchant_pdp", "brand", "benefit")


def test_familia_f2_nu_e_citabila():
    """Proza AURA se importă, dar nu devine evidence.

    Ca evidence ar fi sursă citabilă pentru `grounding_guard`, care ar confirma afirmații
    derivate de altcineva din fapte pe care nu le avem.
    """
    for key in ("Cui i se potrivește", "Cum se compară cu alte produse", "Recomandare AURA"):
        c = ss.classify_section(key)
        assert c is not None, key
        assert (c.source, c.voice, c.evidence_role) == ("aura", "assistant", None), key


def test_cheie_necunoscuta_nu_e_eroare_dar_nici_tacere():
    """O cheie nouă la sursă întoarce None, iar importerul o scrie cu alertă."""
    assert ss.classify_section("O cheie care nu exista") is None


# ============================================================================
# Rutină, disponibilitate, categorii, slug
# ============================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("AM/PM - Include produsul in rutina ta de dimineata sau de seara.", "am_pm"),
        ("AM - Include produsul in rutina ta de dimineata.", "am"),
        ("PM - Include produsul in rutina ta de seara.", "pm"),
        ("", None),
        (None, None),
    ],
)
def test_moment_rutina(text, expected):
    assert ss.parse_routine_time(text) == expected


def test_disponibilitate_necunoscuta_cade_pe_epuizat():
    """A promite un produs pe care nu-l ai costă o comandă; a nu-l promite costă o afișare."""
    assert ss.parse_availability("in stoc") == "in_stock"
    assert ss.parse_availability("stoc epuizat") == "out_of_stock"
    assert ss.parse_availability(None) == "out_of_stock"
    assert ss.parse_availability("ceva nou") == "out_of_stock"


def test_cale_categorie():
    assert ss.parse_category_path("Ten > Ingrijirea tenului") == ["Ten", "Ingrijirea tenului"]
    assert ss.parse_category_path("Ten") == ["Ten"]
    assert ss.parse_category_path("None") == []
    assert ss.parse_category_path(None) == []


def test_slug_stabil_la_diacritice():
    """Numele din sursă n-au diacritice, dar o corectură la sursă n-are voie să creeze duplicat."""
    assert ss.slugify("Masca de fata") == ss.slugify("Mască de față")
    assert ss.slugify("NUMBUZIN No.9 NAD+Bio") == "numbuzin-no-9-nad-bio"


# ============================================================================
# INCI
# ============================================================================


def test_inci_pastreaza_ordinea():
    """Ordinea e reglementată (concentrație descrescătoare), deci e informație, nu întâmplare."""
    got = ss.split_inci("Ingrediente:\nWater, Dipropylene Glycol, Glycerin, Niacinamide")
    assert got == ["Water", "Dipropylene Glycol", "Glycerin", "Niacinamide"]


def test_inci_elimina_subetichetele_de_componenta():
    """Măștile multi-componentă au sub-etichete („Upper Sheet") care nu sunt ingrediente."""
    got = ss.split_inci("Ingrediente:\nUpper Sheet\nWater, Glycerin, Adenosine")
    assert "Upper Sheet" not in got
    assert got == ["Water", "Glycerin", "Adenosine"]


def test_inci_deduplica_pastrand_prima_pozitie():
    got = ss.split_inci("Water, Glycerin, water, Glycerin, Panthenol")
    assert got == ["Water", "Glycerin", "Panthenol"]


# ============================================================================
# Acoperire pe vocabularul COMPLET (necesită baza sursă)
# ============================================================================


@needs_source
def test_toate_badge_urile_reale_sunt_clasificate():
    """Toate cele 111 valori distincte primesc un `kind`, și niciuna nu cade pe `other`.

    `other` e ieșirea de siguranță a clasificatorului. Dacă o valoare reală ajunge acolo,
    înseamnă că vocabularul s-a schimbat și clasificarea trebuie actualizată, nu că e în regulă.
    """
    conn = sqlite3.connect(SOLE_DB)
    labels: set[str] = set()
    for (raw,) in conn.execute("select badges from products"):
        try:
            labels.update(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    conn.close()

    assert len(labels) == 111, f"vocabularul s-a schimbat: {len(labels)} valori"
    unclassified = sorted(x for x in labels if ss.classify_badge(x) == "other")
    assert not unclassified, f"badge-uri neclasificate: {unclassified}"


@needs_source
def test_toate_cheile_de_sectiune_reale_sunt_cunoscute():
    conn = sqlite3.connect(SOLE_DB)
    keys: set[str] = set()
    for (raw,) in conn.execute("select sections_json from products"):
        try:
            keys.update(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    conn.close()

    unknown = sorted(k for k in keys if ss.classify_section(k) is None)
    assert not unknown, f"chei de sectiune necunoscute: {unknown}"
    assert keys == set(ss.SECTION_KEYS), "sursa si taxonomia noastra au divergat"


@needs_source
def test_pao_ramane_null_pe_intreaga_sursa():
    """Garda de fond: pe TOATE produsele reale, PAO nu se extrage niciodată."""
    conn = sqlite3.connect(SOLE_DB)
    checked = 0
    for (raw,) in conn.execute("select sections_json from products"):
        try:
            text = json.loads(raw).get("Depozitare si valabilitate")
        except (json.JSONDecodeError, TypeError):
            continue
        if not text:
            continue
        assert ss.parse_storage(text).pao_months is None
        checked += 1
    conn.close()
    assert checked > 2000, f"prea putine sectiuni verificate: {checked}"


@needs_source
def test_niciun_produs_cu_pret_nu_pierde_pretul():
    """Toate cele 2.758 de produse valide trebuie să producă un `PriceFacts`."""
    conn = sqlite3.connect(SOLE_DB)
    rows = conn.execute(
        "select price_regular, price_promo, promo_code from products"
        " where price_regular is not null"
    ).fetchall()
    conn.close()
    parsed = [ss.parse_price(*r) for r in rows]
    assert all(p is not None for p in parsed)

    # 2.131 de randuri au `promo_code`, dar 8 dintre ele n-au reducere reala (vezi testul
    # de anomalii), deci raman 2.123 de preturi de cupon.
    with_coupon = sum(1 for p in parsed if p.coupon_code)
    assert with_coupon == 2123, f"asteptam 2123 preturi de cupon, am gasit {with_coupon}"


@needs_source
def test_sursa_nu_are_nicio_reducere_neconditionata():
    """ZERO produse din 2.767 au reducere reala. `sale_price` ramane NULL pe tot catalogul.

    Pare ca ar avea 102: acolo `price_promo < price_regular` in SQLite. Dar diferenta e sub
    un ban pe toate (30.0 vs 29.99953) — reziduu de virgula mobila dintr-un calcul procentual
    la scraping. Rotunjite la cat stocam de fapt (`numeric(12,2)`), sunt egale.

    Consecinta, care nu e despre parser: intreaga mecanica de reducere (`sale_start`/`sale_end`
    din migrarea 032, afisarea „-15%" din `web-view.v2`, rotunjirea reducerii din
    `src/web/localization.py`) n-are pe ce rula. Singurul avantaj de pret real din catalog e
    cuponul, si de aceea are nevoie de coloane proprii: fara ele, singurul mod de a arata un
    pret mai mic ar fi sa minti.
    """
    conn = sqlite3.connect(SOLE_DB)
    rows = conn.execute(
        "select price_regular, price_promo, promo_code from products"
        " where price_regular is not null"
    ).fetchall()
    conn.close()

    with_sale = [p for p in (ss.parse_price(*r) for r in rows) if p.sale_price is not None]
    assert not with_sale, f"asteptam 0 reduceri neconditionate, am gasit {len(with_sale)}"


@needs_source
def test_promotiile_incoerente_sunt_semnalate_nu_reparate():
    """8 produse au pretul „promotional" MAI MARE decat cel normal (90 lei -> 216,66).

    Una dintre cele doua cifre e gresita si nu se poate sti care. Alegerea sigura e sa ignori
    promotia si sa RAPORTEZI; a alege tacit una dintre cifre ar insemna sa inventam adevarul.
    """
    conn = sqlite3.connect(SOLE_DB)
    rows = conn.execute(
        "select price_regular, price_promo, promo_code from products"
        " where price_regular is not null"
    ).fetchall()
    conn.close()

    flagged = [p for p in (ss.parse_price(*r) for r in rows) if p.anomalies]
    assert len(flagged) == 8, f"asteptam 8 anomalii de pret, am gasit {len(flagged)}"
    for p in flagged:
        assert p.sale_price is None, "o promotie incoerenta nu devine niciodata sale_price"
        assert p.coupon_price is None, "si nici pret de cupon"
        assert p.price > 0


# ============================================================================
# Slug de produs: unicitate garantata
# ============================================================================


def test_slug_de_produs_are_intotdeauna_sufix_de_sku():
    """Sufixul e NECONDITIONAT, ca functia sa ramana pura si importul idempotent.

    „Adauga sufix doar la coliziune" ar depinde de intregul set, deci de ordinea importului:
    al doilea import, cu un produs nou intercalat, ar da alt slug aceluiasi produs, iar
    `on conflict (business_id, slug)` ar insera un duplicat in loc sa actualizeze.
    """
    assert ss.product_slug("Masca de fata", "F88540") == "masca-de-fata-f88540"
    assert ss.product_slug("X", "F1") == ss.product_slug("X", "F1")


@needs_source
def test_slug_urile_reale_nu_se_ciocnesc():
    """Pe numele singur, 125 de slug-uri se ciocnesc si afecteaza 427 de produse."""
    conn = sqlite3.connect(SOLE_DB)
    rows = conn.execute(
        "select name, memox_code from products where name is not null and memox_code is not null"
    ).fetchall()
    conn.close()

    naive = [ss.slugify(n) for n, _ in rows]
    assert len(set(naive)) < len(naive), "asteptam coliziuni pe numele singur"

    unique = [ss.product_slug(n, sku) for n, sku in rows]
    assert len(set(unique)) == len(unique), "slug-urile cu sufix de SKU trebuie sa fie unice"
    assert all(len(s) <= 90 for s in unique), "slug-ul trebuie sa incapa in limita"


def test_rolurile_de_evidence_sunt_din_vocabularul_inchis_nx205():
    """`product_evidence_chunks.role` are CHECK cu vocabular inchis (contractul NX-205).

    Un rol inventat ("description") nu da eroare la clasificare, ci la INSERT, in mijlocul
    importului, dupa ce produsele au fost deja scrise.
    """
    import typing

    from src.domain.contracts import EvidenceChunk

    allowed = set(typing.get_args(typing.get_type_hints(EvidenceChunk)["role"]))
    roles = {
        c.evidence_role
        for c in (ss.classify_section(k) for k in ss.SECTION_KEYS)
        if c and c.evidence_role
    }
    assert roles <= allowed, f"roluri in afara vocabularului: {roles - allowed}"


def test_proza_aura_nu_primeste_rol_de_evidence():
    aura = [ss.classify_section(k) for k in ss.SECTION_KEYS]
    assert all(c.evidence_role is None for c in aura if c and c.source == "aura")
    assert all(c.evidence_role is not None for c in aura if c and c.source == "merchant_pdp")
