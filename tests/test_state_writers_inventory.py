"""NX-327 pasul 0.5: testul care ține la zi inventarul scriitorilor de stare.

Trei preocupări, în ordinea din card:
  1. extractorul chiar VEDE fiecare formă 1-5 (+ alias + acces dinamic) -- pe un fixture
     sintetic, ca un extractor „orb" să nu poată produce un inventar gol și un test verde;
  2. fiecare scriitor găsit pe `src/` are o soartă DECLARATĂ în `state_writers_fate.json`,
     și nicio intrare declarată nu e moartă;
  3. `docs/STATE-WRITERS.md` e exact ce generează scriptul acum (`--check`, ca la NX-247).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import state_writers as sw

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "state_writers" / "sample.py"


def _find(result: sw.ExtractionResult, *, function: str, form: str | None = None):
    return [
        w for w in result.writers if w.function == function and (form is None or w.form == form)
    ]


@pytest.fixture(scope="module")
def fields() -> sw.FieldsConfig:
    return sw.load_fields()


@pytest.fixture(scope="module")
def fixture_result(fields: sw.FieldsConfig) -> sw.ExtractionResult:
    source = FIXTURE_PATH.read_text(encoding="utf-8")
    return sw.scan_source(source, "tests/fixtures/state_writers/sample.py", fields)


# ---------------------------------------------------------------------------------------------
# 1. Fixture-ul acoperă fiecare formă 1-5 + alias + unresolved (auto-test, scris ÎNTÂI)
# ---------------------------------------------------------------------------------------------


class TestFixtureForms:
    def test_form1_assign(self, fixture_result):
        hits = _find(fixture_result, function="form1_assign", form="assign")
        assert len(hits) == 1
        assert hits[0].raw_key == "pending_question"
        assert hits[0].field == "clarification"
        assert hits[0].path == "v1"

    def test_form1_alias_mutating_call(self, fixture_result):
        """Edge case din card: `s = ctx.state; s.constraints.update(x)` -> prins prin alias."""
        hits = _find(fixture_result, function="form1_alias_mutating_call", form="mutating_call")
        assert len(hits) == 1
        assert hits[0].raw_key == "constraints"
        assert hits[0].field == "needs_topic"

    def test_form1_subscript_assign(self, fixture_result):
        hits = _find(fixture_result, function="form1_subscript_assign", form="subscript_assign")
        assert len(hits) == 1
        assert hits[0].raw_key == "constraints"

    def test_form2_patch_subscript(self, fixture_result):
        hits = _find(fixture_result, function="form2_patch_subscript", form="patch_subscript")
        assert len(hits) == 1
        assert hits[0].raw_key == "active_search"
        assert hits[0].field == "active_search"

    def test_form2_patch_update(self, fixture_result):
        hits = _find(fixture_result, function="form2_patch_update", form="patch_update")
        assert len(hits) == 1
        assert hits[0].raw_key == "displayed_products"
        assert hits[0].field == "references"

    def test_form2_tool_result(self, fixture_result):
        """Edge case din card: `ToolResult(state_patch={"active_search": …})` -> prins."""
        hits = _find(fixture_result, function="form2_tool_result", form="tool_result_kwarg")
        assert len(hits) == 1
        assert hits[0].raw_key == "active_search"

    def test_form3_dict_literal(self, fixture_result):
        hits = _find(fixture_result, function="form3_dict_literal", form="dict_literal")
        assert len(hits) == 1
        assert hits[0].raw_key == "constraints"
        assert hits[0].field == "needs_topic"

    def test_form4_dataclasses_replace(self, fixture_result):
        """Edge case din card: `replace(state, active_search=None)` -> prins (forma 4)."""
        hits = _find(
            fixture_result, function="form4_dataclasses_replace", form="dataclasses_replace"
        )
        assert len(hits) == 1
        assert hits[0].raw_key == "active_search"
        assert hits[0].field == "active_search"
        assert hits[0].path == "v2"

    def test_form5_proposal_constructor(self, fixture_result):
        hits = _find(
            fixture_result, function="form5_proposal_constructor", form="proposal_constructor"
        )
        assert len(hits) == 1
        assert hits[0].raw_key == "set_need"
        assert hits[0].field == "needs_topic"
        assert hits[0].path == "v2"

    def test_form5_proposal_call_indirect(self, fixture_result):
        """Forma 5, a doua jumătate: apel către o funcție care CONSTRUIEȘTE propunerea,
        urmărit o singură dată prin numele funcției."""
        hits = _find(fixture_result, function="form5_proposal_call", form="proposal_call")
        assert len(hits) == 1
        assert hits[0].field == "needs_topic"

    def test_unresolved_setattr_dynamic(self, fixture_result):
        """Failure case din card: `setattr(ctx.state, name, v)` -> unresolved, nu se ignoră."""
        matches = [
            u
            for u in fixture_result.unresolved
            if u.function == "unresolved_setattr_dynamic" and u.reason == "setattr_dynamic"
        ]
        assert len(matches) == 1

    def test_a_blind_extractor_would_fail_this(self, fixture_result):
        """Un extractor orb (care nu vede nimic) ar produce un inventar gol și un test verde
        din întâmplare -- de asta cardul cere fixture-ul: pragul de mai jos ar pica pe un
        extractor gol, chiar dacă restul asertărilor de mai sus ar fi fost omise."""
        assert len(fixture_result.writers) >= 10


# ---------------------------------------------------------------------------------------------
# 2. Verificări punctuale pe cod real (Happy Path + Edge Cases din card)
# ---------------------------------------------------------------------------------------------


class TestRealCodeSpotChecks:
    def test_build_new_state_dict_literal_forms(self, fields: sw.FieldsConfig):
        """Happy path din card: `processor._build_new_state` -> rânduri pentru
        displayed_products, pending_question, constraints, asked_intents,
        search_constraints, active_search (forma 3)."""
        path = REPO_ROOT / "src" / "worker" / "processor.py"
        result = sw.scan_source(path.read_text(encoding="utf-8"), "src/worker/processor.py", fields)
        hits = _find(result, function="_build_new_state", form="dict_literal")
        raw_keys = {h.raw_key for h in hits}
        assert raw_keys == {
            "displayed_products",
            "pending_question",
            "constraints",
            "asked_intents",
            "search_constraints",
            "active_search",
        }

    def test_state_v2_serialize_replace_active_search(self, fields: sw.FieldsConfig):
        """Edge case din card: `replace(state, active_search=None)` în `state_v2.py` ⇒
        prins (forma 4). Pe codul real e într-o lambdă din lista de `shrink` a lui
        `serialize` -- dacă lambda ar fi tratată ca un scope opac, intrarea ar dispărea."""
        path = REPO_ROOT / "src" / "conversation" / "state_v2.py"
        result = sw.scan_source(
            path.read_text(encoding="utf-8"), "src/conversation/state_v2.py", fields
        )
        hits = [
            w
            for w in result.writers
            if w.form == "dataclasses_replace" and w.raw_key == "active_search"
        ]
        assert hits, "replace(s, active_search=None, ...) din shrink list-ul lui serialize()"

    def test_catalog_tools_tool_result_state_patch(self, fields: sw.FieldsConfig):
        """`ToolResult(state_patch={...})` real, în `src/tools/commerce_tools.py`."""
        path = REPO_ROOT / "src" / "tools" / "commerce_tools.py"
        result = sw.scan_source(
            path.read_text(encoding="utf-8"), "src/tools/commerce_tools.py", fields
        )
        hits = [w for w in result.writers if w.form == "tool_result_kwarg"]
        raw_keys = {h.raw_key for h in hits}
        assert {"cart", "cart_ref"} <= raw_keys


# ---------------------------------------------------------------------------------------------
# 3. Contractul de inventar: fate.json complet + fără intrări moarte + doc la zi
# ---------------------------------------------------------------------------------------------


class TestInventoryContract:
    def test_every_found_writer_has_a_declared_fate(self):
        result = sw.scan_src()
        fate = sw.load_fate()

        found_keys = result.fate_keys()
        declared_keys = set(fate.keys())

        missing = sorted(found_keys - declared_keys)
        assert not missing, (
            "Scriitori fara soarta declarata in scripts/state_writers_fate.json "
            "(adauga o intrare {file, function, field, why, fate} pentru fiecare):\n"
            + "\n".join(f"  - file={f!r} function={fn!r} field={fld!r}" for f, fn, fld in missing)
        )

    def test_no_dead_fate_entries(self):
        result = sw.scan_src()
        fate = sw.load_fate()

        declared_keys = set(fate.keys())
        found_keys = result.fate_keys()
        dead = sorted(declared_keys - found_keys)
        assert not dead, (
            "Intrari moarte in scripts/state_writers_fate.json (scriitorul nu mai exista -- "
            "sterge-le sau repara referinta):\n"
            + "\n".join(f"  - file={f!r} function={fn!r} field={fld!r}" for f, fn, fld in dead)
        )

    def test_fate_values_are_closed_vocabulary(self):
        fate = sw.load_fate()
        for entry in fate.values():
            assert entry.fate in sw.FATE_VALUES, f"fate necunoscut {entry.fate!r} la {entry.key}"
            assert entry.why.strip(), f"why gol pentru {entry.key}"

    def test_doc_matches_generator(self):
        fields = sw.load_fields()
        result = sw.scan_src()
        fate = sw.load_fate()
        expected = sw.generate_doc(result, fields, fate)
        actual = sw.DEFAULT_DOC_PATH.read_text(encoding="utf-8")
        assert actual == expected, (
            "docs/STATE-WRITERS.md nu corespunde cu ce generează scriptul -- "
            "ruleaza `python scripts/state_writers.py` si comite diff-ul."
        )

    def test_cli_check_mode_matches(self):
        assert sw.main(["--check"]) == 0
