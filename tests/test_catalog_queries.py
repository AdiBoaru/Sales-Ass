"""NX-78 — query-urile noi de prompt din catalog (`list_category_names` / `list_routing_aliases`).

Fără DB reală: un `conn` fals captează SQL-ul + params și întoarce rânduri scriptate. Verificăm
CONTRACTUL SQL (izolare `business_id = $1`, `status='approved'` la aliase = P9, top-level la
categorii, `order by` determinist pt prefixul de cache) + maparea rândurilor. Filtrarea efectivă
o face Postgres; aici garantăm că filtrul e în interogare (nu rutăm pe candidați neaprobați).
"""


class FakeConn:
    """Conn asyncpg minimal: reține SQL-ul + params, întoarce rândurile scriptate."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = ""
        self.params = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return self._rows


async def test_list_category_names_top_level_scoped():
    from src.db.queries.catalog import list_category_names

    conn = FakeConn([{"name": "Creme"}, {"name": "Parfumuri"}])
    out = await list_category_names(conn, "biz-1")

    assert out == ["Creme", "Parfumuri"]  # maparea r["name"]
    assert "business_id = $1" in conn.sql  # izolare (P7)
    assert "parent_id is null" in conn.sql  # DOAR categorii top-level
    assert "order by name" in conn.sql  # determinist → prefix de cache stabil
    assert conn.params[0] == "biz-1"


async def test_list_routing_aliases_only_approved():
    from src.db.queries.catalog import list_routing_aliases

    conn = FakeConn([{"phrase_norm": "crema fata", "target": "creme"}])
    out = await list_routing_aliases(conn, "biz-1")

    assert out == [("crema fata", "creme")]  # (phrase_norm, target)
    assert "status = 'approved'" in conn.sql  # P9: candidații NU ajung în prompt
    assert "business_id = $1" in conn.sql  # izolare (P7)
    assert conn.params[0] == "biz-1"
    assert conn.params[1] == 20  # limită implicită (hint scurt, nu listă lungă)
