"""Unit (fără DB/LLM) pentru P0/P1 retrieval (ARCH-product-retrieval): order-clause pe sort_mode +
tie-break determinist, relax-ladder cu preț fixat, matcher-ul de intenție „mai ieftin", mesajul
cheapest-already. Kill-switch-ele verificate pe ambele poziții."""

import pytest

from src.config import get_settings
from src.db.queries import catalog
from src.tools.catalog_tools import _relax_ladder
from src.worker.stages.agent import _CHEAPER_RE, _cheapest_already_msg


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_sort_mode_enabled", True)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_sort_mode_enabled", False)


# --- order clause (filter-then-sort) -----------------------------------------


def test_order_clause_price_asc_sorts_by_price_then_id(flag_on):
    c = catalog._order_clause("price_asc")
    assert "order by" in c
    assert "coalesce(vp.price" in c and "asc" in c
    assert c.rstrip().endswith("p.id")  # tie-break determinist final


def test_order_clause_relevance_has_shrunk_rating_and_tiebreak(flag_on):
    c = catalog._order_clause("relevance")
    assert "review_count" in c  # shrunk_rating (Bayesian)
    assert c.rstrip().endswith("p.id")


def test_order_clause_rating_desc_uses_shrunk(flag_on):
    c = catalog._order_clause("rating_desc")
    assert "review_count" in c and "desc" in c


def test_order_clause_killswitch_off_is_legacy(flag_off):
    # OFF → ORDER BY-ul vechi (byte-identic), chiar și pe price_asc
    c = catalog._order_clause("price_asc")
    assert "p.rating desc" in c
    assert "p.id" not in c and "review_count" not in c


def test_order_clause_killswitch_off_semantic_keeps_cosine(flag_off):
    # OFF pe calea semantică = revert EXACT la cosine, NU rating (regression-guard real)
    c = catalog._order_clause("relevance", qvec_ph="$2")
    assert "embedding <=>" in c and "p.rating" not in c


def test_order_clause_unknown_mode_falls_to_relevance(flag_on):
    assert catalog._order_clause("bogus") == catalog._order_clause("relevance")


def test_order_clause_semantic_relevance_is_cosine(flag_on):
    c = catalog._order_clause("relevance", qvec_ph="$2")
    assert "embedding <=>" in c


def test_order_clause_semantic_price_asc_ignores_cosine(flag_on):
    c = catalog._order_clause("price_asc", qvec_ph="$2")
    assert "embedding <=>" not in c  # price intent → sort pe preț, nu cosine
    assert "coalesce(vp.price" in c and "asc" in c


# --- relax ladder (preț + stoc fixate pe flag ON) ----------------------------


def test_relax_ladder_pins_price_when_flag_on(flag_on):
    steps = _relax_ladder(price_max=80.0, concerns=["oily"], category="parfum", in_stock_only=False)
    assert all(s["price_max"] == 80.0 for s in steps)  # prețul NU se relaxează niciodată
    assert any(s["concerns"] is None for s in steps)  # softul (concerns) se relaxează
    assert any(s["category"] is None for s in steps)


def test_relax_ladder_legacy_drops_price_first(flag_off):
    steps = _relax_ladder(price_max=80.0, concerns=None, category=None, in_stock_only=False)
    assert any(s["price_max"] is None for s in steps)  # comportamentul vechi (price relaxat)


# --- matcher intenție „mai ieftin" -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ceva mai ieftin",
        "mai ieftin",
        "cel mai ieftin",
        "cea mai ieftina varianta",
        "ai ceva mai accesibil?",
        "vreau un pret mai mic",
        "cheaper",
        "the cheapest one",
        "valami olcsóbb",
    ],
)
def test_cheaper_re_matches(text):
    assert _CHEAPER_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "vreau un parfum bun",
        "care e cea mai buna?",
        "arata-mi cremele",
        "compara primele doua",
        "ce ai pentru ten gras?",
    ],
)
def test_cheaper_re_no_false_positive(text):
    assert _CHEAPER_RE.search(text) is None


def test_cheapest_already_msg_locale():
    assert "cea mai ieftină" in _cheapest_already_msg("ro")
    assert "cheapest" in _cheapest_already_msg("en")
    assert _cheapest_already_msg("xx") == _cheapest_already_msg("ro")  # locale necunoscut → ro


# --- NX-113a: search_products_lexical (FTS + pg_trgm) — assert pe SQL, fără DB ----


class _CaptureConn:
    """Conn fals: captează SQL-ul + params (zero DB). search_products_lexical execută conn.fetch."""

    def __init__(self):
        self.sql = ""
        self.params: tuple = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return []


async def test_lexical_uses_fts_and_trgm():
    conn = _CaptureConn()
    await catalog.search_products_lexical(conn, "biz-1", "cremă pentru ten gras", pool=50)
    assert "websearch_to_tsquery('simple'" in conn.sql  # FTS real, nu ILIKE
    assert "ts_rank_cd" in conn.sql  # rank lexical
    assert "p.name %" in conn.sql and "similarity(p.name" in conn.sql  # pg_trgm (typo/SKU)
    assert "p.business_id = $1" in conn.sql  # P7
    assert conn.params[0] == "biz-1" and "cremă pentru ten gras" in conn.params
    assert "ilike '%" not in conn.sql.lower()  # NU mai e substring ILIKE pe frază


async def test_lexical_query_param_reused_single_placeholder():
    conn = _CaptureConn()
    await catalog.search_products_lexical(conn, "b", "abc", pool=10)
    # query_text e UN singur placeholder ($2), reutilizat în match + rank (nu dublat în params)
    assert conn.params.count("abc") == 1


async def test_lexical_relevance_orders_by_rank(flag_on):
    conn = _CaptureConn()
    await catalog.search_products_lexical(conn, "b", "q", sort_mode="relevance", pool=10)
    order = conn.sql.split("order by")[-1]  # [-1] = ORDER BY final (nu cel din lateral-ul img)
    assert "ts_rank_cd" in order and "desc" in order and "p.id" in order  # rang + tie-break


async def test_lexical_explicit_sort_keeps_filter_but_sorts(flag_on):
    conn = _CaptureConn()
    await catalog.search_products_lexical(conn, "b", "q", sort_mode="price_asc", pool=10)
    assert "websearch_to_tsquery" in conn.sql  # filtrul lexical păstrat
    assert "coalesce(vp.price" in conn.sql and "asc" in conn.sql  # ordonare pe preț
    assert "ts_rank_cd" not in conn.sql.split("order by")[-1]  # NU pe rank în ORDER BY final


async def test_lexical_applies_hard_filters():
    conn = _CaptureConn()
    await catalog.search_products_lexical(
        conn, "b", "q", brand="Nivea", price_max=80.0, in_stock_only=True, pool=10
    )
    assert "b.name ilike" in conn.sql  # brand = filtru dur
    assert "in ('in_stock', 'low_stock')" in conn.sql  # stoc
    assert "Nivea" in str(conn.params) and 80.0 in conn.params
