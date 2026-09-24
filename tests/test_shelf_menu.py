"""Meniul ÎNCHIS de rafturi — conversația reală `cd98a513` (`sole-ro`, 2026-09-24).

«vreau o crema de fata» → modelul a trimis `category="fata"`, adică Machiaj > Fata, fiindcă
promptul îi arăta rafturile prin NUME (21 din 45 se repetă). Pe turul 2 garda NX-313 n-a putut
judeca raftul (treapta `relaxed`), iar clientul a primit BB cream-uri și pudre la «calmare».
Acum modelul vede CHEILE. O valoare din afara listei se numără (`category_off_menu`) și cade pe
rezolvarea liberă, cu gărzile ei: blocată, ar fi stricat numele unice
(`scripts/shelf_menu_probe.py --replay`).

Pur sau cu DB stubuită: fără rețea, fără credite.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.prompt_builder import PromptInputs, _store_header
from src.catalog.vocabulary import (
    CatalogVocabulary,
    ResolutionStatus,
    VocabEntry,
    category_on_menu,
)
from src.config import get_settings
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import search_products_tool
from src.worker.runner import PipelineDeps

MACHIAJ = VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj")
MACHIAJ_FATA = VocabEntry(key="machiaj-fata", label="Fata", count=274, path="machiaj/fata")
TEN = VocabEntry(key="ten", label="Ten", count=1461, path="ten")
TEN_ING = VocabEntry(
    key="ten-ingrijirea-tenului", label="Ingrijirea tenului", count=933, path="ten/ingrijirea"
)
BARBATI_TEN = VocabEntry(
    key="barbati-ingrijirea-tenului", label="Ingrijirea tenului", count=1, path="barbati/ing"
)
_SOLE = CatalogVocabulary(
    business_id="b",
    dimensions={"category": (MACHIAJ, MACHIAJ_FATA, TEN, TEN_ING, BARBATI_TEN)},
)


# --- funcția pură ------------------------------------------------------------


def test_exact_key_is_on_the_menu() -> None:
    r = category_on_menu(_SOLE, "ten-ingrijirea-tenului")
    assert r.status is ResolutionStatus.KNOWN and r.key == "ten-ingrijirea-tenului"
    assert r.matched_by == "menu"
    # Majusculele și spațiile de pe margine nu fac altă cheie.
    assert category_on_menu(_SOLE, " Ten-Ingrijirea-Tenului ").key == "ten-ingrijirea-tenului"


def test_a_shelf_name_is_not_a_key() -> None:
    """«Fata» e Machiaj > Fata, iar «Ingrijirea tenului» există de două ori: numele nu alege."""
    for term in ("fata", "Fata", "Ingrijirea tenului", "ingrijirea-tenului"):
        r = category_on_menu(_SOLE, term)
        assert r.status is ResolutionStatus.UNKNOWN and r.reason == "off_menu", term
        assert r.constraint_keys == ()


def test_unavailable_vocabulary_is_not_judged() -> None:
    """DB jos ⇒ n-am putut judeca, nu „valoare greșită" (garda off-category nu are voie să taie)."""
    r = category_on_menu(CatalogVocabulary(business_id="b"), "ten")
    assert r.reason == "unknown_dimension"


# --- promptul -----------------------------------------------------------------


def _inputs(shelf_menu: bool) -> PromptInputs:
    cats = [("machiaj-fata", 274), ("ten-ingrijirea-tenului", 933)]
    return PromptInputs.build("SOLE", "ecommerce", "ro", cats, [], shelf_menu=shelf_menu)


def test_prompt_presents_keys_as_a_closed_menu() -> None:
    header = _store_header(_inputs(True))
    assert "machiaj-fata (270)" in header and "ten-ingrijirea-tenului (930)" in header
    assert "ÎNCHISĂ" in header and "EXACT" in header


def test_prompt_flag_off_is_the_old_wording() -> None:
    header = _store_header(_inputs(False))
    assert "Vinzi din aceste categorii" in header and "ÎNCHISĂ" not in header


# --- cablajul în unealta de căutare (DB stubuită) ----------------------------


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _ctx(body: str) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )


@pytest.fixture
def lexical_calls(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        calls.append({"query": query_text, **kwargs})
        category = tuple(kwargs.get("category") or ())
        if "machiaj-fata" in category:
            return [{"id": f"bb{i}", "name": f"BB Cream {i}", "price": 50.0} for i in range(6)]
        return [
            {"id": f"cr{i}", "name": f"Crema de fata {i}", "price": 60.0, "lexical_step": "strict"}
            for i in range(20)
        ]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return _SOLE

    monkeypatch.setattr(get_settings(), "search_category_menu_enabled", True)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    return calls


async def test_off_menu_name_is_counted_and_falls_back_to_the_guards(lexical_calls) -> None:
    """Turul `13c17496` dacă modelul tot trimite «fata»: nu se blochează (numele unice, «buze»,
    «styling», erau corecte pe trafic), ci se numără și trece prin garda NX-319 + NX-313, care
    scoate raftul de machiaj."""
    ctx = _ctx("vreau o crema de fata")
    result = await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "category": "fata"},
    )
    off = [e for e in ctx.events if e.type == "category_off_menu"]
    assert off and off[0].properties["value"] == "fata"
    assert off[0].properties["resolved"] == "known"
    assert [e for e in ctx.events if e.type == "category_subshelf_homograph"]
    assert result.products and all(str(p["id"]).startswith("cr") for p in result.products)


async def test_key_from_the_menu_filters_and_is_not_counted(lexical_calls) -> None:
    ctx = _ctx("vreau o crema de fata")
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "category": "ten-ingrijirea-tenului"},
    )
    assert "ten-ingrijirea-tenului" in tuple(lexical_calls[0]["category"])
    assert not [e for e in ctx.events if e.type == "category_off_menu"]
    ev = [e for e in ctx.events if e.type == "vocabulary_resolved"]
    assert ev and ev[0].properties["matched_by"] == "menu"


async def test_flag_off_counts_nothing(lexical_calls, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "search_category_menu_enabled", False)
    ctx = _ctx("vreau o crema de fata")
    await search_products_tool(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM()),
        {"query": "crema de fata", "category": "fata"},
    )
    assert not [e for e in ctx.events if e.type == "category_off_menu"]
