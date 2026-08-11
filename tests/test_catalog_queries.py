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


# --- NX-118: _row_to_product decode variants (codec str/list/None/malformed) ---


def test_row_to_product_decodes_jsonb_str():
    from src.db.queries.catalog import _row_to_product

    out = _row_to_product({"id": "p1", "variants": '[{"id": "v1", "price": 9.5}]'})
    assert out["variants"] == [{"id": "v1", "price": 9.5}]


def test_row_to_product_passes_list_through():
    from src.db.queries.catalog import _row_to_product

    out = _row_to_product({"id": "p1", "variants": [{"id": "v1"}]})
    assert out["variants"] == [{"id": "v1"}]


def test_row_to_product_null_and_malformed_to_empty():
    from src.db.queries.catalog import _row_to_product

    assert _row_to_product({"id": "p1", "variants": None})["variants"] == []
    assert _row_to_product({"id": "p1", "variants": "{not json"})["variants"] == []


def test_row_to_product_without_variants_key_untouched():
    from src.db.queries.catalog import _row_to_product

    assert "variants" not in _row_to_product({"id": "p1", "price": 10.0})


def test_row_to_product_decodes_attributes():
    from src.db.queries.catalog import _row_to_product

    out = _row_to_product({"id": "p1", "attributes": '{"key_ingredients": ["acid hialuronic"]}'})
    assert out["attributes"] == {"key_ingredients": ["acid hialuronic"]}
    assert _row_to_product({"id": "p1", "attributes": None})["attributes"] == {}


# --- Tier 2b p2: _feature_clause (filtru de feature normalizat, chei parametrizate) ------------


def test_feature_clause_normalized_and_parameterized():
    from src.db.queries.catalog import _feature_clause

    params: list = []

    def placeholder(v):
        params.append(v)
        return f"${len(params)}"

    sql = _feature_clause(("key_ingredients", "concerns"), ["niacinamida"], placeholder)
    assert "jsonb_array_elements_text" in sql  # expandă array-urile
    assert "translate(lower(fe), 'ăâîșț', 'aaist')" in sql  # match NORMALIZAT (RO)
    assert "||" in sql  # uniunea fațetelor căutabile
    assert sql.count("case when jsonb_typeof") == 2  # o expresie array / cheie
    # chei PARAMETRIZATE (safe) + valorile la final
    assert params == ["key_ingredients", "concerns", ["niacinamida"]]


# --- NX-226: scala rangului lexical (ts_rank_cd vs similarity) --------------------------------
#
# CI n-are Postgres: aici verificăm CONTRACTUL SQL (ce expresie ajunge în `ORDER BY`, ce NU se
# schimbă în `WHERE`) + semantica formulei, replicată numeric din ACELEAȘI constante. Executantul
# rămâne Postgres; testele fixează ce i se cere.

_OLD_RANK = (
    "ts_rank_cd(p.search_tsv, websearch_to_tsquery('simple', ro_unaccent($2)))"
    " + similarity(ro_unaccent(p.name), ro_unaccent($2))"
)


def _order_clause_of(sql: str) -> str:
    """Clauza de sortare de NIVEL SUPERIOR (ultima; lateralele au propriile `order by`)."""
    return sql[sql.rindex(" order by ") :]


async def _lexical_sql(monkeypatch, *, v2: bool, **kwargs) -> str:
    from src.config import get_settings
    from src.db.queries.catalog import search_products_lexical

    monkeypatch.setattr(get_settings(), "lexical_rank_v2_enabled", v2)
    conn = FakeConn([])
    await search_products_lexical(conn, "biz-1", "crema hidratanta fata", **kwargs)
    return conn.sql


async def test_lexical_rank_off_is_byte_identical(monkeypatch):
    """Kill-switch OFF → exact expresia de dinainte de NX-226 (fără normalizare, fără ponderi)."""
    sql = await _lexical_sql(monkeypatch, v2=False)

    assert f" order by ({_OLD_RANK}) desc, p.id" in sql
    assert "over ()" not in sql  # nicio fereastră de normalizare
    assert "0.6" not in sql and "0.4" not in sql


async def test_lexical_rank_v2_normalizes_both_signals(monkeypatch):
    """ON → ambele semnale raportate la maximul pool-ului + ponderi explicite."""
    sql = await _lexical_sql(monkeypatch, v2=True)
    order = _order_clause_of(sql)

    assert "0.6 * coalesce(ts_rank_cd(" in order  # FTS primar
    assert "0.4 * coalesce(similarity(" in order  # trgm secundar
    assert order.count("max(") == 2  # fiecare semnal are maximul LUI
    assert order.count("over ()") == 2  # fereastră peste tot pool-ul (înainte de LIMIT)
    assert order.count("nullif(") == 2  # pool fără semnal → NULL, nu diviziune cu zero
    assert _OLD_RANK not in order  # suma brută a dispărut
    assert ") desc, p.id limit " in order  # tie-break determinist neatins


async def test_lexical_rank_v2_does_not_touch_recall(monkeypatch):
    """Doar ordinea se schimbă: `WHERE` (deci setul de id-uri întors) e identic ON vs OFF."""
    off = await _lexical_sql(monkeypatch, v2=False, category="creme", price_max=100.0)
    on = await _lexical_sql(monkeypatch, v2=True, category="creme", price_max=100.0)

    assert off.replace(_order_clause_of(off), "") == on.replace(_order_clause_of(on), "")
    assert off != on  # ... dar ordinea DA


async def test_lexical_rank_v2_ignored_on_explicit_sort(monkeypatch):
    """Sort explicit (price/rating) nu trece prin rangul lexical — neatins de flag."""
    on = await _lexical_sql(monkeypatch, v2=True, sort_mode="price_asc")

    assert "ts_rank_cd" not in _order_clause_of(on)
    assert "over ()" not in on


def _score_v2(fts: float, trgm: float, *, fts_max: float, trgm_max: float) -> float:
    """Oglinda numerică a expresiei SQL (aceleași constante) — documentează ce ORDONEAZĂ."""
    from src.db.queries.catalog import _LEX_W_FTS, _LEX_W_TRGM

    return _LEX_W_FTS * (fts / fts_max if fts_max else 0.0) + _LEX_W_TRGM * (
        trgm / trgm_max if trgm_max else 0.0
    )


def test_lexical_rank_v2_strong_fts_beats_strong_trgm():
    """Cazul care motivează cardul: FTS puternic + trgm slab vs FTS zero + trgm puternic.

    Valori realiste pentru catalogul nostru: `search_tsv` e construit fără `setweight` (015/033),
    deci ts_rank_cd stă în 0,01–0,3, iar `similarity` peste pragul `%` = 0.3.
    """
    fts_max, trgm_max = 0.30, 0.62
    fraza_potrivita = _score_v2(0.30, 0.31, fts_max=fts_max, trgm_max=trgm_max)
    doar_nume_similar = _score_v2(0.02, 0.62, fts_max=fts_max, trgm_max=trgm_max)

    assert fraza_potrivita > doar_nume_similar
    # ... exact inversul sumei brute, unde trgm-ul (scală de 3-30x) decidea singur
    assert (0.30 + 0.31) < (0.02 + 0.62)


def test_lexical_rank_v2_keeps_typo_net():
    """Ponderea 0.4 nu omoară funcția de typo: fără niciun match FTS, trgm-ul ordonează singur."""
    assert _score_v2(0.0, 0.71, fts_max=0.0, trgm_max=0.71) > _score_v2(
        0.0, 0.34, fts_max=0.0, trgm_max=0.71
    )
