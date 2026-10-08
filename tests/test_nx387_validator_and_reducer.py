"""NX-387 (`kernel.v8.0`) — validatorul nu mai respinge valoarea corectă pe un cuvânt al altei
dimensiuni, iar reducerul nu mai respinge o valoare nouă pe o cheie de listă golită de client.

Turele reale (setul wide-2026-10-07):
- `w2_cadou_adolescenta_coreean_retur#3` «un tint si un luciu»: `luciu de buze` respins
  `semantic_mismatch`, fiindcă «luciu» e și un finisaj;
- `w3_cadou_iubit_barba#3` «ceva pt fata lui»: raftul `ten` respins, fiindcă «fata» e eticheta
  subraftului Machiaj > Fata (omograful NX-319);
- `w5_contradictie_fond_ten#4` «las-o baltă cu spf, contează doar să fie hidratant»: retragerea
  singurei nevoi de pe `concerns` bloca `hydration` (`revoked_key`).

Zero model, zero DB."""

from __future__ import annotations

from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.conversation.provenance import UserWords, check_changes
from src.conversation.state_reducer import reduce_all
from src.conversation.state_v2 import ConversationStateV2
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_provenance import ch, interp

SOLE = fc.pack("sole-ro")
VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "finish": (VocabEntry("glossy", "luciu", 60), VocabEntry("matte", "mat", 80)),
        "product_type": (
            VocabEntry("luciu de buze", "luciu de buze", 30),
            VocabEntry("nuantator pentru buze", "nuantator pentru buze", 20),
        ),
        "category": (
            VocabEntry("ten", "Ten", 1461, path="ten"),
            VocabEntry("machiaj", "Machiaj", 681, path="machiaj"),
            VocabEntry("machiaj-fata", "Fata", 274, path="machiaj/fata"),
        ),
        "skin_type": (VocabEntry("dry", "dry", 400),),
        "concerns": (VocabEntry("redness", "redness", 40),),
    },
)


def _check(change, said):
    [checked] = check_changes(
        interp(change), words=UserWords(said, ()), vocab=VOCAB, pack=SOLE, locale="ro"
    )
    return checked


def test_a_word_of_another_dimension_does_not_reject_the_named_type():
    c = _check(
        ch(dimension="product_type", relation="eq", value="luciu de buze", quote="un luciu"),
        "un tint si un luciu ar fi perfect",
    )
    assert c.rejected is None and c.canonical_value == "luciu de buze"


def test_a_quote_naming_nothing_of_the_value_is_still_a_mismatch():
    """Contraexemplul păstrat: «roșeața» nu numește nimic din „dry"."""
    c = _check(ch(dimension="skin_type", value="dry", quote="roșeața"), "am roșeața pe obraz")
    assert c.rejected == "semantic_mismatch"


def test_a_subshelf_homograph_still_competes_declared():
    """Declarat: «pt fata lui» pe raftul `ten` rămâne contrazis de Machiaj > Fata. O excepție pentru
    omograf ar fi lăsat să treacă orice raft («pt fata lui» pe `par`, recenzia); notițele de
    magazin NX-380 țin raftul în amonte."""
    c = _check(
        ch(op="set", dimension="category", relation="eq", value="ten", quote="pt fata lui"),
        "ok atunci ceva pt fata lui, are tenul gras",
    )
    assert c.rejected == "semantic_mismatch"


def test_a_shelf_word_never_contradicts_a_facet_value():
    """Recenzia (H1): regula NX-330 rămâne: «pt fata» (raftul Machiaj > Fata) nu contrazice
    hidratarea."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            **VOCAB.dimensions,
            "concerns": (VocabEntry("hydration", "hidratare", 90),),
        },
    )
    [c] = check_changes(
        interp(ch(dimension="concerns", value="hydration", quote="ceva hidratant pt fata")),
        words=UserWords("vreau ceva hidratant pt fata", ()),
        vocab=vocab,
        pack=SOLE,
        locale="ro",
    )
    assert c.rejected is None


def test_the_tail_of_a_name_is_not_its_head():
    """Recenzia (M1, NX-350): «ten» e coada lui „fond de ten", deci «ten gras» (tip de ten) o
    contrazice în continuare."""
    vocab = CatalogVocabulary(
        business_id="b",
        dimensions={
            **VOCAB.dimensions,
            "product_type": (VocabEntry("fond de ten", "fond de ten", 50),),
            "skin_type": (VocabEntry("oily", "ten gras", 300),),
        },
    )
    [c] = check_changes(
        interp(ch(dimension="product_type", value="fond de ten", quote="pentru ten gras")),
        words=UserWords("vreau ceva pentru ten gras", ()),
        vocab=vocab,
        pack=SOLE,
        locale="ro",
    )
    assert c.rejected == "semantic_mismatch"


def test_a_subshelf_with_its_root_named_still_competes():
    """«machiaj de fata» numește chiar subraftul: raftul `ten` propus e contrazis."""
    c = _check(
        ch(op="set", dimension="category", relation="eq", value="ten", quote="machiaj de fata"),
        "vreau machiaj de fata",
    )
    assert c.rejected == "semantic_mismatch"


# --- reducerul: tombstone-ul unei chei de listă ---------------------------------------------------


def _proposal(op, key, value, source="user_explicit"):
    from src.conversation.state_reducer import StateUpdateProposal

    return StateUpdateProposal(op, key=key, value=value, source=source, strength="soft")


def _after_retracting(key, value):
    from src.agent.interpreted_turn import reducer_policy

    policy = reducer_policy(SOLE)
    state = reduce_all(ConversationStateV2(), [_proposal("set_need", key, value)], policy).state
    state = reduce_all(state, [_proposal("revoke", key, value)], policy).state
    assert not [n for n in state.needs if n.is_active and n.key == key]
    return state, policy


def test_a_new_value_on_an_emptied_list_key_is_not_a_revival():
    state, policy = _after_retracting("concerns", "redness")
    result = reduce_all(
        state, [_proposal("set_need", "concerns", "hydration", "user_implicit")], policy
    )
    assert [r.reason for r in result.rejected] == []
    active = {(n.key, n.normalized_value) for n in result.state.needs if n.is_active}
    assert ("concerns", "hydration") in active


def test_the_retracted_value_itself_stays_retracted():
    state, policy = _after_retracting("concerns", "redness")
    result = reduce_all(
        state, [_proposal("set_need", "concerns", "redness", "user_implicit")], policy
    )
    assert [r.reason for r in result.rejected] == ["revoked_key"]


def test_a_cleared_list_key_still_blocks_a_revival_by_description():
    """Recenzia (M2): după «uită criteriile» (tombstone de cod pe cheie), o descriere nu reînvie
    valoarea pe o cheie de listă."""
    from src.agent.interpreted_turn import reducer_policy

    policy = reducer_policy(SOLE)
    state = reduce_all(
        ConversationStateV2(), [_proposal("set_need", "concerns", "redness")], policy
    ).state
    state = reduce_all(state, [_proposal("clear_all", None, None)], policy).state
    result = reduce_all(
        state, [_proposal("set_need", "concerns", "redness", "user_implicit")], policy
    )
    assert [r.reason for r in result.rejected] == ["revoked_key"]


def test_a_scalar_key_keeps_the_key_level_tombstone():
    """Pe o cheie scalară regula rămâne: «nu contează tipul de ten», apoi o descriere nu-l
    reînvie."""
    state, policy = _after_retracting("skin_type", "dry")
    result = reduce_all(
        state, [_proposal("set_need", "skin_type", "oily", "user_implicit")], policy
    )
    assert [r.reason for r in result.rejected] == ["revoked_key"]


# --- ușa spre v1: o citire fără țintă ------------------------------------------------------------


async def test_a_read_without_any_target_says_so_instead_of_falling_to_v1(monkeypatch):
    """`w4_alergie_lista_ingrediente#3` «primul are cocos sau nu?» fără listă pe ecran: pe `main`
    turul cădea pe v1, care răspundea despre un produs ales de el."""
    from types import SimpleNamespace as NS

    from src.agent import kernel_executors as kx
    from src.agent.turn_planner import PlannedTurn
    from src.conversation.ambiguity_gate import GateOutcome
    from src.conversation.interpretation import AmbiguityDecision, TurnPlan
    from tests.kernel import stage_harness as sh

    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    plan = TurnPlan(executor="reply_only", product_ids=[], search_args=None, depends_on=None)
    planned = PlannedTurn(plans=(plan,), primary=0, disclosures=((0, "no_target"),))
    outcome = GateOutcome(decision=AmbiguityDecision(verdict="act", reason="none", question=None))
    ctx = sh.build_ctx(cat, ConversationStateV2(), "primul are cocos?")
    deps = NS(db=sh.RecordingDb(), llm=sh.StageLLM())
    assert await kx.execute_read_plans(ctx, deps, planned, outcome) is True
    assert ctx.reply.text == kx.kernel_sentence(cat.pack, "ro", "no_target")


# --- scopul rutinei -------------------------------------------------------------------------------


def _bundle_plan(changes):
    from src.agent.turn_planner import plan_turn
    from src.conversation.interpretation import CheckedChange
    from src.conversation.state_v2 import Topic
    from tests.test_kernel_planner import _gate
    from tests.test_kernel_planner import _interp as kp_interp

    interp = kp_interp(acts=[{"kind": "bundle"}], changes=[dict(c) for c in changes])
    checked = [
        CheckedChange(
            change=c,
            dimension=c.dimension,
            canonical_value=c.value,
            provenance="explicit",
            strength="soft",
            rejected=None,
        )
        for c in interp.changes
    ]
    state = ConversationStateV2(revision=1, topic=Topic(category_key="par", product_type="sampon"))
    planned = plan_turn(
        interp,
        state,
        (),
        (),
        _gate(),
        changed=True,
        pack=SOLE,
        vocab=fc.vocabulary("sole-ro"),
        locale="ro",
        checked=checked,
    )
    return planned.plans[planned.primary]


def _type(value):
    return {"op": "add", "dimension": "product_type", "relation": "eq", "value": value,
            "quote": value}  # fmt: skip


def test_the_steps_named_in_the_turn_scope_the_routine():
    """`w3_rutina_par_uscat_buget#1` «șampon, balsam și mască»: rutina pe toată familia păr, sub
    buget, a ieșit fără mască."""
    plan = _bundle_plan([_type("sampon"), _type("balsam"), _type("masca de par")])
    assert (plan.family, plan.steps) == ("par", ["spalare", "conditionare", "tratament"])


def test_an_avoided_type_is_not_an_asked_step():
    """Recenzia (M3): «șampon și balsam, fără mască» nu cere tratamentul."""
    avoid = {**_type("masca de par"), "relation": "avoid"}
    plan = _bundle_plan([_type("sampon"), _type("balsam"), avoid])
    assert plan.steps == ["spalare", "conditionare"]


def test_one_type_next_to_a_routine_is_not_a_scope():
    assert _bundle_plan([_type("sampon")]).steps == []


async def test_the_planned_routine_asks_candidates_only_for_the_asked_steps(monkeypatch):
    from types import SimpleNamespace as NS

    from src.tools import routine_tools as rt
    from tests.kernel import stage_harness as sh

    asked = []

    async def candidates(conn, business_id, *, values, **kw):
        asked.append(list(values))
        return []

    monkeypatch.setattr(rt, "routine_candidates", candidates)
    cat = sh.catalog("sole-ro")
    sh.install(monkeypatch, cat, executors=True)
    ctx = sh.build_ctx(cat, ConversationStateV2(), "rutina")
    deps = NS(db=sh.RecordingDb(), llm=sh.StageLLM())
    args = rt.RoutineArgs(family="par", steps=["spalare", "tratament"])
    await rt.run_planned_routine(ctx, deps, args)
    assert asked and asked[0] == ["par:spalare", "par:tratament"]
