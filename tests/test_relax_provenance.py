"""NX-299 felia 1 — scara de relaxare ordonează după PROVENIENȚĂ, nu doar după tipul câmpului.

Turul care a cerut cardul (`conversation_traces`, 2026-09-17 20:39, `42744330`): la «vreau ceva sa
scap de cosuri», modelul a trimis `category="dermato-cosmetice"` (raft de 6 produse din 2.758) și
`concerns=["coșuri"]` (rezolvat `acne`, 518 produse). Scara a aruncat fațeta ROSTITĂ pe treapta 1 și
a păstrat raftul GHICIT până la capăt, iar treapta terminală `filters_only` a servit raftul greșit.

Testele de aici sunt scrise pe ORDINE, nu pe rezultate de DB: ordinea e chiar decizia pe care o
schimbă felia, iar un test pe rezultate ar trece și cu ordinea veche dacă întâmplător catalogul are
produse pe ambele drumuri.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.models import Message
from src.tools.catalog_tools import _relax_ladder, uttered_by_client


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_sort_mode_enabled", True)
    monkeypatch.setattr(get_settings(), "search_relax_by_provenance_enabled", True)
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", True)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_sort_mode_enabled", True)
    monkeypatch.setattr(get_settings(), "search_relax_by_provenance_enabled", False)
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", True)


class _Msg:
    def __init__(self, body: str) -> None:
        self.body = body


class _Ctx:
    """Minimul pe care îl atinge `uttered_by_client`: mesajul turului + istoricul."""

    def __init__(self, body: str, history: list[Message] | None = None) -> None:
        self.message = _Msg(body)
        self.history = history or []


def _fields_relaxed(steps: list[dict]) -> list[str]:
    """Câmpurile soft, în ordinea în care scara renunță la ele."""
    order: list[str] = []
    for prev, cur in zip(steps, steps[1:], strict=False):
        for field in ("facet_filters", "category"):
            if prev[field] is not None and cur[field] is None:
                order.append(field)
    return order


# --- proveniența: ce a rostit clientul ---------------------------------------------------------


def test_uttered_reads_the_argument_not_the_resolved_key():
    """Modelul TRANSCRIE, codul confirmă (NX-251).

    Pe turul real clientul a scris «cosuri», modelul a trimis «coșuri», iar cheia rezolvată e
    `acne` — cuvânt pe care clientul nu l-a rostit niciodată. Coroborarea pe CHEIE ar fi declarat
    fațeta drept ghicită, adică exact inversul adevărului, și ar fi relaxat-o prima."""
    ctx = _Ctx("vreau ceva sa scap de cosuri")
    assert uttered_by_client(ctx, "coșuri") is True
    assert uttered_by_client(ctx, "acne") is False
    assert uttered_by_client(ctx, "dermato-cosmetice") is False


def test_uttered_looks_at_client_history_not_just_this_turn():
    """Asimetria care cere istoricul: aici consecința lui `False` e RELAXAREA.

    «vreau sa vad produse de par» → «altceva ?». Pe al doilea tur mesajul nu mai numește raftul,
    deci coroborarea doar pe turul curent l-ar declara ghicit și l-ar arunca din `WHERE` — iar
    clientul ar primi tot catalogul în loc de raftul pe care naviga."""
    hist = [Message(direction="inbound", author="contact", body="vreau sa vad produse de par")]
    assert uttered_by_client(_Ctx("altceva ?", hist), "par") is True
    assert uttered_by_client(_Ctx("altceva ?"), "par") is False


def test_uttered_ignores_what_the_bot_said():
    """O parafrază a botului nu e o afirmație a clientului. Altfel botul și-ar putea corobora
    singur propriile ipoteze, iar poarta ar deveni decorativă."""
    hist = [Message(direction="outbound", author="bot", body="uite produse de par")]
    assert uttered_by_client(_Ctx("altceva ?", hist), "par") is False


# --- ordinea treptelor -------------------------------------------------------------------------


def test_inferred_category_relaxes_before_uttered_facet(flag_on):
    """Cazul turului `42744330`. PICĂ pe codul vechi: acolo `facet_filters` cade prima, iar
    categoria nu primește deloc treaptă (`search_category_hard_enabled`)."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=["dermato-cosmetice"],
        in_stock_only=False,
        category_uttered=False,
        facets_uttered=True,
    )
    assert _fields_relaxed(steps) == ["category", "facet_filters"]
    # Treapta terminală (singura care oferă `filters_only`) nu mai poartă raftul ghicit.
    assert steps[-1]["category"] is None


def test_uttered_category_is_never_relaxed(flag_on):
    """Regresia pentru care a fost scris `search_category_hard_enabled`: clientul a cerut raftul,
    deci nu i se servește altul, oricât de gol ar fi."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=["par"],
        in_stock_only=False,
        category_uttered=True,
        facets_uttered=True,
    )
    assert all(s["category"] == ["par"] for s in steps)
    assert _fields_relaxed(steps) == ["facet_filters"]


def test_inferred_facet_relaxes_before_uttered_category(flag_on):
    """Simetria regulii: proveniența, nu câmpul. Clientul a numit raftul, modelul a adăugat o
    nevoie de la el; nevoia cade prima."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["dry"]},
        category=["par"],
        in_stock_only=False,
        category_uttered=True,
        facets_uttered=False,
    )
    assert _fields_relaxed(steps) == ["facet_filters"]
    assert all(s["category"] == ["par"] for s in steps)  # rostit ⇒ rămâne dur


def test_equal_provenance_keeps_the_historical_order(flag_on):
    """`sorted` e STABIL: la proveniență egală nimic nu se mișcă față de ordinea de dinainte."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=["ten"],
        in_stock_only=False,
        category_uttered=True,
        facets_uttered=True,
    )
    assert _fields_relaxed(steps) == ["facet_filters"]


def test_flag_off_is_byte_identical(flag_off):
    """Kill-switch: cu flagul stins, o categorie ghicită redevine inviolabilă și ordinea e cea
    veche, indiferent de proveniență."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=["dermato-cosmetice"],
        in_stock_only=False,
        category_uttered=False,
        facets_uttered=True,
    )
    assert _fields_relaxed(steps) == ["facet_filters"]
    assert all(s["category"] == ["dermato-cosmetice"] for s in steps)


def test_default_arguments_preserve_old_behaviour(flag_on):
    """Apelanții care nu cunosc încă proveniența (teste vechi, alte căi) primesc implicit
    `True`/`True`, deci ordinea istorică. Fără asta, felia ar schimba tăcut comportamentul
    oricărui apel care n-a fost actualizat."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=["dermato-cosmetice"],
        in_stock_only=False,
    )
    assert _fields_relaxed(steps) == ["facet_filters"]


def test_inferred_category_stays_hard_when_it_is_the_only_subject(flag_on):
    """`corroborated_by` e o potrivire LITERALĂ, deci sinonimul, cuvântul străin și forma
    flexionată produc toate același fals „ghicit": la «vreau makeup» cu `category="machiaj"`
    întoarce `False`, deși modelul a TRADUS, nu a ghicit.

    Consecința e acceptabilă cât timp rămâne un subiect după relaxare (fațeta duce cererea mai
    departe) și inacceptabilă când categoria e singurul subiect: acolo interogarea ar rămâne fără
    subiect, iar pagina ar deveni „cele mai bine notate produse" — eșecul măsurat și RESPINS la
    NX-298."""
    steps = _relax_ladder(
        price_max=None,
        facet_filters=None,
        category=["machiaj"],
        in_stock_only=False,
        category_uttered=False,
        facets_uttered=True,
    )
    assert all(s["category"] == ["machiaj"] for s in steps)
    assert _fields_relaxed(steps) == []


def test_legacy_soft_category_ladder_is_untouched(monkeypatch):
    """Cu `search_category_hard_enabled` stins, categoria avea DEJA o treaptă, indiferent de
    proveniență. Felia nu are voie s-o schimbe: acolo trăiește garda off-category, iar o scutire
    scrisă doar pe `not category_uttered` ar fi dezarmat-o tăcut."""
    monkeypatch.setattr(get_settings(), "search_sort_mode_enabled", True)
    monkeypatch.setattr(get_settings(), "search_relax_by_provenance_enabled", True)
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", False)
    steps = _relax_ladder(
        price_max=None,
        facet_filters=None,
        category=["machiaj"],
        in_stock_only=False,
        category_uttered=False,
        facets_uttered=True,
    )
    assert _fields_relaxed(steps) == ["category"]  # treapta veche există în continuare
