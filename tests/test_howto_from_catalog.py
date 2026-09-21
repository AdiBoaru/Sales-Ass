"""NX-307 — «cum se folosește» se răspunde din catalog, nu din memoria modelului.

Pe conversația reală `70da107c` (`sole-ro`), turele 2 și 3 au rulat cu ZERO apeluri de tool
(`retrieval_ids: []`), iar modelul a compus șapte pași de aplicare din ce știa el. În catalogul
aceluiași tenant, `product_sections.usage` există pe **2.746 din 2.758** de produse, iar pe produsul
discutat spune chiar fraza care lipsea din răspuns: „folosind mainile sau o manusa speciala".

Cauza era un kind declarat fără PRODUCĂTOR: `explain` e în `ObligationKind` de la început și are doi
consumatori (`answer_plan`, `chip_moves.roles_for`), dar nimic nu-l emitea, deci un „cum folosesc"
ieșea ca `answer` generic.
"""

from __future__ import annotations

from src.agent.brain_models import extract_obligations
from src.agent.turn_profile import (
    PER_PROFILE_FLAGS,
    PROFILES,
    enabled_names,
    gate,
    select,
)
from src.runtime.turn_budget import turn_class_for


def _kinds(text: str) -> set[str]:
    return {o.kind for o in extract_obligations(text)}


def _profile_for(text: str) -> str:
    obligations = extract_obligations(text)
    return select(turn_class_for(obligations), obligations, has_routine=True).name


# ── producătorul ────────────────────────────────────────────────────────────────────────────────


def test_the_real_message_produces_an_explain_obligation():
    """Mesajul EXACT al turului 2. Pe codul vechi ieșea `answer`, adică un tur ca oricare altul."""
    assert "explain" in _kinds("pai si cum folosesc ?")


def test_usage_phrasings_are_recognised():
    for text in (
        "cum se foloseste?",
        "cum il aplic",
        "cum se aplica pe fata",
        "cum se utilizeaza",
        "mod de folosire",
        "instructiuni de utilizare",
        "how do i use it",
        "how to apply this",
    ):
        assert "explain" in _kinds(text), text


def test_cart_intent_is_not_mistaken_for_a_usage_question():
    """„cum pun in cos" e o ACȚIUNE. De aceea verbul „pun" cere pronume în regex, spre deosebire de
    celelalte: fără condiția asta, un tur de comerț ar fi plecat cu sufixul de instrucțiuni."""
    kinds = _kinds("cum pun in cos produsul")
    assert "explain" not in kinds


def test_a_usage_question_with_a_pronoun_still_counts():
    assert "explain" in _kinds("cum il pun pe fata")


def test_a_routine_request_stays_a_routine():
    """«ce rutină folosesc» conține și declanșatorul de instrucțiuni. Secvența e forma mai
    specifică, deci `routine` se testează prima."""
    assert "routine" in _kinds("ce rutina folosesc dimineata")
    assert _profile_for("ce rutina folosesc dimineata") == "routine"


def test_plain_questions_do_not_become_explain():
    for text in ("cat costa?", "ce creme ai", "aveti stoc?", "vreau un autobronzant"):
        assert "explain" not in _kinds(text), text


# ── profilul ────────────────────────────────────────────────────────────────────────────────────


def test_the_real_message_selects_the_howto_profile():
    assert _profile_for("pai si cum folosesc ?") == "howto"


def test_the_howto_suffix_sends_the_model_to_the_tool():
    suffix = PROFILES["howto"].suffix
    assert "get_product_details" in suffix
    # Regula care face diferența: instrucțiunile sunt ale MAGAZINULUI, iar lipsa lor se spune.
    assert "magazinului" in suffix
    assert "get_product_details" in PROFILES["howto"].extra_tools


def test_a_turn_that_also_asks_for_a_recommendation_stays_on_recommend():
    """Urmează precedentul lui `exact`: un tur care cere și o recomandare n-are ce căuta pe un sufix
    care îl trimite să răspundă dintr-o singură fișă de produs."""
    text = "cum se foloseste si vreau si o crema de fata"
    assert {"explain", "recommend"} <= _kinds(text)
    assert _profile_for(text) == "recommend"


# ── poarta de flag-uri ──────────────────────────────────────────────────────────────────────────


class _Settings:
    def __init__(self, **kw: bool) -> None:
        self.routine_enabled = kw.get("routine_enabled", False)
        self.howto_from_catalog_enabled = kw.get("howto_from_catalog_enabled", False)


def test_every_per_profile_flag_names_a_real_profile():
    """Un flag care numește un profil inexistent n-ar aprinde nimic și n-ar da nicio eroare, adică
    exact „flagul legal și inert" pe care NX-304 l-a găsit costisitor."""
    assert set(PER_PROFILE_FLAGS) <= set(PROFILES)


def test_howto_is_off_by_default():
    assert enabled_names(_Settings()) == frozenset()
    assert gate(PROFILES["howto"], all_on=False, enabled_names=frozenset()) is None


def test_its_own_flag_lights_only_that_profile():
    on = enabled_names(_Settings(howto_from_catalog_enabled=True))
    assert on == {"howto"}
    assert gate(PROFILES["howto"], all_on=False, enabled_names=on) is PROFILES["howto"]
    assert gate(PROFILES["routine"], all_on=False, enabled_names=on) is None


def test_the_global_flag_still_lights_everything():
    for profile in PROFILES.values():
        assert gate(profile, all_on=True, enabled_names=frozenset()) is profile
