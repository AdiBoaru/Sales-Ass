"""NX-319 — conversația reală `1518d1d9` (`sole-ro`, 2026-09-24), un test per mecanism.

Fiecare test pinuiește un tur care a ieșit greșit și, lângă el, cazul vecin pe care reparația NU
are voie să-l strice (măsurat pe trafic cu `scripts/nx319_constraint_provenance_probe.py`).
Pur sau cu DB stubuită: fără rețea, fără credite.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import deterministic as det
from src.agent.deterministic import (
    _detail_answer,
    _review_answer,
    is_compare_with_similar,
    pick_similar_partner,
    try_pre_intents,
)
from src.agent.voice import naturalize
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry, named_only_as_subshelf
from src.config import get_settings
from src.domain.pack import SectionSpec
from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import (
    PRICE_BOUND_RELATIVE_REQUEST,
    PRICE_BOUND_SESSION,
    PRICE_BOUND_SPOKEN_EARLIER,
    PRICE_BOUND_SPOKEN_NOW,
    price_bound_source,
    search_products_tool,
)
from src.worker.runner import PipelineDeps

# Arborele REAL de pe catalogul SOLE, pe cheile care contează aici.
MACHIAJ = VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj")
MACHIAJ_FATA = VocabEntry(key="machiaj-fata", label="Fata", count=274, path="machiaj/fata")
TEN = VocabEntry(key="ten", label="Ten", count=1461, path="ten")
TEN_ING = VocabEntry(
    key="ten-ingrijirea-tenului",
    label="Ingrijirea tenului",
    count=933,
    path="ten/ingrijirea-tenului",
)
_SOLE = CatalogVocabulary(
    business_id="b", dimensions={"category": (MACHIAJ, MACHIAJ_FATA, TEN, TEN_ING)}
)


# --- 1. marginea de preț fără sursă ------------------------------------------


def test_price_bound_carried_into_a_new_subject_has_no_source() -> None:
    """Turul `38b47d4a`: «ai ceva anti aging?» a plecat cu `price_max=29.99`, dus de model din
    «mai ieftin»-ul de dinainte. Clientul n-a rostit niciun număr și nu cere nimic mai ieftin."""
    texts = ["ai ceva anti aging?", "si ceva mai ieftin ?", "vreau o crema de fata"]
    assert (
        price_bound_source(29.99, texts=texts, relative_request=False, session_price_max=34.99)
        is None
    )


def test_relative_price_request_keeps_the_derived_bound() -> None:
    """Turul `d2bcbaf9` («si ceva mai ieftin ?», `price_max=34.99`) era corect și rămâne."""
    assert (
        price_bound_source(
            34.99, texts=["si ceva mai ieftin ?"], relative_request=True, session_price_max=None
        )
        == PRICE_BOUND_RELATIVE_REQUEST
    )


def test_spoken_budget_is_a_source_now_and_later() -> None:
    """Un buget rostit rămâne buget și la turele următoare (clasa NX-235), nu doar pe turul lui."""
    assert (
        price_bound_source(
            100, texts=["ceva sub 100 lei"], relative_request=False, session_price_max=None
        )
        == PRICE_BOUND_SPOKEN_NOW
    )
    assert (
        price_bound_source(
            100,
            texts=["si pentru ochi?", "ceva sub 100 lei"],
            relative_request=False,
            session_price_max=None,
        )
        == PRICE_BOUND_SPOKEN_EARLIER
    )


def test_same_bound_as_the_active_session_is_pagination() -> None:
    assert (
        price_bound_source(34.99, texts=["altele"], relative_request=False, session_price_max=34.99)
        == PRICE_BOUND_SESSION
    )
    # „altelte" pe trafic real: sesiunea avea 659.99, modelul a trimis 59. Alt număr, altă cerere.
    assert (
        price_bound_source(59, texts=["altelte"], relative_request=False, session_price_max=659.99)
        is None
    )


# --- 2. subraftul omonim -----------------------------------------------------


def test_subshelf_named_only_by_a_common_word_is_not_uttered() -> None:
    """Turul `707fde73`: «vreau o crema de fata» coroborează literal raftul Machiaj > Fata."""
    assert named_only_as_subshelf(_SOLE, ["machiaj-fata"], ["vreau o crema de fata"])


def test_subshelf_with_its_root_named_stays_uttered() -> None:
    assert not named_only_as_subshelf(_SOLE, ["machiaj-fata"], ["un machiaj pentru fata"])
    # Rădăcina rostită într-un mesaj ANTERIOR ajunge: subiectul a fost deja numit.
    assert not named_only_as_subshelf(_SOLE, ["machiaj-fata"], ["ceva de fata", "vreau machiaj"])


def test_root_shelf_and_unknown_keys_keep_the_old_behaviour() -> None:
    assert not named_only_as_subshelf(_SOLE, ["ten"], ["ceva pentru ten"])
    assert not named_only_as_subshelf(_SOLE, ["nu-exista"], ["ceva de fata"])
    assert not named_only_as_subshelf(CatalogVocabulary(business_id="b"), ["machiaj-fata"], ["x"])


# --- cablajul în unealta de căutare (DB stubuită) ----------------------------


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _ctx(body: str, *, history: tuple[str, ...] = (), session: dict | None = None) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body=h) for h in history
        ],
        state=ConversationState(active_search=session),
    )


@pytest.fixture
def lexical_calls(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        calls.append({"query": query_text, **kwargs})
        category = tuple(kwargs.get("category") or ())
        if "machiaj-fata" in category:  # setul cu raftul ghicit: creme BB
            return [
                {"id": f"bb{i}", "name": f"BB Cream {i}", "price": 50.0, "lexical_step": "strict"}
                for i in range(6)
            ]
        # aceeași cerere fără raft: creme de îngrijire, niciuna pe raftul de machiaj
        return [
            {"id": f"cr{i}", "name": f"Crema de fata {i}", "price": 60.0, "lexical_step": "strict"}
            for i in range(20)
        ]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return _SOLE

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    return calls


async def test_unsourced_price_bound_never_reaches_sql(lexical_calls) -> None:
    ctx = _ctx("ai ceva anti aging?", history=("si ceva mai ieftin ?", "vreau o crema de fata"))
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema anti aging", "price_max": 29.99},
    )
    assert lexical_calls and all(c.get("price_max") is None for c in lexical_calls)
    ev = [e for e in ctx.events if e.type == "price_bound_provenance"]
    assert ev and ev[0].properties["source"] == "unsupported"
    assert ev[0].properties["kept"] is False


async def test_relative_request_bound_still_reaches_sql(lexical_calls) -> None:
    ctx = _ctx("si ceva mai ieftin ?")
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "price_max": 34.99, "sort_mode": "price_asc"},
    )
    assert lexical_calls[0]["price_max"] == 34.99


async def test_price_bound_flag_off_is_the_old_behaviour(lexical_calls, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "search_price_bound_provenance_enabled", False)
    ctx = _ctx("ai ceva anti aging?")
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema anti aging", "price_max": 29.99},
    )
    assert lexical_calls[0]["price_max"] == 29.99
    assert not [e for e in ctx.events if e.type == "price_bound_provenance"]


async def test_homograph_subshelf_is_judged_on_data_and_dropped(lexical_calls) -> None:
    """Turul `707fde73` cap-coadă: raftul devine ipoteză, NX-313 îl confruntă cu aceeași cerere
    fără el și, fiindcă 0 din 20 de potriviri stau pe raft, îl scoate."""
    ctx = _ctx("vreau o crema de fata")
    result = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "category": "fata"},
    )
    assert [e for e in ctx.events if e.type == "category_subshelf_homograph"]
    assert [e for e in ctx.events if e.type == "guessed_filter_rescued"]
    assert result.products and all(str(p["id"]).startswith("cr") for p in result.products)


async def test_homograph_guard_off_keeps_the_makeup_shelf(lexical_calls, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "search_subshelf_homograph_guard_enabled", False)
    ctx = _ctx("vreau o crema de fata")
    result = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "category": "fata"},
    )
    assert all(str(p["id"]).startswith("bb") for p in result.products)


async def test_one_card_per_family_also_under_explicit_sort(monkeypatch) -> None:
    """Turul `8aaec031`: `price_asc`, cinci role GESKE cu același nume afișat din șase."""
    rows = [
        {"id": "brush", "name": "GESKE Sonic Facial Brush 5 in 1 - Aparat", "price": 145.0},
        *[
            {"id": f"roll{i}", "name": f"GESKE Sonic Facial Roller 4 in 1 - Nr {i}", "price": 190.0}
            for i in range(5)
        ],
        *[
            {"id": f"other{i}", "name": f"Alt aparat {i} - descriere", "price": 200.0 + i}
            for i in range(3)
        ],
    ]

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        return [dict(r) for r in rows]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return CatalogVocabulary(business_id=business_id)

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    ctx = _ctx("da ceva aparat as vrea")
    result = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "aparat", "sort_mode": "price_asc", "limit": 5},
    )
    ids = [p["id"] for p in result.products]
    assert ids == ["brush", "roll0", "other0", "other1", "other2"]


# --- 4. «Compară-l cu un produs similar» -------------------------------------


def test_compare_with_similar_recognizes_only_our_own_chip() -> None:
    assert is_compare_with_similar("Compară-l cu un produs similar", "ro")
    assert is_compare_with_similar("compara-l cu un produs similar", "ro")
    assert not is_compare_with_similar("compara-l cu COSRX Snail 92", "ro")
    assert not is_compare_with_similar("", "ro")


MAGENTA = "GESKE SmartAppGuided Sonic Facial Brush | 5 in 1 Magenta"


def _cand(pid: str, name: str, price: float, brand: str) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "price": price,
        "brand_id": brand,
        "anchor_name": "GESKE Sonic Facial Brush 5 in 1 - Aparat Sonic - Starlight",
        "anchor_price": 145.0,
        "anchor_brand_id": "geske",
    }


def test_partner_skips_the_color_twin_and_the_same_family() -> None:
    """Turul `ee1a49f0` a servit peria Magenta: același brand, același preț, alt nume afișat."""
    cands = [
        _cand("same-fam", "GESKE Sonic Facial Brush 5 in 1 - Aparat - Black", 145.0, "geske"),
        _cand("magenta", MAGENTA, 145.0, "geske"),
        _cand("thermo", "GESKE Sonic Thermo Facial Brush 6 in 1 - Aparat", 240.0, "geske"),
        _cand("touch", "TOUCH BEAUTY TB-1788 - Aparat sonic", 318.0, "touch"),
    ]
    assert pick_similar_partner(cands) == "thermo"
    assert pick_similar_partner(cands[:2]) is None


async def test_compare_with_similar_serves_the_pair(monkeypatch) -> None:
    served: dict[str, Any] = {}

    async def fake_candidates(conn, business_id, anchor_id, **kw):
        served["anchor"] = anchor_id
        return [
            _cand("magenta", MAGENTA, 145.0, "geske"),
            _cand("thermo", "GESKE Sonic Thermo Facial Brush 6 in 1", 240.0, "geske"),
        ]

    async def fake_compare(ctx, deps, ids):
        served["ids"] = ids
        return True

    monkeypatch.setattr(det, "similar_candidates", fake_candidates)
    monkeypatch.setattr(det, "serve_comparison", fake_compare)
    ctx = _ctx("Compară-l cu un produs similar")
    ctx.state.displayed_products = [ProductRef(product_id="brush", name="GESKE Brush", price=145.0)]
    ctx.route = RouteDecision(route=Route.SALES)
    assert await try_pre_intents(ctx, PipelineDeps(conn=object(), redis=None, llm=None))
    assert served == {"anchor": "brush", "ids": ["brush", "thermo"]}


async def test_compare_with_similar_without_partner_falls_to_the_model(monkeypatch) -> None:
    async def no_candidates(conn, business_id, anchor_id, **kw):
        return []

    monkeypatch.setattr(det, "similar_candidates", no_candidates)
    ctx = _ctx("Compară-l cu un produs similar")
    ctx.state.displayed_products = [ProductRef(product_id="brush", name="GESKE Brush", price=145.0)]
    ctx.route = RouteDecision(route=Route.SALES)
    assert not await try_pre_intents(ctx, PipelineDeps(conn=object(), redis=None, llm=None))
    ev = [e for e in ctx.events if e.type == "compare_with_similar"]
    assert ev and ev[0].properties["reason"] == "no_partner"


# --- 5. detaliul și recenziile -----------------------------------------------


class _Pack:
    comparison_facets = ()
    detail_sections = (
        SectionSpec(kind="summary", max_chars=240),
        SectionSpec(kind="usage", max_chars=400),
    )
    howto_sections = ("usage", "dosage")


_BRUSH = {
    "id": "brush",
    "name": "GESKE Sonic Facial Brush 5 in 1 - Aparat Sonic Pentru Curatarea Tenului - Starlight",
    "ai_summary": None,
    "sections": [
        {"kind": "summary", "body": "Potrivit pentru o curățare profundă, dar delicată, acasă."},
        {"kind": "usage", "body": "Umezește fața, aplică gelul de curățare și folosește peria."},
    ],
    "review_summary": "Unele recenzii spun că dă rezultate vizibile.",
    "top_pros": ["dă rezultate vizibile"],
    "top_cons": [],
    "rating": 5.0,
    "review_count": 110,
}


def test_detail_why_comes_from_the_summary_section_not_the_name() -> None:
    """Turul `5cde771c`: sub „De ce ți-l recomand" stătea numele întreg de marketing."""
    ctx = _ctx("Spune-mi mai multe despre GESKE Sonic Facial Brush 5 in 1")
    ctx.business.domain_pack = _Pack()
    text = _detail_answer(dict(_BRUSH), ctx)
    assert "Potrivit pentru o curățare profundă" in text
    assert "Cum se folosește" in text and "Umezește fața" in text
    assert "Pentru Curatarea Tenului - Starlight" not in text


def test_review_answer_uses_the_short_name_and_does_not_repeat_itself() -> None:
    text, _ = _review_answer(dict(_BRUSH), "ro")
    assert text.count("dă rezultate vizibile") == 1
    assert "Starlight" not in text


def test_naturalize_replaces_the_caron_a_lookalike() -> None:
    """Turul `ee1a49f0`: «sonicǎ», de două ori."""
    assert naturalize("o perie facială sonicǎ") == "o perie facială sonică"
    assert naturalize("Ş și ţ rămân") == "Ş și ţ rămân"
