"""Registrul de tipuri de relație — teste PURE (fără DB, fără OpenAI), rulate în CI fast.

Ce apără testele astea, în ordinea importanței:

  1. **Tăcerea nu acordă traversare.** Un `kind` care apare în date fără să fie declarat în config
     nu poate produce lanțuri. E singura garanție care ține când seedul, sync-ul sau un import
     inventează o muchie nouă.
  2. **Un mod și adâncimea lui nu pot fi în contradicție.** Obiectul nu există dacă ar fi incoerent
     (validare în `__post_init__`, ca la `vocabulary.Resolution`), deci nu există cale prin care un
     `neighbors` să ajungă traversat în adâncime.
  3. **Plafonul de adâncime nu e negociabil din config** — un tur are un buget monoton (NX-241).
  4. **O intrare invalidă e respinsă individual**, nu doboară restul registrului, fiindcă direcția
     de fail e sigură aici (pierzi o capabilitate, nu lărgești un filtru).
"""

from __future__ import annotations

import pytest

from src.domain.relation_kinds import (
    MAX_RELATION_KINDS,
    MAX_TRAVERSAL_DEPTH,
    RelationKindConfigError,
    RelationKindRegistry,
    RelationKindSpec,
    RelationPurpose,
    TraversalMode,
    load_relation_kinds,
)

# --- 1. tăcerea nu acordă traversare ------------------------------------------------------------


def test_undeclared_kind_is_neighbors_only():
    """Invariantul central: un tip nedeclarat se comportă exact ca azi (vecini direcți)."""
    reg = load_relation_kinds({"routine_next": {"mode": "chain", "max_depth": 4}})
    spec = reg.get("compatible_with")  # nedeclarat
    assert spec.mode is TraversalMode.NEIGHBORS
    assert spec.max_depth == 1
    assert spec.traversable is False


def test_new_kind_in_data_cannot_create_chains():
    """Cineva inserează rânduri cu un `kind` nou. Registrul nu-l cunoaște ⇒ nu se înlănțuie.
    Fără proprietatea asta, o schimbare de SEED ar schimba tăcut comportamentul de recomandare."""
    reg = load_relation_kinds({})
    for invented in ("consumable_for", "fits_model", "requires", "spare_part"):
        assert reg.get(invented).max_depth == 1


def test_garbage_kind_gets_inert_spec_not_crash():
    """Un `kind` care nu trece nici de regexul de identificator (coloană stricată, import prost) tot
    primește un răspuns, dar unul inert. Apelantul nu are ramură de `None` în care să greșească."""
    reg = RelationKindRegistry()
    for bad in ("", "  ", "Routine Next", "kind;drop", "9lives"):
        spec = reg.get(bad)
        assert spec.traversable is False
        assert spec.max_depth == 1


# --- 2. modul și adâncimea nu pot fi în contradicție --------------------------------------------


def test_neighbors_cannot_have_depth_above_one():
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(kind="complement", mode=TraversalMode.NEIGHBORS, max_depth=3)


def test_neighbors_cannot_be_ordered():
    """Un set de vecini n-are ordine: ordinea presupune un drum."""
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(kind="complement", mode=TraversalMode.NEIGHBORS, ordered=True)


@pytest.mark.parametrize("mode", [TraversalMode.CHAIN, TraversalMode.BOUNDED])
def test_traversable_modes_reject_depth_one(mode):
    """Adâncime 1 pe un mod tranzitiv e o contradicție: ar fi `neighbors` cu alt nume, iar codul
    care se uită la `mode` ar lua altă ramură decât cea pe care o descrie configul."""
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(kind="routine_next", mode=mode, max_depth=1)


def test_bool_depth_rejected():
    """`True` e `int` în Python. Fără gardă explicită, `max_depth: true` ar trece ca adâncime 1."""
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(kind="routine_next", mode=TraversalMode.CHAIN, max_depth=True)


# --- 3. plafonul de adâncime nu e negociabil din config -----------------------------------------


def test_depth_above_hard_cap_rejected():
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(
            kind="routine_next", mode=TraversalMode.CHAIN, max_depth=MAX_TRAVERSAL_DEPTH + 1
        )


def test_config_cannot_raise_the_cap():
    """Un config care cere adâncime uriașă nu ridică plafonul: intrarea e respinsă și tipul rămâne
    vecini-direcți. Bugetul de tur (NX-241) nu se poate cheltui prin config."""
    reg = load_relation_kinds({"routine_next": {"mode": "chain", "max_depth": 50}})
    assert reg.get("routine_next").max_depth == 1
    assert reg.traversable() == ()


# --- 4. o intrare invalidă e respinsă individual -------------------------------------------------


def test_invalid_entry_does_not_take_down_the_registry():
    reg = load_relation_kinds(
        {
            "routine_next": {"mode": "chain", "max_depth": 4, "ordered": True},
            "broken": {"mode": "teleport"},  # mod inexistent
            "substitute": {"mode": "bounded", "max_depth": 2},
        }
    )
    assert reg.get("routine_next").mode is TraversalMode.CHAIN
    assert reg.get("substitute").mode is TraversalMode.BOUNDED
    assert reg.get("broken").mode is TraversalMode.NEIGHBORS  # respins ⇒ inert, nu absent


@pytest.mark.parametrize("raw", [None, [], "chain", 7])
def test_non_mapping_config_yields_empty_registry(raw):
    assert load_relation_kinds(raw).traversable() == ()


def test_kind_cardinality_is_bounded():
    reg = load_relation_kinds(
        {f"k{i}": {"mode": "chain", "max_depth": 2} for i in range(MAX_RELATION_KINDS + 10)}
    )
    assert len(reg.traversable()) <= MAX_RELATION_KINDS


# --- etichete: text către client, deci locale-aware (D3) ----------------------------------------


def test_label_falls_back_to_base_locale():
    spec = RelationKindSpec(
        kind="routine_next",
        mode=TraversalMode.CHAIN,
        max_depth=4,
        ordered=True,
        labels={"ro": "Pași recomandați", "en": "Recommended steps"},
    )
    assert spec.label("ro-RO") == "Pași recomandați"
    assert spec.label("en") == "Recommended steps"


def test_label_without_match_returns_default_not_invention():
    """Un bloc fără titlu e onest; unul cu titlu în limba greșită nu e (P11)."""
    spec = RelationKindSpec(kind="routine_next", mode=TraversalMode.CHAIN, max_depth=2)
    assert spec.label("hu") is None
    assert spec.label("hu", default="Pasi") == "Pasi"


@pytest.mark.parametrize("labels", [{"romanian": "x"}, {"ro": "   "}, {"r": "x"}])
def test_bad_labels_rejected(labels):
    with pytest.raises(RelationKindConfigError):
        RelationKindSpec(kind="routine_next", mode=TraversalMode.CHAIN, max_depth=2, labels=labels)


# --- purpose: o cerință nu e un upsell ----------------------------------------------------------


def test_purpose_defaults_to_upsell_and_parses_requirement():
    reg = load_relation_kinds(
        {
            "requires": {"mode": "chain", "max_depth": 3, "purpose": "requirement"},
            "complement": {"mode": "neighbors"},
        }
    )
    assert reg.get("requires").purpose is RelationPurpose.REQUIREMENT
    assert reg.get("complement").purpose is RelationPurpose.UPSELL


def test_unknown_purpose_is_rejected_not_coerced():
    """Un `purpose` necunoscut NU devine `upsell` tăcut: o cerință degradată la ocazie s-ar putea
    suprima din răspuns, iar clientul ar primi un aparat fără piesa fără care nu funcționează."""
    reg = load_relation_kinds({"requires": {"mode": "chain", "max_depth": 3, "purpose": "vital"}})
    assert reg.get("requires").mode is TraversalMode.NEIGHBORS  # intrarea a fost respinsă


# --- registrul e stabil (determinism) -----------------------------------------------------------


def test_traversable_is_sorted_and_excludes_neighbors():
    reg = load_relation_kinds(
        {
            "substitute": {"mode": "bounded", "max_depth": 2},
            "complement": {"mode": "neighbors"},
            "routine_next": {"mode": "chain", "max_depth": 4},
        }
    )
    assert [s.kind for s in reg.traversable()] == ["routine_next", "substitute"]


def test_measured_demo_spec_is_expressible():
    """Specul derivat din `scripts/relations_graph_probe.py` pe catalogul demo trebuie să fie
    exprimabil în contract. Dacă vreodată nu mai e, contractul s-a rupt de realitatea măsurată."""
    reg = load_relation_kinds(
        {
            "routine_next": {"mode": "chain", "max_depth": 4, "ordered": True},
            "substitute": {"mode": "bounded", "max_depth": 2},
            "complement": {"mode": "neighbors"},
        }
    )
    assert reg.get("routine_next").traversable and reg.get("routine_next").ordered
    assert reg.get("substitute").max_depth == 2
    assert reg.get("complement").traversable is False
