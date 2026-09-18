"""NX-299 felia 2 — forma răspunsului e un contract, nu o speranță.

Promptul cerea deja forma iZi („În `intro` spune ce tipuri ai pus pe masă"), dar nimic nu verifica
dacă slotul a ieșit. Pe turul `42744330` modelul scrisese încadrarea corectă și clientul n-a primit
nimic: `scrub_intro` a aruncat paragraful ÎNTREG pentru un superlativ din prima propoziție.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import answer_shape as shape
from src.worker import compose

_PACK = SimpleNamespace(
    answer_shape_templates={
        "framing": {"ro": "Ți-am ales {types}.", "en": "I picked out {types} for you."},
        "list_glue": {"ro": " și ", "en": " and "},
    }
)


def _p(pid: str, product_type: str | None) -> dict:
    return {"id": pid, "name": pid, "price": 10.0, "product_type": product_type}


# --- condițiile de emitere ---------------------------------------------------------------------


def test_framing_required_only_when_the_set_spans_classes():
    """O frază care anunță o varietate inexistentă e mai rea decât tăcerea."""
    many = shape.shape_for(n_items=6, product_types=("plasturi", "ser", "crema"), n_axes=2)
    one = shape.shape_for(n_items=6, product_types=("crema",), n_axes=2)
    assert shape.SLOT_FRAMING in many.required
    assert shape.SLOT_FRAMING not in one.required
    assert one.reason_for(shape.SLOT_FRAMING) == "single_type"


def test_factual_turn_asks_for_nothing_it_cannot_justify():
    """Un singur card, zero axe: nici încadrare, nici criterii de alegere.

    Asta e ce face contractul DINAMIC și nu un șablon. Cinci îngustări și o listă de criterii sub
    un răspuns punctual arată ca un bot care nu te-a auzit — aceeași regulă ca `roles_for`
    (NX-296), extinsă de la chips la tot răspunsul."""
    s = shape.shape_for(n_items=1, product_types=("crema",), n_axes=0)
    assert s.required == (shape.SLOT_FIT_LINE,)
    assert s.reason_for(shape.SLOT_CLOSING) == "too_few_items"


def test_closing_follows_the_axes_the_set_actually_varies_on():
    with_axes = shape.shape_for(n_items=4, product_types=("ser", "crema"), n_axes=3)
    flat = shape.shape_for(n_items=4, product_types=("ser", "crema"), n_axes=0)
    assert shape.SLOT_CLOSING in with_axes.required
    assert shape.SLOT_CLOSING not in flat.required
    assert flat.reason_for(shape.SLOT_CLOSING) == "no_axes"


def test_no_items_asks_for_no_prose_slots():
    s = shape.shape_for(n_items=0, product_types=(), n_axes=0)
    assert s.required == ()
    assert s.reason_for(shape.SLOT_FRAMING) == "no_items"


def test_missing_slots_are_reported_in_registry_order():
    s = shape.shape_for(n_items=6, product_types=("plasturi", "ser"), n_axes=2)
    assert shape.missing_slots(s, {}) == (
        shape.SLOT_FRAMING,
        shape.SLOT_FIT_LINE,
        shape.SLOT_CLOSING,
    )
    filled = dict.fromkeys(shape.SLOTS, True)
    assert shape.missing_slots(s, filled) == ()


def test_reasons_come_from_a_closed_vocabulary():
    """Eticheta unui event trebuie să vină dintr-o mulțime mărginită (cardinalitate)."""
    for n_items in (0, 1, 2, 6):
        for types in ((), ("a",), ("a", "b")):
            for axes in (0, 2):
                s = shape.shape_for(n_items=n_items, product_types=types, n_axes=axes)
                assert all(v.reason in shape.REASONS for v in s.verdicts)
                assert all(v.name in shape.SLOTS for v in s.verdicts)


# --- tipurile -----------------------------------------------------------------------------------


def test_missing_type_is_not_a_class():
    """Un sfert din catalog n-are `product_type` derivat. Tratate împreună, ar apărea drept «încă
    un fel de produs» tocmai când nu știm ce sunt — același motiv ca la cota din NX-298."""
    types = shape.distinct_types([_p("a", "crema"), _p("b", None), _p("c", None)])
    assert types == ("crema",)


def test_types_keep_first_appearance_order():
    """Ordinea de pe ecran, nu cea alfabetică: o încadrare care le numește altfel decât le vede
    clientul contrazice pagina."""
    types = shape.distinct_types([_p("a", "ser"), _p("b", "crema"), _p("c", "ser")])
    assert types == ("ser", "crema")


def test_types_read_from_attributes_too():
    types = shape.distinct_types([{"id": "a", "attributes": {"product_type": "plasturi"}}])
    assert types == ("plasturi",)


# --- textul de rezervă ---------------------------------------------------------------------------


def test_framing_text_names_the_classes_that_were_served():
    text = shape.framing_text(_PACK, "ro", ("plasturi", "ser", "crema"))
    assert text == "Ți-am ales plasturi, ser și crema."


def test_framing_text_is_silent_on_a_single_class():
    assert shape.framing_text(_PACK, "ro", ("crema",)) is None


def test_framing_text_follows_the_locale():
    assert shape.framing_text(_PACK, "en", ("patches", "serum")) == (
        "I picked out patches and serum for you."
    )


def test_framing_text_fails_open_without_a_template():
    """Fail-OPEN, spre deosebire de poarta per chip: un pachet incomplet nu are voie să ȘTEARGĂ
    o frază pe care modelul a scris-o corect, doar să nu poată oferi una de rezervă."""
    assert shape.framing_text(SimpleNamespace(), "ro", ("a", "b")) is None
    assert shape.framing_text(_PACK, "hu", ("a", "b")) is None


def test_framing_text_survives_a_misconfigured_template():
    pack = SimpleNamespace(
        answer_shape_templates={
            "framing": {"ro": "Ți-am ales {altceva}."},
            "list_glue": {"ro": " și "},
        }
    )
    assert shape.framing_text(pack, "ro", ("a", "b")) is None


def test_framing_copy_has_no_forbidden_punctuation():
    """P13: textul pleacă spre client, deci fără liniuță de pauză și fără punct și virgulă.
    Verificat pe pachetele REALE, nu pe fixture: acolo trăiește copy-ul."""
    import glob
    import json

    for path in glob.glob("src/domain/defaults/*.json"):
        with open(path, encoding="utf-8") as fh:
            table = json.load(fh).get("answer_shape_templates") or {}
        for per_locale in table.values():
            for text in per_locale.values():
                assert ";" not in text, path
                assert " - " not in text and " — " not in text and " – " not in text, path


# --- scrub pe propoziție --------------------------------------------------------------------------


def test_intro_keeps_the_clean_sentence_next_to_the_superlative():
    """Fraza exactă a turului `42744330`. Înainte, clientul primea zero text."""
    t = (
        "Pentru coșuri, cele mai potrivite sunt produsele ZEROID pentru ten gras și "
        "imperfecțiuni. Gelul curăță sebumul și porii, iar tonerul adaugă exfoliere blândă."
    )
    out = compose.scrub_intro(t, set())
    assert out == "Gelul curăță sebumul și porii, iar tonerul adaugă exfoliere blândă."


def test_medical_claim_still_kills_the_whole_paragraph():
    """Granularitatea e o relaxare, iar o relaxare n-are voie să atingă o protecție P0: un claim
    terapeutic împărțit în două propoziții ar trece pe bucăți dacă am judeca doar bucăți."""
    assert compose.scrub_intro("Curata porii. Trateaza acneea severa.", set()) is None


def test_numbered_list_is_not_renumbered_by_the_splitter():
    """Defectul găsit de suită la trecerea pe propoziții: «1. Curatare, 2. tonifiere, 4. ceva»
    devenea «1. Curatare, 2. ceva». Nu trunchiat, RENUMEROTAT — un text care arată valid și spune
    altceva. Cu ordinalul exceptat, rutina e o singură propoziție, deci cade sau trece întreagă."""
    assert compose.scrub_intro("1. Curatare, 2. tonifiere, 4. ceva inventat", set()) is None
    ok = compose.scrub_intro("1. Curatare, 2. tonifiere", {"1", "2"})
    assert ok == "1. Curatare, 2. tonifiere"
