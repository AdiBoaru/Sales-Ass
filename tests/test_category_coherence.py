"""NX-167 (A) — filtrarea de categorie pe arbore (product_category_map + descendenți `path`).

Teste de CONTRACT pe SQL (ca `test_catalog_queries`): fără DB reală, un `FakeConn` captează SQL-ul.
Verificăm că predicatul de categorie e BYTE-IDENTIC cu vechiul cod când flag-ul e OFF, și că devine
un `exists(...)` pe arbore (primary SAU product_category_map; categoria cerută SAU un descendent)
când e ON. Filtrarea efectivă o face Postgres — aici garantăm doar contractul SQL.
"""

from src.config import get_settings


class FakeConn:
    """Conn asyncpg minimal: reține SQL-ul + params, întoarce rânduri scriptate (implicit gol)."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.sql = ""
        self.params = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return self._rows


def _placeholder():
    params: list = []

    def placeholder(v):
        params.append(v)
        return f"${len(params)}"

    return placeholder, params


# --- _category_clause direct (contractul predicatului) --------------------------------------


def test_category_clause_off_is_byte_identical(monkeypatch):
    from src.db.queries.catalog import _category_clause

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", False)
    placeholder, params = _placeholder()
    sql = _category_clause("machiaj", placeholder)

    # forma VECHE exactă: două placeholder-e, match pe slug/nume al primary_category_id (alias c)
    assert sql == "(lower(c.slug) = lower($1) or lower(c.name) = lower($2))"
    assert params == ["machiaj", "machiaj"]


def test_category_clause_on_matches_tree(monkeypatch):
    from src.db.queries.catalog import _category_clause

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", True)
    placeholder, params = _placeholder()
    sql = _category_clause("machiaj", placeholder)

    assert sql.startswith("exists (select 1 from categories reqc")
    assert "product_category_map m" in sql  # match și pe map, nu doar primary
    assert "sub.path like reqc.path || '/%'" in sql  # descendenți (materialized path)
    assert "sub.id = p.primary_category_id" in sql  # SAU pe primary
    assert "reqc.business_id = p.business_id" in sql  # corelat pe tenant (P7)
    assert params == ["machiaj"]  # UN singur placeholder, reutilizat de 2 ori
    # ON folosește propriul alias `reqc` (nu `c` din SELECT) → sigur pe calea semantică
    assert "lower(reqc.slug)" in sql and "lower(reqc.name)" in sql


# --- wiring end-to-end prin cele 3 funcții de search ----------------------------------------


async def test_lexical_wires_tree_clause_when_on(monkeypatch):
    from src.db.queries.catalog import search_products_lexical

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", True)
    conn = FakeConn()
    await search_products_lexical(conn, "biz-1", "fond de ten", category="machiaj")

    assert "product_category_map m" in conn.sql
    assert "sub.path like reqc.path" in conn.sql
    assert "biz-1" in conn.params


async def test_lexical_old_form_when_off(monkeypatch):
    from src.db.queries.catalog import search_products_lexical

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", False)
    conn = FakeConn()
    await search_products_lexical(conn, "biz-1", "fond de ten", category="machiaj")

    assert "lower(c.slug) = lower(" in conn.sql  # forma veche
    assert "product_category_map m" not in conn.sql  # arborele NU se activează


async def test_sql_only_search_wires_tree_clause_when_on(monkeypatch):
    from src.db.queries.catalog import search_products

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", True)
    conn = FakeConn()
    await search_products(conn, "biz-1", category="machiaj")

    assert "product_category_map m" in conn.sql
    assert "sub.path like reqc.path" in conn.sql


async def test_semantic_wires_tree_clause_when_on(monkeypatch):
    from src.db.queries.catalog import search_products_semantic

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", True)
    conn = FakeConn()
    await search_products_semantic(conn, "biz-1", [0.0] * 1536, category="machiaj")

    assert "product_category_map m" in conn.sql
    assert "sub.path like reqc.path" in conn.sql


# --- NX-167 (B): gardă „no off-category cards" (category_dropped) -----------------------------


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _fresh_ctx(body):
    from src.models import (
        BusinessConfig,
        Contact,
        ConversationState,
        InboundMessage,
        TurnContext,
    )

    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )


def _stub_dropping_search(monkeypatch):
    """Scriptează retrievalul ca să FORȚEZE category-drop: gol când categoria e cerută (strict),
    un produs off-category după ce ladder-ul renunță la categorie (category=None) → relaxat."""
    from src.tools import catalog_tools as ct

    async def fake_lexical(
        conn,
        business_id,
        *,
        query_text,
        price_max,
        concerns,
        category,
        brand,
        sort_mode,
        in_stock_only,
        pool,
        **kwargs,
    ):
        if category:  # treapta strictă pe categorie → nimic
            return []
        return [{"id": "hair-1", "name": "Accesoriu par", "price": 9.99}]  # off-category

    async def no_embeddings(conn, business_id):
        return False

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "map_concerns", lambda dp, c: ([str(x) for x in c] if c else None))


async def test_offcategory_guard_suppresses_when_category_dropped(monkeypatch):
    from src.tools.catalog_tools import search_products_tool
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "search_offcategory_guard_enabled", True)
    _stub_dropping_search(monkeypatch)

    ctx = _fresh_ctx("vreau makeup")
    res = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "machiaj", "category": "machiaj"},
    )

    assert res.products == []  # cardurile off-category sunt SUPRIMATE
    assert "«machiaj»" in res.llm_view  # semnal de clarificare cu categoria cerută
    assert [e for e in ctx.events if e.type == "offcategory_suppressed"]
    assert "active_search" not in ctx.state_patch  # sesiunea nu paginează gunoiul suprimat


async def test_offcategory_guard_off_keeps_cards(monkeypatch):
    from src.tools.catalog_tools import search_products_tool
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "search_offcategory_guard_enabled", False)
    _stub_dropping_search(monkeypatch)

    ctx = _fresh_ctx("vreau makeup")
    res = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "machiaj", "category": "machiaj"},
    )

    assert res.products  # OFF → comportamentul vechi: cardurile off-category rămân (cu disclosure)
    assert res.relevance is not None and res.relevance.category_dropped is True
    assert not [e for e in ctx.events if e.type == "offcategory_suppressed"]
