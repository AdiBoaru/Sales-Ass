"""NX-384 (`kernel.v7.0`) — ce a înțeles modelul ajunge în căutare: rafinarea moștenește sesiunea,
felul nemapat e subiect, raftul nu se judecă pe propriul nume, «nu vreau X» nu e subiectul.

Turele reale sunt din setul wide-2026-10-07 (release `302820b`), etichetate „interpretarea bună,
codul a stricat-o":
- `w1_produs_inexistent_similar#3` «sub 100 lei» după măști de noapte ⇒ textul căutat era «sub 100
  lei»; `w5_negatie_brand_cosrx#2` «si nu prea scump» ⇒ un produs; `w5_dermatita_maini#3` «arata-mi
  ceva fara parfum» ⇒ `no_query`, nimic arătat;
- `w2_rimel_ochi_sensibili_lentile#2` «waterproof» după «rimel» (tip absent din vocabular) ⇒ textul
  «waterproof», un produs;
- `w5_un_cuvant_machiaj_ochi#2` raftul `machiaj-ochi` spus, textul «ochi», iar garda NX-313 a scos
  raftul (0,04 din potriviri pe el) ⇒ șase produse de îngrijire a ochilor;
- `w5_negatie_fara_creme#1` «nu vreau creme» ⇒ umbrela de creme ca subiect, căutarea pe «crema».

Zero model, zero DB."""

from __future__ import annotations

from typing import Any

import pytest

from src.agent.turn_planner import plan_turn
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.delta import to_delta
from src.conversation.interpretation import CheckedChange
from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.tools import catalog_tools as ct
from src.tools.catalog_tools import SearchArgs, query_names_only_shelf, run_planned_search
from src.worker.runner import PipelineDeps
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import SOLE_SHELF, _gate, _interp, _need, _search, _state


def _session(query: str, *, category: str | None = SOLE_SHELF, name: str | None = None) -> dict:
    return {
        "filters": {"query": query, "category": category, "product_name": name},
        "pool": ["p1", "p2"],
        "cursor": 2,
        "fp": "x",
        "page": 0,
    }


def _checked(interp, meta):
    return [
        CheckedChange(
            change=change,
            dimension=dimension or change.dimension,
            canonical_value=change.value if change.number is None else change.number,
            provenance=provenance,
            strength="soft",
            rejected=None,
            matched=tuple(matched),
        )
        for change, (dimension, provenance, matched) in zip(interp.changes, meta, strict=True)
    ]


def _plan(query, changes=(), meta=None, *, session=None, needs=(), shelf=SOLE_SHELF, ptype=None):
    interp = _interp(acts=[{"kind": "find", "query": query}], changes=[dict(c) for c in changes])
    meta = meta or [(None, "explicit", ()) for _ in interp.changes]
    state = _state(shelf, needs=tuple(needs), active_search=session, product_type=ptype)
    return plan_turn(
        interp,
        state,
        (),
        (),
        _gate(),
        changed=bool(changes),
        pack=fc.pack("sole-ro"),
        vocab=fc.vocabulary("sole-ro"),
        locale="ro",
        checked=_checked(interp, meta),
    )


_UNDER_100 = {
    "op": "set",
    "dimension": "price",
    "relation": "lte",
    "value": None,
    "number": 100,
    "quote": "sub 100 lei",
}


# --- 2. felul nemapat e subiect -------------------------------------------------------------------


def _kind(value, quote):
    return {"op": "set", "dimension": "product_type", "relation": "eq", "value": value,
            "quote": quote}  # fmt: skip


def _unmapped(value, quote):
    return {"op": "add", "dimension": "unmapped", "relation": "contains", "value": value,
            "quote": quote}  # fmt: skip


def test_an_unmapped_kind_heads_the_search_with_its_modifiers():
    """«vreau un rimel waterproof»: tipul nu e în vocabular, validatorul îl duce pe `unmapped`; el
    rămâne capul textului, lângă modificatorul nemapat."""
    planned = _plan(
        "vreau un rimel waterproof",
        [_kind("rimel", "rimel"), _unmapped("waterproof", "waterproof")],
        [("unmapped", "explicit", ("rimel",)), ("unmapped", "explicit", ("waterproof",))],
        shelf=None,
    )
    assert _search(planned).query.split() == ["rimel", "waterproof"]


def test_an_unmapped_kind_beats_a_spoken_facet_as_the_search_text():
    """«un rimel pentru ten sensibil»: pe `main` fațeta filtrată (`named_filters`) bătea termenii
    nemapați, deci textul era «sensibil» și rimelul doar ordona."""
    sensitive = {"op": "set", "dimension": "skin_type", "relation": "eq", "value": "sensitive",
                 "quote": "ten sensibil"}  # fmt: skip
    planned = _plan(
        "un rimel pentru ten sensibil",
        [_kind("rimel", "rimel"), sensitive],
        [("unmapped", "explicit", ("rimel",)), (None, "explicit", ("ten", "sensibil"))],
        shelf=None,
        needs=[_need("skin_type", "sensitive")],
    )
    assert _search(planned).query.split()[0] == "rimel"


def test_an_unmapped_kind_next_to_a_typed_subject_joins_its_label():
    """Recenzia: o valoare pusă de model pe tip («mat»), dusă pe `unmapped`, nu înlocuiește tipul
    subiectului din stare: se adaugă etichetei lui."""
    planned = _plan(
        "dar mat",
        [_kind("mat", "mat")],
        [("unmapped", "explicit", ("mat",))],
        ptype="crema de fata",
    )
    words = _search(planned).query.split()
    assert "mat" in words and len(words) > 1, "eticheta tipului rămâne în text"


# --- 3. raftul nu se judecă pe propriul nume -----------------------------------------------------

MACHIAJ = VocabEntry(key="machiaj", label="Machiaj", count=681, path="machiaj")
OCHI = VocabEntry(key="machiaj-ochi", label="Ochi", count=210, path="machiaj/ochi")
_VOCAB = CatalogVocabulary(business_id="b", dimensions={"category": (MACHIAJ, OCHI)})


@pytest.mark.parametrize(
    ("query", "expected"),
    [("Ochi", True), ("ochi", True), ("machiaj ochi", True), ("crema ochi", False), ("", False)],
)
def test_query_names_only_shelf(query, expected):
    assert query_names_only_shelf(_VOCAB, ["machiaj-ochi"], query, "ro") is expected


def test_without_a_shelf_or_vocabulary_nothing_is_decided():
    assert not query_names_only_shelf(_VOCAB, [], "ochi", "ro")
    assert not query_names_only_shelf(None, ["machiaj-ochi"], "ochi", "ro")


@pytest.fixture
def lexical(monkeypatch) -> list[dict[str, Any]]:
    """Raftul `machiaj-ochi`: palete; fără raft, «ochi» prinde îngrijirea ochilor (mai multe)."""
    calls: list[dict[str, Any]] = []

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        calls.append({"query": query_text, **kwargs})
        if "machiaj-ochi" in tuple(kwargs.get("category") or ()):
            return [
                {"id": f"pal{i}", "name": f"Paleta {i}", "price": 80.0, "lexical_step": "strict"}
                for i in range(2)
            ]
        return [
            {"id": f"eye{i}", "name": f"Crema ochi {i}", "price": 60.0, "lexical_step": "strict"}
            for i in range(20)
        ]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return _VOCAB

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    return calls


class _LLM:
    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]


def _ctx(body: str, history: tuple[str, ...] = ()) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        history=[
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body=h) for h in history
        ],
        state=ConversationState(),
    )


async def test_the_planned_shelf_is_not_dropped_for_its_own_name(lexical):
    """`w5_un_cuvant_machiaj_ochi#2` («machiaj», apoi «ochi»): raftul Machiaj > Ochi, textul =
    numele lui. Pe `main` garda NX-313 îl scotea (`contradicted`) și servea îngrijirea ochilor."""
    ctx = _ctx("ochi", history=("machiaj",))
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    result = await run_planned_search(ctx, deps, SearchArgs(query="Ochi", category="machiaj-ochi"))
    assert [e for e in ctx.events if e.type == "category_query_is_shelf"]
    assert not [e for e in ctx.events if e.type == "guessed_filter_rescued"]
    assert result.products and all(str(p["id"]).startswith("pal") for p in result.products)


MACHIAJ_FATA = VocabEntry(key="machiaj-fata", label="Fata", count=274, path="machiaj/fata")
_FATA = CatalogVocabulary(business_id="b", dimensions={"category": (MACHIAJ, MACHIAJ_FATA)})


async def test_the_homograph_guard_still_judges_a_subshelf_named_by_a_common_word(monkeypatch):
    """Recenzia: «ceva pentru fata» pe Machiaj > Fata e un omograf (NX-319, cd98a513); textul «fata»
    e și numele raftului, dar rădăcina (Machiaj) n-a numit-o nimeni, deci NX-313 îl judecă."""

    async def fake_lexical(conn, business_id, *, query_text, **kwargs):
        if "machiaj-fata" in tuple(kwargs.get("category") or ()):
            return [{"id": f"bb{i}", "name": f"BB {i}", "price": 50.0, "lexical_step": "strict"}
                    for i in range(3)]  # fmt: skip
        return [{"id": f"cr{i}", "name": f"Crema fata {i}", "price": 60.0,
                 "lexical_step": "strict"} for i in range(20)]  # fmt: skip

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return _FATA

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)
    ctx = _ctx("ceva pentru fata")
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    await run_planned_search(ctx, deps, SearchArgs(query="fata", category="machiaj-fata"))
    types = [e.type for e in ctx.events]
    assert "category_query_is_shelf" not in types


# --- 4. «nu vreau X» nu e subiectul ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("dimension", "value"),
    [("product_type", "crema de fata"), ("category", "machiaj-fata")],
)
@pytest.mark.parametrize("provenance", ["explicit", "implicit"])
def test_an_avoided_subject_is_an_exclusion_not_the_topic(dimension, value, provenance):
    """`w5_negatie_fara_creme#1`: «nu vreau creme» devenea umbrela subiectului."""
    change = {"op": "set", "dimension": dimension, "relation": "avoid", "value": value,
              "quote": "nu vreau creme"}  # fmt: skip
    interp = _interp(acts=[{"kind": "find", "query": "nu vreau creme"}], changes=[change])
    checked = _checked(interp, [(None, provenance, ("creme",))])
    delta = to_delta(interp, checked, turn_id="t1")
    ops = [(p.op, p.key) for p in delta.proposals]
    assert not [op for op, _ in ops if op == "set_topic"], ops
    assert ("set_need", "restriction") in ops
