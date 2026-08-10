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


# --- NX-222: _exclusion_clause (excludere DURĂ „fără parfum") ----------------------------------


def _ph_recorder():
    params: list = []

    def placeholder(v):
        params.append(v)
        return f"${len(params)}"

    return params, placeholder


def test_exclusion_clause_is_none_of_semantics():
    """„NU are NICIUNUL dintre termenii excluși" — `not exists(... = any(values))`, un singur
    predicat peste toată lista. NU «nu le are pe toate» (ce ar da o negare naivă a unui AND)."""
    from src.db.queries.catalog import _exclusion_clause

    params, placeholder = _ph_recorder()
    sql = _exclusion_clause(("key_ingredients",), ["parfum", "alcool"], placeholder)

    assert sql.startswith("not exists (")  # excluderea, nu includerea
    assert "= any($2::text[])" in sql  # ambii termeni în ACELAȘI any() → „niciunul"
    assert params == ["key_ingredients", ["parfum", "alcool"]]  # chei parametrizate (safe)


def test_exclusion_clause_normalized_ro():
    """Match normalizat prin `ro_unaccent` (033) — acoperă și sedila (ş/ţ), deci strict mai mult
    decât `translate`-ul din `_feature_clause`. Direcția SIGURĂ: excluderea nu scapă produse."""
    from src.db.queries.catalog import _exclusion_clause

    _, placeholder = _ph_recorder()
    sql = _exclusion_clause(("key_ingredients",), ["parfum"], placeholder)
    assert "ro_unaccent(xf)" in sql


def test_exclusion_clause_spans_all_searchable_facets():
    """Uniunea TUTUROR fațetelor căutabile (ca `_feature_clause`) — un termen exclus ascuns pe a
    doua fațetă trebuie să excludă la fel."""
    from src.db.queries.catalog import _exclusion_clause

    params, placeholder = _ph_recorder()
    sql = _exclusion_clause(("key_ingredients", "concerns"), ["parfum"], placeholder)
    assert "||" in sql and sql.count("case when jsonb_typeof") == 2
    assert params == ["key_ingredients", "concerns", ["parfum"]]


def test_exclusion_clause_absence_policy_lets_unpopulated_pass():
    """POLITICA DE ABSENȚĂ: fațetă nepopulată → `'[]'` → zero elemente → `not exists` = TRUE →
    produsul TRECE. Garanția e „nu conține în fațetele cunoscute", nu una de compoziție."""
    from src.db.queries.catalog import _exclusion_clause

    _, placeholder = _ph_recorder()
    sql = _exclusion_clause(("key_ingredients",), ["parfum"], placeholder)
    assert "else '[]'::jsonb end" in sql  # lipsă/ne-array → array gol, nu NULL (care ar propaga)


def test_exclusion_clause_is_not_a_negated_feature_clause():
    """Clauză DEDICATĂ: dacă cineva schimbă semantica de includere, excluderea nu are voie să se
    schimbe tăcut odată cu ea (alias propriu `xf` vs `fe`, normalizare proprie)."""
    from src.db.queries.catalog import _exclusion_clause, _feature_clause

    _, ph1 = _ph_recorder()
    _, ph2 = _ph_recorder()
    incl = _feature_clause(("key_ingredients",), ["parfum"], ph1)
    excl = _exclusion_clause(("key_ingredients",), ["parfum"], ph2)
    assert excl != f"not {incl}"


# --- NX-222: excluderea ajunge în SQL-ul AMBELOR retrievere (paritate) -------------------------


async def test_lexical_applies_exclusion_scoped():
    from src.db.queries.catalog import search_products_lexical

    conn = FakeConn([])
    await search_products_lexical(
        conn,
        "biz-1",
        "ser",
        exclude_features=["parfum"],
        searchable_facets=("key_ingredients",),
    )
    assert "not exists" in conn.sql and "ro_unaccent(xf)" in conn.sql
    assert "p.business_id = $1" in conn.sql and conn.params[0] == "biz-1"  # P7
    assert ["parfum"] in conn.params


async def test_semantic_applies_exclusion_scoped():
    """Paritate obligatorie: un filtru de protecție pe un singur picior al hibridului ar lăsa
    celălalt să reintroducă produsul prin fuziunea RRF."""
    from src.db.queries.catalog import search_products_semantic

    conn = FakeConn([])
    await search_products_semantic(
        conn,
        "biz-1",
        [0.0] * 8,
        exclude_features=["parfum"],
        searchable_facets=("key_ingredients",),
    )
    assert "not exists" in conn.sql and "ro_unaccent(xf)" in conn.sql
    assert "p.business_id = $1" in conn.sql and conn.params[0] == "biz-1"  # P7
    assert ["parfum"] in conn.params


async def test_retrievers_without_exclusion_are_unchanged():
    """Fără `exclude_features` (sau fără searchable_facets) → SQL byte-identic cu main."""
    from src.db.queries.catalog import search_products_lexical

    base = FakeConn([])
    await search_products_lexical(base, "biz-1", "ser", searchable_facets=("key_ingredients",))
    empty = FakeConn([])
    await search_products_lexical(
        empty, "biz-1", "ser", exclude_features=[], searchable_facets=("key_ingredients",)
    )
    no_facets = FakeConn([])
    await search_products_lexical(no_facets, "biz-1", "ser", exclude_features=["parfum"])

    assert "not exists" not in base.sql
    assert empty.sql == base.sql and empty.params == base.params
    assert no_facets.sql == base.sql and no_facets.params == base.params
