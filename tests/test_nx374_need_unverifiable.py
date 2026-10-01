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
from src.agent.turn_planner import DISCLOSURES
from src.conversation.interpretation import KERNEL_CONTRACT_VERSION
from src.conversation.state_v2 import ConversationStateV2, Topic
from src.domain.pack import KERNEL_SENTENCE_CODES
from tests.kernel import fixture_catalog as fc
from tests.test_kernel_planner import _interp, _need, _plan, _search, _state
from tests.test_nx349_bool_facets import FLAG, _step, flag

CODE = "need_unverifiable"


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


def test_the_same_need_only_in_state_is_not_disclosed():
    planned = _plan(
        "electronics",
        _interp(acts=[{"kind": "find"}]),
        _state("telefoane", needs=(_need("use_case", "birou"),)),
    )
    _search(planned)
    assert planned.gaps == ("unsupported_need",) and not _codes(planned)


@pytest.mark.parametrize("name", ["sole-ro", "electronics", "fashion"])
def test_the_executor_says_the_pack_sentence(name):
    pack = fc.pack(name)
    ctx = NS(business=NS(domain_pack=pack), language="ro", events=[], emit=lambda *a, **k: None)
    text = kx._disclosure_text(ctx, NS(disclosures=[(0, CODE)]))
    assert text == kx.kernel_sentence(pack, "ro", "need_unverifiable") and text


def test_the_disclosure_is_in_both_closed_vocabularies_and_the_contract_is_minor():
    assert "need_unverifiable" in DISCLOSURES and "need_unverifiable" in KERNEL_SENTENCE_CODES
    assert KERNEL_CONTRACT_VERSION == "kernel.v6.1"


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


def test_a_removal_is_not_a_requirement_to_disclose():
    planned = _plan(
        "electronics",
        _interp(acts=[{"kind": "find"}], changes=[_use_case("remove", None)]),
        _state("telefoane", needs=(_now("use_case", "birou"),)),
    )
    assert not _codes(planned)


def test_a_need_updated_in_an_earlier_revision_is_not_disclosed():
    """Schimbarea turului e pe aceeași dimensiune, dar nevoia activă e din altă revizie (reducerul
    n-a aplicat-o acum): nu se re-dezvăluie."""
    old = dataclasses.replace(_need("use_case", "birou"), updated_revision=0)
    planned = _plan(
        "electronics",
        _interp(acts=[{"kind": "find"}], changes=[_use_case()]),
        _state("telefoane", needs=(old,)),
    )
    assert not _codes(planned)


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
