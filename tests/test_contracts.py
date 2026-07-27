"""NX-205 — contractul tipizat al adevărului (Facts / Evidence / Provenance / DerivedSignals).

Pur: fără DB, fără LLM. Verifică INVARIANTELE, nu formulările:
  - un claim important fără sursă nu trece;
  - o contradicție internă nu trece (fail-closed, nu „publică cu warning");
  - artefactele derivate poartă business_id + locale + schema_version (D3);
  - contractul din cod NU divergează de `catalog_v3.schema.json` (sursa 168d).
"""

import json
import pathlib

import pytest
from pydantic import ValidationError

from src.domain.contracts import (
    CONTRACT_SCHEMA_VERSION,
    CONTRAINDICATION_LEVELS,
    EVIDENCE_ROLES,
    PROVENANCE_KINDS,
    CategoryRequirements,
    ClaimProvenance,
    DerivedSignal,
    EvidenceChunk,
    NotRecommendedFor,
    ProductFacts,
    build_category_requirements,
    contradictions,
    validate_vocabulary,
)
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig

_SCHEMA = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "db/seed/catalog_v3.schema.json").read_text(
        encoding="utf-8"
    )
)


def _pack(vertical: str = "beauty_salon"):
    return load_domain_pack(
        BusinessConfig(id="b", slug="s", name="n", vertical=vertical, settings={})
    )


def _prov(value: str, kind: str = "ingredient") -> ClaimProvenance:
    return ClaimProvenance(
        kind=kind, value=value, source="INCI", source_ref="inci-2026-01", verified_at="2026-07-01"
    )


def _facts(**kw) -> ProductFacts:
    base = {"business_id": "biz", "product_id": "prod", "locale": "ro"}
    return ProductFacts(**{**base, **kw})


# --- provenance -------------------------------------------------------------


def test_claim_provenance_requires_all_five_fields():
    """Shape-ul 168d: fără source/source_ref/verified_at un claim nu e verificabil."""
    for missing in ("source", "source_ref", "verified_at", "value"):
        payload = {
            "kind": "ingredient",
            "value": "retinol",
            "source": "INCI",
            "source_ref": "ref",
            "verified_at": "2026-07-01",
        }
        payload[missing] = ""
        with pytest.raises(ValidationError):
            ClaimProvenance(**payload)


def test_key_ingredient_without_provenance_is_rejected():
    """DoD: claims fără sursă nu trec — nu „trec cu warning"."""
    with pytest.raises(ValidationError, match="key_ingredients fără proveniență"):
        _facts(key_ingredients=("retinol",))
    # cu proveniență → trece
    assert _facts(
        key_ingredients=("retinol",), claim_provenance=(_prov("retinol"),)
    ).key_ingredients


def test_badge_without_provenance_is_rejected():
    with pytest.raises(ValidationError, match="badges fără proveniență"):
        _facts(badges=("vegan",), claim_provenance=(_prov("vegan", kind="ingredient"),))
    assert _facts(badges=("vegan",), claim_provenance=(_prov("vegan", kind="badge"),)).badges


# --- contraindicații --------------------------------------------------------


def test_hard_contraindication_requires_inline_provenance():
    """`hard` = excludere dură; fără sursă ar fi o afirmație medicală nesusținută (P0-safety)."""
    with pytest.raises(ValidationError, match="hard fără proveniență"):
        NotRecommendedFor(value="pregnancy", level="hard")
    ok = NotRecommendedFor(
        value="pregnancy",
        level="hard",
        source="ANMDMR",
        source_ref="rcp-retinoid",
        verified_at="2026-07-01",
        rule_id="retinoid_pregnancy",
    )
    assert ok.is_enforceable


def test_soft_contraindication_requires_reason():
    with pytest.raises(ValidationError, match="soft fără `reason`"):
        NotRecommendedFor(value="sensitive", level="soft")
    assert NotRecommendedFor(value="sensitive", level="soft", reason="poate irita").level == "soft"


def test_backfilled_fields_are_part_of_the_contract():
    """`rule_id`/`reviewed_by`/`matched_on` sunt scrise de backfill-ul NX-173 — contractul trebuie
    să le accepte, altfel datele reale ar fi respinse de propriul lor contract."""
    n = NotRecommendedFor(
        value="pregnancy",
        level="hard",
        source="ANMDMR",
        source_ref="rcp",
        verified_at="2026-07-01",
        rule_id="retinoid_pregnancy",
        reviewed_by="farmacist",
        matched_on="retinol",
    )
    assert n.rule_id == "retinoid_pregnancy"


def test_unknown_field_is_rejected():
    """`extra=forbid`: un câmp inventat nu se strecoară tăcut în contract."""
    with pytest.raises(ValidationError):
        NotRecommendedFor(value="x", level="soft", reason="y", junk=True)


# --- contradicții -----------------------------------------------------------


def test_suitable_and_contraindicated_is_a_contradiction():
    with pytest.raises(ValidationError, match="suitable_for.*not_recommended_for"):
        _facts(
            suitable_for=("sensitive",),
            not_recommended_for=(
                NotRecommendedFor(value="sensitive", level="soft", reason="poate irita"),
            ),
        )


def test_concern_treated_and_hard_contraindicated_is_a_contradiction():
    hard = NotRecommendedFor(
        value="acne", level="hard", source="s", source_ref="r", verified_at="2026-07-01"
    )
    with pytest.raises(ValidationError, match="contraindicat HARD"):
        _facts(concerns=("acne",), not_recommended_for=(hard,))


def test_free_of_and_key_ingredient_is_a_contradiction():
    with pytest.raises(ValidationError, match="free_of"):
        _facts(
            free_of=("Alcohol",),
            key_ingredients=("alcohol",),
            claim_provenance=(_prov("alcohol"),),
        )


def test_duplicate_contraindication_is_flagged():
    soft = NotRecommendedFor(value="x", level="soft", reason="a")
    hard = NotRecommendedFor(
        value="x", level="hard", source="s", source_ref="r", verified_at="2026-07-01"
    )
    facts = ProductFacts.model_construct(
        business_id="b", product_id="p", locale="ro", not_recommended_for=(soft, hard)
    )
    assert any("duplicată" in p for p in contradictions(facts))


def test_clean_facts_have_no_contradictions():
    facts = _facts(
        concerns=("oily",),
        suitable_for=("oily",),
        key_ingredients=("niacinamida",),
        claim_provenance=(_prov("niacinamida"),),
        not_recommended_for=(NotRecommendedFor(value="dry", level="soft", reason="matifiant"),),
    )
    assert contradictions(facts) == []


# --- artefacte derivate: D3 -------------------------------------------------


def test_evidence_chunk_requires_source_and_carries_d3_fields():
    """Un fragment fără sursă nu e evidence. Și poartă business_id + locale + schema_version."""
    with pytest.raises(ValidationError):
        EvidenceChunk(
            business_id="b", product_id="p", locale="ro", role="benefit", text="t", source="  "
        )
    ev = EvidenceChunk(
        business_id="b",
        product_id="p",
        locale="ro",
        role="warning",
        text="A nu se folosi…",
        source="RCP",
    )
    assert (ev.business_id, ev.locale, ev.schema_version) == ("b", "ro", CONTRACT_SCHEMA_VERSION)


def test_evidence_role_vocabulary_is_closed():
    with pytest.raises(ValidationError):
        EvidenceChunk(
            business_id="b", product_id="p", locale="ro", role="marketing", text="t", source="s"
        )


def test_derived_signal_requires_inputs_and_rule():
    """Un semnal fără `derived_from` nu e derivat, e inventat; fără `rule_id` nu e reparabil."""
    with pytest.raises(ValidationError):
        DerivedSignal(
            business_id="b",
            product_id="p",
            locale="ro",
            signal="good_for_oily",
            derived_from=(),
            rule_id="r1",
        )
    with pytest.raises(ValidationError):
        DerivedSignal(
            business_id="b",
            product_id="p",
            locale="ro",
            signal="good_for_oily",
            derived_from=("attributes.concerns",),
            rule_id="",
        )
    ok = DerivedSignal(
        business_id="b",
        product_id="p",
        locale="ro",
        signal="good_for_oily",
        derived_from=("attributes.concerns", "attributes.finish"),
        rule_id="oily_from_matte_v1",
    )
    assert ok.schema_version == CONTRACT_SCHEMA_VERSION


def test_no_pii_shaped_fields():
    """Contractele descriu PRODUSE: niciun câmp de contact/utilizator (P12)."""
    forbidden = {"phone", "email", "wa_id", "external_id", "contact_id", "display_name", "address"}
    for model in (ClaimProvenance, NotRecommendedFor, EvidenceChunk, DerivedSignal, ProductFacts):
        assert forbidden.isdisjoint(model.model_fields), model.__name__


# --- vocabular: din DomainPack, nu din cod ----------------------------------


def test_vocabulary_validation_uses_pack_not_code():
    facts = _facts(concerns=("oily", "inventat"), finish="glowy")
    problems = validate_vocabulary(
        facts, {"concerns": {"oily", "dry"}, "finish": {"matte", "dewy"}}
    )
    assert any("concerns" in p and "inventat" in p for p in problems)
    assert any("finish" in p for p in problems)
    # fără vocabular declarat pentru un câmp → nevalidat (decide gate-ul NX-206, nu contractul)
    assert validate_vocabulary(facts, {}) == []


# --- cerințe per categorie (DomainPack) -------------------------------------


def test_requirements_come_from_domain_pack():
    reqs = _pack().required_attributes
    assert reqs.required_for("fond-de-ten", "machiaj") == frozenset(
        {"finish", "coverage", "suitable_for", "texture"}
    )
    assert reqs.required_for("seruri-pentru-ten", "ingrijirea-tenului") == frozenset(
        {"concerns", "texture", "usage", "key_ingredients"}
    )


def test_slug_overrides_root_not_union():
    """Semantica R10 din NX-168d: produsele de ochi NU moștenesc `finish` de la `machiaj`."""
    reqs = _pack().required_attributes
    assert reqs.required_for("mascara", "machiaj") == frozenset({"key_benefit"})
    assert "finish" not in reqs.required_for("farduri-de-ochi", "machiaj")


def test_missing_reports_absent_and_empty_fields():
    reqs = _pack().required_attributes
    attrs = {"finish": "matte", "coverage": "", "suitable_for": [], "texture": None}
    assert reqs.missing(attrs, "fond-de-ten", "machiaj") == ("coverage", "suitable_for", "texture")


def test_vertical_without_contract_has_no_requirements():
    """Verticalele fără contract de conținut rămân exact ca azi (P6)."""
    assert _pack("ecommerce").required_attributes.required_for("orice", "orice") == frozenset()


def test_build_requirements_is_fail_closed_per_entry():
    reqs = build_category_requirements(
        {
            "by_slug": {
                "ok": ["finish"],
                "gol": [],
                "nu-e-lista": "finish",
                "": ["x"],
                "cu-gunoi": ["finish", 42, "", "coverage"],
            },
            "by_root": "nu e dict",
        }
    )
    assert reqs.by_slug["ok"] == frozenset({"finish"})
    assert reqs.by_slug["cu-gunoi"] == frozenset({"finish", "coverage"})  # gunoiul cade, restul stă
    assert set(reqs.by_slug) == {"ok", "cu-gunoi"}
    assert reqs.by_root == {}
    assert build_category_requirements(None) == CategoryRequirements(by_slug={}, by_root={})


# --- consistență cu sursa 168d (anti-drift) ---------------------------------


def test_contract_vocabularies_match_json_schema():
    """Vocabularele din cod NU au voie să divergă de `catalog_v3.schema.json` (sursa NX-168d).
    Fără testul ăsta, contractul Python devine a patra copie care se desincronizează tăcut."""
    defs = _SCHEMA["$defs"]
    assert set(defs["claimProvenance"]["properties"]["kind"]["enum"]) == set(PROVENANCE_KINDS)
    assert set(defs["notRecommendedFor"]["properties"]["level"]["enum"]) == set(
        CONTRAINDICATION_LEVELS
    )
    assert set(defs["claimProvenance"]["required"]) == set(ClaimProvenance.model_fields)
    # câmpurile permise de schemă = câmpurile modelului (additionalProperties: false pe ambele)
    assert set(defs["notRecommendedFor"]["properties"]) == set(NotRecommendedFor.model_fields)


def test_evidence_roles_match_the_card_contract():
    """Rolurile sunt contract de card (D9) — dacă se schimbă, se schimbă deliberat."""
    assert EVIDENCE_ROLES == {
        "benefit",
        "usage",
        "warning",
        "ingredient",
        "faq",
        "review_summary",
        "policy",
    }


def test_audit_requirements_are_the_pack_requirements():
    """Auditul NU mai are o copie proprie a obligatoriilor: citește pack-ul (P9)."""
    from scripts.audit_catalog_v2 import REQUIRED_V3_BY_ROOT, REQUIRED_V3_BY_SLUG

    reqs = _pack().required_attributes
    assert {k: frozenset(v) for k, v in REQUIRED_V3_BY_SLUG.items()} == dict(reqs.by_slug)
    assert {k: frozenset(v) for k, v in REQUIRED_V3_BY_ROOT.items()} == dict(reqs.by_root)


# --- invariante de tenant pe migrarea 035 (static, fără DB) ------------------

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1] / "docs/035_evidence_and_derived_signals.sql"
).read_text(encoding="utf-8")


@pytest.mark.parametrize("table", ["product_evidence_chunks", "product_derived_signals"])
def test_migration_enforces_tenant_invariants(table: str):
    """P7 + D3 verificate pe TEXTUL migrării: fiecare tabel nou are `business_id` NOT NULL, FK
    COMPUS către produs (cross-tenant structural imposibil, ca la 027), RLS activat cu politica pe
    `current_business_id()`, iar botul primește DOAR select (scrierea e a joburilor de conținut).
    Testul e static ca să prindă regresia la review, nu abia după aplicarea migrării."""
    body = _MIGRATION.split(f"create table if not exists {table}", 1)[1].split(");", 1)[0]
    assert "business_id    uuid not null references businesses(id)" in body
    assert "locale         text not null" in body
    assert "schema_version integer not null" in body
    assert "foreign key (business_id, product_id) references products (business_id, id)" in body, (
        "FK compus lipsă → un rând ar putea referi produsul altui tenant"
    )
    assert f"alter table {table} enable row level security;" in _MIGRATION
    assert f"grant select on {table} to bot_runtime;" in _MIGRATION
    assert f"create policy bot_runtime_tenant on {table} to bot_runtime" in _MIGRATION
    assert "using (business_id = current_business_id())" in _MIGRATION
    for write in ("insert", "update", "delete"):
        assert f"grant {write} on {table} to bot_runtime" not in _MIGRATION


def test_migration_number_is_not_a_burned_one():
    """030/031 sunt ARSE (înregistrate/revendicate de branch-uri nemerge-uite) — vezi docs/034."""
    numbers = sorted(
        int(p.name.split("_", 1)[0])
        for p in (pathlib.Path(__file__).resolve().parents[1] / "docs").glob("0*.sql")
    )
    assert 35 in numbers and 30 not in numbers and 31 not in numbers
