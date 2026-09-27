"""NX-332 felia 4a-1 — șabloanele de clarificare sunt ale PACHETULUI
(`DomainPack.clarify_templates`).

Contractul (§„Planner and answer policy"): textul unei întrebări e dată de pachet, per locale și per
fel (`clarify_templates[locale][kind]`), iar kernelul completează doar `{options}` cu etichete
canonice. Loaderul e fail-closed pe ȘABLON (o frază cu alt marcator se aruncă și se loghează) și
fail-open pe PACHET (restul se încarcă). Zero model, zero DB."""

from __future__ import annotations

import logging

import pytest

from src.conversation.ambiguity_gate import valid_template
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay

OPTION_KINDS = ("reference", "scope", "value", "subject", "confirm", "conflict", "generic")
BOUND_KINDS = ("bound_lte", "bound_gte")


def _pack(override: dict | None = None, vertical: str = "ecommerce"):
    settings = {"domain_pack": override} if override is not None else {}
    business = BusinessConfig(id="b-t", slug="t", name="t", vertical=vertical, settings=settings)
    pack = load_domain_pack(business)
    assert pack is not None
    return pack


@pytest.mark.parametrize("locale", ["ro", "en"])
def test_the_ecommerce_defaults_carry_every_kind_in_both_locales(locale):
    table = _pack().clarify_templates[locale]
    for kind in OPTION_KINDS:
        assert table[kind].count("{options}") == 1, kind
    for kind in BOUND_KINDS:
        assert table[kind].count("{value}") == 1, kind


def test_the_form_is_locale_then_kind_the_inverse_of_answer_shape_templates():
    pack = _pack()
    assert set(pack.clarify_templates) >= {"ro", "en"}
    assert "reference" in pack.clarify_templates["ro"]
    # `answer_shape_templates` rămâne kind → locale: două forme, două normalizatoare.
    assert "ro" in pack.answer_shape_templates["list_glue"]


def test_the_defaults_obey_the_voice_rule():
    """P13: niciun șablon nu poartă liniuța de pauză sau punct și virgulă."""
    for locale, table in _pack().clarify_templates.items():
        for kind, phrase in table.items():
            assert "—" not in phrase and "–" not in phrase and " - " not in phrase, (locale, kind)
            assert ";" not in phrase, (locale, kind)


def test_a_tenant_override_wins_per_kind_and_keeps_the_rest():
    pack = _pack({"clarify_templates": {"ro": {"subject": "Pentru ce: {options}?"}}})
    assert pack.clarify_templates["ro"]["subject"] == "Pentru ce: {options}?"
    assert pack.clarify_templates["ro"]["reference"] == _pack().clarify_templates["ro"]["reference"]


@pytest.mark.parametrize(
    "phrase",
    [
        "La care te referi: {option}?",  # marcator greșit
        "Între {options} și {options}?",  # două marcatoare
        "Alege {options} sau {value}",  # un marcator în plus
        "Nimic de completat",  # niciun marcator
        "Acolade stricate {options",  # șablon imposibil de formatat
        "Pozițional {}",
    ],
)
def test_a_bad_options_template_is_dropped_and_the_pack_still_loads(phrase, caplog):
    with caplog.at_level(logging.WARNING):
        pack = _pack(
            {"clarify_templates": {"ro": {"reference": phrase, "value": "Ok: {options}?"}}}
        )
    assert "reference" not in pack.clarify_templates["ro"]
    assert pack.clarify_templates["ro"]["value"] == "Ok: {options}?"
    assert pack.facets == _pack().facets  # restul pachetului nu e atins
    assert any("clarify_templates" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("phrase", ["sub {options}", "sub {value} și {value}", "sub", "{value"])
def test_a_bound_label_needs_exactly_one_value_marker(phrase):
    pack = _pack({"clarify_templates": {"ro": {"bound_lte": phrase}}})
    assert "bound_lte" not in pack.clarify_templates["ro"]
    assert pack.clarify_templates["ro"]["bound_gte"].count("{value}") == 1


@pytest.mark.parametrize(
    ("kind", "phrase"),
    [
        ("reference", "La care: {options}?"),
        ("reference", "La care: {option}?"),
        ("reference", "{options} {options}"),
        ("reference", "Stricat {options"),
        ("scope", "sub {value}"),
        ("bound_lte", "sub {value}"),
        ("bound_gte", "minim {options}"),
        ("bound_gte", "minim"),
    ],
)
def test_the_loader_and_the_gate_judge_a_template_the_same_way(kind, phrase):
    """Loaderul nu importă kernelul, deci regula e scrisă de două ori: aici se cere să nu
    diverge."""
    loaded = _pack({"clarify_templates": {"xx": {kind: phrase}}}, vertical="other")
    assert (kind in loaded.clarify_templates.get("xx", {})) == valid_template(kind, phrase)


@pytest.mark.parametrize("raw", [None, "text", ["ro"], {"ro": "text"}, {"ro": {"reference": 3}}])
def test_garbage_is_ignored_without_crashing(raw):
    pack = _pack({"clarify_templates": raw}, vertical="other")
    assert all(isinstance(t, dict) for t in pack.clarify_templates.values())


def test_a_vertical_without_templates_has_none():
    assert _pack(vertical="other").clarify_templates == {}


@pytest.mark.parametrize("name", replay.FIXTURE_PACKS)
def test_every_fixture_pack_has_templates_for_ro(name):
    table = fc.pack(name).clarify_templates.get("ro", {})
    assert set(OPTION_KINDS) <= set(table)


def test_a_fixture_pack_can_own_its_wording():
    """Pachetul de cadouri își scrie propria întrebare de subiect: frazele sunt date, nu cod."""
    assert (
        fc.pack("gifts").clarify_templates["ro"]["subject"]
        != _pack().clarify_templates["ro"]["subject"]
    )


@pytest.mark.parametrize(
    ("name", "facet", "label"),
    [
        ("electronics", "screen", "Ecran"),
        ("electronics", "storage", "Stocare"),
        ("furniture", "seats", "Locuri"),
        ("furniture", "width", "Latime"),
    ],
)
def test_the_numeric_facets_of_the_fixture_packs_carry_row_labels(name, facet, label):
    """Eticheta de rând e citirea din care politica de răspuns află dimensiunea decisivă."""
    [typed] = [f for f in fc.pack(name).facets if f.key == facet]
    assert typed.labels["ro"] == label
