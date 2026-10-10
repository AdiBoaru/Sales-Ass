"""NX-408 — agentul explică folosirea ca un specialist (promptul `assistant.v7`).

Comparația cu iZi din 2026-10-10 (setul `explain-2026-10-10`): la «Cum se folosește?» agentul
răspundea pentru UN produs ales singur («dacă te referi la…»), în două propoziții, iar la «Cum o
combin cu SPF?» sărea peste pașii spuși deja. Promptul avea trei frâne: tot ce nu e pe fișă era
„nu pot confirma”, know-how-ul general era permis pe o listă de trei exemple, iar regula
anti-repetiție tăia chiar secvența cerută. Comportamentul se judecă pe set, cu modelul real; aici
se fixează regulile și forma lor generică (P11).
"""

from __future__ import annotations

import re

from src.assistant import prompt as aprompt


def _text() -> str:
    return aprompt.instructions(
        store="X", locale="ro", families=("fata",), max_shown=6, chip_count=5
    )


def test_prompt_is_v7_or_later():
    assert aprompt.PROMPT_VERSION >= "assistant.v7"


def test_the_how_to_shape_asks_for_steps_with_technique():
    text = _text()
    for anchor in (
        "explain it like a specialist",
        "says why the way or the order matters",
        "each under a short bold heading",
        "what to do, how much, where, how",
        "how long to let it settle",
        "morning and evening as separate steps",
        "what to do if the",
        "end with one practical tip",
    ):
        assert anchor in text, anchor


def test_general_know_how_is_allowed_but_product_facts_stay_on_the_sheet():
    text = _text()
    assert "use your own\n  knowledge, as long as it does not contradict the sheet" in text
    assert "comes only from its\n  sheet" in text
    # cifrele din know-how ar pica la poarta de adevăr (NX-403): se spun în cuvinte
    assert "never write a\n  figure" in text
    # „nu pot confirma” doar pentru un fapt al produsului cerut explicit, nu pentru orice
    assert "If the sheet does not say, say you cannot\n  confirm." not in text
    assert "do not know it for this product" in text


def test_a_vague_reference_is_answered_for_the_kind_of_product():
    text = _text()
    assert "When a question does not say which product it is about" in text
    assert "answer for that kind of product, naming them" in text
    assert "ask which one only when that is not clear" in text


def test_combining_redoes_the_whole_sequence():
    text = _text()
    assert "Do not repeat what an earlier reply already said" in text
    assert "the whole sequence is the\n  answer, so give every step again" in text


def test_the_instructions_stay_generic_and_fully_substituted():
    text = _text()
    for word in ("ten", "piele", "crema", "skin", "hair", "cream"):
        assert not re.search(rf"\b{word}\b", text), word
    assert "{" not in text.replace("{}", ""), "niciun marcator nesubstituit"
    # forma cerută nu poartă ea însăși o cifră pe care poarta ar respinge-o
    shape = text[text.index("How to use, how to apply") : text.index("A quick question")]
    assert not re.search(r"\d", shape)
