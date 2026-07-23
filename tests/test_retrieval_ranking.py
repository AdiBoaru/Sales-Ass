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


def test_relax_ladder_features_relax_last(flag_on):
    # Tier 2b p2: feature („cu niacinamidă") e hard requirement → relaxat DUPĂ category (P6).
    steps = _relax_ladder(
        price_max=None,
        concerns=["oily"],
        category="creme",
        in_stock_only=False,
        features=["niacinamida"],
    )
    assert steps[0]["features"] == ["niacinamida"]  # prima treaptă = strict
    assert steps[-1]["features"] is None  # feature relaxat la final
    feat_idx = next(i for i, s in enumerate(steps) if s["features"] is None)
    cat_idx = next(i for i, s in enumerate(steps) if s["category"] is None)
    assert feat_idx > cat_idx  # feature relaxat DUPĂ category (păstrat cât mai mult)


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
    # NX-178: trgm-ul compară acum expresii NORMALIZATE (fără diacritice), pe indexul dedicat —
    # altfel „sampon" nu găsea niciun „șampon". Contractul rămâne același: FTS + fuzzy pe nume.
    assert "ro_unaccent(p.name) %" in conn.sql
    assert "similarity(ro_unaccent(p.name)" in conn.sql
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


async def test_semantic_sql_injects_cosine_and_sends_vector_list():
    """NX-113c: SELECT-ul semantic conține `cosine_distance` (semnal de calitate) și vectorul de
    query e trimis ca list[float] (codec pgvector), nu inline ca text. Assert pe SQL, fără DB."""
    conn = _CaptureConn()
    vec = [0.1, 0.2, 0.3]
    await catalog.search_products_semantic(conn, "biz-1", vec, pool=50)
    assert "cosine_distance" in conn.sql  # coloana de distanță injectată
    assert "pe.embedding <=>" in conn.sql and "join product_embeddings pe" in conn.sql
    assert "p.business_id = $1" in conn.sql  # P7
    assert conn.params[0] == "biz-1" and vec in conn.params  # list[float] direct, fără literal text


async def test_lexical_applies_hard_filters():
    conn = _CaptureConn()
    await catalog.search_products_lexical(
        conn, "b", "q", brand="Nivea", price_max=80.0, in_stock_only=True, pool=10
    )
    assert "ro_unaccent(b.name) like" in conn.sql  # brand = filtru dur, fără diacritice
    assert "in ('in_stock', 'low_stock')" in conn.sql  # stoc
    assert "Nivea" in str(conn.params) and 80.0 in conn.params


# --- NX-113b: RRF fusion + merge determinist (pur, fără DB) -----------------------


def test_rrf_in_both_lists_beats_single_list():
    from src.db.queries.fusion import rrf_fuse

    # „dual" la rang 3 lexical + rang 1 vector; „solo" doar la rang 1 vector.
    lexical = [{"id": "a"}, {"id": "b"}, {"id": "dual"}]
    vector = [{"id": "dual"}, {"id": "solo"}]
    order = rrf_fuse(lexical, vector)
    assert order[0] == "dual"  # prezent în ambele → scor cumulat → primul
    assert order.index("dual") < order.index("solo")


def test_rrf_tie_break_stable_on_id():
    from src.db.queries.fusion import rrf_fuse

    # două produse, fiecare la rang 1 într-o singură listă → scor egal → tie-break pe id (crescător)
    order = rrf_fuse([{"id": "z1"}], [{"id": "a1"}])
    assert order == ["a1", "z1"]


def test_rrf_accepts_bare_ids():
    from src.db.queries.fusion import rrf_fuse

    assert rrf_fuse(["x", "y"], ["y"])[0] == "y"  # acceptă și liste de id-uri, nu doar dict-uri


def test_fuse_relevance_uses_rrf_returns_dicts():
    from src.db.queries.fusion import fuse_candidates

    lexical = [{"id": "a", "price": 10.0}, {"id": "dual", "price": 5.0}]
    vector = [{"id": "dual", "price": 5.0}, {"id": "c", "price": 7.0}]
    fused = fuse_candidates(lexical, vector, sort_mode="relevance")
    assert fused[0]["id"] == "dual"  # în ambele → primul
    assert {p["id"] for p in fused} == {"a", "dual", "c"}  # union, dedup pe id


def test_fuse_price_asc_resorts_union_deterministic():
    from src.db.queries.fusion import fuse_candidates

    lexical = [{"id": "scump", "price": 90.0}]
    vector = [{"id": "ieftin", "price": 10.0}]
    fused = fuse_candidates(lexical, vector, sort_mode="price_asc")
    assert [p["id"] for p in fused] == ["ieftin", "scump"]  # re-sort pe preț, nu pe RRF


def test_fuse_rating_desc_uses_shrunk_rating():
    from src.db.queries.fusion import fuse_candidates

    # rating brut egal (5.0), review_count diferit → shrunk rating departajează (mai multe = sus)
    few = {"id": "few", "rating": 5.0, "review_count": 1, "price": 10.0}
    many = {"id": "many", "rating": 5.0, "review_count": 500, "price": 10.0}
    fused = fuse_candidates([few], [many], sort_mode="rating_desc")
    assert [p["id"] for p in fused] == ["many", "few"]  # shrunk: 500 recenzii > 1 recenzie


# --- NX-113c: deterministic_rerank (pur) -----------------------------------------


def test_rerank_breaks_rrf_tie_by_instock_and_sale():
    from src.db.queries.fusion import deterministic_rerank

    out = deterministic_rerank(
        [
            {"id": "a", "availability": "out_of_stock"},
            {"id": "b", "availability": "in_stock", "on_sale": True},
        ],
        {"a": 0.5, "b": 0.5},  # scor RRF EGAL → boost departajează
    )
    assert [p["id"] for p in out] == ["b", "a"]  # in_stock + sale urcă


def test_rerank_does_not_override_relevance():
    from src.db.queries.fusion import deterministic_rerank

    out = deterministic_rerank(
        [
            {"id": "a", "availability": "in_stock", "on_sale": True},  # boost mare ...
            {"id": "b", "availability": "out_of_stock"},  # ... dar scor mai mic
        ],
        {"a": 0.1, "b": 0.9},  # scor RRF DIFERIT → relevanța primează, boost nu răstoarnă
    )
    assert [p["id"] for p in out] == ["b", "a"]


def test_rerank_concern_overlap_lifts_on_tie():
    from src.db.queries.fusion import deterministic_rerank

    out = deterministic_rerank(
        [{"id": "a", "concerns": ["dry"]}, {"id": "b", "concerns": ["oily", "sensitive"]}],
        {"a": 0.5, "b": 0.5},
        concerns=["oily", "sensitive"],
    )
    assert [p["id"] for p in out] == ["b", "a"]  # b: 2 overlap, a: 0


def test_rerank_tie_break_stable_on_id():
    from src.db.queries.fusion import deterministic_rerank

    out = deterministic_rerank([{"id": "z"}, {"id": "a"}], {"z": 0.5, "a": 0.5})
    assert [p["id"] for p in out] == ["a", "z"]  # tot egal → id crescător


def test_rerank_parses_jsonb_string_concerns():
    from src.db.queries.fusion import deterministic_rerank

    # asyncpg fără codec întoarce jsonb ca TEXT → parsat defensiv în rerank
    out = deterministic_rerank(
        [{"id": "a", "concerns": "[]"}, {"id": "b", "concerns": '["oily"]'}],
        {"a": 0.5, "b": 0.5},
        concerns=["oily"],
    )
    assert [p["id"] for p in out] == ["b", "a"]


def test_fuse_relevance_applies_rerank_on_tie():
    from src.db.queries.fusion import fuse_candidates

    # fiecare produs la rang 1 într-o singură listă → scor RRF egal → in_stock urcă
    lexical = [{"id": "lo", "availability": "out_of_stock"}]
    vector = [{"id": "hi", "availability": "in_stock"}]
    out = fuse_candidates(lexical, vector, sort_mode="relevance")
    assert out[0]["id"] == "hi"


# --- ARCH-2026 P0: scor BLENDED (social proof în scorul primar) -------------------


def test_minmax_norm_degenerate_and_empty():
    from src.db.queries.fusion import _minmax_norm

    assert _minmax_norm({}) == {}
    assert _minmax_norm({"a": 5.0, "b": 5.0}) == {"a": 1.0, "b": 1.0}  # toate egale → constant 1.0
    n = _minmax_norm({"lo": 0.0, "hi": 10.0, "mid": 5.0})
    assert n["lo"] == 0.0 and n["hi"] == 1.0 and n["mid"] == 0.5


def test_blended_rerank_social_proof_lifts_better_rated():
    """Reparația centrală: la relevanță egală (RRF), un produs MAI BINE COTAT (4.6 din 148 recenzii)
    urcă peste unul mai slab (4.4 din 28) — chiar dacă id-ul l-ar pune sub (alpha<zeta)."""
    from src.db.queries.fusion import blended_rerank

    weaker = {"id": "alpha", "rating": 4.4, "review_count": 28, "availability": "in_stock"}
    better = {"id": "zeta", "rating": 4.6, "review_count": 148, "availability": "in_stock"}
    out = blended_rerank([weaker, better], {"alpha": 0.5, "zeta": 0.5})  # RRF egal
    assert [p["id"] for p in out] == ["zeta", "alpha"]  # social proof urcă, NU id-ul (tie vechi)


def test_blended_rerank_relevance_stays_dominant():
    """Relevanța (pondere 1.0) domină social-proof-ul (0.35): un produs mult mai relevant dar cu
    rating mic NU e răsturnat de unul slab relevant dar mai bine cotat."""
    from src.db.queries.fusion import blended_rerank

    relevant = {"id": "rel", "rating": 4.0, "review_count": 200}
    rated = {"id": "rat", "rating": 5.0, "review_count": 200}
    out = blended_rerank([relevant, rated], {"rel": 1.0, "rat": 0.0})  # RRF: rel >> rat
    assert out[0]["id"] == "rel"


def test_blended_rerank_weight_override_can_flip():
    """Ponderile sunt tunabile (per-vertical): cu `rating` urcat suficient, social-proof-ul poate
    învinge o diferență mică de relevanță (un vertical „premium")."""
    from src.db.queries.fusion import blended_rerank

    relevant = {"id": "rel", "rating": 4.0, "review_count": 200}
    rated = {"id": "rat", "rating": 5.0, "review_count": 200}
    # diferență MICĂ de RRF + pondere rating mare → rating răstoarnă
    out = blended_rerank(
        [relevant, rated], {"rel": 0.51, "rat": 0.5}, weights={"relevance": 1.0, "rating": 5.0}
    )
    assert out[0]["id"] == "rat"


def test_fuse_relevance_blended_with_weights_uses_rating():
    from src.db.queries.fusion import fuse_candidates

    # fiecare la rang 1 într-o listă → RRF egal; cu `weights` → blend pe rating (zeta urcă)
    weaker = {"id": "alpha", "rating": 4.4, "review_count": 28}
    better = {"id": "zeta", "rating": 4.6, "review_count": 148}
    out = fuse_candidates([weaker], [better], sort_mode="relevance", weights={})
    assert [p["id"] for p in out] == ["zeta", "alpha"]


def test_fuse_relevance_none_weights_is_legacy_rrf():
    from src.db.queries.fusion import fuse_candidates

    # weights=None (kill-switch OFF) → deterministic_rerank: RRF egal → tie pe id, rating IGNORAT
    weaker = {"id": "alpha", "rating": 4.4, "review_count": 28}
    better = {"id": "zeta", "rating": 4.6, "review_count": 148}
    out = fuse_candidates([weaker], [better], sort_mode="relevance", weights=None)
    assert [p["id"] for p in out] == ["alpha", "zeta"]  # id tie-break, NU rating (legacy RRF)


# --- _rank_weights (sursa ponderilor: DomainPack / default / kill-switch) ----------


def test_rank_weights_none_when_flag_off(monkeypatch):
    from types import SimpleNamespace

    from src.tools import catalog_tools

    monkeypatch.setattr(get_settings(), "search_blended_rank_enabled", False)
    ctx = SimpleNamespace(business=SimpleNamespace(domain_pack=None))
    assert catalog_tools._rank_weights(ctx) is None


def test_rank_weights_dict_when_flag_on(monkeypatch):
    from types import SimpleNamespace

    from src.domain.pack import DomainPack
    from src.tools import catalog_tools

    monkeypatch.setattr(get_settings(), "search_blended_rank_enabled", True)
    # fără pack → {} (blend cade pe default-urile RANK_WEIGHTS)
    ctx0 = SimpleNamespace(business=SimpleNamespace(domain_pack=None))
    assert catalog_tools._rank_weights(ctx0) == {}
    # cu pack.rank_weights → override per-vertical
    pack = DomainPack(vertical="beauty_salon", rank_weights={"sale": 0.5})
    ctx1 = SimpleNamespace(business=SimpleNamespace(domain_pack=pack))
    assert catalog_tools._rank_weights(ctx1) == {"sale": 0.5}
