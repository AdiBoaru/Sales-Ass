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


# --- wiring: rezerva în `assemble` + raportul runner-ului ------------------------------------
# Găurile astea au fost descoperite la auditul de după livrare: ambele CĂI funcționau, dar niciun
# test nu le atingea. Un contract de formă fără teste pe wiring e exact defectul pe care îl repară
# cardul, mutat cu un nivel mai sus.


def _pack():
    return SimpleNamespace(
        answer_shape_templates={
            "framing": {"ro": "Ți-am ales {types}."},
            "list_glue": {"ro": " și "},
        },
        comparison_facets=(),
        badge_rules=None,
    )


def _ctx():
    from src.models import BusinessConfig, Contact, InboundMessage, TurnContext

    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="s", name="S"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="vreau ceva de acnee"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    ctx.business.domain_pack = _pack()
    return ctx


_RETRIEVED = [
    {
        "id": "p1",
        "name": "Plasturi A",
        "price": 60.0,
        "availability": "in_stock",
        "url": "u1",
        "attributes": {"product_type": "plasturi"},
    },
    {
        "id": "p2",
        "name": "Crema B",
        "price": 90.0,
        "availability": "in_stock",
        "url": "u2",
        "attributes": {"product_type": "crema de fata"},
    },
]
_J = {
    "items": [
        {"product_id": "p1", "fit_clause": "bun"},
        {"product_id": "p2", "fit_clause": "bun"},
    ],
    "pick": None,
    "education": None,
    "suggestions": [],
}


def test_assemble_falls_back_to_server_framing_when_the_intro_dies():
    """Cazul turului `42744330`, dar pe calea completă: superlativul omoară intro-ul, iar clientul
    primește totuși o încadrare, construită din clasele REAL servite."""
    from src.worker import compose

    j = {**_J, "intro": "Acestea sunt cele mai potrivite produse."}
    rich = compose.assemble(_ctx(), j, _RETRIEVED)
    assert rich.intro == "Ți-am ales plasturi și crema de fata."


def test_assemble_never_overwrites_a_good_model_intro():
    """Rezerva e pentru slotul GOL, nu o înlocuire. Altfel proza contextuală a modelului ar fi
    schimbată cu un șablon la fiecare tur."""
    from src.worker import compose

    j = {**_J, "intro": "Gelul curata sebumul, iar crema hidrateaza."}
    rich = compose.assemble(_ctx(), j, _RETRIEVED)
    assert rich.intro == "Gelul curata sebumul, iar crema hidrateaza."


def test_assemble_framing_fallback_has_a_kill_switch(monkeypatch):
    from src.config import get_settings
    from src.worker import compose

    monkeypatch.setattr(get_settings(), "answer_shape_enabled", False)
    j = {**_J, "intro": "Acestea sunt cele mai potrivite produse."}
    assert compose.assemble(_ctx(), j, _RETRIEVED).intro is None  # byte-identic cu înainte


def test_runner_report_names_the_slot_that_did_not_come_out():
    """Raportul e singura cale prin care un tur „bogat dar gol" se deosebește de unul bun.
    `response_shape` (NX-159) ar raporta AMBELE ca succes: are produse, are chips, e rich."""
    from src.agent import response_quality
    from src.worker import compose

    ctx = _ctx()
    rich = compose.assemble(ctx, {**_J, "intro": "Gelul curata, crema hidrateaza."}, _RETRIEVED)
    ctx.retrieval = SimpleNamespace(products=_RETRIEVED, relevance=None)
    ctx.reply = SimpleNamespace(rich=rich, text="", products=None, comparison=None)

    report = response_quality.answer_shape_report(ctx)
    assert report is not None
    assert report["n_types"] == 2
    assert shape.SLOT_FRAMING in report["required"]
    # `education` e gol în `_J`, deci încheierea lipsește — și raportul spune DE CE se cerea.
    assert report["missing"] == [shape.SLOT_CLOSING]
    assert report["reasons"][shape.SLOT_CLOSING] in shape.REASONS
    assert report["complete"] is False


def test_runner_report_is_silent_without_cards():
    """Un tur fără carduri n-are formă de judecat, deci nu produce event (cardinalitate)."""
    from src.agent import response_quality

    ctx = _ctx()
    ctx.reply = SimpleNamespace(rich=None, text="nu am gasit", products=None, comparison=None)
    assert response_quality.answer_shape_report(ctx) is None
