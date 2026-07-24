"""NX-208 — înțelegerea deterministă a interogării (`build_query_spec`).

Pur (fără DB/LLM): folosește pack-ul default beauty_salon (are `query_expansions` + concern_map
extins). Verifică pattern-urile de limbă (preț, referință, fără-parfum) + expandarea."""

from src.agent.query_rewrite import build_query_spec
from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, Route, TurnContext
from src.worker.stages.triage import _emit_query_spec_shadow


def _pack():
    return load_domain_pack(
        BusinessConfig(id="b", slug="s", name="n", vertical="beauty_salon", settings={})
    )


def _facets(spec):
    return {(c.facet, c.op, c.value, c.strength) for c in spec.constraints}


def test_price_extracted_as_hard_constraint():
    spec = build_query_spec("ceva bun sub 120", _pack())
    assert ("price", "lte", 120.0, "hard") in _facets(spec)


def test_price_from_budget_phrasing():
    spec = build_query_spec("machiaj de nuntă, buget 200", _pack())
    assert ("price", "lte", 200.0, "hard") in _facets(spec)


def test_reference_cheaper_sets_sort_and_reference():
    spec = build_query_spec(
        "vreau ceva ca Coral Theory Fresh Apă micelară, dar mai accesibil", _pack()
    )
    assert spec.sort == "price_asc"
    assert spec.intent == "find_alternative"
    assert spec.reference_terms  # a extras descriptorul referinței
    assert "coral theory fresh" in spec.reference_terms[0]


def test_fragrance_free_positive_facet():
    spec = build_query_spec("vreau produse fără parfum pentru rutină", _pack())
    assert ("fragrance_free", "eq", True, "hard") in _facets(spec)


def test_concern_scan_anti_shine():
    # „mă lucesc" (anti-luciu) → concern oily (high-confidence, D6/concern_map).
    spec = build_query_spec("ceva să nu mă lucesc peste zi", _pack())
    assert ("concern", "contains", "oily", "soft") in _facets(spec)


def test_vocabulary_expansion_appended_to_search_text():
    # search_text-ul expandat conține termenii canonici (hrănesc lexical + semantic).
    spec = build_query_spec("ceva să nu mă lucesc, să reziste pe căldură", _pack())
    st = spec.search_text.lower()
    assert "matifiant" in st and "mat" in st
    assert "rezistent" in st
    # raw-ul e păstrat intact (cele 3 reprezentări coexistă)
    assert spec.raw_query == "ceva să nu mă lucesc, să reziste pe căldură"


def test_compare_intent_detected():
    spec = build_query_spec("care e diferența între produsul A și produsul B?", _pack())
    assert spec.intent == "compare"


def test_no_domain_pack_degrades_gracefully():
    # Fără pack: doar pattern-urile de limbă (preț), zero expandare de vocabular. Nu crapă (P6).
    spec = build_query_spec("ceva să nu mă lucesc sub 120", None)
    assert ("price", "lte", 120.0, "hard") in _facets(spec)
    assert spec.search_text == spec.normalized_query  # niciun termen adăugat
    assert all(c.facet != "concern" for c in spec.constraints)


def test_plain_query_unchanged():
    # Interogare fără trigger → search_text == normalized, fără constrângeri derivate.
    spec = build_query_spec("recomandă-mi un fond de ten", _pack())
    assert spec.sort == "relevance"
    assert not spec.reference_terms
    assert spec.search_text == spec.normalized_query


# --- shadow emit (D6/D11): telemetrie fără PII, gated ------------------------

_PII_RAW = "sunt Maria, 0722123456, vreau ceva să nu mă lucesc sub 120"


def _ctx(body: str) -> TurnContext:
    biz = BusinessConfig(id="b", slug="s", name="n", vertical="beauty_salon", settings={})
    biz.domain_pack = load_domain_pack(biz)
    return TurnContext(
        turn_id="t",
        business=biz,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        language="ro",
    )


def test_shadow_disabled_by_default_emits_nothing(monkeypatch):
    monkeypatch.setattr(get_settings(), "query_spec_shadow_enabled", False)
    ctx = _ctx(_PII_RAW)
    _emit_query_spec_shadow(ctx, Route.SALES)
    assert not [e for e in ctx.events if e.type == "query_spec_shadow"]


def test_shadow_enabled_emits_no_pii(monkeypatch):
    monkeypatch.setattr(get_settings(), "query_spec_shadow_enabled", True)
    ctx = _ctx(_PII_RAW)
    _emit_query_spec_shadow(ctx, Route.SALES)
    evs = [e for e in ctx.events if e.type == "query_spec_shadow"]
    assert len(evs) == 1
    blob = repr(evs[0].properties)
    assert "0722123456" not in blob and "maria" not in blob.lower()  # zero raw/PII
    assert evs[0].properties["intent"] == "recommend"
    assert "price" in evs[0].properties["facets"]  # constrângeri canonice OK


def test_shadow_skips_non_sales_routes(monkeypatch):
    monkeypatch.setattr(get_settings(), "query_spec_shadow_enabled", True)
    ctx = _ctx(_PII_RAW)
    _emit_query_spec_shadow(ctx, Route.SIMPLE)
    assert not [e for e in ctx.events if e.type == "query_spec_shadow"]
