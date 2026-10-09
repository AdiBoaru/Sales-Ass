"""NX-388 — familia rutinei se citește din ce a spus clientul («rutină pentru ten gras»).

Turele reale (`sole-ro`, 2026-10-01…07, `conversation_traces`): «fa-mi o rutina de seara pt ten gras
cu porii dilatati, maxim 200 lei tot» (`26e68379`, `afb75bbf`, `8eb2dadd`) și «fa-mi o rutina de
dimineata pentru ten sensibil cu roseata» (`a65edfa8`). Interpretarea scria doar `skin_type` din
citatul „ten gras”, niciun raft, deci `routine_family` n-avea familie, iar planul `bundle` devenea
o căutare (2 produse, respectiv 4 SPF). Stratul e interpretarea (validatorul n-a respins nimic):
regula promptului citea „ten gras” ca stare a clientului, deci raftul se pierdea. Reparația e o
regulă a promptului (`interpret.v6`); aici, lanțul pur pe interpretarea pe care o cere regula. Zero
model, zero DB."""

from __future__ import annotations

from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary, VocabEntry
from src.conversation.interpretation import TurnInterpretation
from src.conversation.interpretation_check import INTERPRET_PROMPT_VERSION
from src.conversation.state_v2 import ConversationStateV2
from src.conversation.turn_interpreter import _INSTRUCTIONS
from tests.kernel import fixture_sole_catalog as sole
from tests.kernel.fixture_catalog import kernel_step

MESSAGE = "fa-mi o rutina de seara pt ten gras cu porii dilatati, maxim 200 lei tot"


def _vocab() -> CatalogVocabulary:
    """Vocabularul catalogului SOLE sintetic, cu rafturile pe CĂI, ca în producție: rădăcina `ten`
    (cheia din `family_by_shelf`) și subraftul ei."""
    base = sole.vocabulary()
    shelves = (
        VocabEntry(key="ten", label="Ten", count=900, path="ten"),
        VocabEntry(
            key="ten-ingrijirea-tenului",
            label="Ingrijirea tenului",
            count=600,
            path="ten/ten-ingrijirea-tenului",
        ),
        VocabEntry(key="par", label="Par", count=300, path="par"),
        VocabEntry(key="corp", label="Corp", count=200, path="corp"),
        VocabEntry(key="machiaj", label="Machiaj", count=500, path="machiaj"),
        VocabEntry(key="machiaj-fata", label="Fata", count=200, path="machiaj/machiaj-fata"),
    )
    return CatalogVocabulary(
        business_id=base.business_id, dimensions={**base.dimensions, CATEGORY_DIMENSION: shelves}
    )


def _change(dimension, value, quote, relation="eq", **extra):
    return {
        "op": "set",
        "dimension": dimension,
        "value": value,
        "quote": quote,
        "relation": relation,
        "number": extra.get("number"),
        "unit": extra.get("unit"),
        "target": None,
        "relative_to": None,
    }


def _interp(*changes, act="bundle") -> TurnInterpretation:
    return TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": act, "targets": [], "query": None}],
            "changes": list(changes),
            "references": [],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )


def _step(interp: TurnInterpretation, message: str = MESSAGE):
    return kernel_step("sole-ro", ConversationStateV2(revision=1), interp, message, vocab=_vocab())


def _primary(step):
    return step.planned.plans[step.planned.primary]


# --- lanțul, pe interpretarea cerută de regulă --------------------------------------------------


def test_a_routine_for_oily_skin_names_the_face_shelf_and_gets_the_face_family():
    """Interpretarea `interpret.v6`: raftul `ten` lângă tipul de ten ⇒ `bundle` pe familia `fata`,
    cu bugetul spus în tur păstrat."""
    step = _step(
        _interp(
            _change("routine_time", "pm", "de seara"),
            _change("category", "ten", "ten"),
            _change("skin_type", "oily", "ten gras"),
            _change("concerns", "pores", "porii dilatati"),
            _change("price", None, "maxim 200 lei tot", "lte", number=200.0, unit="lei"),
        )
    )
    rejected = [c for c in step.checked if c.rejected is not None]
    assert rejected == []
    assert step.state_after.topic.category_key == "ten"
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "fata")
    assert plan.search_args is not None and plan.search_args.price_max == 200.0


def test_the_same_turn_without_the_shelf_is_todays_search():
    """Interpretarea de pe `interpret.v5` (traceul real), pe o nevoie fără familie în date
    (`skin_type:oily`: față 73 / machiaj 153 / păr 35): fără raft, planul e căutarea de azi. Testul
    fixează că reparația e a interpretării, nu o derivare din citat în kernel."""
    step = _step(
        _interp(
            _change("routine_time", "pm", "de seara"), _change("skin_type", "oily", "ten gras")
        ),
        "fa-mi o rutina de seara pt ten gras",
    )
    assert step.state_after.topic.category_key is None
    assert _primary(step).executor == "search"


def test_pores_already_give_the_face_family_through_the_need_map():
    """Constatare NX-388: harta `family_by_need` aplicată pe 2026-10-09 are `concerns:pores` →
    `fata`, deci turul real cu «porii dilatati» are familia chiar fără raft (traceurile sunt de
    dinaintea hărții)."""
    step = _step(
        _interp(
            _change("skin_type", "oily", "ten gras"), _change("concerns", "pores", "porii dilatati")
        )
    )
    assert (_primary(step).executor, _primary(step).family) == ("bundle", "fata")


def test_a_routine_for_oily_hair_gets_the_hair_family():
    step = _step(
        _interp(_change("category", "par", "par"), _change("skin_type", "oily", "par gras")),
        "vreau o rutina pentru par gras",
    )
    plan = _primary(step)
    assert (plan.executor, plan.family) == ("bundle", "par")


def test_a_find_for_oily_skin_stays_a_find():
    """«sotul meu are tenul gras ... vrea UN singur produs»: nicio rutină, chiar cu raftul setat."""
    step = _step(
        _interp(
            _change("category", "ten", "tenul"),
            _change("skin_type", "oily", "tenul gras"),
            act="find",
        ),
        "sotul meu are tenul gras si nu are rabdare de rutine, vrea UN singur produs",
    )
    assert _primary(step).executor == "search"


# --- sonda: stratul vinovat ----------------------------------------------------------------------


def _trace(changes, *, family=None, topic=None, rejected=()):
    checked = [
        {"change": c, "rejected": "semantic_mismatch" if c["dimension"] in rejected else None}
        for c in changes
    ]
    return {
        "interpretation": {"acts": [{"kind": "bundle"}], "changes": changes},
        "checked_changes": checked,
        "plans": [{"executor": "bundle" if family else "search", "family": family}],
        "state_after": {"topic": topic},
    }


def test_the_probe_names_the_layer_that_lost_the_family():
    from scripts.nx388_routine_family_probe import classify

    oily = _change("skin_type", "oily", "ten gras")
    shelf = _change("category", "ten", "ten")
    assert classify(_trace([oily], family="fata"))["layer"] == "family"
    assert classify(_trace([oily]))["layer"] == "interpretation"
    assert classify(_trace([shelf, oily], rejected=("category",)))["layer"] == "validator"
    assert classify(_trace([shelf, oily]))["layer"] == "reducer"
    assert classify(_trace([shelf, oily], topic="ten"))["layer"] == "planner"


# --- promptul ------------------------------------------------------------------------------------


def test_the_prompt_version_moved():
    assert int(INTERPRET_PROMPT_VERSION.removeprefix("interpret.v").split(".")[0]) >= 6


def test_the_prompt_says_the_part_a_routine_is_for_is_a_shelf():
    """Regula e GENERICĂ (engleză, fără exemple din domeniu, P11/I14): în cererea unui set, partea
    despre care clientul își descrie starea e raftul ei."""
    text = " ".join(_INSTRUCTIONS.split())
    assert "the part the customer's condition is about" in text
    for word in ("ten", "piele", "gras", "skin"):
        assert f" {word} " not in f" {text.lower()} "
