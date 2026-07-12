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
