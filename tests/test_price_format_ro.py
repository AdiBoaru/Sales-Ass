"""Prețul ca TEXT pe calea v1: format românesc + validatorul care trebuie să-l recunoască.

Cele două se țin de mână, si de aceea stau în același fișier. `89.00 lei` scris într-o frază
românească e o traducere, nu un preț; dar dacă schimbi doar formatarea, `1.234,50 lei` devine un
preț „inventat" pentru validator (P2), iar răspunsul CORECT e respins, urmat de retry și fallback.

Bugul de parsare exista ÎNAINTE de schimbarea de format: era latent doar fiindcă tot catalogul
demo e sub 1000 de lei.
"""

from __future__ import annotations

import pytest

from src.agent.fallbacks import _deterministic_reply
from src.agent.validator import _prices_ok, parse_amount
from src.web.localization import amount_text


class TestParseAmount:
    """Forma decide valoarea, fără localizare: nu știm în ce convenție a scris modelul."""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("89", 89.0),
            ("89,00", 89.0),  # zecimal ro
            ("89.00", 89.0),  # zecimal en
            ("1.234,50", 1234.5),  # grupare ro + zecimal ro
            ("1,234.50", 1234.5),  # grupare en + zecimal en
            ("1.500", 1500.0),  # grupare ro fără zecimale
            ("1,500", 1500.0),  # grupare en fără zecimale
            ("1.234.567", 1234567.0),  # grupare multiplă
            ("1.5", 1.5),  # o singură zecimală rămâne zecimală
            ("999.99", 999.99),
        ],
    )
    def test_forma_decide(self, token: str, expected: float) -> None:
        assert parse_amount(token) == pytest.approx(expected)

    def test_regresia_care_a_motivat_functia(self) -> None:
        """`replace(",", ".")` citea „1.500" ca 1.5, deci un preț real de 1.500 lei era raportat
        ca preț inventat."""
        assert parse_amount("1.500") == 1500.0


class TestValidatorulAcceptaFormatulRomanesc:
    PRODUCTS = [{"price": 1234.50}, {"price": 89.0}, {"price": 1500.0}]

    @pytest.mark.parametrize(
        "reply",
        [
            "Costă 1.234,50 lei.",
            "Costă 1234,50 lei.",
            "Costă 89,00 lei.",
            "Costă 89.00 lei.",
            "Costă 1.500 lei.",
            "Costă 1500 lei.",
            "Costă 1.500,00 lei.",
        ],
    )
    def test_pretul_real_trece_in_orice_scriere(self, reply: str) -> None:
        assert _prices_ok(reply, self.PRODUCTS) is True

    @pytest.mark.parametrize(
        "reply",
        [
            "Costă 1.499 lei.",  # aproape de 1500, dar în afara toleranței de 0,5
            "Costă 999,99 lei.",
            "Costă 12.345,00 lei.",
            "Costă 2.000 lei.",
        ],
    )
    def test_pretul_inventat_e_tot_respins(self, reply: str) -> None:
        """Validatorul nu s-a relaxat: recunoașterea mai multor scrieri NU înseamnă că acceptă
        mai multe VALORI. Asta ar fi transformat un fix de format într-o gaură de grounding."""
        assert _prices_ok(reply, self.PRODUCTS) is False


class TestFormatareaInProza:
    def test_zecimalele_ro_folosesc_virgula(self) -> None:
        assert amount_text(89.0, "ro") == "89,00"
        assert amount_text(66.49, "ro") == "66,49"

    def test_miile_ro_folosesc_punctul(self) -> None:
        assert amount_text(1234.5, "ro") == "1.234,50"

    def test_engleza_ramane_engleza(self) -> None:
        """Locale-aware, nu „ro" hardcodat (D3, principiul 11)."""
        assert amount_text(1234.5, "en") == "1,234.50"

    @pytest.mark.parametrize("bad", ["nu-i număr", None, "", object()])
    def test_suma_necitibila_nu_devine_zero(self, bad: object) -> None:
        """Contractul lui `format_amount` („None ⇒ omite câmpul") nu se poate onora într-o frază
        deja compusă în jurul cifrei („costă None lei"), dar nici invers: un preț pe care nu-l
        putem citi e UNKNOWN, nu „0 lei". Gol, nu zero, și fără excepție pe drumul de randare."""
        assert amount_text(bad, "ro") == ""

    def test_fallbackul_determinist_scrie_romaneste(self) -> None:
        text = _deterministic_reply([{"name": "Cremă X", "price": 89.0}])
        assert "89,00 lei" in text
        assert "89.00" not in text

    def test_pretul_formatat_trece_propriul_validator(self) -> None:
        """Bucla închisă: ce SCRIEM trebuie să treacă de ce VALIDĂM. Altfel fallback-ul
        determinist ar produce un text pe care propriul nostru validator îl declară inventat."""
        products = [{"price": 1234.5}]
        reply = f"Costă {amount_text(1234.5, 'ro')} lei."
        assert _prices_ok(reply, products) is True
