"""NX-205 — contractul tipizat al adevărului (Facts / Evidence / Provenance / DerivedSignals).

Pur: fără DB, fără LLM. Verifică INVARIANTELE, nu formulările:
  - un claim important fără sursă nu trece — nici măcar cu o „sursă" din spații;
  - o contradicție internă nu trece, și nu se poate ocoli cu majuscule/diacritice;
  - necunoscutul (`None`) rămâne distinct de „cunoscut și gol" (`()`) până în serializare;
  - artefactele derivate poartă business_id + locale + schema_version (D3);
  - prețul/stocul NU pot fi capturate într-un artefact derivat;
  - contractul chiar parsează catalogul REAL (300/300), nu doar exemplele din teste;
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
    LiveFacts,
    NotRecommendedFor,
    ProductFacts,
    build_category_requirements,
    contradictions,
    parse_product,
    unknown_attribute_keys,
    validate_vocabulary,
)
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA = json.loads((_ROOT / "db/seed/catalog_v3.schema.json").read_text(encoding="utf-8"))
_CATALOG = json.loads((_ROOT / "db/seed/catalog_v2.json").read_text(encoding="utf-8"))


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


@pytest.mark.parametrize("blank", ["   ", "\t", "\n ", "   "])
def test_whitespace_only_provenance_is_rejected(blank: str):
    """Review #250: `" "` e truthy — o „sursă" din spații susținea un claim. Acum e respinsă la
    nivel de TIP (`NonBlank` face strip înainte de validare), în toate câmpurile."""
    with pytest.raises(ValidationError):
        ClaimProvenance(
            kind="ingredient", value="retinol", source=blank, source_ref="r", verified_at="2026"
        )
    with pytest.raises(ValidationError):
        NotRecommendedFor(
            value="pregnancy", level="hard", source=blank, source_ref="r", verified_at="2026"
        )
    with pytest.raises(ValidationError):
        NotRecommendedFor(value="sensitive", level="soft", reason=blank)


def test_audit_rule_also_rejects_whitespace_provenance():
    """Aceeași regulă în auditul R8 — altfel contractul e strict, dar poarta de publicare nu."""
    from scripts.audit_catalog_v2 import rule_claim_provenance

    product = {
        "slug": "p1",
        "status": "active",
        "attributes": {
            "key_ingredients": ["retinol"],
            "claim_provenance": [
                {
                    "kind": "ingredient",
                    "value": "retinol",
                    "source": "   ",
                    "source_ref": " ",
                    "verified_at": "\t",
                }
            ],
        },
    }
    findings = rule_claim_provenance([product])
    assert any("fără claim_provenance" in f["message"] for f in findings)


def test_key_ingredient_without_provenance_is_rejected():
    with pytest.raises(ValidationError, match="key_ingredients fără proveniență"):
        _facts(key_ingredients=("retinol",))
    assert _facts(
        key_ingredients=("retinol",), claim_provenance=(_prov("retinol"),)
    ).key_ingredients


def test_provenance_match_is_normalized():
    """«Retinol» acoperă «retinol» — altfel am cere proveniență de două ori pentru același lucru."""
    assert _facts(key_ingredients=("Retinol ",), claim_provenance=(_prov("retinol"),))


def test_badge_without_provenance_is_rejected():
    with pytest.raises(ValidationError, match="badges fără proveniență"):
        _facts(badges=("vegan",), claim_provenance=(_prov("vegan", kind="ingredient"),))
    assert _facts(badges=("vegan",), claim_provenance=(_prov("vegan", kind="badge"),)).badges


# --- contraindicații --------------------------------------------------------


def test_hard_contraindication_requires_inline_provenance():
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


@pytest.mark.parametrize(
    ("recommended", "contraindicated"),
    [("Sensitive", "sensitive"), ("sensitive ", "SENSITIVE"), ("sensibil", "sensibil")],
)
def test_contradiction_cannot_be_bypassed_by_casing_or_spaces(recommended, contraindicated):
    """Review #250: fără normalizare, „Sensitive" și „sensitive" erau valori diferite — o
    contradicție se ocolea cu o majusculă."""
    with pytest.raises(ValidationError, match="suitable_for"):
        _facts(
            suitable_for=(recommended,),
            not_recommended_for=(
                NotRecommendedFor(value=contraindicated, level="soft", reason="r"),
            ),
        )


def test_concern_treated_and_hard_contraindicated_is_a_contradiction():
    hard = NotRecommendedFor(
        value="ACNE", level="hard", source="s", source_ref="r", verified_at="2026-07-01"
    )
    with pytest.raises(ValidationError, match="contraindicat HARD"):
        _facts(concerns=("acne",), not_recommended_for=(hard,))


def test_free_of_and_key_ingredient_is_a_contradiction():
    """Și aici comparația e normalizată: «Alcohol » vs «alcohol» e același ingredient."""
    with pytest.raises(ValidationError, match="free_of"):
        _facts(
            free_of=("Alcohol ",),
            key_ingredients=("alcohol",),
            claim_provenance=(_prov("alcohol"),),
        )


def test_duplicate_contraindication_is_flagged_normalized():
    soft = NotRecommendedFor(value="Pregnancy", level="soft", reason="a")
    hard = NotRecommendedFor(
        value="pregnancy ", level="hard", source="s", source_ref="r", verified_at="2026-07-01"
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


# --- necunoscut ≠ gol (D5) --------------------------------------------------


def test_unknown_is_structurally_distinct_from_empty():
    """Review #250: dacă necunoscutul se serializează ca listă goală, „nu știm" devine „nu are"."""
    unknown = _facts()
    known_empty = _facts(concerns=())
    assert unknown.concerns is None and known_empty.concerns == ()
    assert "concerns" not in unknown.to_artifact()  # necunoscutul e OMIS, nu aplatizat
    assert known_empty.to_artifact()["concerns"] == ()  # golul CONFIRMAT rămâne vizibil


def test_from_product_preserves_unknown_vs_empty():
    absent = ProductFacts.from_product(
        {"slug": "p", "attributes": {}}, business_id="b", locale="ro"
    )
    empty = ProductFacts.from_product(
        {"slug": "p", "attributes": {"concerns": []}}, business_id="b", locale="ro"
    )
    assert absent.concerns is None
    assert empty.concerns == ()


# --- preț/stoc: fapte LIVE, niciodată snapshot ------------------------------


def test_product_facts_cannot_capture_price_or_stock():
    """Un preț copiat într-un artefact derivat e un preț care va fi greșit. Garanția e
    STRUCTURALĂ (câmpul nu există + `extra=forbid`), nu o convenție de scriere."""
    live_only = {"price", "sale_price", "availability", "stock_total"}
    assert live_only.isdisjoint(ProductFacts.model_fields)
    assert live_only.isdisjoint(EvidenceChunk.model_fields)
    assert live_only == set(LiveFacts.model_fields)
    with pytest.raises(ValidationError):
        _facts(price=99.0)


# --- acoperirea scope-ului din card -----------------------------------------


def test_facts_cover_the_card_scope():
    """Scope-ul NX-205 §1, verificat pe model (review #250: lipseau usage/wear_time/variants)."""
    required = {
        "finish",
        "texture",
        "coverage",
        "suitable_for",
        "concerns",
        "free_of",
        "key_ingredients",
        "usage",
        "wear_time",
        "net_content",
        "not_recommended_for",
        "variants",
    }
    assert required <= set(ProductFacts.model_fields)


def test_parses_every_demo_product():
    """Contractul trebuie să încapă peste catalogul REAL, nu doar peste exemple de test
    (review #250: respingea 300/300). Regimul de RAPORT (`parse_product`) nu aruncă — întoarce
    faptele + golurile, ca gate-ul NX-206 să le poată număra."""
    products = _CATALOG["products"]
    assert len(products) >= 300
    parsed = [parse_product(p, business_id="biz", locale="ro") for p in products]
    assert len(parsed) == len(products)
    # faptele chiar ajung în model (nu „parsează" golind totul)
    assert sum(1 for f, _ in parsed if f.best_for) == sum(
        1 for p in products if (p.get("attributes") or {}).get("best_for")
    )
    assert sum(1 for f, _ in parsed if f.usage is not None) == sum(
        1 for p in products if isinstance((p.get("attributes") or {}).get("usage"), dict)
    )
    assert any(f.net_content for f, _ in parsed) and any(f.claim_provenance for f, _ in parsed)


def test_demo_catalog_gap_is_exactly_unsourced_badges():
    """Golul REAL descoperit de contract: 195/300 produse au badge-uri, ZERO au
    `claim_provenance` kind=badge — iar R8 nu-l vedea (căuta badge-urile în `attributes`, dar în
    seed stau la nivel de produs). Testul FIXEAZĂ constatarea, ca NX-206 să nu o poată pierde:
    dacă golul se închide, testul pică și se șterge deliberat."""
    problems = [
        (p["slug"], probs)
        for p in _CATALOG["products"]
        for _, probs in [parse_product(p, business_id="b", locale="ro")]
        if probs
    ]
    assert all(len(probs) == 1 and probs[0].startswith("badges") for _, probs in problems)
    assert len(problems) == 195


def test_audit_now_sees_product_level_badges():
    """Regula R8 se uită în AMBELE locuri. Locul nou e warning (popularea datelor = NX-206), dar
    nu mai e invizibil."""
    from scripts.audit_catalog_v2 import rule_claim_provenance

    findings = rule_claim_provenance(
        [{"slug": "p1", "status": "active", "badges": ["Vegan"], "attributes": {}}]
    )
    assert findings and findings[0]["severity"] == "warning"
    assert "nivel produs" in findings[0]["message"]


def test_unknown_attribute_keys_are_visible_not_silent():
    """Catalogul demo chiar are chei-gunoi (nume de ingredient folosit drept cheie). Contractul nu
    le acceptă ca fapte, dar nici nu le înghite tăcut."""
    junk = {k for p in _CATALOG["products"] for k in unknown_attribute_keys(p)}
    assert junk, "dacă seed-ul se curăță, testul devine trivial — atunci scoate-l"
    assert unknown_attribute_keys({"attributes": {"concerns": [], "Ulei de soia (50%)": "x"}}) == (
        "Ulei de soia (50%)",
    )


# --- artefacte derivate: D3 -------------------------------------------------


def test_evidence_chunk_requires_source_and_carries_d3_fields():
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
    with pytest.raises(ValidationError):
        DerivedSignal(
            business_id="b",
            product_id="p",
            locale="ro",
            signal="good_for_oily",
            derived_from=(),
            rule_id="r1",
        )
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            DerivedSignal(
                business_id="b",
                product_id="p",
                locale="ro",
                signal="good_for_oily",
                derived_from=("attributes.concerns",),
                rule_id=bad,
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


# --- vocabular: „nevalidat" ≠ „valid" ---------------------------------------


def test_vocabulary_report_separates_problems_from_unchecked():
    """Review #250: câmpurile fără vocabular declarat se raportează EXPLICIT ca neverificate."""
    facts = _facts(concerns=("oily", "inventat"), finish="glowy", texture="gel")
    report = validate_vocabulary(facts, {"concerns": {"oily", "dry"}, "finish": {"matte", "dewy"}})
    assert any("concerns" in p and "inventat" in p for p in report.problems)
    assert any("finish" in p for p in report.problems)
    assert "texture" in report.unchecked  # prezent, dar fără vocabular → NU e declarat „ok"
    assert not report.is_clean

    nothing_declared = validate_vocabulary(facts, {})
    assert nothing_declared.problems == ()
    assert set(nothing_declared.unchecked) == {"concerns", "finish", "texture"}
    assert (
        not nothing_declared.is_clean
    )  # „n-am verificat nimic" nu e o promisiune de corectitudine


def test_vocabulary_ignores_unknown_fields_but_reports_nothing_for_them():
    """Un câmp NECUNOSCUT (None) nu e nici valid, nici invalid, nici „neverificat" — nu există."""
    report = validate_vocabulary(_facts(), {"concerns": {"oily"}})
    assert report.problems == () and report.unchecked == ()
    assert report.is_clean


def test_vocabulary_match_is_normalized():
    report = validate_vocabulary(_facts(concerns=("Oily ",)), {"concerns": {"oily"}})
    assert report.problems == ()


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


def test_missing_distinguishes_absent_from_empty():
    """Ambele încalcă cerința, dar auditul trebuie să poată spune CARE e care."""
    reqs = _pack().required_attributes
    attrs = {"finish": "matte", "coverage": "", "suitable_for": []}
    assert reqs.missing(attrs, "fond-de-ten", "machiaj") == ("coverage", "suitable_for", "texture")
    assert reqs.missing_detail(attrs, "fond-de-ten", "machiaj") == (
        ("coverage", "empty"),
        ("suitable_for", "empty"),
        ("texture", "absent"),
    )


def test_vertical_without_contract_has_no_requirements():
    assert _pack("ecommerce").required_attributes.required_for("orice", "orice") == frozenset()


def test_build_requirements_is_fail_closed_per_entry():
    reqs = build_category_requirements(
        {
            "by_slug": {
                "ok": ["finish"],
                "gol": [],
                "nu-e-lista": "finish",
                "": ["x"],
                "cu-gunoi": ["finish", 42, "  ", "coverage"],
            },
            "by_root": "nu e dict",
        }
    )
    assert reqs.by_slug["ok"] == frozenset({"finish"})
    assert reqs.by_slug["cu-gunoi"] == frozenset({"finish", "coverage"})
    assert set(reqs.by_slug) == {"ok", "cu-gunoi"}
    assert reqs.by_root == {}
    assert build_category_requirements(None) == CategoryRequirements(by_slug={}, by_root={})


def test_audit_requirements_survive_runtime_kill_switch(monkeypatch):
    """Review #250: auditul crăpa cu `DOMAIN_PACK_ENABLED=false`. E o unealtă OFFLINE — citește
    defaults-ul direct, deci un kill-switch de RUNTIME nu-l atinge (și nici nu-l golește tăcut)."""
    from scripts.audit_catalog_v2 import _load_requirements
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "domain_pack_enabled", False)
    reqs = _load_requirements()
    assert reqs.required_for("fond-de-ten", "machiaj")


def test_audit_requirements_are_the_pack_requirements():
    from scripts.audit_catalog_v2 import REQUIRED_V3_BY_ROOT, REQUIRED_V3_BY_SLUG

    reqs = _pack().required_attributes
    assert {k: frozenset(v) for k, v in REQUIRED_V3_BY_SLUG.items()} == dict(reqs.by_slug)
    assert {k: frozenset(v) for k, v in REQUIRED_V3_BY_ROOT.items()} == dict(reqs.by_root)


# --- consistență cu sursa 168d (anti-drift) ---------------------------------


def test_contract_vocabularies_match_json_schema():
    defs = _SCHEMA["$defs"]
    assert set(defs["claimProvenance"]["properties"]["kind"]["enum"]) == set(PROVENANCE_KINDS)
    assert set(defs["notRecommendedFor"]["properties"]["level"]["enum"]) == set(
        CONTRAINDICATION_LEVELS
    )
    assert set(defs["claimProvenance"]["required"]) == set(ClaimProvenance.model_fields)
    assert set(defs["notRecommendedFor"]["properties"]) == set(NotRecommendedFor.model_fields)


def test_evidence_roles_match_the_card_contract():
    assert EVIDENCE_ROLES == {
        "benefit",
        "usage",
        "warning",
        "ingredient",
        "faq",
        "review_summary",
        "policy",
    }


# --- invariante de tenant pe migrarea 035 -----------------------------------

_MIGRATION = (_ROOT / "docs/035_evidence_and_derived_signals.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("table", ["product_evidence_chunks", "product_derived_signals"])
def test_migration_enforces_tenant_invariants(table: str):
    """P7 + D3 pe TEXTUL migrării: `business_id` NOT NULL, FK COMPUS către produs (cross-tenant
    structural imposibil, ca la 027), RLS cu politica pe `current_business_id()`, bot select."""
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


def test_migration_rejects_empty_derived_from():
    """Review #250: `array_length(x, 1)` întoarce NULL pe array gol, iar un CHECK NULL TRECE —
    `derived_from = '{}'` era acceptat. `cardinality()` întoarce 0, deci chiar respinge.
    Verificat și pe un PostgreSQL 16 efemer (vezi descrierea PR-ului)."""
    assert "array_length(derived_from" not in _MIGRATION
    assert "check (cardinality(derived_from) >= 1)" in _MIGRATION
    assert "check (array_position(derived_from, null) is null)" in _MIGRATION


def test_migration_number_is_not_a_burned_one():
    """030/031 sunt ARSE (înregistrate/revendicate de branch-uri nemerge-uite) — vezi docs/034."""
    numbers = sorted(int(p.name.split("_", 1)[0]) for p in (_ROOT / "docs").glob("0*.sql"))
    assert 35 in numbers and 30 not in numbers and 31 not in numbers
