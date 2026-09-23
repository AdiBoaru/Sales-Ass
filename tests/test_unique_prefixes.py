"""NX-318 — „câte cuvinte identifică un produs” se decide față de SETUL afișat.

Un singur utilitar (`render_text.unique_prefixes`), trei consumatori: ordinea cardurilor după
text (`compose._order_by_first_mention`), ancora chip-ului (`chip_moves.from_cards` /
`_fit_anchor`) și rezolvarea apăsării (`reference_resolver.match_name`). Seturile de mai jos sunt
cardurile REALE ale conversației `f4e1431e` (`sole-ro`, 2026-09-23, «vreau o crema de hidratare»),
turele 1 și 4, cu numele scurte exact cum au ajuns la client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.reference_resolver import match_name, resolve_from_displayed
from src.catalog.render_text import unique_prefixes
from src.config import get_settings
from src.conversation import chip_moves
from src.models import MAX_CHIP_LEN, ProductRef
from src.worker.compose import _order_by_first_mention

# Turul 1, în ordinea de ranking în care au ajuns la client (EUBOS pe locul 5).
TURN1 = {
    "6d9c": "SOME BY MI Yuja Niacin Anti-Blemish Cream",
    "ec2a": "By Wishtrend Vitamin A-mazing Bakuchiol Crema de fata de noapte",
    "6924": "HARUHARU WONDER Centella Phyto and 5 Peptide Concentrate Cream Refill Crema de fata",
    "e5af": "Dear Klairs Midnight Blue Crema pentru fata Midnight Blue",
    "d6bd": "EUBOS Diabetic Skin Care Cream",
    "3a77": "HARUHARU WONDER Black Rice 10 Hyaluronic Cream crema de fata",
}
# Textul real care numea produsele (`rich_raw.education` pe turul 46961eb8).
TURN1_TEXT = (
    "Dacă ai ten uscat și vrei o cremă pentru dimineața și seara, EUBOS este alegerea potrivită. "
    "Pentru ten sensibil, SOME BY MI oferă o textură ușoară și ingrediente calmante, iar dacă "
    "preferi aplicarea doar seara și urmărești și fermitate, By Wishtrend aduce retinal și "
    "bakuchiol."
)

# Turul 4 («si ceva mai ieftin»): două „The Fresh” și două „SOME BY MI Real” pe ecran.
TURN4 = {
    "1c1f": "MIZON Pore Fresh Clear Nose Pack",
    "09ff": "IT'S SKIN The Fresh Blueberries",
    "0d2d": "IT'S SKIN The Fresh Coconut",
    "490b": "VILLAGE 11 FACTORY Active Clean Sheet Mask Lemon",
    "2b2a": "SOME BY MI Real Snail Skin Barrier Care Mask",
    "3880": "SOME BY MI Real Honey Luminous Care Mask",
}

PACK = SimpleNamespace(
    chip_templates={
        "detail": {"ro": "Spune-mi mai multe despre {slot}"},
        "reviews": {"ro": "Ce spun recenziile despre {slot}"},
        "compare": {"ro": "Compara {slot} cu {slot_b}"},
        "link": {"ro": "Trimite-mi linkul la {slot}"},
        "price_band": {"ro": "Vreau ceva sub {slot} lei"},
    }
)


def _cards(names: dict[str, str], price: float = 10.0) -> list[dict]:
    return [{"product_id": pid, "name": n, "price": price} for pid, n in names.items()]


class _Ctx:
    def __init__(self) -> None:
        self.language = "ro"
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, **props) -> None:
        self.events.append((name, props))


# --- 1. utilitarul ----------------------------------------------------------------------------


def test_turn1_prefixes_match_the_card():
    got = unique_prefixes(TURN1, locale="ro")
    assert got["d6bd"] == ("eubos",)
    assert got["6924"] == ("haruharu", "wonder", "centella")
    assert got["3a77"] == ("haruharu", "wonder", "black")
    assert got["ec2a"] == ("by",)


def test_turn4_prefixes_need_the_distinguishing_word():
    got = unique_prefixes(TURN4, locale="ro")
    assert got["09ff"] == ("its", "skin", "the", "fresh", "blueberries")
    assert got["0d2d"] == ("its", "skin", "the", "fresh", "coconut")
    assert got["2b2a"] == ("some", "by", "mi", "real", "snail")
    assert got["3880"] == ("some", "by", "mi", "real", "honey")
    assert got["1c1f"] == ("mizon",)


def test_name_that_is_a_full_prefix_of_another_gets_the_whole_name():
    got = unique_prefixes({"a": "Petala Rich", "b": "Petala Rich Night"})
    assert got["a"] == ("petala", "rich")
    assert got["b"] == ("petala", "rich", "night")


def test_identical_names_both_get_the_whole_name():
    got = unique_prefixes({"a": "Petala Rich", "b": "Petala Rich"})
    assert got == {"a": ("petala", "rich"), "b": ("petala", "rich")}


def test_independent_of_input_order():
    forward = unique_prefixes(TURN4, locale="ro")
    backward = unique_prefixes(dict(reversed(list(TURN4.items()))), locale="ro")
    assert forward == backward


def test_stopword_is_never_a_one_word_prefix():
    got = unique_prefixes({"a": "La Roche Cream", "b": "Ser Petala"}, locale="ro")
    assert got["a"] == ("la", "roche")
    assert got["b"] == ("ser",)


def test_single_card_gets_its_first_word():
    assert unique_prefixes({"a": "EUBOS Diabetic Skin Care Cream"}) == {"a": ("eubos",)}


def test_empty_or_missing_name_is_ignored():
    got = unique_prefixes({"a": "", "b": "   ", "c": "EUBOS Cream"})
    assert got == {"c": ("eubos",)}


def test_diacritics_and_case_fold():
    got = unique_prefixes({"a": "Șampon Hidratant", "b": "sampon matifiant"})
    assert got == {"a": ("sampon", "hidratant"), "b": ("sampon", "matifiant")}


# --- 2. ordinea cardurilor --------------------------------------------------------------------


def test_turn1_text_puts_eubos_first():
    ids = list(TURN1)
    facts = {pid: {"name": name} for pid, name in TURN1.items()}
    ctx = _Ctx()
    got = _order_by_first_mention(ids, facts, {"intro": TURN1_TEXT}, ctx)
    assert got[:3] == ["d6bd", "6d9c", "ec2a"]
    # restul își păstrează ordinea de ranking
    assert got[3:] == ["6924", "e5af", "3a77"]
    assert ctx.events == [("rich_order_realigned", {"n": 3, "matched_by": "unique_prefix"})]


def test_common_brand_does_not_match():
    ids = list(TURN1)
    facts = {pid: {"name": name} for pid, name in TURN1.items()}
    got = _order_by_first_mention(ids, facts, {"intro": "HARUHARU e un brand bun."}, _Ctx())
    assert got == ids  # „haruharu” nu e unic în set ⇒ nu numește niciun produs


def test_unique_brand_matches_only_as_a_whole_word():
    ids = ["a", "b"]
    facts = {"a": {"name": "Ser Petala"}, "b": {"name": "Bloom Night Cream"}}
    intro = {"intro": "Bloomul de primăvară. Ser Petala."}
    got = _order_by_first_mention(ids, facts, intro, _Ctx())
    assert got == ["a", "b"]  # „bloomul” nu e „bloom”


def test_order_flag_off_is_the_old_rule(monkeypatch):
    monkeypatch.setattr(get_settings(), "unique_name_prefix_enabled", False)
    ids = list(TURN1)
    facts = {pid: {"name": name} for pid, name in TURN1.items()}
    ctx = _Ctx()
    got = _order_by_first_mention(ids, facts, {"intro": TURN1_TEXT}, ctx)
    # „SOME BY”/„By Wishtrend” au ≥2 cuvinte, EUBOS scris singur nu ⇒ EUBOS rămâne unde era
    assert got == ["6d9c", "ec2a", "6924", "e5af", "d6bd", "3a77"]
    assert ctx.events == []


# --- 3. ancora chip-ului ----------------------------------------------------------------------


def _chips(names: dict[str, str], *, unique: bool, stats: dict | None = None):
    moves = chip_moves.from_cards(_cards(names), unique_anchor=unique, locale="ro")
    return chip_moves.renderable(moves, PACK, "ro", stats=stats)


def test_turn4_no_two_products_share_an_anchor():
    anchors: dict[str, set[str]] = {}
    for move in _chips(TURN4, unique=True):
        if move.kind in ("detail", "reviews", "link"):
            pid = move.move_id.split(":", 1)[1]
            anchors.setdefault(move.anchor, set()).add(pid)
    assert anchors, "turul 4 trebuie să ofere mutări pe carduri"
    assert all(len(pids) == 1 for pids in anchors.values()), anchors
    assert "IT'S SKIN The Fresh" not in anchors


def test_pressing_a_unique_anchor_resolves_to_its_product():
    names = list(TURN4.values())
    ids = list(TURN4)
    for move in _chips(TURN4, unique=True):
        if move.kind not in ("detail", "reviews", "link"):
            continue
        text = chip_moves.render_move(move, PACK, "ro")
        assert text is not None and len(text) <= MAX_CHIP_LEN
        pid = move.move_id.split(":", 1)[1]
        refs = [ProductRef(product_id=i, name=n, price=10.0) for i, n in zip(ids, names)]
        resolution = resolve_from_displayed(text, refs)
        assert resolution.product_id == pid, (text, resolution)


def test_turn4_chips_flag_off_keep_the_ambiguous_anchor():
    anchors = {m.anchor for m in _chips(TURN4, unique=False) if m.kind == "detail"}
    assert "IT'S SKIN The Fresh" in anchors  # comportamentul de azi, pinuit


def test_anchor_that_cannot_fit_is_not_offered_and_counted():
    names = {
        "a": "Aaaaa Bbbbbbbbbb Cccccccccccc Dddddddddd Eeeeeeeeee Ffff One",
        "b": "Aaaaa Bbbbbbbbbb Cccccccccccc Dddddddddd Eeeeeeeeee Ffff Two",
        "c": "Zeta Cream",
    }
    stats: dict[str, int] = {}
    moves = _chips(names, unique=True, stats=stats)
    offered = {(m.kind, m.move_id.split(":", 1)[1]) for m in moves}
    assert ("detail", "a") not in offered and ("detail", "b") not in offered
    assert ("detail", "c") in offered and ("link", "c") in offered
    assert stats["dropped_ambiguous_anchor"] >= 6  # detail/reviews/link × 2 produse
    # cu pragul vechi, aceeași mutare ar fi ieșit cu ancora comună „Aaaaa Bbbbbbbbbb…”
    old = {(m.kind, m.move_id.split(":", 1)[1]) for m in _chips(names, unique=False)}
    assert ("detail", "a") in old


def test_card_without_name_is_ignored():
    cards = [{"product_id": "a", "name": ""}, {"product_id": "b", "name": "Zeta Cream"}]
    moves = chip_moves.from_cards(cards, unique_anchor=True, locale="ro")
    assert {m.move_id for m in moves} == {"detail:b", "reviews:b", "link:b"}


def test_from_cards_flag_off_is_identical_to_before():
    cards = _cards(TURN4)
    assert chip_moves.from_cards(cards) == chip_moves.from_cards(cards, unique_anchor=False)
    assert all(m.floors == () for m in chip_moves.from_cards(cards))


# --- 4. rezolvarea apăsării -------------------------------------------------------------------


#: Numele întreg e mai lung decât ancora chip-ului, deci treapta „nume complet în query” nu prinde,
#: iar pe tokeni ≥3 caractere cele două sunt la egalitate („01”/„02” sunt prea scurte).
_TIED = ["Petala Rich 01 Crema de fata hidratanta", "Petala Rich 02 Crema de fata hidratanta"]


def test_match_name_breaks_a_token_tie_with_the_unique_prefix():
    names = _TIED
    query = "Spune-mi mai multe despre Petala Rich 02"
    assert match_name(query, names, unique=False) is None  # scorul pe tokeni ≥3 e egal
    assert match_name(query, names, unique=True) == 1


def test_match_name_ambiguous_prefix_still_asks():
    names = list(TURN4.values())
    assert match_name("Spune-mi mai multe despre IT'S SKIN The Fresh", names, unique=True) is None


@pytest.mark.parametrize("flag", [True, False])
def test_match_name_reads_the_flag(monkeypatch, flag):
    monkeypatch.setattr(get_settings(), "unique_name_prefix_enabled", flag)
    names = _TIED
    expected = 1 if flag else None
    assert match_name("despre Petala Rich 02", names) == expected


def test_match_name_never_uses_a_stopword_as_a_whole_name():
    """Verificare post-implementare: `match_name` apela `unique_prefixes` FĂRĂ limbă, deci „la”
    din «linkul la crema aia» devenea prefixul unic al unui «La Roche…» și îl alegea. Cu limba
    turului (sau, fără ea, cu cuvintele goale ale tuturor limbilor) „la” nu e niciodată un nume."""
    names = ["La Roche-Posay Cicaplast Baume B5", "IT'S SKIN The Fresh Blueberries"]
    query = "vreau linkul la crema aia"
    assert match_name(query, names, unique=True, locale="ro") is None
    assert match_name(query, names, unique=True) is None  # fără limbă: direcția sigură
    refs = [ProductRef(product_id=str(i), name=n, price=1.0) for i, n in enumerate(names)]
    assert resolve_from_displayed(query, refs, locale="ro").product_id is None
    # numele întreg rămâne rezolvabil
    assert match_name("despre La Roche-Posay Cicaplast", names, unique=True, locale="ro") == 0


def test_unique_prefixes_without_locale_guards_every_known_language():
    got = unique_prefixes({"a": "La Roche Cream", "b": "Ser Petala"})
    assert got["a"] == ("la", "roche")
