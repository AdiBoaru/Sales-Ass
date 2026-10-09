"""NX-374 (`kernel.v6.1`): o cerință spusă pe care catalogul nu o poate verifica se spune.

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa D1, k2 T3):
«să fie și fără parfum» a ajuns în stare (`fragrance_free = true`, NX-349), plannerul a scris golul
`unsupported_need` (catalogul SOLE nu are atributul), iar clientul a primit „Ți-am ales cremă de
față." + șase creme, fără să afle că cerința n-a contat. Golurile nu devin text (contractul); doar
dezvăluirile. Acum o nevoie SPUSĂ chiar în tur (`explicit`) care cade în `unsupported_need` aduce și
dezvăluirea `need_unverifiable`, o singură dată, cu fraza pachetului.
Zero model, zero DB.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace as NS

import pytest

from src.agent import kernel_executors as kx
from src.agent.interpreted_turn import _disclosure_memory
from src.agent.turn_planner import DISCLOSURES, disclosed_need_key, disclosure_memory
from src.conversation.ambiguity_gate import GateOutcome
from src.conversation.interpretation import KERNEL_CONTRACT_VERSION, AmbiguityDecision
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, reduce_all
from src.conversation.state_v2 import AskedQuestion, ConversationStateV2, Topic
from src.domain.pack import KERNEL_SENTENCE_CODES
from src.worker.kernel_commit import MEMORY_OPS
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import _interp, _need, _plan, _search, _state
from tests.test_kernel_provenance import ch
from tests.test_nx349_bool_facets import FLAG, _step, flag

CODE = "need_unverifiable"


def _gate_ask() -> GateOutcome:
    return GateOutcome(
        AmbiguityDecision(verdict="must_ask", reason="no_subject", question="Ce cauți?"),
        asked_key="category",
        asked_kind="pending",
    )


def _now(key: str, value: object):
    """Nevoia scrisă de reducer în ACEST tur (revizia stării din `_state`)."""
    return dataclasses.replace(_need(key, value), updated_revision=1)


def _codes(planned) -> list[tuple[int, str]]:
    return [d for d in planned.disclosures if d[1] == CODE]


#: Subiectul turului real k2 T3: creme de față (din turele dinainte).
CREAMS = ConversationStateV2(revision=1, topic=Topic(product_type="crema de fata"))


def test_a_spoken_unverifiable_need_is_disclosed():
    """Turul real k2 T3: «sa fie si fara parfum», pe subiectul creme de față."""
    step = _step("sa fie si fara parfum", flag(quote="fara parfum"), state=CREAMS)
    assert step.planned.plans[step.planned.primary].executor == "search"
    assert "unsupported_need" in step.planned.gaps
    assert _codes(step.planned) == [(step.planned.primary, CODE)]


def test_an_abandoned_search_leaves_no_disclosure():
    """Fără subiect și fără cuvinte de căutat, căutarea cade (`no_query`): golurile ei ies, iar
    dezvăluirea odată cu ele (nu ar mai fi legată de niciun răspuns)."""
    step = _step("sa fie si fara parfum", flag(quote="fara parfum"))
    assert step.planned.gaps == ("no_query",)
    assert not _codes(step.planned)


def test_a_described_need_is_a_gap_without_a_disclosure():
    """`implicit` (descriere, nu valoarea numită): golul rămâne intern, ca înainte."""
    step = _step("un sampon sa nu contina parfum", flag(quote="sa nu contina parfum"))
    assert "unsupported_need" in step.planned.gaps
    assert not _codes(step.planned)


def test_an_earlier_need_is_not_disclosed_again():
    """Turul următor nu repetă dezvăluirea: nevoia e din starea de dinainte."""
    first = _step("sa fie si fara parfum", flag(quote="fara parfum"), state=CREAMS)
    assert any(n.key == FLAG for n in first.state_after.active_needs())
    second = _step("vreau ceva hidratant", state=first.state_after)
    assert "unsupported_need" in second.planned.gaps
    assert not _codes(second.planned)


def test_on_another_domain_the_rule_is_the_same():
    """Electronice: o nevoie fără fațetă (`use_case`), spusă în tur."""
    interp = _interp(
        acts=[{"kind": "find"}],
        changes=[
            {
                "op": "set",
                "dimension": "use_case",
                "relation": "eq",
                "value": "birou",
                "quote": "pentru birou",
            }
        ],
    )
    planned = _plan("electronics", interp, _state("telefoane", needs=(_now("use_case", "birou"),)))
    _search(planned)
    assert planned.gaps == ("unsupported_need",) and _codes(planned) == [(0, CODE)]


def test_a_spoken_need_only_in_state_is_disclosed_until_told():
    """Recenzia A2: o nevoie spusă de client (`user_explicit`) pe un tur care n-a căutat se spune pe
    PRIMA căutare care o poartă, nu doar pe turul în care a fost scrisă."""
    planned = _plan(
        "electronics",
        _interp(acts=[{"kind": "find"}]),
        _state("telefoane", needs=(_need("use_case", "birou"),)),
    )
    _search(planned)
    assert planned.gaps == ("unsupported_need",) and _codes(planned) == [(0, CODE)]
    assert planned.disclosed_needs == ("use_case",)


@pytest.mark.parametrize("name", ["sole-ro", "electronics", "fashion"])
def test_the_executor_says_the_pack_sentence(name):
    pack = fc.pack(name)
    ctx = NS(business=NS(domain_pack=pack), language="ro", events=[], emit=lambda *a, **k: None)
    text = kx._disclosure_text(ctx, NS(disclosures=[(0, CODE)]))
    assert text == kx.kernel_sentence(pack, "ro", "need_unverifiable") and text


def test_the_disclosure_is_in_both_closed_vocabularies_and_the_contract_is_minor():
    assert "need_unverifiable" in DISCLOSURES and "need_unverifiable" in KERNEL_SENTENCE_CODES
    # dezvăluirea a intrat ca minor în v6.1; un major de după (v7.0, NX-384) o păstrează
    major, minor = (int(x) for x in KERNEL_CONTRACT_VERSION.removeprefix("kernel.v").split("."))
    assert (major, minor) >= (6, 1)


def test_the_sentences_respect_the_voice_rules():
    pack = fc.pack("electronics")
    for locale in ("ro", "en"):
        phrase = kx.kernel_sentence(pack, locale, "need_unverifiable")
        assert phrase and ";" not in phrase and " — " not in phrase and " - " not in phrase


# --- recenzia adversarială ---------------------------------------------------------------------


def _use_case(op: str = "set", value: str | None = "birou") -> dict:
    return {
        "op": op,
        "dimension": "use_case",
        "relation": "eq",
        "value": value,
        "quote": "pentru birou",
        **({"target": "c1"} if op in ("remove", "replace") else {}),
    }


def test_the_disclosure_belongs_to_the_search_plan_not_to_the_turn():
    """Coș + căutare dependentă: dezvăluirea e pe planul de căutare (indexul 1), deci un coș picat
    (căutarea nu rulează) n-o mai spune."""
    interp = _interp(
        acts=[{"kind": "cart", "targets": ["r1"]}, {"kind": "find", "query": "o husa"}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
        changes=[_use_case()],
    )
    resolved = (
        NS(
            ref_id="r1",
            kind="ordinal",
            outcome="exact",
            product_ids=["p1"],
            source="shown_now",
            reason="ordinal_in_set",
        ),
    )
    planned = _plan(
        "electronics",
        interp,
        _state("telefoane", needs=(_now("use_case", "birou"),)),
        resolved=resolved,
        changed=True,
    )
    executors = [p.executor for p in planned.plans]
    assert executors[0] == "cart" and "search" in executors
    search_index = executors.index("search")
    assert _codes(planned) == [(search_index, CODE)]
    pack = fc.pack("electronics")
    ctx = NS(business=NS(domain_pack=pack), language="ro", events=[], emit=lambda *a, **k: None)
    served = kx._disclosure_text(ctx, planned)
    skipped = kx._disclosure_text(ctx, planned, frozenset({search_index}))
    assert served and not skipped


def test_a_retracted_need_is_not_a_requirement_to_disclose():
    """O nevoie retrasă (reducerul a scris `revoked`) nu mai e activă: nu se dezvăluie."""
    retracted = dataclasses.replace(_now("use_case", "birou"), status="revoked")
    planned = _plan(
        "electronics",
        _interp(acts=[{"kind": "find"}], changes=[_use_case("remove", None)]),
        _state("telefoane", needs=(retracted,)),
    )
    assert not _codes(planned)


def test_a_need_already_disclosed_is_not_disclosed_again():
    """Memoria dezvăluirii (`asked_questions`, cheia `unverifiable:<nevoie>`) oprește repetarea,
    chiar dacă modelul re-emite schimbarea și reducerul re-scrie nevoia în tur (idempotent)."""
    told = AskedQuestion(disclosed_need_key("use_case"), revision=1)
    state = dataclasses.replace(
        _state("telefoane", needs=(_now("use_case", "birou"),)), asked_questions=(told,)
    )
    planned = _plan("electronics", _interp(acts=[{"kind": "find"}], changes=[_use_case()]), state)
    assert planned.gaps == ("unsupported_need",) and not _codes(planned)
    assert planned.disclosed_needs == ()


def test_a_flag_relaxed_to_false_is_not_disclosed():
    state = ConversationStateV2(
        revision=1,
        topic=Topic(product_type="crema de fata"),
        needs=(dataclasses.replace(_need(FLAG, False), updated_revision=1),),
    )
    planned = _plan(
        "sole-ro",
        _interp(
            acts=[{"kind": "find", "query": "crema"}],
            changes=[
                {
                    "op": "set",
                    "dimension": FLAG,
                    "relation": "eq",
                    "value": "false",
                    "quote": "poate avea parfum",
                }
            ],
        ),
        state,
    )
    assert not _codes(planned)


# --- recenzia adversarială a PR-ului (A2, A7) ---------------------------------------------------


def _type(value: str = "crema de fata"):
    return ch("set", dimension="product_type", relation="eq", value=value, quote=value)


def test_a2_a_need_spoken_on_a_turn_without_search_is_disclosed_on_the_first_search():
    """Recenzia A2, reprodusă: T1 «vreau ceva fara parfum» pe stare goală ⇒ `reply_only`
    (`no_query`), nevoia ajunge în stare; T2 «o crema de fata» caută cu nevoia nesusținută. Pe codul
    vechi T2 avea golul, dar nu și dezvăluirea. T3 nu o mai repetă (memoria scrisă la T2)."""
    first = _step("vreau ceva fara parfum", flag(quote="fara parfum"))
    assert first.planned.gaps == ("no_query",) and not _codes(first.planned)
    assert any(n.key == FLAG for n in first.state_after.active_needs())
    second = _step("o crema de fata", _type(), state=first.state_after)
    assert second.planned.plans[second.planned.primary].executor == "search"
    assert _codes(second.planned) == [(second.planned.primary, CODE)]
    assert second.planned.disclosed_needs == (FLAG,)
    assert second.state_after.asked(disclosed_need_key(FLAG)) is not None
    third = _step("si ceva hidratant", state=second.state_after)
    assert "unsupported_need" in third.planned.gaps and not _codes(third.planned)


def test_a2_after_a_gate_question_the_first_search_discloses():
    """Recenzia A2, a doua formă: poarta a întrebat raftul (`must_ask`, planul `ask`), deci nimic
    nu s-a ales; turul următor, care caută, spune cerința."""
    state = ConversationStateV2(
        revision=1, needs=(dataclasses.replace(_need(FLAG, True), updated_revision=1),)
    )
    asking = _plan("sole-ro", _interp(acts=[{"kind": "find"}]), state, gate=_gate_ask())
    assert [p.executor for p in asking.plans] == ["ask"] and not _codes(asking)
    later = dataclasses.replace(state, revision=2, topic=Topic(product_type="crema de fata"))
    searching = _plan("sole-ro", _interp(acts=[{"kind": "find", "query": "crema"}]), later)
    _search(searching)
    assert _codes(searching) == [(0, CODE)]


def test_a2_a_described_need_is_never_disclosed_on_later_turns():
    """Doar ce a SPUS clientul (`user_explicit`): o nevoie descrisă (`user_implicit`) rămâne gol."""
    described = dataclasses.replace(_need(FLAG, True, source="user_implicit"), updated_revision=1)
    state = ConversationStateV2(
        revision=2, topic=Topic(product_type="crema de fata"), needs=(described,)
    )
    planned = _plan("sole-ro", _interp(acts=[{"kind": "find", "query": "crema"}]), state)
    assert "unsupported_need" in planned.gaps and not _codes(planned)


def test_a2_the_memory_is_written_only_when_the_sentence_reached_the_client():
    """Orchestratorul scrie memoria (`note_asked unverifiable:<nevoie>`) doar dacă fraza e chiar în
    răspuns; un plan care n-a servit (fraza sărită) lasă nevoia de spus pe următoarea căutare."""
    pack = fc.pack("sole-ro")
    sentence = kx.kernel_sentence(pack, "ro", CODE)
    planned = NS(disclosures=((0, CODE),), disclosed_needs=(FLAG,))

    def ctx(text: str) -> NS:
        reply = NS(text=text, rich=None)
        return NS(business=NS(domain_pack=pack), language="ro", reply=reply, turn_id="t9")

    told = _disclosure_memory(ctx(f"{sentence}\n\nȚi-am ales cremă de față."), planned)
    assert [(p.op, p.key, p.source) for p in told] == [
        ("note_asked", disclosed_need_key(FLAG), "policy")
    ]
    assert _disclosure_memory(ctx("Ți-am ales cremă de față."), planned) == ()


def test_a2_the_memory_passes_the_fixed_second_pass_of_the_commit():
    """`note_asked` e în `MEMORY_OPS`: a doua trecere a commit-ului îl aplică, fără cheie nouă de
    stare, iar cheia cu prefix nu atinge întrebările porții (`asked(fragrance_free)` rămâne gol)."""
    proposals = disclosure_memory(NS(disclosed_needs=(FLAG,)), "t1")
    assert {p.op for p in proposals} <= MEMORY_OPS
    policy = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(fc.pack("sole-ro")))
    after = reduce_all(ConversationStateV2(revision=3), proposals, policy, revision=3).state
    assert after.asked(disclosed_need_key(FLAG)) is not None and after.asked(FLAG) is None
    assert after.revision == 3


def test_a7_a_flag_need_never_reaches_search_args_so_the_disclosure_is_true():
    """Recenzia A7: o fațetă da/nu ajunge MEREU în `unsupported_need`, fără să privească datele.
    Dezvăluirea spune „nu o pot verifica", iar asta e adevărat prin construcție doar cât timp
    `SearchArgs` n-are niciun câmp care să filtreze sau să ordoneze pe un atribut boolean. Testul
    fixează presupunerea: nevoia nu apare în niciun câmp al căutării. Un filtru boolean adăugat în
    SQL trebuie să mute ramura (și să schimbe acest test)."""
    step = _step("sa fie si fara parfum", flag(quote="fara parfum"), state=CREAMS)
    args = step.planned.plans[step.planned.primary].search_args
    assert args is not None and _codes(step.planned)
    dumped = args.model_dump()
    for field in ("concerns", "features", "rank_terms"):
        assert FLAG not in (dumped.get(field) or []) and "true" not in (dumped.get(field) or [])
    for field in ("prefer", "exclude"):
        assert FLAG not in (dumped.get(field) or {})
