"""NX-377 — o pagină subțire pe un filtru de fațetă neauditat se completează cu necunoscute (D7).

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa C3, k1):
«caut un ser de față pentru pete pigmentare, am tenul gras». `skin_type` e completat pe 1.098 din
2.758 de produse, iar filtrul pe fațetă EXCLUDE un produs fără atribut, deși contractul spune
UNKNOWN ≠ MISMATCH (D7) și `_facet_prefer_rank` (NX-322) îl tratează deja așa la ordonare.
Intersecția hiperpigmentare ∧ ten gras a avut 5 produse; scara se oprește la prima treaptă cu
ORICE rezultat, deci clientul a primit 5 carduri, iar «mai arată-mi» după reluare, nimic.

Acum, pe o pagină SUBȚIRE, după potriviri vin produsele care NU poartă deloc atributul unei fațete
neauditate (`enforce_ready: false`), una câte una, cu celelalte filtre păstrate; niciodată cele care
îl contrazic. O pagină plină nu se atinge. Zero DB (căutarea lexicală e falsă și înregistrează).
"""

from __future__ import annotations

import dataclasses

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import get_settings
from src.domain.loader import load_domain_pack
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.base import run_tool
from src.worker.runner import PipelineDeps
from tests.kernel.replay import load_pack


def _row(pid: str, name: str, price: float = 80.0) -> dict:
    return {"id": pid, "name": name, "brand": pid, "price": price, "availability": "in_stock"}


MATCHES = [_row(f"m{i}", f"Ser Pete Gras {i}") for i in range(3)]
UNKNOWN = [_row(f"u{i}", f"Ser Pete {i}") for i in range(8)]
VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "skin_type": (VocabEntry("oily", "oily", 400), VocabEntry("dry", "dry", 500)),
        "routine_time": (VocabEntry("am", "am", 900), VocabEntry("pm", "pm", 900)),
        "product_type": (VocabEntry("ser de fata", "ser de fata", 255),),
        "concerns": (VocabEntry("acne", "acne", 518), VocabEntry("redness", "redness", 900)),
    },
)


def _sole_pack(audited: frozenset[str] = frozenset()):
    doc = load_pack("sole-ro")
    pack = load_domain_pack(
        BusinessConfig(
            id="b",
            slug="sole-ro",
            name="SOLE",
            vertical="ecommerce",
            settings={"domain_pack": doc["domain_pack"]},
        )
    )
    facets = tuple(dataclasses.replace(f, enforce_ready=f.key in audited) for f in pack.facets)
    return dataclasses.replace(pack, facets=facets)


@pytest.fixture
def lexical(monkeypatch):
    calls: list[dict] = []
    state = {"matches": list(MATCHES)}

    async def fake_lex(conn, business_id, **k):
        calls.append(k)
        rows = UNKNOWN if k.get("missing_facets") else state["matches"]
        return [dict(r) for r in rows]

    async def no_emb(conn, business_id):
        return False

    async def vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(ct, "has_embeddings", no_emb)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lex)
    monkeypatch.setattr(ct, "get_vocabulary", vocab)
    return {"calls": calls, "state": state}


def _ctx(pack) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz-1", slug="s", name="n", domain_pack=pack),
        contact=Contact(id="c", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m", body="ser pentru pete, am tenul gras"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    return ctx


async def _search(ctx, **args):
    """Forma turului real k1: tipul de ten (`concerns` → `skin_type`) + nevoia (`features`)."""
    deps = PipelineDeps(conn=object(), redis=None, llm=None)
    base = {"query": "ser", "concerns": ["oily"], "features": ["hyperpigmentation"]}
    return await run_tool(ctx, deps, "search_products", {**base, **args})


def _fills(calls) -> list[dict]:
    return [c for c in calls if c.get("missing_facets")]


async def test_a_thin_page_is_filled_with_unknowns_after_the_matches(lexical):
    ctx = _ctx(_sole_pack())
    res = await _search(ctx)
    ids = [p["id"] for p in res.products]
    assert ids[:3] == ["m0", "m1", "m2"]  # potrivirile primele
    assert ids[3:] == ["u0", "u1", "u2"]  # apoi necunoscutele, până la pagină
    [fill] = _fills(lexical["calls"])
    assert fill["missing_facets"] == ("skin_type",) and not fill["facet_filters"]
    assert fill["features"]  # nevoia rămâne filtru pe completare
    event = next(e for e in ctx.events if e.type == "search_unknown_fill")
    assert event.properties["facets"] == ["skin_type"] and event.properties["page"] == 3


async def test_the_rest_of_the_unknowns_feed_the_paging_pool_not_the_page(lexical):
    ctx = _ctx(_sole_pack())
    await _search(ctx)
    pool = ctx.state_patch["active_search"]["pool"]
    assert pool[:3] == ["m0", "m1", "m2"]
    assert {"u3", "u4", "u5"} <= set(pool)


async def test_a_full_page_is_not_touched(lexical):
    lexical["state"]["matches"] = [_row(f"m{i}", f"Ser {i}") for i in range(6)]
    await _search(_ctx(_sole_pack()))
    assert not _fills(lexical["calls"])


async def test_an_audited_facet_keeps_the_right_to_exclude(lexical):
    await _search(_ctx(_sole_pack(audited=frozenset({"skin_type"}))))
    assert not _fills(lexical["calls"])


async def test_the_kill_switch_restores_the_thin_page(lexical, monkeypatch):
    monkeypatch.setattr(get_settings(), "search_unknown_fill_enabled", False)
    res = await _search(_ctx(_sole_pack()))
    assert [p["id"] for p in res.products] == ["m0", "m1", "m2"]
    assert not _fills(lexical["calls"])


async def test_two_qualifying_facets_are_filled_one_at_a_time(lexical):
    """Necunoscut pe O fațetă, potrivire pe celelalte; toate deodată ar cere produse fără niciun
    atribut, adică exact cele care nu potrivesc nimic."""
    await _search(_ctx(_sole_pack()), query="ser luminos", concerns=["oily", "ser de fata"])
    fills = _fills(lexical["calls"])
    assert sorted(c["missing_facets"] for c in fills) == [("product_type",), ("skin_type",)]
    for c in fills:
        (missing,) = c["missing_facets"]
        assert missing not in (c["facet_filters"] or {}) and c["facet_filters"]


# --- recenzia adversarială ----------------------------------------------------------------------


async def test_the_facet_that_carries_the_need_is_never_filled(lexical):
    """`concerns` e aditivă: un produs fără ea nu e „poate pentru acnee", e altă cerere."""
    await _search(_ctx(_sole_pack()), concerns=["acne"], features=None)
    assert not _fills(lexical["calls"])


async def test_without_another_subject_constraint_there_is_no_fill(lexical):
    """Fațeta lăsată e SINGURUL filtru: completarea ar căuta prin tot catalogul."""
    await _search(_ctx(_sole_pack()), features=None)
    assert not _fills(lexical["calls"])


async def test_the_fill_keeps_the_client_words_as_a_gate(lexical):
    await _search(_ctx(_sole_pack()))
    [fill] = _fills(lexical["calls"])
    assert fill["allow_filters_only"] is False


@pytest.mark.parametrize("step", ["filters_only", "fuzzy", "relaxed_any", "relaxed"])
async def test_rows_from_loose_text_steps_are_not_used(lexical, monkeypatch, step):
    async def loose(conn, business_id, **k):
        lexical["calls"].append(k)
        if k.get("missing_facets"):
            return [{**r, "lexical_step": step} for r in UNKNOWN]
        return [dict(r) for r in MATCHES]

    monkeypatch.setattr(ct, "search_products_lexical", loose)
    res = await _search(_ctx(_sole_pack()))
    assert [p["id"] for p in res.products] == ["m0", "m1", "m2"]


async def test_a_filled_row_is_tagged_and_the_model_is_told(lexical):
    from src.agent.finalize import _rich_bundle

    res = await _search(_ctx(_sole_pack()))
    filled = [p for p in res.products if p.get("facet_unknown")]
    assert filled and all(p["facet_unknown"] == ["skin_type"] for p in filled)
    assert "nu știm dacă se potrivește la skin_type" in res.llm_view
    assert "nu știm dacă se potrivește la skin_type" in _rich_bundle(filled)
    matched = [p for p in res.products if not p.get("facet_unknown")]
    assert "nu știm" not in _rich_bundle(matched)


async def test_the_paging_tail_is_one_page_per_facet(lexical):
    ctx = _ctx(_sole_pack())
    await _search(ctx)
    event = next(e for e in ctx.events if e.type == "search_unknown_fill")
    assert event.properties["tail"] <= 6


async def test_an_unknown_already_on_the_page_is_not_repeated(lexical):
    lexical["state"]["matches"] = [*MATCHES, dict(UNKNOWN[0])]
    res = await _search(_ctx(_sole_pack()))
    ids = [p["id"] for p in res.products]
    assert len(ids) == len(set(ids))


# --- SQL ----------------------------------------------------------------------------------------


class _FakeConn:
    def __init__(self):
        self.sql: list[str] = []
        self.params: list[tuple] = []

    async def fetch(self, sql, *params):
        self.sql.append(sql)
        self.params.append(params)
        return []


async def _lexical_sql(**kwargs) -> tuple[str, tuple]:
    from src.db.queries.catalog import search_products_lexical

    conn = _FakeConn()
    await search_products_lexical(
        conn, "biz-1", "ser pete", facet_filters={"concerns": ["acne"]}, locale="ro", **kwargs
    )
    return conn.sql[0], conn.params[0]


async def test_no_missing_facets_keeps_the_sql_byte_identical():
    assert await _lexical_sql() == await _lexical_sql(missing_facets=())


async def test_a_missing_facet_is_a_parameterized_absence_condition():
    sql, params = await _lexical_sql(missing_facets=("skin_type",))
    assert "not (p.attributes ? $" in sql and "skin_type" in params
    assert "skin_type" not in sql  # cheia e parametru, niciodată interpolată


async def test_a_query_made_only_of_facet_words_is_not_filled(lexical):
    """Recenzia NX-378: textul e purtat întreg de filtre («ser» = tipul din filtru); fără atribut,
    potrivirea strictă ar fi orice produs care scrie cuvântul. Setul filtrelor rămâne răspunsul."""
    await _search(_ctx(_sole_pack()), concerns=["oily", "ser de fata"])
    assert not _fills(lexical["calls"])
