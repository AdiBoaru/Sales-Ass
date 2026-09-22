"""NX-305 — un filtru de subiect pe care nimeni nu l-a rostit trebuie să-și merite locul.

Turul real `78b347fa` (`sole-ro`, 2026-09-21 08:35). Clientul a întrebat «cat cost una?» despre
mănușa de aplicare pe care botul tocmai o pomenise. Modelul a trimis `category="accesorii"` (adică
`Machiaj > Accesorii`, raftul pensulelor) și `concerns=["autobronzant","aplicare"]`, din care
„autobronzant" s-a rezolvat pe `product_type` cu 7 produse.

Amândouă erau GHICITURI ale modelului, se contraziceau, intersecția era goală. Scara de relaxare
le-a ordonat după TIP, cum face de la NX-299 când proveniența e la egalitate, deci a păstrat-o pe
cea GROSIERĂ și a aruncat-o pe cea SPECIFICĂ: seturi de pensule de 1.300 de lei la o întrebare
despre o mănușă de 50.

Măsurat pe catalogul real (`python -m scripts.nx305_guessed_filter_probe`): fără filtrele ghicite,
aceeași interogare aterizează pe `strict` și scoate pe locul 1 «B.tan I don't want tan on my
hands», mănușă de aplicare autobronzant.
"""

from __future__ import annotations

from src.tools.catalog_tools import (
    _RUNG_ORDER,
    _STEP_NOTE_RO,
    _rung_of,
    rescue_wins,
    should_rescue_guessed_filters,
)


def _rows(step: str | None, n: int = 3) -> list[dict[str, object]]:
    return [{"id": f"p{i}", "lexical_step": step} for i in range(n)]


# ── vocabularul de trepte ───────────────────────────────────────────────────────────────────────


def test_rung_order_covers_every_declared_step() -> None:
    """Poarta mecanică. O treaptă adăugată în `_STEP_NOTE_RO` și uitată aici ar primi tăcut rangul
    „necunoscut" (99), deci n-ar putea nici salva, nici fi salvată — o degradare invizibilă."""
    assert set(_STEP_NOTE_RO) | {"strict"} == set(_RUNG_ORDER)


def test_strict_is_the_best_rung_and_filters_only_the_worst() -> None:
    assert _RUNG_ORDER["strict"] == min(_RUNG_ORDER.values())
    assert _RUNG_ORDER["filters_only"] == max(_RUNG_ORDER.values())


def test_rung_of_reads_the_first_labelled_row() -> None:
    assert _rung_of(_rows("relaxed_any")) == "relaxed_any"
    assert _rung_of([{"id": "p0"}, {"id": "p1", "lexical_step": "fuzzy"}]) == "fuzzy"


def test_rung_of_treats_unlabelled_rows_as_strict() -> None:
    assert _rung_of(_rows(None)) == "strict"


def test_rung_of_treats_the_empty_set_as_the_worst_rung() -> None:
    """Un set gol n-are cum să bată nimic. Citit ca `strict`, „n-am găsit" ar arăta ca o potrivire
    curată și ar putea ÎNLOCUI un rezultat real."""
    assert _rung_of([]) == "filters_only"


# ── cine se adoptă ──────────────────────────────────────────────────────────────────────────────


def test_rescue_wins_only_on_a_strictly_better_rung() -> None:
    assert rescue_wins(_rows("relaxed_any"), _rows(None)) is True  # relaxed_any → strict
    assert rescue_wins(_rows("filters_only"), _rows("relaxed")) is True


def test_rescue_does_not_win_on_an_equal_rung() -> None:
    """Fără filtre, orice interogare prinde mai ușor ceva. „Mai multe rezultate" nu înseamnă
    „rezultate mai bune", iar pe treaptă egală setul filtrat poartă și ipoteza modelului."""
    assert rescue_wins(_rows("relaxed", n=2), _rows("relaxed", n=40)) is False


def test_rescue_does_not_win_on_a_worse_rung_or_empty() -> None:
    assert rescue_wins(_rows(None), _rows("fuzzy")) is False
    assert rescue_wins(_rows("relaxed_any"), []) is False


# ── când se încearcă ────────────────────────────────────────────────────────────────────────────


def _gate(**kw: object) -> bool:
    base: dict[str, object] = {
        "enabled": True,
        "category_uttered": False,
        "facets_uttered": False,
        "has_guessed_subject": True,
        "has_content_terms": True,
        "degraded": True,
    }
    base.update(kw)
    return should_rescue_guessed_filters(**base)  # type: ignore[arg-type]


def test_the_real_turn_triggers_the_rescue() -> None:
    """«cat cost una?» — subiectul vine din replica BOTULUI, deci niciun filtru nu se
    coroborează cu ce a scris clientul."""
    assert _gate() is True


def test_an_uttered_shelf_is_never_dropped() -> None:
    """Dacă a rostit clientul raftul, a-l scoate ar însemna să ignorăm cererea."""
    assert _gate(category_uttered=True) is False


def test_an_uttered_facet_is_never_dropped() -> None:
    """Poarta care ține NX-298 și NX-299 neatinse: acolo clientul a scris «cosuri», deci fațeta e
    rostită, iar salvarea nu se declanșează. Fără condiția asta, cardul ăsta le-ar fi anulat."""
    assert _gate(facets_uttered=True) is False


def test_a_clean_match_is_never_second_guessed() -> None:
    """Plafonul de cost al regulii: a doua interogare rulează doar pe turele ieșite prost."""
    assert _gate(degraded=False) is False


def test_nothing_to_drop_means_nothing_to_retry() -> None:
    assert _gate(has_guessed_subject=False) is False


def test_without_content_terms_the_rescue_has_nothing_to_stand_on() -> None:
    """După ce scoatem filtrele, TEXTUL e tot ce rămâne. Fără cuvinte de conținut am căuta în gol,
    iar `filters_only` nici nu se cere pe interogarea de salvare, tocmai ca să nu servim catalogul
    ordonat după rating."""
    assert _gate(has_content_terms=False) is False


def test_kill_switch_restores_the_previous_path() -> None:
    assert _gate(enabled=False) is False
