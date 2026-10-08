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
from src.models import BusinessConfig, Contact, ConversationState, InboundMessage, TurnContext
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


# --- 1. rafinarea moștenește sesiunea ------------------------------------------------------------


def test_a_price_refinement_searches_what_the_client_was_shown():
    """`w1_produs_inexistent_similar#3`: «sub 100 lei» după o căutare de măști de noapte."""
    needs = [_need("budget_max", 100.0, "hard")]
    args = _search(
        _plan("sub 100 lei", [_UNDER_100], session=_session("masca de noapte"), needs=needs)
    )
    assert args.query == "masca de noapte"
    assert args.price_max == 100.0


def test_without_a_session_the_refinement_keeps_the_old_text():
    needs = [_need("budget_max", 100.0, "hard")]
    args = _search(_plan("sub 100 lei", [_UNDER_100], needs=needs))
    assert args.query != "masca de noapte"


def test_a_flag_only_turn_refines_the_session_instead_of_giving_up():
    """`w5_dermatita_maini#3`: «arata-mi ceva fara parfum» ⇒ `no_query` pe `main`."""
    flag = {
        "op": "set",
        "dimension": "fragrance_free",
        "relation": "eq",
        "value": "true",
        "quote": "fara parfum",
    }
    session = _session("ce crema pe maini dermatita")
    planned = _plan(
        "arata-mi ceva fara parfum",
        [flag],
        [(None, "explicit", ("fara", "parfum"))],
        session=session,
        needs=[_need("fragrance_free", True)],
    )
    assert _search(planned).query == "ce crema pe maini dermatita"


def test_a_turn_without_changes_keeps_its_own_words():
    """Un tur fără nicio schimbare poate fi o cerere NOUĂ pe care interpretarea n-a etichetat-o:
    nu moștenește sesiunea."""
    args = _search(_plan("vreau ceva pentru picioare uscate", session=_session("masca de noapte")))
    assert "masca" not in args.query


@pytest.mark.parametrize(
    "session",
    [
        _session("masca de noapte", category="par"),  # sesiunea altui raft
        _session("Laneige Water Sleeping Mask", name="laneige water sleeping mask"),  # pe nume
    ],
)
def test_a_session_of_another_subject_is_not_inherited(session):
    needs = [_need("budget_max", 100.0, "hard")]
    args = _search(_plan("sub 100 lei", [_UNDER_100], session=session, needs=needs))
    assert args.query not in ("masca de noapte", "Laneige Water Sleeping Mask")


def test_a_typed_subject_beats_the_session():
    """Cu tipul subiectului în stare, eticheta lui rămâne textul (NX-352)."""
    needs = [_need("budget_max", 100.0, "hard")]
    args = _search(
        _plan(
            "sub 100 lei",
            [_UNDER_100],
            session=_session("ceva vechi"),
            needs=needs,
            ptype="crema de fata",
        )
    )
    assert args.query != "ceva vechi"


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


def test_the_next_modifier_refines_the_unmapped_kind_through_the_session():
    """`w2_rimel_ochi_sensibili_lentile#2`: «waterproof» la turul următor caută tot rimelul."""
    planned = _plan(
        "waterproof",
        [_unmapped("waterproof", "waterproof")],
        [("unmapped", "explicit", ("waterproof",))],
        session=_session("rimel", category=None),
        shelf=None,
        needs=[_need("unmapped", "rimel")],
    )
    assert _search(planned).query.split() == ["waterproof", "rimel"] or (
        set(_search(planned).query.split()) == {"rimel", "waterproof"}
    )


def test_a_new_unmapped_kind_is_not_glued_to_the_old_session():
    """Un fel nou spus în tur («iluminator» după rimel) e subiectul turului, nu o rafinare."""
    planned = _plan(
        "si un iluminator",
        [_kind("iluminator", "iluminator")],
        [("unmapped", "explicit", ("iluminator",))],
        session=_session("rimel", category=None),
        shelf=None,
    )
    assert _search(planned).query == "iluminator"


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


def _ctx(body: str) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )


async def test_the_planned_shelf_is_not_dropped_for_its_own_name(lexical):
    """`w2_paleta_ochi_caprui_mate#2` («doar mate, fara sclipici»): raftul din stare, textul =
    eticheta lui («Ochi»). Pe `main` garda NX-313 îl scotea (`contradicted`) și servea îngrijirea
    ochilor."""
    ctx = _ctx("doar mate, fara sclipici")
    deps = PipelineDeps(conn=object(), redis=None, llm=_LLM())
    result = await run_planned_search(ctx, deps, SearchArgs(query="Ochi", category="machiaj-ochi"))
    assert [e for e in ctx.events if e.type == "category_query_is_shelf"]
    assert not [e for e in ctx.events if e.type == "guessed_filter_rescued"]
    assert result.products and all(str(p["id"]).startswith("pal") for p in result.products)


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
