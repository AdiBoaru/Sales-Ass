"""Vocea răspunsului (`src/agent/voice.py`): ce scoate plasa și, la fel de important, ce NU atinge.

Regula e a lui Adi și e fermă: mesajul către client nu trebuie să „se vadă că e făcut cu AI".
Semnele care îl dau de gol în română sunt liniuța de pauză și punctul-și-virgula.
"""

from __future__ import annotations

import pytest

from src.agent import prompt_builder
from src.agent.voice import VOICE_RULES, naturalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Liniuța de pauză ca aside: exact tiparul de AI pe care îl vânăm.
        (
            "Potrivit dacă ai ten sensibil — cu niacinamidă, pentru calmare.",
            "Potrivit dacă ai ten sensibil, cu niacinamidă, pentru calmare.",
        ),
        ("Gata - am adăugat crema în coș.", "Gata, am adăugat crema în coș."),
        ("Am găsit trei creme – toate ies ieftin.", "Am găsit trei creme, toate ies ieftin."),
        ("Merge bine -- îți place.", "Merge bine, îți place."),
        # Punct și virgulă: virgulă, nu punct (nu cerem recapitalizare, nu rupem enumerarea).
        ("Am verificat stocul; mai sunt 3 bucăți.", "Am verificat stocul, mai sunt 3 bucăți."),
        ("Bună dacă vrei X; dacă ai ten gras, ia Y.", "Bună dacă vrei X, dacă ai ten gras, ia Y."),
        # Punctuație deja existentă: nu o dublăm.
        ("Ai ales bine, — merge cu tenul tău.", "Ai ales bine, merge cu tenul tău."),
        ("Uite ce am: — cremă și ser.", "Uite ce am: cremă și ser."),
        # Liniuță orfană la capăt de rând.
        ("Am găsit trei creme —", "Am găsit trei creme"),
        # Em dash lipit între litere.
        ("cuvânt—cuvânt lipit", "cuvânt, cuvânt lipit"),
    ],
)
def test_scoate_semnele_de_ai(raw: str, expected: str) -> None:
    assert naturalize(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Ortografie corectă: cratima din cuvinte NU e stil, e limbă.
        "Nu-s sigur că ți-l pot trimite azi, dar să-ți spun ce am.",
        # Interval numeric deja lipit + en dash tipografic între cifre.
        "Livrarea durează 2-3 zile.",
        "Are între 2–19 recenzii.",
        # Reducerea negativă de pe badge și SKU-urile cu cratimă.
        "Reducere -50% la seruri.",
        "Comanda ORD-123 e pe drum.",
        # Linkurile nu se ating.
        "Uite linkul: https://shop.ro/p/crema-de-fata",
        # Bulletul de la începutul rândului nu e „în timpul propoziției".
        "Optiuni:\n- Cremă A\n- Cremă B",
        "Optiuni:\n  - Cremă A\n  - Cremă B",
    ],
)
def test_nu_atinge_ce_e_corect(text: str) -> None:
    assert naturalize(text) == text


def test_interval_scris_cu_spatii_devine_interval_nu_enumerare() -> None:
    """„2 - 3 zile" e un interval prost tipografiat, nu o pauză. O virgulă acolo ar schimba
    SENSUL („2, 3 zile"), deci normalizarea îl strânge în loc să-l despartă."""
    assert naturalize("Livrarea durează 2 - 3 zile.") == "Livrarea durează 2-3 zile."


def test_idempotenta() -> None:
    """Plasa se aplică pe mai multe straturi (scrub în compose + `set_reply`); a doua trecere
    trebuie să fie un no-op, altfel s-ar acumula virgule."""
    raw = "Ten sensibil — cu niacinamidă; merge zilnic - dimineața."
    once = naturalize(raw)
    assert naturalize(once) == once


def test_gol_si_none_trec_neatinse() -> None:
    assert naturalize(None) is None
    assert naturalize("") == ""


def test_nu_schimba_cifrele_si_numele() -> None:
    """Normalizarea rulează DUPĂ validator (P2). Dacă ar putea muta o cifră sau un nume, ar
    invalida exact textul pe care validatorul tocmai l-a aprobat."""
    raw = "Crema Hidratantă Ultra — 89,00 lei; rating 4,8."
    out = naturalize(raw) or ""
    for token in ("Crema Hidratantă Ultra", "89,00", "4,8"):
        assert token in out


class TestPromptulNuPredaSemneleInterzise:
    """Un exemplu de răspuns cu liniuță în prompt îl învață pe model exact ce îi interzicem.
    Erau două în `_RICH_RULES` („Potrivit dacă ai ten sensibil — cu niacinamidă", „vrei X; ia Y")
    și sunt cauza pentru care regula scrisă doar în memorie n-a ținut."""

    @staticmethod
    def _prompturi() -> dict[str, str]:
        inp = prompt_builder.PromptInputs.build(
            business_name="Sole Demo",
            vertical="beauty",
            locale="ro",
            categories=["creme", "seruri"],
            aliases=[("crema fata", "creme")],
        )
        return {
            "agent": prompt_builder.build_agent_system(inp),
            "reco": prompt_builder.build_reco_system(inp),
            "rich": prompt_builder.build_rich_system(inp),
            "order": prompt_builder.ORDER_RECO_SYSTEM,
        }

    @pytest.mark.parametrize("name", ["agent", "reco", "rich", "order"])
    def test_fara_liniuta_de_pauza_sau_punct_si_virgula(self, name: str) -> None:
        text = self._prompturi()[name]
        # Blocul de voce CITEAZĂ semnele ca să le interzică; le scoatem înainte de a căuta.
        body = text.replace(VOICE_RULES, "")
        assert naturalize(body) == body, f"promptul {name} conține semne pe care le interzicem"

    @pytest.mark.parametrize("name", ["agent", "reco", "rich", "order"])
    def test_contine_contractul_de_voce(self, name: str) -> None:
        assert VOICE_RULES in self._prompturi()[name]
