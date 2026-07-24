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
    """`to_safe()` e singura punte — textul liber (raw + numele referinței) nu ajunge în Safe.
    Fără `allowed_values`, niciun string nu iese (fail-closed); numerele rămân."""
    safe = _runtime().to_safe()
    dumped = safe.model_dump_json()
    assert _CANARY not in dumped
    assert "coral theory fresh" not in dumped.lower()  # numele referinței nu se persistă
    assert "apa-micelara" not in dumped  # fără vocabular permis → dropat (fail-closed)
    # facet-ul + valoarea numerică a constrângerii de preț supraviețuiesc (telemetrie)
    price = next(c for c in safe.constraints if c.facet == "price")
    assert price.value == 120


def test_to_safe_redacts_unvalidated_strings_keeps_numbers():
    """Review #246: valorile string neverificate contra vocabularului sunt REDACTATE la None (nu
    dropăm constrângerea — facet/op rămân); numărul/boolean-ul rămân."""
    rt = RuntimeQuerySpec(
        raw_query=_CANARY,
        normalized_query=_CANARY,
        search_text=_CANARY,
        constraints=(
            Constraint(facet="price", op="lte", value=120, strength="hard"),  # număr → rămâne
            Constraint(facet="concern", op="contains", value="oily", strength="soft"),  # str
            Constraint(facet="note", op="eq", value=_CANARY, strength="soft"),  # PII
        ),
    )
    # fără allowed_values → toate string-urile → None (dar facet keys rămân)
    safe = rt.to_safe()
    assert _CANARY not in safe.model_dump_json()
    byf = {c.facet: c.value for c in safe.constraints}
    assert byf == {"price": 120, "concern": None, "note": None}
    # cu vocabular controlat → doar valoarea permisă trece; PII rămâne redactat
    safe2 = rt.to_safe(allowed_values={"concern": {"oily"}})
    byf2 = {c.facet: c.value for c in safe2.constraints}
    assert byf2 == {"price": 120, "concern": "oily", "note": None}


def test_to_safe_slug_form_pii_does_not_pass():
    """Review re-review #246: un slug-PII (`ion_popescu_0722123456`) NU trece — nu e într-un
    vocabular controlat, deci e redactat (heuristicul de token ar fi lăsat un astfel de slug)."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        category="ion_popescu_0722123456",
        constraints=(
            Constraint(facet="concern", op="eq", value="ion_popescu_0722123456", strength="hard"),
        ),
        reference_categories=("ion_popescu_0722123456",),
    )
    safe = rt.to_safe(allowed_values={"concern": {"oily"}, "category": {"seruri-pentru-ten"}})
    assert "ion_popescu_0722123456" not in safe.model_dump_json()
    assert safe.category is None
    assert safe.reference_categories == ()
    assert safe.constraints[0].value is None


def test_runtime_spec_not_serializable():
    """RuntimeQuerySpec nu expune nicio cale de serializare (fără model_dump/json) — trăiește
    doar în memoria turului. Unica ieșire e `to_safe()`."""
    rt = _runtime()
    assert not hasattr(rt, "model_dump")
    assert not hasattr(rt, "model_dump_json")
    assert not hasattr(rt, "json")


def test_to_safe_preserves_canonical_metadata():
    # cu vocabular controlat, slug-urile canonice trec; metadatele non-string rămân mereu
    safe = _runtime().to_safe(allowed_values={"category": {"seruri-pentru-ten", "apa-micelara"}})
    assert safe.locale == "ro"
    assert safe.category == "seruri-pentru-ten"
    assert safe.reference_categories == ("apa-micelara",)
    assert safe.schema_version == 1  # D3: prezent chiar dacă pilotul e ro-RO


def test_constraint_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Constraint.model_validate({"facet": "price", "op": "lte", "value": 1, "junk": True})
