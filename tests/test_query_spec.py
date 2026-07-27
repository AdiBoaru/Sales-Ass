"""NX-208 — contractul QuerySpec: separarea Runtime/Safe (D6) + invariantul de confidențialitate.

Dovada CENTRALĂ a cardului: `raw_query` NU poate ajunge în reprezentarea persistabilă/
telemetrizabilă (`SafeQuerySpec`) — garanție de TIP, nu de convenție. Pur (fără DB/LLM)."""

import pytest
from pydantic import ValidationError

from src.agent.query_spec import (
    Constraint,
    RuntimeQuerySpec,
    SafeQuerySpec,
    SafeVocabulary,
    band_label,
)

# String „PII-like" (nume + telefon) folosit ca test canary: dacă apare în serializarea Safe,
# invariantul e rupt.
_CANARY = "Ion Popescu 0722123456 strada Florilor 5"
_PHONE_NUM = 722123456  # ACELAȘI telefon, ca NUMĂR — „e număr, deci nu e PII" e fals


def _vocab() -> SafeVocabulary:
    """Vocabularul controlat tipic (fațete din cod + valori canonice + slug-uri + locale)."""
    return SafeVocabulary(
        facet_values={"concern": frozenset({"oily"})},
        numeric_bands={"price": (0, 50, 100, 150, 200, 300, 500, 1000)},
        bool_facets=frozenset({"fragrance_free"}),
        categories=frozenset({"seruri-pentru-ten", "apa-micelara"}),
        locales=frozenset({"ro"}),
    )


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
    """`to_safe()` e singura punte — textul liber (raw + numele referinței) nu ajunge în Safe."""
    safe = _runtime().to_safe(_vocab())
    dumped = safe.model_dump_json()
    assert _CANARY not in dumped
    assert "coral theory fresh" not in dumped.lower()  # numele referinței nu se persistă
    # slug-ul canonic (vocabular controlat) trece; prețul iese ca BANDĂ, nu ca număr brut
    assert safe.reference_categories == ("apa-micelara",)
    price = next(c for c in safe.constraints if c.facet == "price")
    assert price.value == "100-150"


def test_to_safe_without_vocabulary_is_fully_fail_closed():
    """Fără vocabular NU iese NIMIC: nici constrângeri (numele fațetei e el însuși un câmp de text),
    nici categorie, nici locale. Fail-closed pe toată suprafața, nu doar pe `value`."""
    safe = _runtime().to_safe()
    assert safe.constraints == ()
    assert safe.category is None
    assert safe.reference_categories == ()
    assert safe.locale is None
    assert _CANARY not in safe.model_dump_json()


def test_to_safe_redacts_unvalidated_values_keeps_canonical():
    """Valorile string neverificate contra vocabularului sunt REDACTATE la None (facet/op rămân
    pentru telemetrie); valoarea canonică trece; numericul trece sub o fațetă numerică."""
    rt = RuntimeQuerySpec(
        raw_query=_CANARY,
        normalized_query=_CANARY,
        search_text=_CANARY,
        constraints=(
            Constraint(facet="price", op="lte", value=120, strength="hard"),  # numeric declarat
            Constraint(facet="concern", op="contains", value="oily", strength="soft"),  # canonic
            Constraint(facet="concern", op="eq", value=_CANARY, strength="soft"),  # PII → None
        ),
    )
    safe = rt.to_safe(_vocab())
    assert _CANARY not in safe.model_dump_json()
    assert [(c.facet, c.value) for c in safe.constraints] == [
        ("price", "100-150"),
        ("concern", "oily"),
        ("concern", None),
    ]


def test_to_safe_slug_form_pii_does_not_pass():
    """Un slug-PII (`ion_popescu_0722123456`) NU trece — nu e într-un vocabular controlat, deci e
    redactat (un heuristic de „formă de token" ar fi lăsat un astfel de slug să treacă)."""
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
    safe = rt.to_safe(_vocab())
    assert "ion_popescu_0722123456" not in safe.model_dump_json()
    assert safe.category is None
    assert safe.reference_categories == ()
    assert safe.constraints[0].value is None


def test_to_safe_drops_constraint_with_unknown_facet_name():
    """Re-review #246: `facet` e un câmp string ca oricare altul — dacă poartă PII, redactarea
    valorii nu ajută. Fațetă necunoscută vocabularului → constrângerea e DROPATĂ integral."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        constraints=(
            Constraint(facet=_CANARY, op="eq", value=None, strength="hard"),
            Constraint(facet="ion_popescu_0722123456", op="eq", value="oily", strength="soft"),
            Constraint(facet="price", op="lte", value=120, strength="hard"),  # cunoscută → rămâne
        ),
    )
    safe = rt.to_safe(_vocab())
    dumped = safe.model_dump_json()
    assert _CANARY not in dumped
    assert "ion_popescu" not in dumped
    assert [(c.facet, c.value) for c in safe.constraints] == [("price", "100-150")]


def test_to_safe_drops_constraint_with_uncontrolled_op_strength_source():
    """`op`/`strength`/`source` sunt tot string-uri libere în contract → validate contra
    vocabularelor ÎNCHISE din cod; orice altceva = DROP (nu doar redactare de valoare)."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        constraints=(
            Constraint(facet="price", op=_CANARY, value=120, strength="hard"),
            Constraint(facet="price", op="lte", value=120, strength=_CANARY),
            Constraint(facet="price", op="lte", value=120, strength="hard", source=_CANARY),
            Constraint(facet="price", op="lte", value=120, strength="hard", source="derived"),
        ),
    )
    safe = rt.to_safe(_vocab())
    assert _CANARY not in safe.model_dump_json()
    assert len(safe.constraints) == 1
    assert safe.constraints[0].source == "derived"


def test_to_safe_numeric_pii_needs_declared_numeric_facet():
    """Un NUMĂR nu e sigur prin natura lui: sub o fațetă enum/bool valoarea numerică e redactată."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        constraints=(
            Constraint(facet="concern", op="eq", value=_PHONE_NUM, strength="soft"),
            Constraint(facet="fragrance_free", op="eq", value=_PHONE_NUM, strength="hard"),
            Constraint(facet="fragrance_free", op="eq", value=True, strength="hard"),
        ),
    )
    safe = rt.to_safe(_vocab())
    assert str(_PHONE_NUM) not in safe.model_dump_json()
    assert [(c.facet, c.value) for c in safe.constraints] == [
        ("concern", None),
        ("fragrance_free", None),
        ("fragrance_free", True),
    ]


def test_to_safe_never_emits_a_raw_number():
    """Re-review 2 #246: declararea fațetei ca numerică validează TIPUL, nu valoarea — un telefon e
    un număr valid. Deci numărul brut NU iese niciodată: iese banda, iar în afara domeniului
    declarat (un telefon nu e un preț) valoarea e redactată."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        constraints=(
            # ACELAȘI telefon, de data asta sub fațeta declarată NUMERICĂ (findingul Codex)
            Constraint(facet="price", op="lte", value=_PHONE_NUM, strength="hard"),
            Constraint(facet="price", op="lte", value=120, strength="hard"),
        ),
    )
    safe = rt.to_safe(_vocab())
    dumped = safe.model_dump_json()
    assert str(_PHONE_NUM) not in dumped  # telefonul nu iese nici sub o fațetă numerică
    assert "722" not in dumped  # nici măcar parțial (prefix/sufix)
    assert [(c.facet, c.value) for c in safe.constraints] == [("price", None), ("price", "100-150")]
    # niciun câmp din proiecție nu e un număr brut provenit din tur
    assert all(
        not isinstance(c.value, (int, float)) or isinstance(c.value, bool) for c in safe.constraints
    )


def test_band_label_is_deterministic_and_bounded():
    edges = (0, 50, 100, 150, 200, 300, 500, 1000)
    assert band_label(edges, 0) == "0-50"
    assert band_label(edges, 50) == "50-100"  # benzi semi-deschise [lo, hi)
    assert band_label(edges, 149.99) == "100-150"
    assert band_label(edges, 1000) == "500-1000"  # ultima bandă e închisă
    assert band_label(edges, 1000.01) is None  # peste domeniu → redactat
    assert band_label(edges, -1) is None  # sub domeniu → redactat
    assert band_label((), 100) is None  # fără lattice declarat → nimic nu iese


def test_to_safe_validates_intent_sort_locale():
    """`intent`/`sort`/`locale` sunt câmpuri string → doar din vocabulare controlate: intent/sort
    din cod, locale din `supported_locales`. Text liber în ele → None / default."""
    rt = RuntimeQuerySpec(
        raw_query="x",
        normalized_query="x",
        search_text="x",
        intent=_CANARY,
        sort=_CANARY,
        locale=_CANARY,
    )
    safe = rt.to_safe(_vocab())
    assert _CANARY not in safe.model_dump_json()
    assert safe.intent is None
    assert safe.sort == "relevance"
    assert safe.locale is None
    # locale necunoscut businessului (chiar dacă e un cod valid altundeva) → None
    assert (
        RuntimeQuerySpec(raw_query="x", normalized_query="x", search_text="x", locale="hu")
        .to_safe(_vocab())
        .locale
        is None
    )


def test_runtime_spec_not_serializable():
    """RuntimeQuerySpec nu expune nicio cale de serializare (fără model_dump/json) — trăiește
    doar în memoria turului. Unica ieșire e `to_safe()`."""
    rt = _runtime()
    assert not hasattr(rt, "model_dump")
    assert not hasattr(rt, "model_dump_json")
    assert not hasattr(rt, "json")


def test_to_safe_preserves_canonical_metadata():
    # cu vocabular controlat, slug-urile canonice + metadatele canonice trec
    safe = _runtime().to_safe(_vocab())
    assert safe.locale == "ro"
    assert safe.intent == "recommend"
    assert safe.category == "seruri-pentru-ten"
    assert safe.reference_categories == ("apa-micelara",)
    assert safe.schema_version == 1  # D3: prezent chiar dacă pilotul e ro-RO


def test_constraint_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Constraint.model_validate({"facet": "price", "op": "lte", "value": 1, "junk": True})
