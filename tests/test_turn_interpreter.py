"""NX-335 (kernel pasul 5) — adaptorul de interpretare: vederea, promptul și schema din pachet, UN
apel, `outcome` din vocabularul închis.

Adaptorul e SINGURUL modul de kernel care cheamă modelul (I13). Ce se fixează aici:

- vederea (§1): ce vede modelul din stare, cu handle-urile și pozițiile pe care validatorul și
  resolverul le citesc din ACEEAȘI sursă, deci nu pot diverge;
- schema și promptul per tenant (§2): enumul schemei == `tenant_dimensions(pack)`, prefixul stabil
  la ordinea fațetelor și la driftul de stoc;
- apelul (§3): exact un apel pe invocare, pe toate cele șase `outcome`, fără excepție (P6).

ZERO OpenAI: un fake care numără apelurile. ZERO DB: pachetele și catalogul de fixture."""

from __future__ import annotations

import dataclasses
import json
import re

import httpx
import openai
import pytest

from src.agent import llm as llm_mod
from src.agent.llm import SchemaReply
from src.catalog.vocabulary import CatalogVocabulary, VocabEntry
from src.config import get_settings
from src.conversation import interpretation_check as ic
from src.conversation import turn_interpreter as ti
from src.conversation.interpretation import (
    KERNEL_CONTRACT_VERSION,
    UNIVERSAL_DIMENSIONS,
    TurnInterpretation,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.provenance import (
    _read_quote,
    check_changes,
    need_handles,
    tenant_dimensions,
)
from src.conversation.references import resolve_references, sources_from_state
from src.conversation.state_v2 import ConversationStateV2
from src.domain.pack import FacetSpec
from tests import test_domain_leak as leak
from tests.kernel import fixture_catalog as fc
from tests.kernel import replay
from tests.test_kernel_contract import _vertical_terms_in

ALL_PACKS = (*replay.FIXTURE_PACKS, "sole-ro")


# --- ajutoare ------------------------------------------------------------------------------------


class FakeLLM:
    """Fake care NUMĂRĂ apelurile și întoarce un `SchemaReply` scriptat (sau ridică)."""

    def __init__(self, reply: SchemaReply | None = None, exc: BaseException | None = None):
        self.reply = reply
        self.exc = exc
        self.calls: list[dict] = []

    async def complete_schema_raw(
        self, system, user, schema, *, model=None, reasoning_effort=None, temperature=None
    ):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": schema,
                "effort": reasoning_effort,
                "temperature": temperature,
                "cache_key": llm_mod._prompt_cache_key.get(),
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.reply


def _reply(doc: dict | str | None, *, refusal=None, finish_reason="stop") -> SchemaReply:
    content = doc if isinstance(doc, str) or doc is None else json.dumps(doc)
    return SchemaReply(content=content, refusal=refusal, finish_reason=finish_reason)


def _interp(**fields) -> dict:
    base = {
        "thread": "continue",
        "acts": [],
        "changes": [],
        "references": [],
        "ambiguities": [],
        "corrects_previous_turn": False,
    }
    base.update(fields)
    return replay.expand_interpretation(base)


def _journey(journey_id: str) -> replay.Journey:
    return next(j for j in replay.load_journeys() if j.journey_id == journey_id)


def _chain(journey_id: str, upto: int) -> tuple[replay.Journey, ConversationStateV2]:
    """Starea kernelului după primele `upto` ture ale unui journey, prin lanțul REAL."""
    journey = _journey(journey_id)
    state = ConversationStateV2()
    for i, turn in enumerate(journey.turns[:upto]):
        step = fc.kernel_step(
            journey.pack,
            state,
            turn.expect["interpretation"],
            turn.user_input,
            earlier=tuple(t.user_input for t in journey.turns[:i])[::-1],
            shown_ids=turn.shown,
            turn_id=f"t{i}",
            locale=journey.locale,
        )
        state = step.state_after
    return journey, state


def _history(journey: replay.Journey, upto: int) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for turn in journey.turns[:upto]:
        out.append(("user", turn.user_input))
        out.append(("bot", "Uite ce am găsit. Spune-mi dacă vrei altceva."))
    return tuple(out)


def _input(
    name: str,
    state: ConversationStateV2 | None = None,
    *,
    message: str = "Caut ceva nou.",
    history: tuple[tuple[str, str], ...] = (),
    pack=None,
    vocab=None,
    menu=None,
) -> ti.InterpretInput:
    return ti.InterpretInput(
        locale="ro",
        pack=fc.pack(name) if pack is None else pack,
        vocab=fc.vocabulary(name) if vocab is None else vocab,
        category_menu=fc.category_menu(name) if menu is None else menu,
        state=state or ConversationStateV2(),
        history=history,
        message=message,
    )


# --- schema per tenant (§2) ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_PACKS)
def test_the_schema_enum_is_the_tenant_dimensions(name):
    pack = fc.pack(name)
    schema = ti.interpretation_schema(pack)
    assert schema["name"] == "turn_interpretation"
    assert schema["strict"] is True
    enum = schema["schema"]["$defs"]["StateChange"]["properties"]["dimension"]["anyOf"][0]["enum"]
    assert set(enum) == set(tenant_dimensions(pack))
    assert enum == sorted(enum)


def test_a_pack_without_facets_has_only_the_universal_dimensions():
    schema = ti.interpretation_schema(None)
    enum = schema["schema"]["$defs"]["StateChange"]["properties"]["dimension"]["anyOf"][0]["enum"]
    assert enum == sorted(UNIVERSAL_DIMENSIONS)


# --- apelul (§3) -------------------------------------------------------------------------------


async def test_happy_path_one_call_the_pack_schema_and_the_event():
    inp = _input("fashion", message="Caut rochii negre.")
    doc = _interp(acts=[{"kind": "find", "query": "rochii negre", "targets": []}])
    fake = FakeLLM(_reply(doc))
    out = await ti.interpret_turn(fake, inp, business_id="b-fashion")
    assert out.outcome == "ok"
    assert len(fake.calls) == 1
    assert fake.calls[0]["schema"] == ti.interpretation_schema(inp.pack)
    assert out.interpretation == TurnInterpretation.model_validate(doc)
    assert out.event["acts"] == ["find"]
    assert out.event["contract_version"] == KERNEL_CONTRACT_VERSION
    assert out.event["outcome"] == "ok"
    assert out.event["vocabulary_snapshot"] == ic.snapshot_id(inp.pack, inp.vocab)


async def test_the_call_uses_the_interpret_effort_temperature_and_cache_key():
    s = get_settings()
    fake = FakeLLM(_reply(_interp()))
    await ti.interpret_turn(fake, _input("fashion"), business_id="b-x")
    call = fake.calls[0]
    assert call["effort"] == s.llm_reasoning_effort_interpret == "medium"
    assert call["temperature"] == s.llm_temperature_interpret
    assert call["cache_key"] == f"b-x:{ic.INTERPRET_PROMPT_VERSION}"


async def test_an_explicit_effort_arm_overrides_the_setting():
    fake = FakeLLM(_reply(_interp()))
    out = await ti.interpret_turn(fake, _input("fashion"), business_id="b", effort="low")
    assert fake.calls[0]["effort"] == "low"
    assert out.event["effort"] == "low"


OUTCOME_CASES = [
    ("refused", _reply(None, refusal="nu pot"), None),
    ("refused", _reply(None, finish_reason="content_filter"), None),
    # ordinea din §3: refuzul bate tăierea
    ("refused", _reply("{", refusal="nu pot", finish_reason="length"), None),
    ("truncated", _reply('{"thread": "cont', finish_reason="length"), None),
    ("invalid_json", _reply(None), None),
    ("invalid_json", _reply(""), None),
    ("invalid_json", _reply("{not json"), None),
    ("schema_violation", _reply("[1, 2]"), None),
    ("schema_violation", _reply({**_interp(), "extra": 1}), None),
    ("schema_violation", _reply({"thread": "switch"}), None),
    (
        "provider_error",
        None,
        openai.APITimeoutError(request=httpx.Request("POST", "https://x")),
    ),
    ("provider_error", None, RuntimeError("boom")),
    ("ok", _reply(_interp()), None),
]


@pytest.mark.parametrize(("outcome", "reply", "exc"), OUTCOME_CASES)
async def test_every_outcome_is_one_call_and_never_raises(outcome, reply, exc):
    fake = FakeLLM(reply, exc)
    out = await ti.interpret_turn(fake, _input("gifts"), business_id="b")
    assert out.outcome == outcome
    assert len(fake.calls) == 1, "nicio rundă de reparație (I13)"
    assert (out.interpretation is not None) == (outcome == "ok")
    assert out.event["outcome"] == outcome
    assert out.event["contract_version"] == KERNEL_CONTRACT_VERSION
    assert set(out.event) == ic.EVENT_KEYS


async def test_a_provider_timeout_after_the_real_retries_is_provider_error(monkeypatch):
    """Clientul REAL: `APITimeoutError` pe toate încercările (NX-126), somnul scos. Adaptorul
    face UN apel `complete_schema_raw`; retry-urile sunt ale clientului, nu ale adaptorului."""

    class _Completions:
        attempts = 0

        async def create(self, **kwargs):
            _Completions.attempts += 1
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x"))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _no_sleep)
    client = llm_mod.LLMClient(
        type("C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})(),
        model_agent="gpt-6-luna",
    )
    raw_calls = []
    original = client.complete_schema_raw

    async def counted(*a, **kw):
        raw_calls.append(1)
        return await original(*a, **kw)

    client.complete_schema_raw = counted  # type: ignore[method-assign]
    out = await ti.interpret_turn(client, _input("gifts"), business_id="b")
    assert out.outcome == "provider_error"
    assert raw_calls == [1]
    assert _Completions.attempts == get_settings().llm_retry_max + 1


async def test_a_pack_without_facets_still_calls_and_has_no_dimension_menu():
    inp = _input("fashion", pack=None, vocab=None)
    inp = dataclasses.replace(inp, pack=None, vocab=None)
    fake = FakeLLM(_reply(_interp()))
    out = await ti.interpret_turn(fake, inp, business_id="b")
    assert out.outcome == "ok"
    assert len(fake.calls) == 1
    assert f"{ti.DIMENSIONS_HEADER} (" not in fake.calls[0]["system"]


# --- vederea (§1) ------------------------------------------------------------------------------


def test_two_needs_render_as_handles_in_need_handles_order_and_remove_passes():
    journey, state = _chain("k01-electronics-park-and-resume", 1)
    inp = _input("electronics", state, history=_history(journey, 1))
    view = ti.render_view(inp)
    handles = need_handles(state.needs, NeedVocabulary.from_pack(inp.pack))
    assert [h.handle for h in handles] == ["c1", "c2"]
    lines = [ln for ln in view.splitlines() if re.match(r"c\d+ ", ln)]
    assert [ln.split()[0] for ln in lines] == ["c1", "c2"]
    for ln, h in zip(lines, handles, strict=True):
        assert ln.split()[1] == h.key
    interp = TurnInterpretation.model_validate(
        _interp(changes=[{"op": "remove", "target": "c2", "quote": "fara buget"}])
    )
    checked = check_changes(
        interp, words=ti.user_words(inp), handles=handles, vocab=inp.vocab, pack=inp.pack
    )
    assert checked[0].rejected is None


@pytest.mark.parametrize("name", replay.FIXTURE_PACKS)
def test_every_rendered_handle_is_accepted_and_every_position_resolves(name):
    """Pe fiecare pachet de fixture, starea de snapshot (`kernel_prompt_snapshot`): fiecare handle
    randat trece de `check_changes`, fiecare `#i` e rezolvat de resolver pe poziția lui, fiecare
    tur de client din vedere e găsit de validator."""
    from scripts import kernel_prompt_snapshot as kps

    inp = kps.snapshot_input(name)
    view = ti.render_view(inp)
    words = ti.user_words(inp)
    handles = need_handles(inp.state.needs, NeedVocabulary.from_pack(inp.pack))
    rendered = re.findall(r"^(c\d+) ", view, flags=re.M)
    assert rendered == [h.handle for h in handles]
    for handle in rendered:
        interp = TurnInterpretation.model_validate(
            _interp(changes=[{"op": "remove", "target": handle, "quote": inp.message}])
        )
        [checked] = check_changes(
            interp, words=words, handles=handles, vocab=inp.vocab, pack=inp.pack, locale="ro"
        )
        assert checked.rejected is None, handle
    positions = [int(p) for p in re.findall(r"^#(\d+) ", view, flags=re.M)]
    shown = inp.state.references.displayed_products
    assert positions == list(range(1, len(shown) + 1))
    refs = [{"id": f"r{i}", "text": f"#{i}", "kind": "ordinal", "ordinal": i} for i in positions]
    interp = TurnInterpretation.model_validate(_interp(references=refs))
    sources = sources_from_state(inp.state, "continue")
    resolved = resolve_references(
        interp.references, sources, fc.facts(name), vocab=inp.vocab, pack=inp.pack, locale="ro"
    )
    assert [r.product_ids for r in resolved] == [[d.product_id] for d in shown]
    # Recenzia NX-335: tot ce poate atinge o referință `earlier` (seturile de mai devreme ȘI setul
    # parcat, `references._Resolver.earlier`) e numit în vedere, fără id.
    blocks = {
        "shown_now": view.split("ON SCREEN", 1)[1].split("EARLIER", 1)[0],
        "shown_earlier": view.split("EARLIER", 1)[1].split("PENDING", 1)[0],
        "parked": view.split("PARKED:", 1)[1].split("ON SCREEN", 1)[0],
    }
    for source, items in sources.sets_in_order():
        for item in items:
            assert ti.display(item.name) in blocks[source], (source, item.name)
            assert item.product_id not in view
    for role, text in ti.window(inp):
        if role == "user":
            assert text in view
            assert _read_quote(text, words, "ro").located, text


def test_the_snapshot_states_cover_every_block_of_the_view():
    """Împreună, stările de snapshot au nevoi, ecran, seturi de mai devreme și un subiect parcat,
    deci testul de mai sus nu e vid pe niciun bloc."""
    from scripts import kernel_prompt_snapshot as kps

    states = [kps.snapshot_input(name).state for name in replay.FIXTURE_PACKS]
    assert any(s.active_needs() for s in states)
    assert all(s.references.displayed_products for s in states)
    assert any(s.references.recent_sets for s in states)
    assert any(s.parked is not None for s in states)


def test_the_screen_has_no_ids_and_no_prices():
    journey, state = _chain("k01-electronics-park-and-resume", 2)
    view = ti.render_view(_input("electronics", state, history=_history(journey, 2)))
    for d in state.references.displayed_products:
        assert d.product_id not in view
        assert f"{d.price:g}" not in view.split("ON SCREEN", 1)[1].split("EARLIER", 1)[0]


def test_parked_topic_is_named_and_a_resume_validates():
    journey, state = _chain("k01-electronics-park-and-resume", 2)
    assert state.parked is not None
    inp = _input("electronics", state, history=_history(journey, 2), message="inapoi la telefoane")
    view = ti.render_view(inp)
    assert f"PARKED: {state.parked.topic.category_key}" in view
    interp = TurnInterpretation.model_validate(_interp(thread="resume"))
    validated = ic.validate(
        interp,
        words=ti.user_words(inp),
        handles=need_handles(state.needs, NeedVocabulary.from_pack(inp.pack)),
        vocab=inp.vocab,
        pack=inp.pack,
        locale="ro",
    )
    assert validated.unknown_refs == []
    assert validated.interpretation == interp


def test_the_earlier_sets_and_the_screen_come_from_the_state():
    journey, state = _chain("k01-electronics-park-and-resume", 2)
    view = ti.render_view(_input("electronics", state, history=_history(journey, 2)))
    assert state.references.recent_sets
    earlier = view.split("EARLIER", 1)[1].split("PENDING", 1)[0]
    for ref in state.references.recent_sets[0]:
        assert ti.display(ref.name) in earlier


def test_a_quote_inside_the_window_is_explicit_and_outside_is_inferred():
    """Citatul scris chiar la marginea ferestrei ⇒ găsit; o pereche mai devreme ⇒ nici vederea
    nu l-a arătat, nici validatorul nu-l găsește (`inferred`). Borna se derivă din
    `MAX_HISTORY_MESSAGES` (20 din 2026-10-08), nu e scrisă de mână."""
    old = "vreau neaparat culoarea albastra"

    def history(pairs_after: int) -> tuple[tuple[str, str], ...]:
        turns = [("user", old), ("bot", "Bine.")]
        for i in range(pairs_after):
            turns += [("user", f"altceva numarul {i}"), ("bot", "Bine.")]
        return tuple(turns)

    pairs = ti.MAX_HISTORY_MESSAGES // 2 - 1
    inside = _input("fashion", history=history(pairs))  # primul mesaj din fereastră
    outside = _input("fashion", history=history(pairs + 1))  # o pereche în afara ei
    assert len(inside.history) == ti.MAX_HISTORY_MESSAGES
    assert old in ti.render_view(inside)
    assert _read_quote(old, ti.user_words(inside), "ro").located
    assert len(outside.history) > ti.MAX_HISTORY_MESSAGES
    assert old not in ti.render_view(outside)
    assert not _read_quote(old, ti.user_words(outside), "ro").located
    interp = TurnInterpretation.model_validate(
        _interp(changes=[{"op": "add", "dimension": "unmapped", "value": "x", "quote": old}])
    )
    [explicit] = check_changes(interp, words=ti.user_words(inside), vocab=None)
    [inferred] = check_changes(interp, words=ti.user_words(outside), vocab=None)
    assert explicit.provenance != "inferred"
    assert inferred.provenance == "inferred"


def test_both_turns_reach_the_view_uncut():
    """NX-356: nicio limită de caractere pe istoric (ca NX-255 pe calea care răspunde). O replică
    de bot de mărimea celor reale (1.200-1.600) intră întreagă, inclusiv ce e la coadă."""
    long_bot = "Prima propozitie e scurta. " + "cuvant " * 200 + "Vrei ceva pentru ten uscat?"
    user = "  Vreau   ceva, cu SPAȚII ciudate!  "
    inp = _input("gifts", history=(("user", user), ("bot", long_bot)))
    view = ti.render_view(inp)
    assert user in view
    bot_line = next(ln for ln in view.splitlines() if ln.startswith("bot: "))
    assert bot_line == "bot: " + " ".join(long_bot.split())
    assert bot_line.endswith("Vrei ceva pentru ten uscat?")


def test_a_facet_without_value_labels_shows_the_canonical_code():
    journey, state = _chain("k01-electronics-park-and-resume", 1)
    view = ti.render_view(_input("electronics", state, history=_history(journey, 1)))
    brand = next(ln for ln in view.splitlines() if re.match(r"c\d+ brand ", ln))
    brand_need = next(n for n in state.needs if n.key == "brand" and n.is_active)
    assert str(brand_need.normalized_value) in brand


def test_a_facet_with_a_value_label_shows_the_label():
    journey, state = _chain("k01-electronics-park-and-resume", 1)
    brand_need = next(n for n in state.needs if n.key == "brand" and n.is_active)
    code = str(brand_need.normalized_value)
    pack = dataclasses.replace(
        fc.pack("electronics"),
        facet_labels=(FacetSpec(key="brand", value_labels={code: {"ro": "Eticheta Marcii"}}),),
    )
    view = ti.render_view(_input("electronics", state, pack=pack))
    assert "Eticheta Marcii" in view


# --- prefixul și amprenta (§2) -----------------------------------------------------------------


def test_the_same_facets_in_another_order_give_the_same_snapshot_and_prefix():
    inp = _input("fashion")
    pack = inp.pack
    flipped = dataclasses.replace(pack, facets=tuple(reversed(pack.facets)))
    other = dataclasses.replace(inp, pack=flipped)
    assert ic.snapshot_id(pack, inp.vocab) == ic.snapshot_id(flipped, inp.vocab)
    assert ti.system_prompt(inp) == ti.system_prompt(other)
    assert ti.interpretation_schema(pack) == ti.interpretation_schema(flipped)


def test_a_shelf_size_moving_inside_its_rounding_step_keeps_the_prefix():
    inp = dataclasses.replace(_input("fashion"), category_menu=(("rochii", 1461), ("fuste", 12)))
    moved = dataclasses.replace(inp, category_menu=(("rochii", 1478), ("fuste", 12)))
    assert ti.system_prompt(inp) == ti.system_prompt(moved)
    grown = dataclasses.replace(inp, category_menu=(("rochii", 1461), ("fuste", 13)))
    assert ti.system_prompt(inp) != ti.system_prompt(grown)


def test_a_vocabulary_entry_changes_the_snapshot_but_not_the_prompt():
    inp = _input("fashion")
    vocab = inp.vocab
    extra = CatalogVocabulary(
        business_id=vocab.business_id,
        dimensions={**vocab.dimensions, "undeclared_key": (VocabEntry("v1", "v1", 3),)},
    )
    grown = dataclasses.replace(inp, vocab=extra)
    assert ti.system_prompt(inp) == ti.system_prompt(grown)
    assert ic.snapshot_id(inp.pack, vocab) != ic.snapshot_id(inp.pack, extra)
    assert re.fullmatch(r"[0-9a-f]{16}", ic.snapshot_id(inp.pack, vocab))


def test_a_stock_move_changes_the_snapshot():
    inp = _input("fashion")
    dim, entries = next(iter(sorted(inp.vocab.dimensions.items())))
    moved = CatalogVocabulary(
        business_id=inp.vocab.business_id,
        dimensions={
            **inp.vocab.dimensions,
            dim: (dataclasses.replace(entries[0], count=entries[0].count + 1), *entries[1:]),
        },
    )
    assert ic.snapshot_id(inp.pack, inp.vocab) != ic.snapshot_id(inp.pack, moved)


def test_the_dimension_menu_is_capped_per_dimension():
    inp = _input("fashion")
    many = tuple(VocabEntry(f"v{i:03d}", f"v{i:03d}", 5) for i in range(200))
    vocab = CatalogVocabulary(
        business_id="b", dimensions={**inp.vocab.dimensions, "material": many}
    )
    system = ti.system_prompt(dataclasses.replace(inp, vocab=vocab))
    material = next(ln for ln in system.splitlines() if ln.startswith("- material"))
    assert material.count("v0") + material.count("v1") <= ti.MAX_MENU_VALUES


def test_the_prompt_names_the_locale_and_the_shelf_keys():
    inp = _input("fashion")
    system = ti.system_prompt(inp)
    assert "ro" in system
    for key, _ in inp.category_menu:
        assert f"- {key} (" in system


def test_the_current_message_is_last_and_not_in_the_history():
    inp = _input("gifts", message="mesajul de acum", history=(("user", "mesajul vechi"),))
    user = ti.user_prompt(inp)
    assert user.rstrip().endswith("mesajul de acum")
    history = user.split("HISTORY", 1)[1]
    assert history.count("mesajul de acum") == 1


# --- I14 pe adaptor ------------------------------------------------------------------------------


def test_the_adapter_holds_no_vertical_literal_and_the_check_can_fail():
    source = (fc.replay.ROOT / "src" / "conversation" / "turn_interpreter.py").read_text(
        encoding="utf-8"
    )
    assert _vertical_terms_in(source) == []
    term = sorted(leak._domain_terms())[0]
    mutated = source + f'\n_INSTRUCTION = "Prefer {term} when unsure."\n'
    assert _vertical_terms_in(mutated)


# --- NX-339b: regulile promptului interpret.v2, pe toate pachetele --------------------------------

#: Câte o frază-ancoră per regulă din cardul NX-339 (§ „Promptul interpret.v2"). Dacă o regulă se
#: pierde la o rescriere, testul arată care.
V2_RULES = {
    "1 actul e cererea, nu ce vinde magazinul": "never whether the store sells it",
    "2 întrebarea despre un articol e detail": "detail: a question about one item on screen",
    "3 other doar când nu se potrivește nimic": "other: only when no other act fits",
    "4 aside doar magazin și conversație": '"aside" only for a question about the store itself',
    "5 ce descrie clientul devine schimbare": "is a change when a menu value names its cause",
    "6 valoarea doar când cuvintele o numesc": "only when the customer's words name that code",
    "7 cart doar la cerere explicită": "cart: only when the customer explicitly asks",
}


@pytest.mark.parametrize("pack", ALL_PACKS)
def test_every_v2_rule_is_in_the_rendered_prompt(pack):
    system = " ".join(ti.system_prompt(_input(pack)).split())
    missing = [rule for rule, anchor in V2_RULES.items() if anchor not in system]
    assert missing == []


def test_the_old_value_rule_is_replaced_not_duplicated():
    """Regula 6 ÎNLOCUIEȘTE formularea v1, ca promptul să nu spună două lucruri."""
    system = " ".join(ti.system_prompt(_input("electronics")).split())
    assert "a code from the menu when one fits" not in system
    assert system.count("value: for product_type") == 1


# --- NX-339c: interpret.v3 = v2 cu regula 2 fără „whether it suits" și bundle reformulat --------


def test_v3_detail_rule_no_longer_covers_whether_an_item_suits():
    """Pe v2, „whether it suits" făcea din descrierea clientului despre sine sau din cererea de
    recomandare un `detail` (11 regresii pe A, 7 pe B). v3 o scoate, restul regulii rămâne."""
    system = " ".join(ti.system_prompt(_input("electronics")).split())
    assert "whether it suits" not in system
    assert "detail: a question about one item on screen" in system


@pytest.mark.parametrize("pack", ALL_PACKS)
def test_v3_bundle_covers_items_used_together_without_naming_them(pack):
    """Pe v2, „fă-mi o rutină" ieșea `find`: bundle cerea articolele numite. Formularea e generică
    (poarta I14 pe adaptor), fără cuvântul unui vertical."""
    system = " ".join(ti.system_prompt(_input(pack)).split())
    assert "bundle: only when the customer asks for several items chosen together" in system
    assert "even when they do not name them" in system
    assert "bundle: a set of items that go together" not in system


# --- NX-347: interpret.v4 = v3 + trei reguli de RECUNOAȘTERE (clasele B, C, E, NX-345) -----

#: Câte o frază-ancoră per regulă nouă. Formulările sunt generice (I14): niciun cuvânt de vertical.
V4_RULES = {
    "B raftul numit, și ca întrebare": "also inside a question, is a set on category",
    "B un calificativ nu e raft": "a shelf word that is part of its name or only says",
    "C ce ESTE clientul, nu rezultatul": "the value that names what they are or have",
    "C un rezultat cerut rămâne schimbare": "a result the customer asks for is a change too",
}


def test_v4_has_no_rule_for_yes_no_dimensions():
    """Recenzia v4: regula E („true/false, never avoid") nu putea produce ce cerea: validatorul
    transformă orice fațetă da/nu în `unmapped` (vocabularul exclude booleenele), deci E ar fi
    împins modelul DE LA singura formă pe care kernelul o poate executa (`avoid`). Defectul e al
    validatorului, card separat (NX-349), nu al promptului."""
    system = " ".join(ti.system_prompt(_input("electronics")).split())
    assert "true or false takes" not in system and "never avoid" not in system


@pytest.mark.parametrize("pack", ALL_PACKS)
def test_every_v4_rule_is_in_the_rendered_prompt(pack):
    system = " ".join(ti.system_prompt(_input(pack)).split())
    missing = [rule for rule, anchor in V4_RULES.items() if anchor not in system]
    assert missing == []


@pytest.mark.parametrize("pack", ALL_PACKS)
def test_v4_keeps_every_v2_and_v3_rule(pack):
    """v4 ADAUGĂ trei reguli; nicio regulă de dinainte nu se pierde la rescriere."""
    system = " ".join(ti.system_prompt(_input(pack)).split())
    assert [rule for rule, anchor in V2_RULES.items() if anchor not in system] == []
    assert "bundle: only when the customer asks for several items chosen together" in system


def test_the_prompt_version_is_v6():
    """NX-380: `v5` = regulile din setul wide-2026-10-07, meniul de tipuri întreg, `STORE NOTES`;
    NX-388: `v6` = partea despre care clientul își descrie starea e raftul ei într-un `bundle`."""
    assert ic.INTERPRET_PROMPT_VERSION == "interpret.v6"


# --- NX-380: interpret.v5 = regulile din setul wide-2026-10-07 + meniul de tipuri + STORE NOTES ---

#: Câte o frază-ancoră per regulă v5. Formulările sunt generice (I14): niciun cuvânt de vertical.
V5_RULES = {
    "find acoperă problema descrisă": "the customer describes a problem or a need",
    "bundle doar pentru set, rutină, pași": "bundle: only when the customer asks for several items",
    "coșul nu e comanda": "Asking about or changing an order already placed is order_status",
    "store_info = regula comună tuturor produselor": "a rule that is the same for every item",
    "question pe detail/compare": "question: for detail and compare",
    "orice tip spus devine schimbare": "never has empty changes",
    "alt fel de produs = set, nu add": "is op set on the dimensions the customer names for it",
    "partea corpului numește raftul": "the part or place the items are used on is",
    "umbrela de tip": "write the first listed code with that head",
    "forma de bază": "in base form (singular, no article)",
    "tipul de mai devreme se păstrează": "set that kind again, quoting that line",
    "referințe doar din mesajul curent": "Never copy a name or a reference from a bot line",
    "name doar pentru nume proprii": "only words that are an item's own name",
    "earlier cu poziție": "also fill ordinal, name or dimension + value",
    "extreme pe rating": "extreme (dimension price, rating",
    "cuvântul tradus numește codul": "a word that translates a code names that code",
}


@pytest.mark.parametrize("pack", ALL_PACKS)
def test_every_v5_rule_is_in_the_rendered_prompt(pack):
    system = " ".join(ti.system_prompt(_input(pack)).split())
    assert [rule for rule, anchor in V5_RULES.items() if anchor not in system] == []
    # v5 rescrie, nu pierde: regulile v2 și v4 rămân
    assert [rule for rule, anchor in V2_RULES.items() if anchor not in system] == []
    assert [rule for rule, anchor in V4_RULES.items() if anchor not in system] == []


def test_v5_keeps_never_narrower_only_outside_the_item_type():
    """Regula „never a narrower code for a broader word" făcea din «cremă» `unmapped` pe tip,
    opusul umbrelei NX-350. Rămâne pentru celelalte dimensiuni, o singură dată."""
    system = " ".join(ti.system_prompt(_input("sole-ro")).split())
    assert system.count("never a narrower code for a broader word") == 1
    assert "For the other dimensions, a code only when" in system


def test_the_item_type_menu_has_its_own_cap():
    """Plafonat la 20, meniul SOLE ascundea 34 din 54 de tipuri (setul wide-2026-10-07)."""
    inp = _input("sole-ro")
    many = tuple(VocabEntry(f"t{i:03d}", f"t{i:03d}", 5) for i in range(200))
    vocab = CatalogVocabulary(
        business_id="b", dimensions={**inp.vocab.dimensions, "product_type": many}
    )
    system = ti.system_prompt(dataclasses.replace(inp, vocab=vocab))
    line = next(ln for ln in system.splitlines() if ln.startswith("- product_type:"))
    assert line.count(", ") + 1 == ti.MAX_SUBJECT_MENU_VALUES
    assert ti.MAX_SUBJECT_MENU_VALUES > ti.MAX_MENU_VALUES


def _with_notes(name: str, notes: dict[str, tuple[str, ...]]) -> ti.InterpretInput:
    inp = _input(name)
    return dataclasses.replace(inp, pack=dataclasses.replace(inp.pack, interpret_notes=notes))


@pytest.mark.parametrize("pack", replay.FIXTURE_PACKS)
def test_without_store_notes_the_prompt_has_no_notes_block(pack):
    assert ti.STORE_NOTES_HEADER not in ti.system_prompt(_input(pack))


def test_the_sole_pack_brings_its_store_notes():
    """Pachetul SOLE din seed (`fc.pack("sole-ro")`) poartă notițele, deci promptul le arată."""
    system = ti.system_prompt(_input("sole-ro"))
    assert ti.STORE_NOTES_HEADER in system
    assert ti.system_prompt(_with_notes("sole-ro", {})).count(ti.STORE_NOTES_HEADER) == 0


def test_store_notes_are_the_last_block_and_leave_the_rest_unchanged():
    plain = ti.system_prompt(_with_notes("sole-ro", {}))
    noted = ti.system_prompt(_with_notes("sole-ro", {"ro": ("Prima notiță.", "A doua notiță.")}))
    assert noted.startswith(plain + "\n\n" + ti.STORE_NOTES_HEADER)
    assert noted.endswith("- Prima notiță.\n- A doua notiță.")


def test_store_notes_follow_the_locale_with_a_base_language_fallback():
    inp = _with_notes("sole-ro", {"ro": ("Notița ro.",), "en": ("Note en.",)})
    assert "Notița ro." in ti.system_prompt(dataclasses.replace(inp, locale="ro-RO"))
    assert "Note en." not in ti.system_prompt(dataclasses.replace(inp, locale="ro-RO"))
    assert ti.STORE_NOTES_HEADER not in ti.system_prompt(dataclasses.replace(inp, locale="hu"))


def test_the_schema_asks_for_the_question_and_allows_rating_only_on_references():
    schema = ti.interpretation_schema(fc.pack("sole-ro"))["schema"]
    act = schema["$defs"]["Act"]
    assert "question" in act["required"] and "default" not in act["properties"]["question"]
    ref_dims = schema["$defs"]["Reference"]["properties"]["dimension"]["anyOf"][0]["enum"]
    change_dims = schema["$defs"]["StateChange"]["properties"]["dimension"]["anyOf"][0]["enum"]
    assert "rating" in ref_dims and "rating" not in change_dims
    assert ref_dims == sorted(ref_dims)


def test_an_interpretation_without_question_still_parses():
    """Journey-urile și interpretările construite în cod n-au câmpul; modelul îl trimite mereu."""
    interp = TurnInterpretation.model_validate(
        {
            "thread": "continue",
            "acts": [{"kind": "detail", "targets": [], "query": None}],
            "changes": [],
            "references": [],
            "ambiguities": [],
            "corrects_previous_turn": False,
        }
    )
    assert interp.acts[0].question is None


def test_store_notes_change_the_snapshot_id():
    """Recenzia NX-380: turele cu și fără notițe trebuie să se despartă în raport (R2)."""
    plain = _with_notes("sole-ro", {})
    noted = _with_notes("sole-ro", {"ro": ("o notiță",)})
    assert ic.snapshot_id(plain.pack, plain.vocab) != ic.snapshot_id(noted.pack, noted.vocab)
