"""NX-208 — contractul QuerySpec: separarea Runtime/Safe (D6) + invariantul de confidențialitate.

Dovada CENTRALĂ a cardului: `raw_query` NU poate ajunge în reprezentarea persistabilă/
telemetrizabilă (`SafeQuerySpec`) — garanție de TIP, nu de convenție. Pur (fără DB/LLM)."""

import pytest
from pydantic import ValidationError

from src.agent.query_spec import Constraint, RuntimeQuerySpec, SafeQuerySpec

# String „PII-like" (nume + telefon) folosit ca test canary: dacă apare în serializarea Safe,
# invariantul e rupt.
_CANARY = "Ion Popescu 0722123456 strada Florilor 5"


def _runtime(raw: str = _CANARY) -> RuntimeQuerySpec:
    return RuntimeQuerySpec(
        raw_query=raw,
        normalized_query=raw.lower(),
        search_text=f"{raw} matifiant",
        intent="recommend",
        category="seruri-pentru-ten",
        constraints=(Constraint(facet="price", op="lte", value=120, strength="hard"),),
        reference_terms=("coral theory fresh apa micelara",),  # nume de brand din raw
        reference_categories=("apa-micelara",),
        sort="relevance",
        locale="ro",
    )


def test_safe_spec_has_no_free_text_fields():
    """SafeQuerySpec NU are câmpuri de text liber — `raw_query` n-are unde să intre (structural)."""
    forbidden = {"raw_query", "normalized_query", "search_text", "reference_terms"}
    assert forbidden.isdisjoint(SafeQuerySpec.model_fields)


def test_safe_spec_rejects_extra_raw_query():
    """`extra=forbid`: încercarea de a strecura raw_query în Safe e o eroare de validare."""
    with pytest.raises(ValidationError):
        SafeQuerySpec.model_validate({"raw_query": _CANARY})


def test_to_safe_drops_raw_and_reference_names():
    """`to_safe()` e singura punte — DROPează tot textul liber (raw + numele referinței)."""
    safe = _runtime().to_safe()
    dumped = safe.model_dump_json()
    assert _CANARY not in dumped
    assert "coral theory fresh" not in dumped.lower()  # numele referinței nu se persistă
    # dar constrângerile canonice + categoriile de referință (slug) SUPRAVIEȚUIESC
    assert "apa-micelara" in dumped
    assert any(c.facet == "price" for c in safe.constraints)


def test_runtime_spec_not_serializable():
    """RuntimeQuerySpec nu expune nicio cale de serializare (fără model_dump/json) — trăiește
    doar în memoria turului. Unica ieșire e `to_safe()`."""
    rt = _runtime()
    assert not hasattr(rt, "model_dump")
    assert not hasattr(rt, "model_dump_json")
    assert not hasattr(rt, "json")


def test_to_safe_preserves_canonical_metadata():
    safe = _runtime().to_safe()
    assert safe.locale == "ro"
    assert safe.category == "seruri-pentru-ten"
    assert safe.reference_categories == ("apa-micelara",)
    assert safe.schema_version == 1  # D3: prezent chiar dacă pilotul e ro-RO


def test_constraint_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Constraint.model_validate({"facet": "price", "op": "lte", "value": 1, "junk": True})
