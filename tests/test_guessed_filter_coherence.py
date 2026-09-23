"""NX-313 — filtrul GHICIT se judecă după cerere; motivul cardului nu cade pe un nume cu cifră;
un card per familie pe pagină.

Turul real `bcd8e5c6` (`sole-ro`, 2026-09-23, «vreau o crema de hidratare»). Modelul a trimis
`category="fata"` (ghicit: raftul „Fata" e MACHIAJ) și `concerns=["hidratare"]` (rostit). Textul
prindea STRICT patru BB-uri pe raftul de machiaj, deci nici NX-305 (cere ca nimic să nu fie rostit
ȘI rezultatul să fie degradat) nu avea ce prinde. Aceeași cerere fără raftul ghicit: 50 de potriviri
stricte, dintre care 0 pe raftul ghicit.

Măsurat pe traficul real (`scripts/nx313_guessed_filter_coherence_probe.py`, 57 de căutări cu
ghicitură): pe `strict`, ghiciturile corecte stau la 0,16 și peste, iar cea din tur la 0,00.
"""

from __future__ import annotations

from src.tools.catalog_tools import (
    guessed_filter_verdict,
    one_per_family,
    should_probe_guessed_filters,
)
from src.worker.compose import grounded_identifiers, scrub_prose
from src.worker.text_scrub import has_unverifiable_claim, identifier_tokens


def _rows(ids: range | list[int], step: str | None = None) -> list[dict[str, object]]:
    return [{"id": f"p{i}", "lexical_step": step} for i in ids]


def _verdict(before, after, *, comparable=True, min_share=0.10, min_rows=5):  # type: ignore[no-untyped-def]
    return guessed_filter_verdict(
        before, after, comparable=comparable, min_share=min_share, min_rows=min_rows
    )


# ── când se face proba ──────────────────────────────────────────────────────────────────────────


def test_a_guessed_shelf_is_probed_even_next_to_an_uttered_facet() -> None:
    """Cazul turului: raftul ghicit, fațeta rostită. NX-305 cerea ca NIMIC să nu fie rostit."""
    assert should_probe_guessed_filters(
        enabled=True, category_guessed=True, facets_guessed=False, has_content_terms=True
    )


def test_nothing_guessed_means_no_probe() -> None:
    assert not should_probe_guessed_filters(
        enabled=True, category_guessed=False, facets_guessed=False, has_content_terms=True
    )


def test_without_content_terms_the_probe_has_nothing_to_match() -> None:
    assert not should_probe_guessed_filters(
        enabled=True, category_guessed=True, facets_guessed=False, has_content_terms=False
    )


def test_the_rescue_kill_switch_also_stops_the_probe() -> None:
    assert not should_probe_guessed_filters(
        enabled=False, category_guessed=True, facets_guessed=True, has_content_terms=True
    )


# ── verdictul ───────────────────────────────────────────────────────────────────────────────────


def test_the_real_turn_is_contradicted() -> None:
    """4 potriviri stricte pe raftul ghicit, niciuna printre primele 50 ale cererii deschise."""
    reason, share = _verdict(_rows(range(4)), _rows(range(100, 150)))
    assert reason == "contradicted"
    assert share == 0.0


def test_a_right_guess_only_narrows_and_is_kept() -> None:
    """«crema hidratanta» cu `category="ten"`: 44 din 50 sunt deja pe raft (0,88 măsurat)."""
    reason, share = _verdict(_rows(range(44)), _rows(list(range(44)) + list(range(100, 106))))
    assert reason is None
    assert share == 0.88


def test_a_narrow_but_right_guess_above_the_threshold_is_kept() -> None:
    """Cea mai mică proporție a unei ghicituri corecte, pe trafic: 0,16 («rutina ten uscat»).
    Pragul trebuie să stea sub ea, altfel scoatem rafturi bune."""
    reason, _ = _verdict(_rows(range(8)), _rows(list(range(8)) + list(range(100, 142))))
    assert reason is None


def test_relaxed_rungs_are_never_judged_by_share() -> None:
    """Pe trepte relaxate setul deschis e dominat de cuvinte comune: proporția ar măsura
    zgomotul. Pe trafic, «si ceva de volum?» ar fi schimbat părul pe plumpere de buze."""
    reason, share = _verdict(_rows(range(4), "relaxed"), _rows(range(100, 150), "relaxed"))
    assert (reason, share) == (None, None)


def test_a_relaxed_filter_ladder_is_not_comparable() -> None:
    """Scara a renunțat deja la o fațetă: `before` a rulat alt WHERE, subsetul nu mai ține."""
    reason, _ = _verdict(_rows(range(4)), _rows(range(100, 150)), comparable=False)
    assert reason is None


def test_too_few_open_rows_say_nothing() -> None:
    reason, _ = _verdict(_rows(range(3)), _rows(range(100, 104)))
    assert reason is None


def test_a_better_rung_still_wins_like_nx305() -> None:
    """Mănușa de autobronzant (`78b347fa`): `relaxed_any` cu raft ghicit → `strict` fără el."""
    reason, _ = _verdict(_rows(range(19), "relaxed_any"), _rows(range(100, 105)))
    assert reason == "better_rung"


def test_a_better_rung_on_a_single_row_is_not_adopted() -> None:
    """«dar eu am zis sa nu fie cremos»: raftul de buze schimbat pe UN produs de ten."""
    reason, _ = _verdict(_rows(range(4), "relaxed"), _rows([100]))
    assert reason is None


def test_an_empty_open_set_keeps_the_guess() -> None:
    assert _verdict(_rows(range(4)), []) == (None, None)


# ── un card per familie ─────────────────────────────────────────────────────────────────────────


def test_repeated_display_names_move_after_first_appearances() -> None:
    """Numele REALE din catalog: întregi diferite, identice pe card (tăiate la prima virgulă)."""
    rows = [
        {"id": "a1", "name": "VILLAGE 11 FACTORY MY Skin Fit BB Cream, #1 Light Ivory, 20 ml - x"},
        {"id": "a2", "name": "VILLAGE 11 FACTORY MY Skin Fit BB Cream, #2 Medium Ivory, 20 ml - x"},
        {"id": "b1", "name": "Dear Klairs Illuminating Supple Blemish Cream - x"},
    ]
    assert [p["id"] for p in one_per_family(rows)] == ["a1", "b1", "a2"]


def test_one_per_family_is_stable_and_lossless() -> None:
    rows = [{"id": str(i), "name": n} for i, n in enumerate(["A", "B", "A", "C", "B"])]
    assert [p["id"] for p in one_per_family(rows)] == ["0", "1", "3", "2", "4"]


# ── motivul cardului: un NUME cu cifră nu e o cantitate ─────────────────────────────────────────


_VILLAGE = {
    "id": "6ab94c26",
    "name": "VILLAGE 11 FACTORY MY Skin Fit BB Cream",
    "price": 55.0,
    "coupon_code": "WELCOME15",
    "url": "https://sole.ro/p/123",
    "attributes": {"key_ingredients": ["complex v11", "acid hialuronic", "niacinamida"]},
}


def test_the_real_reason_survives() -> None:
    """Motivul cardului 1 din turul real, aruncat întreg fiindcă spunea „v11"."""
    fit = "Pentru bariera cutanată, cu complex v11, acid hialuronic, niacinamidă și pantenol."
    assert scrub_prose(fit) is None  # comportamentul vechi, păstrat fără fișă
    assert scrub_prose(fit, grounded_identifiers(_VILLAGE)) is not None


def test_quantities_are_still_rejected_even_when_on_the_record() -> None:
    """„50 ml" nu e un nume: o cifră fără literă rămâne cantitate neverificabilă."""
    g = grounded_identifiers({**_VILLAGE, "name": "X Cream 50 ml"})
    assert scrub_prose("Cu complex v11, 50 ml.", g) is None


def test_an_identifier_absent_from_the_record_is_rejected() -> None:
    assert scrub_prose("Cu vitamina B5.", grounded_identifiers(_VILLAGE)) is None


def test_codes_and_urls_do_not_ground_anything() -> None:
    """Voucherul și adresa au cifre, dar nu sunt numele a ceva din produs."""
    g = grounded_identifiers(_VILLAGE)
    assert "welcome15" not in g
    assert scrub_prose("Folosește codul WELCOME15.", g) is None


def test_identifier_tokens_need_a_letter_and_a_digit() -> None:
    assert identifier_tokens("Complex V11, 50 ml, N05 Toffee, 0.05% retinal") == frozenset(
        {"v11", "n05"}
    )


def test_marketing_claims_stay_rejected_with_grounding() -> None:
    assert has_unverifiable_claim("Cel mai bun, cu complex v11.", frozenset({"v11"}))


# ── SQL: niciun parametru legat și nefolosit ────────────────────────────────────────────────────


class _CaptureConn:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[object, ...] = ()

    async def fetch(self, sql: str, *params: object) -> list[object]:
        self.sql, self.params = sql, params
        return []


def _run_step(step: str, sort_mode: str) -> _CaptureConn:
    import asyncio

    from src.db.queries.catalog import _lexical_fetch

    conn = _CaptureConn()
    asyncio.run(
        _lexical_fetch(
            conn,  # type: ignore[arg-type]
            "biz",
            query_text="ceva mai ieftin crema hidratanta",
            terms=["crema", "hidratanta"],
            step=step,
            v2=True,
            category=["ten"],
            brand=None,
            concerns=None,
            facet_filters=None,
            features=None,
            searchable_facets=(),
            variant_label=None,
            price_max=69.99,
            constraints=(),
            sort_mode=sort_mode,
            in_stock_only=False,
            pool=50,
        )
    )
    return conn


def test_every_bound_parameter_is_referenced_on_every_step_and_sort() -> None:
    """Pe trafic real, «Vreau ceva mai ieftin decât astea» (`price_asc`) crăpa căutarea cu
    `IndeterminateDatatypeError: $2`: treapta `filters_only` lega textul ca ORDONATOR, dar pe un
    sort explicit rangul nu intră în `ORDER BY`, iar Postgres nu poate tipa un parametru
    nefolosit. Poarta e generală: pe ORICE treaptă și ORICE sort, fiecare `$n` apare în SQL."""
    import re

    for step in ("strict", "relaxed", "relaxed_any", "fuzzy", "filters_only"):
        for sort_mode in ("relevance", "price_asc", "price_desc", "rating"):
            c = _run_step(step, sort_mode)
            used = {int(n) for n in re.findall(r"\$(\d+)", c.sql)}
            assert used == set(range(1, len(c.params) + 1)), (step, sort_mode)
