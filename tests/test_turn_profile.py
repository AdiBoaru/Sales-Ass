"""NX-275 felia 4 — profile de tur: direcția răspunsului, aleasă de COD.

Testăm trei lucruri, în ordinea în care contează: că selecția e deterministă și că necunoscutul
URCĂ (o greșeală în direcția asta costă tokeni, cea inversă costă răspunsul); că un profil ADAUGĂ
și niciodată nu scade din nucleu; și că registrul e valid la IMPORT, nu la primul tur.

ZERO OpenAI, ZERO DB: modulul e pur.
"""

from __future__ import annotations

import pytest

from src.agent import turn_profile
from src.agent.brain_models import DetectedObligation
from src.agent.turn_profile import PROFILES, TurnProfile, select
from src.agent.voice import naturalize
from src.runtime.turn_budget import TurnClass
from src.tools import (  # noqa: F401 — importul populează TOOL_REGISTRY prin decoratori
    catalog_tools,
    commerce_tools,
    faq_tools,
    orders_tools,
)
from src.tools.base import TOOL_REGISTRY, enabled_tools


def _o(kind: str) -> DetectedObligation:
    return DetectedObligation(kind, f"{kind}_0", "question")


@pytest.mark.parametrize(
    ("turn_class", "kinds", "expected"),
    [
        (TurnClass.EXACT, ["answer"], "exact"),
        (TurnClass.RECOMMENDATION, ["recommend"], "recommend"),
        (TurnClass.COMPLEX, ["compare"], "compare"),
        (TurnClass.MUTATION, ["action"], "mutation"),
        # Necunoscutul urcă: fără obligații nu știm ce cere turul, deci primim tratamentul bogat.
        (TurnClass.RECOMMENDATION, [], "recommend"),
        # Clasa singură nu ajunge: un tur ieftin care CERE o recomandare n-are ce căuta pe sufixul
        # care interzice recomandările.
        (TurnClass.EXACT, ["recommend"], "recommend"),
        # Mixt: comparația bate recomandarea, fiindcă e forma mai specifică.
        (TurnClass.COMPLEX, ["compare", "recommend"], "compare"),
        # Acțiunea bate tot: o confirmare greșită de coș e mai gravă decât o recomandare ratată.
        (TurnClass.COMPLEX, ["action", "compare"], "mutation"),
        # Safety însoțește un fapt fără să-i schimbe forma.
        (TurnClass.EXACT, ["answer", "safety"], "exact"),
    ],
)
def test_selectia_e_determinista_si_necunoscutul_urca(turn_class, kinds, expected):
    assert select(turn_class, [_o(k) for k in kinds]).name == expected


def test_selectia_e_pura():
    """Același input, același profil — de zece ori. Un profil care ar depinde de ceas sau de stare
    ar face ca aceeași conversație să primească alt răspuns la reluare (reclaim)."""
    obligations = [_o("recommend")]
    assert len({select(TurnClass.RECOMMENDATION, obligations).name for _ in range(10)}) == 1


def test_un_profil_adauga_dar_nu_scade_niciodata():
    """Principiul 4 din design, ca test.

    Un „adaugă în coș" pe care regexul nu-l prinde trebuie să aibă unealta la îndemână oricum.
    Dacă un profil ar putea SCĂDEA din nucleu, exact turul prost clasificat ar rămâne fără unealta
    de care avea nevoie — iar simptomul ar fi un răspuns plauzibil, nu o eroare."""
    core = set(enabled_tools(None, "sales")) | set(enabled_tools(None, "order"))
    for profile in PROFILES.values():
        # Contractul nu ARE noțiunea de excludere: singurul câmp e `extra_tools`. Un profil nu
        # poate scădea nici dacă cineva ar vrea.
        assert not hasattr(profile, "excluded_tools")
        for tool in profile.extra_tools:
            assert tool in TOOL_REGISTRY, f"{profile.name} cere un tool inexistent: {tool}"

    # Reuniunea a tot ce adaugă profilele e MICĂ și declarată: dacă cineva adaugă un tool nou aici,
    # testul cere să fie o decizie, nu o scăpare. `compare_products` e deja în nucleu (deci
    # adăugarea lui e un no-op de dedupe), `related_products` vine cu felia 5.
    extras = {t for p in PROFILES.values() for t in p.extra_tools}
    assert extras <= {"compare_products", "related_products"}
    assert extras & core == extras - {"related_products"}


def test_fiecare_tool_extra_are_si_buget_declarat():
    """Un tool fără spec în `tool_budget` ar rula fără clasificare (citire vs mutație), deci ar
    putea ajunge în `gather`-ul de citiri paralele chiar dacă scrie. NX-241 cere declarația."""
    from src.agent.tool_budget import spec_for

    for profile in PROFILES.values():
        for tool in profile.extra_tools:
            assert spec_for(tool).name == tool


def test_sufixele_respecta_vocea_pe_care_o_cer():
    """P13, aplicat promptului însuși.

    Un exemplu cu liniuță de pauză într-un prompt îl învață pe model exact punctuația pe care i-o
    interzicem în altă parte. Poarta rulează deja la import; testul o face vizibilă."""
    for profile in PROFILES.values():
        assert naturalize(profile.suffix) == profile.suffix
        assert profile.suffix.strip()


def test_registrul_stricat_opreste_procesul_la_import():
    """Fail-fast: un registru invalid nu are voie să treacă de import.

    Verificăm poarta însăși, nu doar starea curentă a registrului — altfel testul ar trece și dacă
    cineva ar șterge validarea."""
    original = dict(PROFILES)
    try:
        PROFILES["rau"] = TurnProfile(name="rau", extra_tools=(), suffix="text cu ; punct virgula")
        with pytest.raises(ValueError, match="vocea"):
            turn_profile._validate_registry()
    finally:
        PROFILES.clear()
        PROFILES.update(original)
    turn_profile._validate_registry()  # registrul real rămâne valid


def test_versiunea_profilului_intra_in_amprenta():
    """Fără ea, două ture cu prompturi diferite ar arăta identic în trace, iar `prompt_hash` ar
    diferi fără să spună de ce."""
    from src.agent.brain import brain_versions

    v = brain_versions("system", [], "gpt-5.6-luna", "recommend")
    assert v["turn_profile"] == "recommend"
    assert brain_versions("system", [], "gpt-5.6-luna")["turn_profile"] is None


# --- NX-275 felia 5: obligația `routine` + tool-ul de graf -------------------


@pytest.mark.parametrize(
    ("text", "is_routine"),
    [
        ("ce rutina de seara imi recomanzi?", True),
        ("imi dai pasii pentru ingrijirea tenului?", True),  # formă articulată
        ("ce folosesc intai, serul sau crema?", True),
        ("dimineata ce pun si seara ce pun?", True),  # se întinde peste DOUĂ clauze
        ("pas cu pas te rog", True),
        # Capcana lexicală: `pasi\w*` lacom ar fi transformat „pasiune" într-o cerere de secvență.
        ("am pasiune pentru parfumuri", False),
        ("vreau o crema pentru ten uscat", False),
        ("cat costa Hidra Boost?", False),
        ("care e mai bun, X sau Y?", False),
    ],
)
def test_detectia_secventei_prinde_forma_nu_vocabularul(text, is_routine):
    from src.agent.brain_models import extract_obligations

    kinds = [o.kind for o in extract_obligations(text)]
    assert ("routine" in kinds) is is_routine


def test_cuvintele_de_secventa_nu_sunt_vocabular_de_domeniu():
    """Poarta NX-264, aplicată intenționat: „rutină", „pași", „dimineața/seara" descriu o FORMĂ de
    cerere, nu un vertical. La un magazin de electrocasnice aceleași cuvinte cer pașii de
    instalare. Dacă vreunul ar ajunge în lista de termeni de domeniu, regexul ar fi cuplat la
    cosmetice și testul trebuie să spună asta."""
    import json
    import pathlib

    raw = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "tests" / "domain_terms.json").read_text(
            encoding="utf-8"
        )
    )
    terms: set[str] = set()
    for key in ("declared", "catalog"):
        value = raw.get(key) or []
        if isinstance(value, dict):
            for v in value.values():
                terms |= set(v if isinstance(v, list) else [v])
        else:
            terms |= set(value)
    assert not ({"rutina", "pasi", "dimineata", "seara", "routine", "steps"} & terms)


def test_profilul_routine_cere_secvente_declarate_de_tenant():
    """Un profil care promite o secvență inexistentă e mai rău decât unul absent: sufixul trimite
    modelul la `related_products`, unealta n-are ce tip de muchie să urmeze, iar modelul umple
    golul singur. Fără secvențe declarate se cade pe `recommend`, care răspunde onest."""
    obligations = [_o("routine")]
    assert select(TurnClass.RECOMMENDATION, obligations, has_sequences=True).name == "routine"
    assert select(TurnClass.RECOMMENDATION, obligations, has_sequences=False).name == "recommend"


def test_o_rutina_cu_un_singur_produs_nu_e_rutina():
    """Acoperirea lui `routine` NU e cea a lui `recommend`. Pragul de 2 e chiar definiția: o
    secvență de un pas e o recomandare cu alt nume, iar validatorul trebuie s-o respingă.
    `no_results` rămâne acoperire validă (P6), altfel o ancoră fără muchii ar cădea mereu în
    fallback."""
    from src.agent.answer_plan import _obligation_covered
    from tests.test_answer_plan_v2 import _plan

    one = _plan(selected_products=_plan().selected_products[:1])
    assert _obligation_covered(one, "routine") is False
    assert _obligation_covered(_plan(no_results=None, comparison=None), "recommend") is not None


def test_faq_si_cache_nu_pot_incheia_un_tur_de_rutina():
    """Un hit de FAQ poate răspunde la o întrebare, dar nu poate compune o secvență ancorată în
    graful tenantului. Fără `routine` în lista blocantă, `answerable` ar fi gol, `complete` ar
    ieși True, iar turul s-ar încheia cu un răspuns care SEAMĂNĂ a răspuns."""
    from types import SimpleNamespace

    from src.agent import control_plane

    ctx = SimpleNamespace(
        state=SimpleNamespace(pending_question=None),
        message=SimpleNamespace(body="ce rutina de seara imi recomanzi?"),
        reply=SimpleNamespace(text="raspuns de faq"),
        emit=lambda *a, **k: None,
        events=[],
    )
    assert control_plane.decide(ctx, "faq_stage").complete is False
    # Contra-proba: aceeași cale, o întrebare obișnuită — FAQ-ul ARE voie s-o încheie. Fără ea,
    # testul ar trece și dacă `decide` ar refuza tot, adică fără să demonstreze nimic.
    ctx.message = SimpleNamespace(body="cat costa transportul?")
    assert control_plane.decide(ctx, "faq_stage").complete is True
