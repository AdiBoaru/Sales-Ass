"""NX-167 (A) — filtrarea de categorie pe arbore (product_category_map + descendenți `path`).

Teste de CONTRACT pe SQL (ca `test_catalog_queries`): fără DB reală, un `FakeConn` captează SQL-ul.
Verificăm că predicatul de categorie e BYTE-IDENTIC cu vechiul cod când flag-ul e OFF, și că devine
un `exists(...)` pe arbore (primary SAU product_category_map; categoria cerută SAU un descendent)
când e ON. Filtrarea efectivă o face Postgres — aici garantăm doar contractul SQL.
"""

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
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


def test_category_clause_off_matches_primary_only(monkeypatch):
    from src.db.queries.catalog import _category_clause

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", False)
    placeholder, params = _placeholder()
    sql = _category_clause("machiaj", placeholder)

    # Match pe slug/nume al primary_category_id (alias c). Predicatul e acum „oricare din listă",
    # ca o rezoluție ambiguă să rămână o constrângere (uniunea familiilor) în loc să fie aruncată.
    assert sql == "(lower(c.slug) = any($1::text[]) or lower(c.name) = any($1::text[]))"
    assert params == [["machiaj"]]


def test_category_clause_accepts_multiple_resolved_keys(monkeypatch):
    """«Cremă» nu identifică o familie anume, dar RĂMÂNE o cerere de cremă.

    Constrângerea pe uniunea familiilor rezolvate ține măștile afară; renunțarea la constrângere
    le-ar lăsa să câștige pe text, fiindcă o mască scrie «Textură cremă» în descriere.
    """
    from src.db.queries.catalog import _category_clause

    monkeypatch.setattr(get_settings(), "search_category_tree_enabled", True)
    placeholder, params = _placeholder()
    sql = _category_clause(["creme-hidratante", "creme-de-ochi"], placeholder)

    assert "any($1::text[])" in sql
    assert params == [["creme-hidratante", "creme-de-ochi"]]


def test_category_clause_refuses_empty_list():
    """Zero chei nu e „fără filtru", e eroare de programare: un predicat care nu prinde nimic ar
    readuce exact zero-ul tăcut pe care rezoluția îl elimină."""
    import pytest

    from src.db.queries.catalog import _category_clause

    placeholder, _ = _placeholder()
    with pytest.raises(ValueError, match="fără nicio categorie"):
        _category_clause([], placeholder)


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
    assert params == [["machiaj"]]  # UN singur placeholder, reutilizat de 2 ori
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

    assert "lower(c.slug) = any(" in conn.sql  # forma fără arbore (doar primary)
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
        category,
        facet_filters=None,
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

    # «machiaj» E o categorie reală, cu produse — deci constrângerea EXISTĂ și abandonarea ei în
    # relaxare e o informație despre rezultate („vin de pe alt raft"), care justifică garda.
    # Distinct de cazul în care categoria nu se poate verifica deloc: acolo filtrul n-a existat
    # niciodată, deci nu e nimic de suprimat, doar de declarat.
    async def fake_vocab(deps, business_id):
        return CatalogVocabulary(
            business_id=business_id,
            dimensions={"category": (VocabEntry(key="machiaj", label="Machiaj", count=101),)},
        )

    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)


async def test_offcategory_guard_suppresses_when_category_dropped(monkeypatch):
    from src.tools.catalog_tools import search_products_tool
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "search_offcategory_guard_enabled", True)
    # NX-220 default keeps category hard; disable it here to exercise the legacy drop + guard seam.
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", False)
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
    # Explicitly restore the legacy category-drop ladder tested by this kill-switch case.
    monkeypatch.setattr(get_settings(), "search_category_hard_enabled", False)
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


# --- NX-167 (C): gardă de coerență la compare (root-branch din path) --------------------------


def _ctx_two_displayed():
    from src.models import (
        BusinessConfig,
        Contact,
        ConversationState,
        InboundMessage,
        ProductRef,
        TurnContext,
    )

    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="compară primele două"),
        conversation_id="conv",
        state=ConversationState(
            displayed_products=[
                ProductRef(product_id="p1", name="Fond A", price=58.99),
                ProductRef(product_id="p2", name="Accesoriu par", price=9.99),
            ]
        ),
    )


def _stub_compare(monkeypatch, roots):
    """Stub `get_products_by_ids` (2 produse valide pt build_comparison) + `product_category_roots`
    cu root-urile date (dict id→root-branch)."""
    from src.agent import deterministic as det

    prods = [
        {
            "id": "p1",
            "name": "Fond A",
            "brand": "X",
            "price": 58.99,
            "rating": 4.8,
            "availability": "in_stock",
            "top_pros": ["acoperire bună"],
        },
        {
            "id": "p2",
            "name": "Accesoriu par",
            "brand": "Y",
            "price": 9.99,
            "rating": 4.6,
            "availability": "in_stock",
            "top_pros": ["prinde bine"],
        },
    ]

    async def _by_ids(conn, bid, ids, *, limit=6):
        order = {pid: i for i, pid in enumerate(ids)}
        return sorted([p for p in prods if p["id"] in ids], key=lambda p: order[p["id"]])[:limit]

    async def _roots(conn, bid, ids):
        return {i: roots[i] for i in ids if i in roots}

    monkeypatch.setattr(det, "get_products_by_ids", _by_ids)
    monkeypatch.setattr(det, "product_category_roots", _roots)


async def test_compare_guard_blocks_incoherent_branches(monkeypatch):
    from src.agent.deterministic import _handle_compare_intent
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "compare_coherence_guard_enabled", True)
    _stub_compare(monkeypatch, {"p1": "machiaj", "p2": "par"})  # ramuri diferite

    ctx = _ctx_two_displayed()
    served = await _handle_compare_intent(
        ctx, PipelineDeps(conn=object(), redis=None, llm=None), "compară primele două"
    )

    assert served is False  # NU a servit tabelul → cade pe bucla LLM
    assert ctx.reply is None  # nicio comparație incoerentă randată
    ev = [e for e in ctx.events if e.type == "compare_incoherent_blocked"]
    assert ev and ev[0].properties["root_branches"] == 2


async def test_compare_guard_allows_same_branch(monkeypatch):
    from src.agent.deterministic import _handle_compare_intent
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "compare_coherence_guard_enabled", True)
    _stub_compare(monkeypatch, {"p1": "machiaj", "p2": "machiaj"})  # ACELAȘI root

    ctx = _ctx_two_displayed()
    served = await _handle_compare_intent(
        ctx, PipelineDeps(conn=object(), redis=None, llm=None), "compară primele două"
    )

    assert served is True  # coerent → serveste comparația
    assert not [e for e in ctx.events if e.type == "compare_incoherent_blocked"]


async def test_compare_guard_off_allows_incoherent(monkeypatch):
    from src.agent.deterministic import _handle_compare_intent
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "compare_coherence_guard_enabled", False)
    _stub_compare(monkeypatch, {"p1": "machiaj", "p2": "par"})

    ctx = _ctx_two_displayed()
    served = await _handle_compare_intent(
        ctx, PipelineDeps(conn=object(), redis=None, llm=None), "compară primele două"
    )

    assert served is True  # OFF → comportamentul vechi (compară orice 2 afișate)


async def test_compare_guard_fail_open_on_missing_path(monkeypatch):
    from src.agent.deterministic import _handle_compare_intent
    from src.worker.runner import PipelineDeps

    monkeypatch.setattr(get_settings(), "compare_coherence_guard_enabled", True)
    _stub_compare(monkeypatch, {})  # niciun root (path lipsă) → fail-open

    ctx = _ctx_two_displayed()
    served = await _handle_compare_intent(
        ctx, PipelineDeps(conn=object(), redis=None, llm=None), "compară primele două"
    )

    assert served is True  # fail-open: fără date de categorie NU blocăm
