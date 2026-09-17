"""Chips-urile ca MUTĂRI: dovadă înainte de text, poartă per mutare, mix de roluri impus.

Fiecare test de aici corespunde unei afirmații din `src/conversation/chip_moves.py`. Cele care
contează cel mai mult sunt cele despre POARTĂ: un chip e o promisiune apăsabilă, iar apăsarea lui
retrimite textul ca mesaj nou al clientului — deci un text din care ancora a dispărut nu e o
nuanță de stil, e o promisiune care duce în gol.
"""

from __future__ import annotations

import pytest

from src.catalog.clarify_menu import ClarifyMenu, MenuOption
from src.conversation import chip_moves as cm
from src.domain.loader import _load_default_json, _norm_chip_templates
from src.models import MAX_CHIP_LEN


class _Pack:
    def __init__(self, templates=None):
        self.chip_templates = (
            templates
            if templates is not None
            else _norm_chip_templates(_load_default_json("ecommerce").get("chip_templates"))
        )


PACK = _Pack()


def _menu(*options: tuple[str, str, str, int]) -> ClarifyMenu:
    return ClarifyMenu(
        options=tuple(MenuOption(phrase=p, dimension=d, key=k, count=c) for p, d, k, c in options),
        reason="topic",
    )


CARDS = [
    {"product_id": "A", "name": "Crema Hidratanta Aqua - cu acid hialuronic", "price": 89.0},
    {"product_id": "B", "name": "Ser Vitamina C", "price": 149.0},
    {"product_id": "C", "name": "Tonic Calmant", "price": 45.0},
]


# --- dovada: o mutare fără set-țintă nu există ------------------------------------------------


def test_menu_options_become_moves_with_evidence() -> None:
    moves = cm.from_menu(
        _menu(("ten uscat", "skin_type", "dry", 120), ("Machiaj", "category", "machiaj", 680))
    )
    kinds = {m.kind for m in moves}
    assert kinds == {"refine_facet", "pivot_shelf"}
    assert all(m.evidence > 0 for m in moves)
    # Round-trip-ul e deja făcut de `build_menu`; ancora e chiar fraza oferită acolo.
    assert {m.anchor for m in moves} == {"ten uscat", "Machiaj"}


def test_zero_evidence_option_is_not_offered() -> None:
    assert cm.from_menu(_menu(("ten mixt", "skin_type", "combination", 0))) == []


def test_price_band_must_split_the_set() -> None:
    """Un prag sub care intră tot nu îngustează nimic; unul sub care nu intră nimic e o
    promisiune goală. Ambele sunt respinse de aceeași condiție."""
    assert cm._price_band([89.0, 149.0, 45.0]) == 50  # 45 sub, 89 și 149 peste
    assert cm._price_band([100.0, 100.0, 100.0]) is None  # nimic nu desparte
    assert cm._price_band([89.0, 149.0]) is None  # sub pragul de eșantion


def test_cards_produce_deep_and_commit_moves() -> None:
    moves = cm.from_cards(CARDS)
    kinds = [m.kind for m in moves]
    assert {"detail", "reviews", "link", "compare", "price_band"} <= set(kinds)
    # Ancora e numele SCURT: numele real de catalog are ~190 de caractere și n-ar încăpea.
    detail = next(m for m in moves if m.kind == "detail")
    assert detail.anchor == "Crema Hidratanta Aqua"


def test_card_without_identity_produces_nothing() -> None:
    """Un chip care numește un produs nerezolvabil la apăsare e fix defectul evitat."""
    assert cm.from_cards([{"name": "Fara id", "price": 10.0}]) == []
    assert cm.from_cards([{"product_id": "X", "price": 10.0}]) == []


# --- poarta: textul modelului trece doar dacă păstrează ancora ---------------------------------


def test_model_may_dress_the_phrase_but_not_lose_the_anchor() -> None:
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    texts, stats = cm.apply_labels(
        [move], {move.move_id: "Am ten uscat si caut ceva hidratant"}, PACK, "ro"
    )
    assert texts == ["Am ten uscat si caut ceva hidratant"]
    assert stats == {"rephrased": 1}


def test_lost_anchor_falls_back_to_template_not_to_silence() -> None:
    """Fail-CLOSED pe text, fail-OPEN pe mutare: clientul nu pierde chip-ul."""
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    texts, stats = cm.apply_labels([move], {move.move_id: "Vreau ceva bun"}, PACK, "ro")
    assert texts == ["Caut ceva pentru ten uscat"]
    assert stats["no_anchor"] == 1


def test_overlong_label_is_rejected_not_truncated() -> None:
    """Trunchierea DUPĂ verificare ar putea scoate tocmai ancora: poarta ar spune «da» despre un
    text, iar la client ar pleca altul."""
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    long = "Am ten uscat " + "foarte " * 12
    assert len(long) > MAX_CHIP_LEN
    texts, stats = cm.apply_labels([move], {move.move_id: long}, PACK, "ro")
    assert texts == ["Caut ceva pentru ten uscat"]
    assert stats["too_long"] == 1


def test_invented_move_id_is_counted_and_ignored() -> None:
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    texts, stats = cm.apply_labels([move], {"refine_facet:inventat": "orice"}, PACK, "ro")
    assert texts == ["Caut ceva pentru ten uscat"]
    assert stats["unknown_move"] == 1


def test_only_menu_moves_are_rephrasable() -> None:
    """Mutările născute din cardurile turului apar DUPĂ plan, deci n-au cum să fie în promptul
    aceluiași apel. A cere un al doilea apel ca să le îmbrace ar fi relay-ul interzis de D1."""
    card_move = next(m for m in cm.from_cards(CARDS) if m.kind == "detail")
    texts, stats = cm.apply_labels(
        [card_move], {card_move.move_id: "Crema Hidratanta Aqua, zi-mi tot"}, PACK, "ro"
    )
    assert texts == ["Spune-mi mai multe despre Crema Hidratanta Aqua"]
    assert "rephrased" not in stats


# --- copy-ul nu e în cod -----------------------------------------------------------------------


def test_pack_without_template_offers_nothing() -> None:
    """P11: fără șablon în pachet, mutarea nu se oferă. Un fallback românesc scris în cod ar face
    pilotul `ro` să pară că merge și ar tăcea pe orice alt tenant."""
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    texts, _ = cm.apply_labels([move], {}, _Pack({}), "ro")
    assert texts == []


def test_unknown_locale_falls_back_to_language_root() -> None:
    move = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))[0]
    texts, _ = cm.apply_labels([move], {}, PACK, "ro-RO")
    assert texts == ["Caut ceva pentru ten uscat"]


def test_every_declared_move_kind_has_copy_in_every_default_pack() -> None:
    """O mutare declarată în registru și fără șablon în pachete ar fi cod mort tăcut."""
    for vertical in ("ecommerce", "beauty_salon", "auto_service", "other"):
        templates = _norm_chip_templates(_load_default_json(vertical).get("chip_templates"))
        assert set(templates) == set(cm.MOVE_ROLES), vertical
        for kind, per_locale in templates.items():
            assert "ro" in per_locale, (vertical, kind)


def test_long_name_shortens_the_anchor_to_whole_words() -> None:
    """Găsit rulând proba pe catalogul SOLE: numele scurt REAL are ~33 de caractere, iar
    «Spune-mi mai multe despre {slot}» are 25 — deci `fit_template` tăia numele, ancora nu se mai
    regăsea în text, și mutarea se arunca. Efectul măsurat: pe un tur factual nu mai rămânea
    NICIO cale de adâncire, exact clasa cea mai utilă acolo.

    Ancora se scurtează pe cuvinte întregi și păstrează minimum două, fiindcă un prefix de două
    cuvinte dintr-un nume afișat e suficient ca `reference_resolver` să-l regăsească — pe când
    «RIEMANN P20 Urban Shi…» nu e nici nume, nici prefix."""
    name = "RIEMANN P20 Urban Shield SPF 50+ Sensitive"
    moves = cm.renderable(
        cm.from_cards([{"product_id": "A", "name": name, "price": 10.0}]), PACK, "ro"
    )
    detail = next(m for m in moves if m.kind == "detail")
    assert detail.anchor != name
    assert name.startswith(detail.anchor), "ancora e un PREFIX, nu o tăietură la caracter"
    assert len(detail.anchor.split()) >= 2
    rendered = cm.render_move(detail, PACK, "ro")
    assert rendered and len(rendered) <= MAX_CHIP_LEN
    assert detail.anchor in rendered


def test_a_move_that_cannot_fit_two_words_is_not_offered() -> None:
    """Sub două cuvinte rămâne brandul, iar catalogul are «Petala Nourish», «Petala Rich»."""
    moves = cm.from_cards(
        [{"product_id": "A", "name": "Supercalifragilistic Extraordinarium", "price": 10.0}]
    )
    assert moves, "mutările se construiesc; filtrul e la exprimare"
    compare_like = [m for m in cm.renderable(moves, PACK, "ro") if m.kind == "compare"]
    assert compare_like == []


def test_short_names_still_render_within_the_contract() -> None:
    moves = cm.from_cards(CARDS)
    rendered = [cm.render_move(m, PACK, "ro") for m in cm.renderable(moves, PACK, "ro")]
    assert rendered and all(r and len(r) <= MAX_CHIP_LEN for r in rendered)


# --- selecția: mix de roluri, nu cinci filtre --------------------------------------------------


def test_selection_mixes_roles_instead_of_taking_the_best_evidence() -> None:
    candidates = cm.from_menu(
        _menu(
            ("ten uscat", "skin_type", "dry", 900),
            ("riduri", "concerns", "anti_aging", 800),
            ("acnee", "concerns", "acne", 700),
            ("Machiaj", "category", "machiaj", 10),  # dovadă mică, dar SINGURA cale laterală
        )
    ) + cm.from_cards(CARDS)
    picked = cm.select(candidates, slots=5, role_order=cm.roles_for(["recommend"]))
    roles = {m.role for m in picked}
    assert cm.ROLE_LATERAL in roles, "o îngustare mai bună nu are voie să scoată singura pivotare"
    assert cm.ROLE_DEEPEN in roles
    assert len(picked) == 5


def test_factual_turn_gets_no_refinement_chips() -> None:
    """«Care e prețul?» + cinci îngustări = un bot care nu te-a auzit. Rolurile absente din
    ordine nu se emit deloc."""
    candidates = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 900))) + cm.from_cards(CARDS)
    picked = cm.select(candidates, slots=5, role_order=cm.roles_for(["answer"]))
    assert all(m.role != cm.ROLE_FORWARD for m in picked)
    assert picked, "un tur factual primește totuși continuări, pe produsul discutat"


def test_already_offered_moves_are_not_repeated() -> None:
    menu = _menu(("ten uscat", "skin_type", "dry", 900), ("riduri", "concerns", "anti_aging", 800))
    first = cm.select(cm.from_menu(menu), slots=1, role_order=cm.roles_for(["recommend"]))
    second = cm.select(
        cm.from_menu(menu, offered_before=[first[0].move_id]),
        slots=5,
        role_order=cm.roles_for(["recommend"]),
        offered_before=[first[0].move_id],
    )
    assert first[0].move_id not in {m.move_id for m in second}


def test_selection_is_deterministic() -> None:
    candidates = cm.from_menu(_menu(("a", "concerns", "k1", 100), ("b", "concerns", "k2", 100)))
    order = cm.roles_for(["recommend"])
    assert [m.move_id for m in cm.select(candidates, slots=2, role_order=order)] == [
        m.move_id for m in cm.select(candidates, slots=2, role_order=order)
    ]


def test_one_kind_yields_to_diversity_but_still_fills_the_turn() -> None:
    """Plafonul pe fel e o PREFERINȚĂ, nu o limită de adevăr.

    Tratat ca limită dură, înfometa exact turul care are cea mai mare nevoie de sugestii: pe
    catalogul SOLE, la PRIMUL tur (fără raft discutat) meniul nu poate oferi fațete — o fațetă
    neancorată pe raft e o promisiune falsă (NX-295) — deci singurul fel disponibil e
    `pivot_shelf`, și ieșeau 2 chips din 5 tocmai când clientul are cel mai puțin context."""
    only_one_kind = cm.from_menu(
        _menu(*[(f"nevoia {i}", "concerns", f"k{i}", 100 - i) for i in range(6)])
    )
    picked = cm.select(only_one_kind, slots=5, role_order=cm.roles_for(["clarify"]))
    assert len(picked) == 5, "fără alt fel disponibil, turul se umple cu ce există"

    # Cu alte feluri prezente, diversitatea câștigă: overflow-ul intră DUPĂ ce fiecare rol a ales.
    mixed = only_one_kind + cm.from_menu(_menu(("Machiaj", "category", "machiaj", 10)))
    picked = cm.select(mixed, slots=5, role_order=cm.roles_for(["clarify"]))
    assert any(m.kind == "pivot_shelf" for m in picked)
    assert picked[1].kind == "pivot_shelf", "un al doilea raft nu ia locul primei căi laterale"


# --- promptul ----------------------------------------------------------------------------------


def test_offer_block_carries_ids_and_anchors_but_not_the_template() -> None:
    """Șablonul dat ca exemplu ar fi copiat de model, și am plăti tokeni ca să primim înapoi exact
    textul pe care îl aveam deja."""
    moves = cm.from_menu(_menu(("ten uscat", "skin_type", "dry", 120)))
    block = cm.offer_block(moves, PACK, "ro", "Sugestii oferibile:")
    assert "ten uscat" in block and moves[0].move_id in block
    assert "Caut ceva pentru" not in block


def test_offer_block_is_empty_without_rephrasable_moves() -> None:
    assert cm.offer_block(cm.from_cards(CARDS), PACK, "ro", "x") == ""


@pytest.mark.parametrize(
    "kinds,expected_first",
    [
        (["recommend"], cm.ROLE_FORWARD),
        (["answer"], cm.ROLE_DEEPEN),
        (["compare"], cm.ROLE_DEEPEN),
        ([], cm.ROLE_FORWARD),
    ],
)
def test_roles_follow_the_turn_obligations(kinds, expected_first) -> None:
    assert cm.roles_for(kinds)[0] == expected_first
