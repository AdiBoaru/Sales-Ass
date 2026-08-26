"""NX-262 — `relation_kinds` intră în DomainPack: default-uri per vertical + override per tenant.

Pur (fără DB/LLM). Ce apără, în ordine:

  1. **Default-ul e comportamentul de azi.** Un vertical fără config de relații nu înlănțuie nimic.
  2. **Specul de beauty e cel MĂSURAT**, nu unul rotunjit de mână. Dacă cineva îl schimbă fără să
     re-ruleze proba, testul cade — exact ce vrei de la o valoare derivată din date.
  3. **Un tenant își poate rescrie semantica** fără deploy (P9), iar o intrare invalidă din override
     nu poate promova tăcut un tip la traversare.
  4. **Flagul e o poartă SEPARATĂ de declarație.** Un pack care declară `chain/4` nu aprinde nimic.
"""

from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.domain.relation_kinds import RelationPurpose, TraversalMode
from src.models import BusinessConfig


def _biz(vertical="beauty_salon", settings=None):
    return BusinessConfig(id="b", slug="s", name="n", vertical=vertical, settings=settings or {})


# --- 1. default = comportamentul de azi ---------------------------------------------------------


def test_vertical_without_relation_config_chains_nothing():
    """`ecommerce` n-are `relation_kinds` în defaults ⇒ registru gol ⇒ vecini direcți peste tot."""
    pack = load_domain_pack(_biz("ecommerce"))
    assert pack is not None
    assert pack.relation_kinds.traversable() == ()
    assert pack.relation_kinds.get("routine_next").max_depth == 1


def test_unknown_kind_never_becomes_traversable():
    """Chiar și pe un vertical CU config, un tip nedeclarat rămâne vecini-direcți."""
    pack = load_domain_pack(_biz("beauty_salon"))
    assert pack.relation_kinds.get("compatible_with").traversable is False


# --- 2. specul de beauty e cel măsurat ----------------------------------------------------------


def test_beauty_defaults_match_the_measured_graph():
    """Valorile vin din `scripts/relations_graph_probe.py` pe catalogul real. Le schimbi doar
    împreună cu o măsurătoare nouă, altfel configul se rupe de datele pe care le descrie."""
    pack = load_domain_pack(_biz("beauty_salon"))
    kinds = pack.relation_kinds

    routine = kinds.get("routine_next")  # aciclic, adâncime reală 4
    assert (routine.mode, routine.max_depth, routine.ordered) == (TraversalMode.CHAIN, 4, True)

    sub = kinds.get("substitute")  # 38 ancore ciclice ⇒ mărginit
    assert (sub.mode, sub.max_depth) == (TraversalMode.BOUNDED, 2)

    # simetric prin natura relației (toate cele 300 de ancore ciclice) ⇒ nu se înlănțuie
    assert kinds.get("complement").traversable is False
    assert kinds.get("accessory").traversable is False


def test_beauty_labels_are_locale_aware_not_hardcoded_romanian():
    """P11/D3: eticheta e text către client, deci are locale. Pilotul e `ro`, nucleul nu e."""
    routine = load_domain_pack(_biz("beauty_salon")).relation_kinds.get("routine_next")
    assert routine.label("ro-RO") == "Pasi recomandati"
    assert routine.label("en") == "Recommended steps"
    assert routine.label("hu") is not None


def test_json_note_keys_do_not_break_loading():
    """Fișierele de defaults poartă chei `_*_note` documentare. Nu sunt tipuri de muchie."""
    kinds = load_domain_pack(_biz("beauty_salon")).relation_kinds
    assert all(not s.kind.startswith("_") for s in kinds.traversable())


# --- 3. override per tenant, fără deploy --------------------------------------------------------


def test_tenant_can_redefine_semantics_without_deploy():
    """Un tenant de electrocasnice pe verticalul generic își declară propriile muchii (P9)."""
    pack = load_domain_pack(
        _biz(
            "ecommerce",
            settings={
                "domain_pack": {
                    "relation_kinds": {
                        "requires": {
                            "mode": "chain",
                            "max_depth": 3,
                            "ordered": True,
                            "purpose": "requirement",
                            "labels": {"ro": "Necesare la instalare"},
                        },
                        "compatible_with": {"mode": "neighbors"},
                    }
                }
            },
        )
    )
    req = pack.relation_kinds.get("requires")
    assert req.mode is TraversalMode.CHAIN and req.max_depth == 3
    assert req.purpose is RelationPurpose.REQUIREMENT
    assert req.label("ro") == "Necesare la instalare"
    # capcana non-tranzitivității: declarată explicit ca vecini-direcți
    assert pack.relation_kinds.get("compatible_with").traversable is False


def test_tenant_can_downgrade_a_default_but_a_bad_override_cannot_promote():
    """Un override poate COBORÎ un tip la vecini-direcți. Un override INVALID nu poate urca nimic:
    intrarea e respinsă și tipul rămâne inert, nu moștenește tăcut default-ul de vertical."""
    pack = load_domain_pack(
        _biz(
            "beauty_salon",
            settings={
                "domain_pack": {
                    "relation_kinds": {
                        "routine_next": {"mode": "neighbors"},
                        "complement": {"mode": "chain", "max_depth": 99},  # peste plafon
                    }
                }
            },
        )
    )
    assert pack.relation_kinds.get("routine_next").traversable is False
    assert pack.relation_kinds.get("complement").traversable is False


# --- 4. flagul e o poartă separată de declarație -------------------------------------------------


def test_traversal_flag_is_dark_by_default():
    """Declararea semanticii NU e activarea ei: adâncimea e decizie de produs (buget de tur,
    calitatea datelor), nu de configurare a vocabularului."""
    assert get_settings().relation_traversal_enabled is False
