"""IZI-anti-drift — `search_products_tool` moștenește categoria/concerns sesiunii active când o
rafinare („mai ieftin") nu le re-specifică. Repară driftul „mai ieftin → mască/ser/toner" pe
calea-model / typo (regex-ul cheaper ratează „mai ifetin"), FĂRĂ wordlist. DB stubuită."""

import pytest

from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import search_products_tool
from src.worker.runner import PipelineDeps


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _ctx_with_session(*, category, concerns):
    """Sesiune activă pe „creme hidratante / ten uscat" (turul 1). fp irelevant (cădem pe căutare
    nouă fiindcă sort_mode diferă), dar `filters` ține categoria/concerns de moștenit."""
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="ceva mai ifetin"),
        conversation_id="conv",
        state=ConversationState(
            active_search={
                "filters": {
                    "query": "crema hidratanta ten uscat",
                    "category": category,
                    "brand": None,
                    "concerns": concerns,
                    "price_max": None,
                    "sort_mode": "relevance",
                    "in_stock_only": False,
                },
                "pool": ["x1", "x2"],
                "cursor": 2,
                "fp": "OLD_FP_RELEVANCE",
                "page": 0,
            }
        ),
    )
    return ctx


@pytest.fixture
def _capture_lexical(monkeypatch):
    seen = {}

    async def fake_lexical(conn, business_id, *, query_text, price_max, concerns, category,
                           brand, sort_mode, in_stock_only, pool):
        seen["category"] = category
        seen["concerns"] = concerns
        return [{"id": "p-cheap", "name": "Crema ieftină", "price": 19.99}]

    async def no_embeddings(conn, business_id):
        return False

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    # concerns mapate identitate (fără DomainPack în test): „dry" rămâne „dry".
    monkeypatch.setattr(ct, "map_concerns", lambda dp, c: ([str(x) for x in c] if c else None))
    return seen


async def test_cheaper_inherits_category_and_concerns(_capture_lexical):
    # Rafinare „mai ifetin" (price_asc), fără categorie/concerns noi → moștenește-le din sesiune.
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "ceva mai ieftin", "sort_mode": "price_asc"},
    )
    assert _capture_lexical["category"] == "creme-hidratante"  # NU drift pe alt raft
    assert _capture_lexical["concerns"] == ["dry"]
    ev = [e for e in ctx.events if e.type == "search_filter_inherited"]
    assert ev and set(ev[0].properties["fields"]) == {"category", "concerns"}


async def test_explicit_category_overrides_inheritance(_capture_lexical):
    # Categoria NOUĂ a modelului (seruri) câștigă; dar concern-ul (ten uscat = atribut al userului)
    # se moștenește — ladder-ul îl relaxează dacă supra-constrânge, deci nu strică.
    ctx = _ctx_with_session(category="creme-hidratante", concerns=["dry"])
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "vreau un ser", "category": "seruri", "sort_mode": "relevance"},
    )
    assert _capture_lexical["category"] == "seruri"  # categoria nouă NU e suprascrisă de moștenire
    ev = [e for e in ctx.events if e.type == "search_filter_inherited"]
    assert ev and ev[0].properties["fields"] == ["concerns"]  # DOAR concern-ul moștenit


async def test_no_session_no_inheritance(_capture_lexical):
    # Fără sesiune activă (prima căutare) → nimic de moștenit, fără event.
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="crema ieftina"),
        conversation_id="conv",
        state=ConversationState(),
    )
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema ieftina", "sort_mode": "price_asc"},
    )
    assert _capture_lexical["category"] is None
    assert not [e for e in ctx.events if e.type == "search_filter_inherited"]
