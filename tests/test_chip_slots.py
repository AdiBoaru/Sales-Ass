"""Câte sugestii ies la client are UN proprietar: `settings.chip_slots`.

Înainte cifra trăia în trei locuri care nu se cunoșteau — producătorul tăia la 4
(`clarify_menu._MAX_CHIPS`), calea bogată v1 la 6 (`compose._MAX_CHIPS`), iar randorul web la 5
(`render._MAX_WEB_CHIPS`). Consecința nu era teoretică: pe calea vie ieșeau 4 chips, al cincilea
slot al widgetului n-a fost folosit NICIODATĂ, iar o creștere a producătorului la 6 s-ar fi
pierdut tăcut în randor, fără ca vreun test să pice.

Testul nu verifică valoarea 5. Verifică PROPAGAREA: ridici cifra într-un loc și toți trei
consumatorii o urmează. O a doua constantă reintrodusă oriunde pe lanț îl face roșu.
"""

from __future__ import annotations

import pytest

from src.catalog.clarify_menu import ClarifyMenu, MenuOption, ground_suggestions
from src.channels.web.render import _web_chips
from src.config import get_settings
from src.worker.compose import _suggestion_chips


@pytest.fixture
def _clean_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _menu(n: int) -> ClarifyMenu:
    return ClarifyMenu(
        options=tuple(
            MenuOption(phrase=f"optiunea {i}", dimension="concerns", key=f"k{i}", count=100 - i)
            for i in range(n)
        ),
        reason="topic",
    )


def test_default_is_five_chips(_clean_settings) -> None:
    """Cererea de produs: 5, ca la iZi. Producătorul le poate livra, nu doar widgetul."""
    assert get_settings().chip_slots == 5
    kept, _ = ground_suggestions((), _menu(10), limit=get_settings().chip_slots)
    assert len(kept) == 5


@pytest.mark.parametrize("slots", [3, 5, 6])
def test_one_owner_propagates_to_all_three_consumers(monkeypatch, _clean_settings, slots) -> None:
    """Producător, calea bogată v1 și randorul web citesc ACEEAȘI cifră."""
    monkeypatch.setenv("CHIP_SLOTS", str(slots))
    get_settings.cache_clear()

    labels = [f"optiunea {i}" for i in range(10)]
    kept, _ = ground_suggestions((), _menu(10), limit=get_settings().chip_slots)
    assert len(kept) == slots, "producătorul (meniul închis)"
    assert len(_suggestion_chips(labels)) == slots, "calea bogată v1 (compose)"
    assert len(_web_chips(labels)) == slots, "randorul web"


def test_web_renderer_does_not_clip_below_producer(monkeypatch, _clean_settings) -> None:
    """Regresia concretă de dinainte: producătorul putea crește, randorul tăia mai jos, tăcut."""
    monkeypatch.setenv("CHIP_SLOTS", "6")
    get_settings.cache_clear()
    produced, _ = ground_suggestions((), _menu(10), limit=get_settings().chip_slots)
    assert len(_web_chips(list(produced))) == len(produced)
