"""NX-247 — testele manifestului canonic de contract. Fără DB, fără rețea, fără browser.

Ce dovedesc, în ordinea în care contează:

1. **Manifestul e la zi.** Dacă schema, fixturile, scenariile sau pragurile s-au schimbat fără să
   se regenereze artefactul, suita pică. Ăsta e gate-ul de drift, și e ieftin: rulează pe fiecare
   PR, în jobul rapid.
2. **Manifestul e determinist.** Două generări din același tree dau aceiași bytes. Fără asta,
   „driftul rupe CI" ar însemna „trecerea timpului rupe CI", iar oamenii ar învăța să ignore
   semnalul.
3. **Orice editare a oricărui artefact rupe gate-ul (R16).** Se testează prin MUTAȚIE — un byte
   schimbat în schema publicată, în fixture sau în praguri trebuie să producă drift. Un test care
   doar compară manifestul cu el însuși ar trece și pe un gate mort.
4. **Fixturile validează contra schemei PUBLICATE**, nu doar contra modelelor Pydantic: frontendul
   are un validator generat din schemă, deci un fixture care trece prin Pydantic dar nu prin schemă
   ar fi un contract divergent.
5. **Setul de fixture e complet.** Un fixture nou care nu intră în manifest e un fixture pe care
   frontendul nu-l validează niciodată.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import stage1_contract_manifest as manifest_script
from src.web.contracts_v2 import (
    TURN_SCHEMA_VERSION,
    VIEW_SCHEMA_VERSION,
    schema_hash,
    turn_json_schema,
    view_json_schema,
)
from tests.e2e import stage1_scenarios as sc

ROOT = Path(__file__).resolve().parents[2]


# ── 1. La zi ────────────────────────────────────────────────────────────────────────────────


def test_manifest_is_current() -> None:
    problems = manifest_script.drift()
    assert not problems, (
        "manifestul de contract a driftat față de cod:\n  "
        + "\n  ".join(problems)
        + "\nRulează: python scripts/stage1_contract_manifest.py --write"
    )


def test_manifest_declares_the_versions_the_code_speaks() -> None:
    pack = manifest_script.read_manifest()
    assert pack["schema_version"] == VIEW_SCHEMA_VERSION
    assert pack["turn_schema_version"] == TURN_SCHEMA_VERSION
    assert pack["schema_sha256"] == schema_hash(view_json_schema())
    assert pack["turn_schema_sha256"] == schema_hash(turn_json_schema())


# ── 2. Determinism ──────────────────────────────────────────────────────────────────────────


def test_contract_pack_is_byte_deterministic() -> None:
    assert manifest_script.render(manifest_script.contract_pack()) == manifest_script.render(
        manifest_script.contract_pack()
    )


def test_manifest_has_no_nondeterministic_field() -> None:
    """`generated_at` (și orice câmp de ceas) e interzis: cardul cere „deterministic sau omis", iar
    „omis" e singura variantă care nu poate minți."""

    def keys_of(node: object, acc: set[str]) -> set[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                acc.add(key.lower())
                keys_of(value, acc)
        elif isinstance(node, list):
            for item in node:
                keys_of(item, acc)
        return acc

    # Se verifică CHEILE, nu textul serializat: `_note` e proză și are voie să conțină cuvântul
    # „timestamp" tocmai ca să explice de ce nu există niciun câmp de ceas.
    keys = keys_of(manifest_script.read_manifest(), set())
    for banned in ("generated_at", "timestamp", "created_at", "updated_at", "run_id", "nonce"):
        assert banned not in keys, f"manifestul conține un câmp nedeterminist: {banned!r}"


def test_backend_commit_is_not_baked_into_the_manifest() -> None:
    """Un fișier nu poate conține hash-ul commitului care îl conține. SHA-ul trăiește în
    certificatul de rulare, iar `null` aici e afirmația explicită a acestui fapt."""
    assert manifest_script.read_manifest()["backend_commit"] is None


def test_schema_bytes_hash_to_the_published_hash() -> None:
    """Bytes-ii pe care îi copiază frontendul TREBUIE să hash-uiască la hashul negociat. Dacă
    cele două ar divergea, negocierea de capability ar promite un contract și livra altul."""
    canonical = manifest_script.canonical_schema_bytes()
    assert hashlib.sha256(canonical).hexdigest() == schema_hash(view_json_schema())


# ── 3. Mutație: driftul rupe gate-ul (R16) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "schema_sha256",
        "fixtures_sha256",
        "projections_sha256",
        "scenarios_sha256",
        "thresholds_sha256",
    ],
)
def test_r16_any_artifact_edit_breaks_the_gate(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Un byte schimbat în ORICE artefact al pachetului ⇒ drift raportat. Se simulează mutând
    valoarea din manifestul CITIT (nu editând fișierul din repo: un test nu are voie să lase
    reziduu într-un artefact versionat)."""
    stored = manifest_script.read_manifest()
    tampered = {**stored, key: "0" * 64}
    monkeypatch.setattr(manifest_script, "read_manifest", lambda: tampered)
    problems = manifest_script.drift()
    assert any(p.startswith(f"{key}:") for p in problems), (
        f"editarea lui {key} nu a fost detectată — gate-ul de contract e mort"
    )


def test_truncating_the_fixture_set_is_detected() -> None:
    """Setul TRUNCHIAT e cazul pe care un hash per-fișier îl ratează: fiecare fișier rămas e
    corect, dar contractul acoperă mai puțin decât spune."""
    full = manifest_script.contract_pack()
    fixtures = dict(full["fixtures"])
    fixtures.pop(next(iter(fixtures)))
    digest = hashlib.sha256(
        json.dumps(fixtures, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest != full["fixtures_sha256"]


# ── 4. Fixturile validează contra schemei publicate ─────────────────────────────────────────


def _fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "web_v2" / name).read_text(encoding="utf-8"))


def test_all_contract_fixtures_validate_against_the_published_schema() -> None:
    validator = Draft202012Validator(view_json_schema())
    views = {k: v for k, v in _fixture("valid_views.json").items() if not k.startswith("_")}
    assert views, "setul de fixture valide e gol"
    for name, payload in views.items():
        try:
            validator.validate(payload)
        except Exception as e:  # noqa: BLE001 — re-ridicat cu numele fixture-ului
            raise AssertionError(f"fixture-ul valid {name!r} nu trece schema publicată: {e}") from e


def test_invalid_fixtures_are_actually_rejected() -> None:
    """Fixturile invalide trebuie să fie invalide — prin schemă SAU prin model. Un fixture
    „invalid" care trece ambele porți e un test negativ mort, iar acelea se strică neobservate."""
    from src.web.contracts_v2 import parse_view

    validator = Draft202012Validator(view_json_schema())
    cases = _fixture("invalid_views.json")["cases"]
    assert cases, "setul de fixture invalide e gol"
    for case in cases:
        payload = case["payload"]
        schema_ok = validator.is_valid(payload)
        try:
            parse_view(payload)
            model_ok = True
        except Exception:  # noqa: BLE001 — orice respingere e o respingere
            model_ok = False
        assert not (schema_ok and model_ok), (
            f"fixture-ul invalid {case['name']!r} trece ambele porți ({case['reason']})"
        )


def test_projection_fixtures_validate_too() -> None:
    """Proiecțiile NX-240 sunt ce randează browserul. Dacă ele n-ar valida, gate-ul ar cere
    frontendului să deseneze ceva ce contractul interzice."""
    validator = Draft202012Validator(view_json_schema())
    base = ROOT / "tests" / "fixtures" / "web_v2_golden"
    files = sorted(base.glob("*.json"))
    assert files, "nu există proiecții golden — projectorul nu e acoperit"
    for path in files:
        validator.validate(json.loads(path.read_text(encoding="utf-8")))


# ── 5. Completitudine ───────────────────────────────────────────────────────────────────────


def test_fixture_set_is_complete() -> None:
    """Orice `tests/fixtures/web_v2/*.json` trebuie să fie declarat în pachetul de contract."""
    on_disk = {
        f"tests/fixtures/web_v2/{p.name}"
        for p in sorted((ROOT / "tests" / "fixtures" / "web_v2").glob("*.json"))
    }
    declared = set(manifest_script.CONTRACT_FIXTURES)
    assert on_disk == declared, (
        f"fixture nedeclarat: {sorted(on_disk - declared)}; declarat inexistent: "
        f"{sorted(declared - on_disk)}"
    )


def test_manifest_points_at_the_single_thresholds_artifact() -> None:
    """Un singur artefact de praguri, validat în ambele repo-uri: dacă manifestul nu-l amprentează,
    frontendul ar putea citi alte praguri decât backendul."""
    pack = manifest_script.read_manifest()
    assert "thresholds_sha256" in pack
    assert pack["thresholds_sha256"] == manifest_script.normalized_sha256(
        manifest_script.THRESHOLDS_PATH
    )
    assert sc.manifest()["thresholds"] == "qa-suite/stage1/web-v2/gate-thresholds.json"


def test_hashing_is_platform_independent(tmp_path: Path) -> None:
    """Același conținut logic, CRLF vs LF, trebuie să dea același hash — altfel cross-repo-ul
    raportează drift fals între Windows (autocrlf) și Linux (CI)."""
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "a": 1\n}\n')
    crlf.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    assert manifest_script.normalized_sha256(lf) == manifest_script.normalized_sha256(crlf)
