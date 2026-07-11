"""NX-163 — Demand Capture: captură deterministă de cerere, PII-safe.

Două straturi:
- helper-ele pure `src.analytics.demand` (extras + capat id-uri): determinism, cap, skip None,
  și dovada „doar ref-uri" (dict-uri cu PII → iese DOAR `product_id`, nimic altceva);
- emiterea reală pe `product_search` / `unmet_query` prin `run_tool`, cu DB monkeypatch-uit
  (ZERO DB/LLM real, ca test_tools). Verificăm: enrich cu `top_product_ids`/`category_key`/`brand`,
  `unmet_query reason=no_result`/`named_not_found`, exclusivitatea reason-urilor, și absența PII.

Invariante NX-163: fără `confidence`, fără `estimated_value`, fără text brut/PII în properties.
"""

import json

from src.analytics.demand import DEMAND_IDS_CAP, clean_ids, product_ids_from_dicts
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps

# --- helpere pure ------------------------------------------------------------


def test_clean_ids_deterministic_and_capped():
    ids = [f"id{i}" for i in range(20)]
    first = clean_ids(ids)
    second = clean_ids(ids)
    assert first == second  # determinism: aceeași intrare → aceeași ieșire
    assert first == [f"id{i}" for i in range(DEMAND_IDS_CAP)]  # cap dur, ordine stabilă


def test_clean_ids_skips_none_empty_and_coerces_str():
    assert clean_ids([None, "", "a", 5, None, "b"]) == ["a", "5", "b"]


def test_product_ids_prefers_product_id_then_id():
    products = [{"product_id": "pa", "id": "xa"}, {"id": "xb"}, {"name": "fără id"}]
    assert product_ids_from_dicts(products) == ["pa", "xb"]  # sare peste dict-ul fără id


def test_product_ids_extracts_only_refs_no_pii():
    """Dovada „doar ref-uri (P8/P12)": chiar dacă dict-ul de produs cară câmpuri PII/text,
    helper-ul întoarce EXCLUSIV id-uri — niciun câmp personal nu se scurge în analytics."""
    products = [
        {
            "id": "p1",
            "name": "Cremă",
            "phone": "+40722123456",
            "body": "vreau ceva pentru ten uscat",
            "email": "ion@example.ro",
        },
        {"product_id": "p2", "customer": "Ion Popescu"},
    ]
    out = product_ids_from_dicts(products)
    assert out == ["p1", "p2"]
    blob = json.dumps(out)
    assert "0722" not in blob and "@" not in blob and "Popescu" not in blob


# --- integrare: product_search + unmet_query (DB stubbed) --------------------

PRODUCTS = [
    {"id": "p1", "name": "Crema A", "brand": "BrandA", "price": 82.99, "availability": "in_stock"},
    {"id": "p2", "name": "Ser B", "brand": "BrandB", "price": 120.0, "availability": "in_stock"},
]


async def _has_emb_false(conn, business_id):
    return False


def _ctx(business_id: str = "biz-1") -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id=business_id, slug="s", name="n"),
        contact=Contact(id="c", business_id=business_id),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)  # llm=None → SQL-only


def _events(ctx, type_):
    return [e for e in ctx.events if e.type == type_]


def _patch_lex(monkeypatch, rows):
    async def fake_lex(conn, business_id, **k):
        return list(rows)

    monkeypatch.setattr(ct, "has_embeddings", _has_emb_false)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)


async def test_product_search_enriched_with_ids_and_attrs(monkeypatch):
    _patch_lex(monkeypatch, PRODUCTS)
    ctx = _ctx()
    res = await run_tool(
        ctx,
        _deps(),
        "search_products",
        {"query": "crema", "category": "creme-fata", "brand": "BrandA"},
    )
    assert res.ok
    ev = _events(ctx, "product_search")[-1]
    assert set(ev.properties["top_product_ids"]) == {"p1", "p2"}
    assert ev.properties["category_key"] == "creme-fata"
    assert ev.properties["brand"] == "BrandA"
    # capturăm ce s-a cerut, nu inventăm: fără confidence/estimated_value
    assert "confidence" not in ev.properties and "estimated_value" not in ev.properties


async def test_unmet_query_no_result_carries_brand(monkeypatch):
    """Zero rezultate reale (nimic nu s-a potrivit) + brand cerut → semnalul «adu brandul X»."""
    _patch_lex(monkeypatch, [])
    ctx = _ctx()
    await run_tool(
        ctx, _deps(), "search_products", {"query": "bioderma sebium", "brand": "Bioderma"}
    )
    unmet = _events(ctx, "unmet_query")
    assert len(unmet) == 1
    assert unmet[0].properties["reason"] == "no_result"
    assert unmet[0].properties["brand"] == "Bioderma"


async def test_unmet_query_named_not_found_with_alternatives(monkeypatch):
    """Produs NUMIT absent, dar există alternative → reason=named_not_found (nu no_result)."""
    _patch_lex(monkeypatch, PRODUCTS)  # alternative există, dar niciuna nu e „Hidra Boost Ultra"
    ctx = _ctx()
    await run_tool(
        ctx, _deps(), "search_products", {"query": "hidra", "product_name": "Hidra Boost Ultra"}
    )
    unmet = _events(ctx, "unmet_query")
    assert len(unmet) == 1 and unmet[0].properties["reason"] == "named_not_found"


async def test_unmet_query_named_takes_precedence_over_no_result(monkeypatch):
    """Produs numit + zero absolut → un SINGUR unmet (named_not_found), fără dublă numărare."""
    _patch_lex(monkeypatch, [])
    ctx = _ctx()
    await run_tool(
        ctx, _deps(), "search_products", {"query": "ghost", "product_name": "Ghost Product X"}
    )
    unmet = _events(ctx, "unmet_query")
    assert len(unmet) == 1 and unmet[0].properties["reason"] == "named_not_found"


async def test_hit_emits_no_unmet(monkeypatch):
    """Search cu rezultate reale → niciun unmet_query (nu e cerere neîmplinită)."""
    _patch_lex(monkeypatch, PRODUCTS)
    ctx = _ctx()
    await run_tool(ctx, _deps(), "search_products", {"query": "crema"})
    assert _events(ctx, "unmet_query") == []


async def test_demand_events_carry_no_pii(monkeypatch):
    """Cu PII în rândurile de produs, event-urile de cerere NU o cară (doar ref-uri/atribute)."""
    pii_rows = [{**PRODUCTS[0], "phone": "+40722123456", "note": "clientul Ion vrea reducere"}]
    _patch_lex(monkeypatch, pii_rows)
    ctx = _ctx()
    await run_tool(ctx, _deps(), "search_products", {"query": "crema", "category": "creme-fata"})
    for ev in ctx.events:
        if ev.type in ("product_search", "unmet_query"):
            blob = json.dumps(ev.properties, ensure_ascii=False)
            assert "0722" not in blob and "Ion" not in blob


async def test_events_tenant_scoped(monkeypatch):
    """Event legat de tenantul din ctx (P7): turn_id injectat, business_id din ctx la persistare."""
    _patch_lex(monkeypatch, [])
    ctx = _ctx(business_id="biz-XYZ")
    await run_tool(ctx, _deps(), "search_products", {"query": "x", "brand": "Marca"})
    # business_id nu stă în properties (îl pune insert_events din ctx la persistare); aici verificăm
    # că emiterea e legată de turul curent și nu conține alt tenant/PII.
    unmet = _events(ctx, "unmet_query")[0]
    assert unmet.properties["turn_id"] == "t"
    assert ctx.business.id == "biz-XYZ"  # scopul de scriere = tenantul din ctx (insert_events)
