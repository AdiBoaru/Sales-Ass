"""NX-370 — o rafinare nu mai pierde produsul de pe ecran care o împlinește.

Conversația c8 din 2026-10-01: «ce șampon îmi recomanzi pt păr creț și uscat» a arătat LEE
STAFFORD Moisture Burst (fără sulfați), iar la «fără sulfați te rog» modelul a cerut
`features=["sulfate_free"]` (cod inventat, zero produse). Doi pași s-au compus:
1. `features` nerostit se relaxa ULTIMUL, deci scara arunca întâi nevoia reală (păr uscat) și
   raftul;
2. prima pagină a oricărei căutări noi excludea ce era pe ecran, deci Moisture Burst, primul în
   pool, nu ajungea pe pagină. Pe c10 aceeași excludere golea pagina la «cât costă și e în stoc?».

Recenzia adversarială a spart prima variantă (excluderea doar pe `show_more_phrase`, o listă de
fraze românești): «altceva?», «alte creme?», «de la alt brand?» re-serveau aceleași carduri. Regula
e acum STRUCTURALĂ: aceleași filtre ca sesiunea activă ⇒ cererea de altele ⇒ excludere; un filtru
schimbat sau adăugat ⇒ rafinare ⇒ afișatele rămân în joc.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from src.catalog.vocabulary import CatalogVocabulary
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, ProductRef, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps


@pytest.fixture
def provenance_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "search_relax_by_provenance_enabled", True)
    monkeypatch.setattr(s, "search_sort_mode_enabled", True)
    return s


def _fields(steps):
    """Ce filtre mai are fiecare treaptă (pentru a vedea ORDINEA relaxării)."""
    return [
        tuple(k for k in ("features", "facet_filters", "category") if step.get(k)) for step in steps
    ]


def test_unuttered_features_relax_first(provenance_on):
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["hair_dryness"]},
        category=["par"],
        in_stock_only=False,
        features=["sulfate_free"],
        facets_uttered=True,
        category_uttered=True,
        features_uttered=False,
    )
    order = _fields(steps)
    assert order[0] == ("features", "facet_filters", "category")
    assert order[1] == ("facet_filters", "category")  # codul inventat pleacă primul


def test_uttered_features_still_relax_last(provenance_on):
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["acne"]},
        category=None,
        in_stock_only=False,
        features=["niacinamida"],
        facets_uttered=False,
        features_uttered=True,
    )
    assert _fields(steps)[-1] == ()
    assert _fields(steps)[-2] == ("features",)  # „cu niacinamidă" rostit rămâne cel mai mult


def test_without_provenance_the_old_order_holds(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_relax_by_provenance_enabled", False)
    steps = ct._relax_ladder(
        price_max=None,
        facet_filters={"concerns": ["x"]},
        category=None,
        in_stock_only=False,
        features=["y"],
        features_uttered=False,
    )
    assert _fields(steps)[-2] == ("features",)


# --- `features` rostite: TOATE valorile, fără confruntare cu vocabularul -------------------------


def _said(*bodies: str) -> NS:
    history = [NS(direction="inbound", body=b) for b in bodies[1:]]
    return NS(message=NS(body=bodies[0]), history=history)


@pytest.mark.parametrize(
    "said, features, uttered",
    [
        (("fara sulfati te rog",), ["sulfate_free"], False),  # c8: codul inventat de model
        # recenzia: un ingredient rar poate lipsi din vocabularul plafonat (200 de valori,
        # suport minim 2); rostit, se ține până la capăt
        (("vreau ceva cu bakuchiol",), ["bakuchiol"], True),
        (("cu niacinamida", "si acid hialuronic"), ["niacinamida", "acid hialuronic"], True),
        (("cu niacinamida",), ["niacinamida", "retinol"], False),  # una nerostită e destul
        (("orice",), [], False),
    ],
)
def test_features_are_held_only_when_every_value_was_said(said, features, uttered):
    assert ct.features_uttered_by_client(_said(*said), features) is uttered


# --- excluderea pe prima pagină: regula structurală -----------------------------------------------

SESSION = ct._session_filters(ct.SearchArgs(query="crema"), None)


def _excl(body, *, filters=None, session=SESSION, product_name=None, **kw):
    if filters is None:
        filters = ct._session_filters(ct.SearchArgs(query="alt text"), None)
    return ct._first_page_excludes(
        NS(message=NS(body=body)),
        {"p1"},
        planned=kw.get("planned", False),
        exclude_shown=kw.get("exclude_shown", False),
        filters=filters,
        session_filters=session,
        product_name=product_name,
    )


@pytest.mark.parametrize(
    "body",
    [
        "altceva?",
        "alta varianta?",
        "alte creme?",
        "ai ceva asemanator?",
        "ai si de la alt brand?",
        "Arata-mi ceva similar cu Crema A",
    ],
)
def test_reworded_same_request_excludes_what_is_on_screen(body):
    """Recenzia (HIGH): aceleași filtre, altă formulare = clientul vrea ALTELE, oricum o spune."""
    assert _excl(body) == {"p1"}


def test_an_added_or_changed_filter_is_a_refinement_and_keeps_what_is_on_screen():
    """c8: «fără sulfați» adaugă `features`; produsul afișat care o împlinește rămâne."""
    refined = ct._session_filters(ct.SearchArgs(query="sampon"), None, ["sulfate_free"])
    assert _excl("fara sulfati te rog", filters=refined) == set()
    cheaper = ct._session_filters(ct.SearchArgs(query="crema", price_max=50.0), None)
    assert _excl("ceva sub 50 lei", filters=cheaper) == set()


def test_wording_only_fields_do_not_make_a_refinement():
    reworded = ct._session_filters(
        ct.SearchArgs(query="alt text", rank_terms=["x"], prefer={"product_type": ["crema"]}), None
    )
    assert _excl("ceva", filters=reworded) == {"p1"}


def test_a_named_product_is_never_excluded():
    """c10: «cât costă și e în stoc?» numește produsul afișat."""
    assert _excl("cat costa si e in stoc?", product_name="Crema A") == set()


def test_without_an_active_session_the_old_exclusion_holds():
    assert _excl("ceva", session={}) == {"p1"}


@pytest.mark.parametrize("body", ["mai arata-mi", "nu ai altele?", "mai arată-mi alte produse"])
def test_the_paging_phrase_still_excludes_even_on_a_refinement(body):
    refined = ct._session_filters(ct.SearchArgs(query="x", brand="BrandA"), None)
    assert _excl(body, filters=refined) == {"p1"}


def test_planned_search_excludes_only_for_a_show_more_act():
    """Recenzia (MEDIUM): pe calea planificată decizia e a actului (`PlannedTurn.excludes_shown`),
    nu a textului."""
    assert _excl("mai arata-mi", planned=True) == set()
    assert _excl("orice", planned=True, exclude_shown=True) == {"p1"}


def test_kill_switch_restores_unconditional_exclusion(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_first_page_keeps_shown_enabled", False)
    assert _excl("fara sulfati", session={}, product_name="Crema A") == {"p1"}


def test_same_request_survives_the_json_round_trip_of_the_state():
    stored = json.loads(
        json.dumps(ct._session_filters(ct.SearchArgs(query="a", price_max=50), None))
    )
    assert ct.same_request(
        ct._session_filters(ct.SearchArgs(query="b", price_max=50), None), stored
    )


# --- capăt la capăt, prin unealtă ----------------------------------------------------------------


def _row(pid: str, name: str, brand: str, price: float) -> dict:
    return {"id": pid, "name": name, "brand": brand, "price": price, "availability": "in_stock"}


P1 = _row("p1", "Crema Alfa", "BrandA", 80.0)
P2 = _row("p2", "Ser Beta", "BrandB", 90.0)
P3 = _row("p3", "Toner Gama", "BrandC", 70.0)


@pytest.fixture
def lexical(monkeypatch):
    rows: list[dict] = [P1, P2, P3]

    async def fake_lex(conn, business_id, **k):
        return [dict(r) for r in rows]

    async def no_emb(conn, business_id):
        return False

    async def vocab(deps, business_id):
        return CatalogVocabulary(business_id="b", dimensions={})

    monkeypatch.setattr(ct, "has_embeddings", no_emb)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_vocabulary", vocab)
    return rows


def _turn(body: str, *, shown=("p1",), session_args: dict | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
    )
    ctx.state.displayed_products = [ProductRef(i, i, 1.0) for i in shown]
    if session_args is not None:
        filters = ct._session_filters(ct.SearchArgs(**session_args), None)
        ctx.state.active_search = {
            "filters": filters,
            "pool": ["p1", "p2", "p3"],
            "cursor": 1,
            "fp": ct._fp(filters),
            "page": 0,
        }
    return ctx


def _deps() -> PipelineDeps:
    return PipelineDeps(conn=object(), redis=None, llm=None)


def _ids(res) -> list[str]:
    return [p["id"] for p in res.products]


async def test_reworded_request_serves_others_through_the_tool(lexical):
    ctx = _turn("alte creme?", session_args={"query": "crema"})
    res = await run_tool(ctx, _deps(), "search_products", {"query": "alte creme"})
    assert _ids(res) and "p1" not in _ids(res)


async def test_refinement_keeps_the_on_screen_match_through_the_tool(lexical):
    ctx = _turn("doar de la BrandA", session_args={"query": "crema"})
    res = await run_tool(ctx, _deps(), "search_products", {"query": "crema", "brand": "BrandA"})
    assert _ids(res)[0] == "p1"


async def test_named_product_beyond_the_page_is_pulled_onto_it(lexical):
    """Recenzia (MEDIUM): produsul numit, găsit în pool dar nu pe pagină, urcă primul, iar
    „lipsește" se judecă pe pagina servită."""
    lexical[:] = [P2, P3, P1]
    ctx = _turn("aveti crema alfa?", shown=())
    res = await run_tool(
        ctx,
        _deps(),
        "search_products",
        {"query": "crema", "product_name": "Crema Alfa", "limit": 1},
    )
    assert _ids(res) == ["p1"]
    assert "nu există ca atare" not in res.llm_view


async def test_named_product_absent_from_the_pool_is_still_disclosed(lexical):
    ctx = _turn("aveti crema omega?", shown=())
    res = await run_tool(
        ctx, _deps(), "search_products", {"query": "crema", "product_name": "Crema Omega"}
    )
    assert "nu există ca atare" in res.llm_view


async def test_search_session_unseen_counts_only_what_was_not_on_screen(lexical):
    ctx = _turn("doar de la BrandA", session_args={"query": "crema"})
    await run_tool(ctx, _deps(), "search_products", {"query": "crema", "brand": "BrandA"})
    ev = next(e for e in ctx.events if e.type == "search_session")
    assert ev.properties["served"] - ev.properties["unseen"] == 1  # p1 era pe ecran
