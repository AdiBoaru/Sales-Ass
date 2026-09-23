"""NX-314 — subiectul conversației: obiectul pur, persistarea pe ambele stări, măsurătoarea.

Fără DB și fără model. SQL-ul consumatorului («mai ieftin») are fișierul lui
(`test_cheaper_subject_sql.py`), iar ordinea pe catalogul real e în `test_cheaper_subject_db.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import get_settings
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import (
    MAX_STATE_BYTES,
    ConversationStateV2,
    DisplayedRef,
    Need,
    References,
    Topic,
    adapt_v1,
    bounded_map,
    project_v1,
    serialize,
)
from src.conversation.subject import (
    MAX_SUBJECT_BYTES,
    SUBJECT_KEY,
    ConversationSubject,
    derive_subject,
    dominant_type,
    resolve_needs,
    resolve_shelf,
    subject_match_report,
)
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    ProductRef,
    Route,
    RouteDecision,
    TurnContext,
)

CREMA = "crema de fata"
SER = "ser de fata"
MASCA = "masca de fata"


def _p(ptype: str | None, **attrs) -> dict:
    a = dict(attrs)
    if ptype is not None:
        a["product_type"] = ptype
    return {"id": f"p-{ptype}-{len(a)}", "attributes": a}


def _vocab() -> CatalogVocabulary:
    return CatalogVocabulary(
        business_id="b1",
        dimensions={
            "category": (
                VocabEntry(
                    key="ten-ingrijirea-tenului", label="Ingrijire ten", count=933, path="ten"
                ),
                VocabEntry(key="machiaj-fata", label="Fata", count=120, path="machiaj/fata"),
            ),
            "concerns": (
                VocabEntry(key="hydration", label="hidratare", count=500),
                VocabEntry(key="acne", label="acnee", count=518),
            ),
        },
    )


# ── regula tipului dominant ──────────────────────────────────────────────────────────────────────


def test_dominant_type_needs_strictly_more_than_half_of_the_typed_products():
    assert dominant_type([CREMA] * 5 + [SER]) == CREMA
    assert dominant_type([CREMA, CREMA, SER, SER]) is None  # jumătate nu e majoritate
    assert dominant_type([CREMA, SER, MASCA]) is None


def test_untyped_products_do_not_count_against_the_majority():
    """24% din catalogul SOLE n-are tip: patru creme și două netipate NU sunt un set amestecat."""
    assert dominant_type([CREMA, CREMA, CREMA, CREMA, None, None]) == CREMA


def test_a_single_typed_product_is_not_a_dominant_type():
    assert dominant_type([CREMA]) is None
    assert dominant_type([CREMA, None, None]) is None


# ── derive_subject: happy path + continuitate ────────────────────────────────────────────────────


def test_a_recommendation_of_creams_with_a_spoken_need_becomes_the_subject():
    subject = derive_subject(
        displayed=[_p(CREMA)] * 5 + [_p(SER)],
        shelf="Ingrijire ten",
        needs=resolve_needs(_vocab(), ["hidratare"]),
        vocab=_vocab(),
        previous=None,
        turn_id="t1",
    )
    assert subject.product_type == CREMA
    assert subject.shelf_key == "ten-ingrijirea-tenului"
    assert subject.needs == (("concerns", "hydration"),)
    assert subject.source_turn_id == "t1"


def test_a_turn_without_new_products_inherits_the_subject():
    """Turul de howto dintre recomandare și «mai ieftin»: subiectul rămâne neschimbat."""
    first = derive_subject(
        displayed=[_p(CREMA)] * 5 + [_p(SER)],
        shelf=None,
        needs=(("concerns", "hydration"),),
        vocab=None,
        previous=None,
        turn_id="t1",
    )
    howto = derive_subject(
        displayed=[], shelf=None, needs=(), vocab=None, previous=first, turn_id="t3"
    )
    assert howto == first


def test_the_real_conversation_keeps_the_cream_through_a_single_product_detail_turn():
    """Conversația `f4e1431e` (sondă): 6 creme → detaliu pe O cremă → «mai ieftin».

    Cu regula strictă „cel puțin 2", turul de detaliu ar fi șters subiectul exact înainte de
    «mai ieftin»; sonda a arătat că setul imediat anterior avea un singur produs."""
    subject = None
    for shown in ([_p(CREMA)] * 6, [_p(CREMA)], [_p(CREMA)]):
        subject = derive_subject(
            displayed=shown, shelf=None, needs=(), vocab=None, previous=subject, turn_id="t"
        )
    assert subject is not None and subject.product_type == CREMA


def test_a_single_product_does_not_override_an_existing_subject():
    first = derive_subject(
        displayed=[_p(CREMA)] * 4, shelf=None, needs=(), vocab=None, previous=None
    )
    detail = derive_subject(displayed=[_p(MASCA)], shelf=None, needs=(), vocab=None, previous=first)
    assert detail.product_type == CREMA


def test_a_single_product_anchors_a_subject_when_there_is_none():
    assert (
        derive_subject(
            displayed=[_p(SER)], shelf=None, needs=(), vocab=None, previous=None
        ).product_type
        == SER
    )


# ── edge: set amestecat, reset, sinonim ──────────────────────────────────────────────────────────


def test_a_mixed_set_has_no_subject_type():
    subject = derive_subject(
        displayed=[_p(CREMA), _p(SER), _p(MASCA), _p(CREMA), _p(SER), _p(MASCA)],
        shelf=None,
        needs=(),
        vocab=None,
        previous=None,
    )
    assert subject.product_type is None


def test_a_new_dominant_type_resets_the_inherited_needs():
    first = derive_subject(
        displayed=[_p(CREMA)] * 4,
        shelf=None,
        needs=(("concerns", "hydration"),),
        vocab=None,
        previous=None,
    )
    second = derive_subject(
        displayed=[_p(SER)] * 4, shelf=None, needs=(), vocab=None, previous=first
    )
    assert second.product_type == SER
    assert second.needs == ()


def test_topic_switch_resets_the_subject():
    first = derive_subject(
        displayed=[_p(CREMA)] * 4,
        shelf=None,
        needs=(("concerns", "hydration"),),
        vocab=None,
        previous=None,
    )
    after = derive_subject(
        displayed=[], shelf=None, needs=(), vocab=None, previous=first, topic_switched=True
    )
    assert after.product_type is None and after.needs == ()


def test_a_shelf_synonym_is_persisted_as_the_resolved_key():
    assert resolve_shelf(_vocab(), "ingrijire ten") == "ten-ingrijirea-tenului"


def test_an_unresolvable_shelf_is_not_persisted():
    assert resolve_shelf(_vocab(), "cabluri usb") is None


def test_vocabulary_unavailable_gives_no_shelf_and_no_exception():
    """DB jos la încărcare ⇒ vocabular gol sau absent ⇒ `shelf_key=None`, fără excepție."""
    empty = CatalogVocabulary(business_id="b1")
    assert resolve_shelf(None, "Ingrijire ten") is None
    assert resolve_shelf(empty, "Ingrijire ten") is None
    assert resolve_needs(None, ["hidratare"]) == ()
    subject = derive_subject(
        displayed=[_p(CREMA)] * 3, shelf="Ingrijire ten", needs=(), vocab=empty, previous=None
    )
    assert subject.shelf_key is None and subject.product_type == CREMA


# ── forma persistată ─────────────────────────────────────────────────────────────────────────────


def test_the_persisted_subject_stays_under_its_byte_budget():
    fat = ConversationSubject(
        shelf_key="x" * 48,
        product_type="y" * 48,
        needs=tuple(("concerns", "z" * 48) for _ in range(3)),
        source_turn_id="0" * 36,
    )
    doc = fat.to_dict()
    assert len(json.dumps(doc, separators=(",", ":")).encode()) <= MAX_SUBJECT_BYTES
    assert doc["type"] == "y" * 48  # tipul nu se sacrifică


def test_a_corrupt_subject_reads_as_none():
    assert ConversationSubject.from_dict("nu e dict") is None
    assert ConversationSubject.from_dict({}) is None
    assert ConversationSubject.from_dict({"needs": [["a"], 3, None]}) is None
    ok = ConversationSubject.from_dict({"type": CREMA, "needs": [["concerns", "hydration"], 7]})
    assert ok == ConversationSubject(product_type=CREMA, needs=(("concerns", "hydration"),))


# ── starea v2: câmpul nou, reducer, proiecție ────────────────────────────────────────────────────


def _policy() -> ReducerPolicy:
    return ReducerPolicy(vocabulary=NeedVocabulary())


def _subject_proposal(ptype: str | None, shelf: str | None = None) -> StateUpdateProposal:
    return StateUpdateProposal(
        "set_topic",
        category_key=shelf,
        product_type=ptype,
        subject=True,
        source="catalog",
        turn_id="t1",
    )


def test_topic_product_type_round_trips_through_jsonb():
    topic = Topic(category_key="ten", product_type=CREMA)
    assert Topic.from_jsonb(topic.to_jsonb()) == topic
    assert Topic.from_jsonb({"category_key": "ten"}).product_type is None  # aditiv


def test_the_subject_proposal_is_applied_without_a_shelf():
    reduced = reduce_all(ConversationStateV2(), [_subject_proposal(CREMA)], _policy())
    assert reduced.state.topic.product_type == CREMA
    assert not reduced.rejected


def test_the_subject_proposal_keeps_the_current_shelf_when_its_own_is_unresolved():
    state = ConversationStateV2(topic=Topic(category_key="ten", product_type=SER))
    reduced = reduce_all(state, [_subject_proposal(CREMA)], _policy())
    assert reduced.state.topic == Topic(category_key="ten", product_type=CREMA)


def test_a_model_inferred_topic_cannot_move_a_subject_backed_by_the_shown_set():
    state = ConversationStateV2(topic=Topic(category_key="ten", product_type=CREMA))
    brain = StateUpdateProposal(
        "set_topic", category_key="machiaj", source="model_inferred", turn_id="t2"
    )
    reduced = reduce_all(state, [brain], _policy())
    assert reduced.state.topic.category_key == "ten"
    assert [r.reason for r in reduced.rejected] == ["subject_owned"]


def test_a_model_inferred_topic_still_works_without_a_subject():
    """Fără subiect coroborat, comportamentul de dinainte rămâne."""
    brain = StateUpdateProposal(
        "set_topic", category_key="machiaj", source="model_inferred", turn_id="t2"
    )
    reduced = reduce_all(ConversationStateV2(), [brain], _policy())
    assert reduced.state.topic.category_key == "machiaj"


def test_project_v1_exposes_the_subject_to_v1_readers():
    state = ConversationStateV2(
        topic=Topic(category_key="ten", product_type=CREMA),
        needs=(
            Need(key="concerns", operator="contains", normalized_value="hydration"),
            Need(key="budget_max", operator="lte", normalized_value=150.0),
            Need(
                key="concerns",
                operator="contains",
                normalized_value="acne",
                source="model_inferred",
            ),
        ),
    )
    subject = ConversationSubject.from_dict(project_v1(state)["search_constraints"][SUBJECT_KEY])
    assert subject is not None
    assert subject.product_type == CREMA and subject.shelf_key == "ten"
    assert subject.needs == (("concerns", "hydration"),)  # doar ROSTITE, doar fațete


def test_no_subject_key_is_projected_without_a_product_type():
    """Flag stins ⇒ nimeni nu scrie tipul ⇒ proiecția e cea de dinainte."""
    state = ConversationStateV2(topic=Topic(category_key="ten"))
    assert SUBJECT_KEY not in project_v1(state)["search_constraints"]


def test_adapt_v1_moves_the_subject_type_on_the_topic_and_not_into_needs():
    raw = {
        "search_constraints": {
            "category_key": "ten",
            SUBJECT_KEY: {"type": CREMA, "needs": [["concerns", "hydration"]]},
        }
    }
    state = adapt_v1(raw, NeedVocabulary())
    assert state.topic.product_type == CREMA
    assert all(n.key != SUBJECT_KEY for n in state.needs)


def test_bounded_map_keeps_short_nested_lists():
    """Reparația 3: pe v2 `active_search.filters.concerns` dispărea la commit."""
    kept = bounded_map({"filters": {"concerns": ["hidratare", "acnee"], "price_max": 100}})
    assert kept == {"filters": {"concerns": ["hidratare", "acnee"], "price_max": 100}}
    long = bounded_map({"filters": {"concerns": [str(i) for i in range(9)]}})
    assert long == {"filters": {"concerns": ["0", "1", "2", "3", "4"]}}
    nested = bounded_map({"filters": {"x": [{"no": 1}]}})
    assert nested == {"filters": {}}


def test_a_near_full_v2_state_with_a_subject_still_fits():
    """Stare v2 aproape de buget ⇒ `serialize` nu depășește `MAX_STATE_BYTES`."""
    state = ConversationStateV2(
        topic=Topic(category_key="k" * 64, product_type="t" * 48),
        needs=tuple(
            Need(key=f"k{i}", operator="eq", normalized_value="v" * 64, source_turn_id="x" * 36)
            for i in range(16)
        ),
        references=References(
            displayed_products=tuple(
                DisplayedRef(product_id=str(i) * 36, name="n" * 80, price=10.0) for i in range(8)
            )
        ),
        active_search={f"f{i}": ["y" * 64] * 5 for i in range(8)},
    )
    doc, size, _ = serialize(state)
    assert size <= MAX_STATE_BYTES
    assert doc["topic"]["product_type"] == "t" * 48


# ── proprietarul unic: `_learn_constraints` ──────────────────────────────────────────────────────


def _ctx(body: str) -> TurnContext:
    ctx = TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m1", body=body),
        conversation_id="conv1",
    )
    ctx.route = RouteDecision(route=Route.SALES)
    return ctx


@pytest.fixture
def _subject_on(monkeypatch):
    from src.worker.stages import agent as agent_mod

    s = get_settings()
    monkeypatch.setattr(s, "observed_constraints_enabled", True)
    monkeypatch.setattr(s, "conversation_subject_enabled", True)

    async def _vocab_of(deps, business_id):  # noqa: ARG001
        return _vocab()

    monkeypatch.setattr(agent_mod, "get_vocabulary", _vocab_of)
    return agent_mod


def _run(retrieved: list[dict], args: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(search_args=args or [{"query": "crema"}], retrieved=retrieved)


async def test_the_owner_writes_the_subject_on_v1_and_proposes_it_on_v2(_subject_on):
    ctx = _ctx("vreau o crema de hidratare")
    run = _run(
        [_p(CREMA)] * 5 + [_p(SER)],
        [{"query": "crema", "category": "ingrijire ten", "concerns": ["hidratare"]}],
    )
    await _subject_on._learn_constraints(ctx, run, ctx.message.body, None)

    subject = ConversationSubject.from_dict(ctx.state.search_constraints[SUBJECT_KEY])
    assert subject == ConversationSubject(
        shelf_key="ten-ingrijirea-tenului",
        product_type=CREMA,
        needs=(("concerns", "hydration"),),
        source_turn_id="t1",
    )
    # Reparația 2: raftul persistat e cheia, nu șirul modelului.
    assert ctx.state.search_constraints["category_key"] == "ten-ingrijirea-tenului"
    topic = [p for p in ctx.state_proposals if p.op == "set_topic"]
    assert len(topic) == 1 and topic[0].subject and topic[0].product_type == CREMA


async def test_the_subject_survives_the_v2_commit(_subject_on, monkeypatch):
    """DoD: pe v2 write, subiectul supraviețuiește commit-ului (docul vine din propuneri)."""
    from src.worker.processor import _build_new_state

    s = get_settings()
    monkeypatch.setattr(s, "conversation_state_v2_enabled", True)
    monkeypatch.setattr(s, "conversation_state_v2_write_enabled", True)
    ctx = _ctx("vreau o crema de hidratare")
    await _subject_on._learn_constraints(
        ctx, _run([_p(CREMA)] * 4, [{"query": "crema"}]), ctx.message.body, None
    )
    ctx.set_reply("ok")
    doc = _build_new_state({}, ctx, is_rich=False, has_products=False)
    assert doc["topic"]["product_type"] == CREMA
    # Și turul următor îl citește prin proiecție, ca orice cititor v1.
    from src.models import ConversationState

    read_back = ConversationState.from_jsonb(doc)
    assert ConversationSubject.from_dict(
        read_back.search_constraints[SUBJECT_KEY]
    ).product_type == (CREMA)


async def test_a_shelf_change_keeps_the_needs_the_client_just_stated_on_v2(
    _subject_on, monkeypatch
):
    """Regresie găsită la verificare: `set_topic` pus DUPĂ `set_need`-urile turului retrăgea
    bugetul și nevoia tocmai rostite (`superseded` + tombstone `topic_reset`), fiindcă reducerul
    leagă nevoia de raftul curent, iar schimbarea de raft o retrăgea imediat."""
    from src.models import ConversationState
    from src.worker.processor import _build_new_state

    s = get_settings()
    monkeypatch.setattr(s, "conversation_state_v2_enabled", True)
    monkeypatch.setattr(s, "conversation_state_v2_write_enabled", True)
    base = {"schema_version": 2, "revision": 3, "topic": {"category_key": "machiaj-fata"}}
    ctx = _ctx("vreau o crema pentru hidratare sub 100 lei")
    ctx.state = ConversationState.from_jsonb(base)
    run = _run(
        [_p(CREMA)] * 4,
        [
            {
                "query": "crema",
                "category": "ingrijire ten",
                "concerns": ["hidratare"],
                "price_max": 100,
            }
        ],
    )
    await _subject_on._learn_constraints(ctx, run, ctx.message.body, None)
    assert ctx.state_proposals[0].op == "set_topic"
    ctx.set_reply("ok")
    doc = _build_new_state(base, ctx, is_rich=False, has_products=False)
    needs = {n["key"]: n for n in doc["needs"]}
    assert needs["budget_max"]["status"] == "active"
    assert needs["concerns"]["status"] == "active"
    assert not doc.get("revocations")
    # Raftul rămâne CHEIA de catalog, nu forma `norm_key` (cu `_`), pe care SQL-ul n-o recunoaște.
    assert doc["topic"]["category_key"] == "ten-ingrijirea-tenului"


async def test_the_v1_subject_survives_a_stack_reset_that_keeps_the_same_subject(_subject_on):
    """`merge_constraints` golește subiectul la o schimbare de raft; dacă subiectul derivat e
    același, cheia trebuie totuși rescrisă."""
    ctx = _ctx("vreau o crema")
    same = ConversationSubject(
        shelf_key="ten-ingrijirea-tenului", product_type=CREMA, source_turn_id="t1"
    )
    ctx.state.search_constraints = {"category_key": "altceva", SUBJECT_KEY: same.to_dict()}
    await _subject_on._learn_constraints(
        ctx,
        _run([_p(CREMA)] * 4, [{"query": "crema", "category": "ingrijire ten"}]),
        ctx.message.body,
        None,
    )
    assert ConversationSubject.from_dict(ctx.state.search_constraints[SUBJECT_KEY]) == same


@pytest.mark.parametrize("retrieved", [[_p(CREMA)] * 4, [_p(CREMA), _p(SER), _p(MASCA)]])
async def test_v1_and_v2_agree_on_the_subject_in_shadow_mode(_subject_on, monkeypatch, retrieved):
    """Cu v2 în SHADOW, `state_diff` compară v1 cu proiecția v2. Un subiect fără tip scris doar pe
    v1 ar apărea ca diferență falsă; cu tip, trebuie să fie pe amândouă."""
    from src.conversation.state_v2 import state_diff
    from src.worker.processor import _build_state_v2

    monkeypatch.setattr(get_settings(), "conversation_state_v2_enabled", True)
    ctx = _ctx("vreau o crema")
    await _subject_on._learn_constraints(
        ctx,
        _run(retrieved, [{"query": "crema", "category": "ingrijire ten"}]),
        "vreau o crema",
        None,
    )
    ctx.set_reply("ok")
    state_v2, _ = _build_state_v2({}, ctx, is_rich=False, has_products=False)
    v1 = {"search_constraints": ctx.state.search_constraints}
    diff = state_diff(v1, project_v1(state_v2))
    assert not any(SUBJECT_KEY in keys for keys in diff.values()), diff


async def test_a_cheaper_turn_does_not_rederive_the_subject(_subject_on):
    """Setul lui «mai ieftin» e ales RELATIV la subiect: a-l citi înapoi ar fi circular."""
    ctx = _ctx("si ceva mai ieftin")
    ctx.state.displayed_products = [ProductRef(product_id="p1", name="Crema", price=110.0)]
    ctx.state.search_constraints = {SUBJECT_KEY: {"type": CREMA}}
    await _subject_on._learn_constraints(
        ctx, _run([_p(MASCA)] * 6, [{"query": "masca"}]), ctx.message.body, None
    )
    assert ctx.state.search_constraints[SUBJECT_KEY] == {"type": CREMA}
    assert not [p for p in ctx.state_proposals if p.op == "set_topic"]


async def test_flag_off_writes_no_subject_and_keeps_the_raw_shelf(_subject_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "conversation_subject_enabled", False)
    ctx = _ctx("vreau o crema")
    await _subject_on._learn_constraints(
        ctx,
        _run([_p(CREMA)] * 4, [{"query": "crema", "category": "ingrijire ten"}]),
        ctx.message.body,
        None,
    )
    assert SUBJECT_KEY not in ctx.state.search_constraints
    assert ctx.state.search_constraints["category_key"] == "ingrijire ten"  # ca înainte
    assert not [p for p in ctx.state_proposals if p.op == "set_topic"]


def test_merge_constraints_carries_the_subject_and_drops_it_on_reset():
    from src.worker.stages.agent import merge_constraints

    stored = {"category_key": "ten", SUBJECT_KEY: {"type": CREMA}}
    kept, _ = merge_constraints(stored, {}, None)
    assert kept[SUBJECT_KEY] == {"type": CREMA}
    dropped, reset = merge_constraints(stored, {}, None, switched_topic=True)
    assert reset and SUBJECT_KEY not in dropped


# ── măsurătoarea `subject_match` ────────────────────────────────────────────────────────────────


def test_subject_match_report_counts_matching_cards_and_names_the_path():
    subject = ConversationSubject(product_type=CREMA, needs=(("concerns", "hydration"),))
    report = subject_match_report(
        subject, [_p(CREMA), _p(CREMA), _p(MASCA)], ["cheaper_followup", "tool_call"]
    )
    assert report == {
        "path": "cheaper",
        "subject_type_known": True,
        "served": 3,
        "type_matched": 2,
        "share": 0.67,
        "needs_count": 1,
        "type_change_expected": False,
    }


def test_cross_sell_is_reported_as_an_intended_type_change():
    report = subject_match_report(
        ConversationSubject(product_type=CREMA), [_p(SER)], ["product_search", "cross_sell"]
    )
    assert report["path"] == "cross_sell" and report["type_change_expected"] is True


def test_cards_without_a_retrieval_event_are_a_rehydrate_and_no_cards_emit_nothing():
    assert subject_match_report(None, [_p(CREMA)], [])["path"] == "rehydrate"
    assert subject_match_report(None, [_p(CREMA)], [])["share"] is None
    assert subject_match_report(ConversationSubject(product_type=CREMA), [], ["x"]) is None


def test_the_runner_emits_subject_match_without_client_text_or_product_ids(monkeypatch):
    from src.worker.runner import _emit_subject_match

    monkeypatch.setattr(get_settings(), "conversation_subject_enabled", True)
    ctx = _ctx("vreau o crema de hidratare")
    ctx.state.search_constraints = {SUBJECT_KEY: {"type": CREMA}}
    ctx.emit("product_search", n=2)
    ctx.set_reply("iata", products=[_p(CREMA), _p(SER)])
    _emit_subject_match(ctx)
    ev = [e for e in ctx.events if e.type == "subject_match"]
    assert len(ev) == 1
    blob = json.dumps(ev[0].properties)
    assert "hidratare" not in blob and "p-" not in blob
    assert ev[0].properties["type_matched"] == 1


# ── modelul află că o completare e completare ───────────────────────────────────────────────────


def test_brief_declares_a_filler_of_another_type():
    from src.tools.catalog_tools import _brief

    rows = [
        {"id": "a", "name": "Crema A", "price": 50.0, "subject_match": True},
        {"id": "b", "name": "Masca B", "price": 10.0, "subject_match": False},
        {"id": "c", "name": "Crema C", "price": 40.0},
    ]
    lines = _brief(rows).splitlines()
    assert "completare" not in lines[0]
    assert "completare, alt tip de produs" in lines[1]
    assert "completare" not in lines[2]


def test_subject_match_never_reaches_the_wire():
    """`subject_match` e o cheie pentru MODEL; cardurile web se construiesc pe câmpuri numite."""
    from src.channels.web.render import render_web

    ctx = _ctx("si ceva mai ieftin")
    ctx.set_reply(
        "iata",
        products=[
            {
                "id": "a",
                "name": "Crema A",
                "price": 50.0,
                "url": "https://x/a",
                "subject_match": False,
            }
        ],
    )
    payload = render_web(ctx.reply, "ro")
    assert "subject_match" not in json.dumps(payload, default=str)
