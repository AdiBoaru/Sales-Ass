"""NX-251 — contextul conversațional și orchestrarea LLM: ce se trimite, cine afirmă, ce se reține.

Cardul închide patru găuri care aveau în comun un lucru: fiecare era invizibilă cât timp te uitai
la un singur tur.

  • sursa unui fapt era decisă de model (`model_inferred` peste tot) — deci, fără triaj, nu mai
    exista nicio constrângere `hard`;
  • o clarificare pusă de brain nu se persista, deci nu putea fi reluată ȘI nu se număra
    împotriva buclei — aceeași întrebare, tur după tur;
  • repair-ul rula fără evidence, deci nu putea repara exact planurile care depindeau de tool-uri;
  • aceleași constrângeri plecau de două ori în prompt, iar copia fără tărie e cea care invită la
    relaxare.

ZERO OpenAI / ZERO DB: refolosim harnessul fals din `test_single_brain` (un singur harness — două
copii ale aceluiași fake divergează, și atunci testezi copia, nu sistemul).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings
from src.conversation.state_v2 import ConversationStateV2, Need
from src.models import Contact, ConversationState, Route, RouteDecision
from src.worker import aftercare as aftercare_mod
from src.worker.context import context_blocks
from src.worker.runner import PipelineDeps
from src.worker.stages import triage as triage_mod
from tests.test_single_brain import (
    _PRODUCT,
    _ctx,
    _events,
    _FakeLLM,
    _FakePort,
    _plan_dict,
    _run,
)


def _proposals(ctx, op: str):
    return [p for p in ctx.state_proposals if p.op == op]


# ── Cine AFIRMĂ faptul: coroborarea (Card 3) ─────────────────────────────────


async def test_a_value_the_client_said_becomes_their_own_assertion(monkeypatch):
    """Modelul transcrie, codul certifică. Fără poarta asta, în momentul în care triajul nu mai
    extrage sloturi, TOATE nevoile ar coborî la `soft` și noțiunea de constrângere inviolabilă ar
    dispărea fără ca nimic să semnaleze."""
    ctx = _ctx("vreau un ser sub 200 de lei")
    plan = _plan_dict(
        state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 200}]
    )
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)

    proposal = _proposals(ctx, "set_need")[0]
    assert proposal.source == "user_explicit"
    assert _events(ctx, "state_proposal_source")[0].properties["source"] == "user_explicit"


async def test_a_value_nobody_said_stays_an_inference(monkeypatch):
    """Cazul periculos: modelul „deduce" un buget. Rămâne `model_inferred` → reducerul îl ține
    `soft` (D7), deci nu poate exclude produse și nu poate rescrie un fapt al clientului."""
    ctx = _ctx("vreau un ser bun pentru ten uscat")
    plan = _plan_dict(
        state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 200}]
    )
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)

    assert _proposals(ctx, "set_need")[0].source == "model_inferred"


async def test_a_retraction_is_corroborated_against_what_is_in_memory(monkeypatch):
    """Revocarea nu se coroborează pe o valoare propusă, ci pe cea AFLATĂ în memorie: „de fapt
    accept Sony" conține exact faptul care se retrage. Un „bugetul nu mai contează" nu conține
    „200" — și tocmai de asta nu poate șterge un plafon declarat de client."""
    ctx = _ctx("de fapt accept si Sony")
    ctx.state_v2 = ConversationStateV2(
        needs=(
            Need(
                key="brand",
                operator="eq",
                normalized_value="sony",
                strength="hard",
                status="active",
                source="user_explicit",
            ),
        )
    )
    plan = _plan_dict(
        obligations=[{"kind": "answer", "key": "question_0"}],
        state_update_proposals=[{"op": "revoke", "key": "brand", "value": None}],
    )
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)

    assert _proposals(ctx, "revoke")[0].source == "user_explicit"


async def test_an_uncorroborated_retraction_cannot_erase_the_clients_limit(monkeypatch):
    ctx = _ctx("bugetul nu mai conteaza")
    ctx.state_v2 = ConversationStateV2(
        needs=(
            Need(
                key="budget_max",
                operator="lte",
                normalized_value=200.0,
                strength="hard",
                status="active",
                source="user_explicit",
            ),
        )
    )
    plan = _plan_dict(
        obligations=[{"kind": "answer", "key": "question_0"}],
        state_update_proposals=[{"op": "revoke", "key": "budget_max", "value": None}],
    )
    await _run(ctx, _FakeLLM(plan=plan, search_args={"query": "ser"}), _FakePort(), monkeypatch)

    # Propunerea pleacă spre reducer ca inferență — iar reducerul o respinge (`unsupported_revoke`,
    # testat în `test_state_reducer`). Aici verificăm doar că nu se dă drept afirmația clientului.
    assert _proposals(ctx, "revoke")[0].source == "model_inferred"


# ── O întrebare pusă trebuie să poată fi reluată (Card 4) ────────────────────


def _clarifying_plan() -> dict:
    return _plan_dict(
        clarification={
            "question": "Ce buget ai în minte?",
            "target_need": "budget_max",
            "reason": "missing_required",
            "options": ["sub 100 lei", "100-200 lei"],
        }
    )


async def test_a_question_the_brain_asks_is_remembered_for_the_next_turn(monkeypatch):
    """Fără asta, răspunsul scurt care urmează („sub 200") repornea de la zero: nimic nu ținea
    minte CE s-a întrebat, deci `clarify_resume_stage` nu avea ce relua."""
    ctx = _ctx("vreau un ser")
    await _run(
        ctx,
        _FakeLLM(plan=_clarifying_plan(), search_args={"query": "ser"}),
        _FakePort(),
        monkeypatch,
    )

    pending = ctx.reply.pending_question
    assert pending is not None
    assert pending["field"] == "budget_max"
    assert pending["resume_route"] == Route.SALES.value
    assert _proposals(ctx, "set_pending_question")[0].key == "budget_max"


async def test_asking_the_same_slot_again_counts_against_the_loop(monkeypatch):
    """`attempts` e semnalul anti-buclă. Brain-ul nu-l incrementa niciodată, deci — cu poarta de
    information gain stinsă, adică defaultul — aceeași întrebare se putea repeta la infinit."""
    ctx = _ctx("vreau un ser")
    ctx.state = ConversationState(pending_question={"field": "budget_max", "attempts": 1})
    plan = _clarifying_plan()
    plan["obligations"] = [
        {"kind": "answer", "key": "pending_clarification"},
        {"kind": "recommend", "key": "recommend_0"},
    ]
    await _run(
        ctx,
        _FakeLLM(plan=plan, search_args={"query": "ser"}),
        _FakePort(),
        monkeypatch,
    )

    assert ctx.reply.pending_question["attempts"] == 2


async def test_a_turn_without_a_question_leaves_no_pending_slot(monkeypatch):
    ctx = _ctx()
    await _run(
        ctx, _FakeLLM(plan=_plan_dict(), search_args={"query": "ser"}), _FakePort(), monkeypatch
    )
    assert not ctx.reply.pending_question


# ── Repair-ul are nevoie de evidence ca să poată repara (Card 5) ─────────────


class _RepairCapturingLLM(_FakeLLM):
    """Reține promptul de repair — singurul lucru care ne interesează aici."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.repair_user = ""

    async def complete_schema(self, system, user, schema, **kw):
        self.repair_user = user
        return await super().complete_schema(system, user, schema, **kw)


async def test_the_repair_sees_the_evidence_it_is_asked_to_cite(monkeypatch):
    """Repair-ul rulează în AFARA conversației în care s-au văzut rezultatele tool-urilor. I se
    cerea să citeze `evidence_ids` pe care nu le mai avea în față — deci singurele planuri
    reparabile erau cele care nu depindeau de evidence, adică aproape niciunul după o căutare."""
    ctx = _ctx()
    invalid = _plan_dict(
        selected_products=[
            {"product_id": "p1", "variant_id": None, "evidence_ids": ["inventat:nu-exista"]}
        ]
    )
    llm = _RepairCapturingLLM(
        plan=invalid, repair=_plan_dict(), search_args={"query": "ser ten uscat"}
    )
    await _run(ctx, llm, _FakePort(), monkeypatch)

    assert llm.repair_calls == 1
    assert "EVIDENCE DISPONIBIL" in llm.repair_user
    assert _PRODUCT["id"] in llm.repair_user


# ── Contextul: aceleași fapte nu pleacă de două ori (Card 1) ─────────────────


def _ctx_with_memory():
    ctx = _ctx()
    ctx.contact = Contact(id="c1", business_id="b1")
    ctx.state = ConversationState(constraints={"budget_max": 200})
    ctx.state_v2 = ConversationStateV2(
        needs=(
            Need(
                key="budget_max",
                operator="lte",
                normalized_value=200.0,
                strength="hard",
                status="active",
                source="user_explicit",
            ),
        )
    )
    return ctx


def test_constraints_are_stated_once_and_with_their_strength():
    """Blocul v1 proiecta aceleași constrângeri ca `memory_block`, dar fără tărie. Două copii ale
    aceluiași fapt în același prompt, dintre care una spune mai puțin — iar cea săracă e exact cea
    care invită la relaxare."""
    ctx = _ctx_with_memory()
    blocks = context_blocks(ctx)

    assert "Constrângeri știute" not in blocks
    assert "obligatoriu" in blocks  # memory_block declară tăria


def test_without_v2_the_legacy_block_still_carries_the_constraints():
    """Cu v2 stins nu se schimbă nimic: contextul rămâne cel de dinainte de card."""
    ctx = _ctx_with_memory()
    ctx.state_v2 = None
    assert "Constrângeri știute" in context_blocks(ctx)


def test_context_size_is_measured_per_block_and_per_consumer():
    """Cât timp triajul și agentul construiesc amândoi contextul, aceeași informație pleacă de
    două ori pe același tur — iar asta trebuie să se vadă într-o cifră, nu într-un comentariu."""
    ctx = _ctx_with_memory()
    context_blocks(ctx, consumer="agent")

    event = _events(ctx, "context_bytes")[0].properties
    assert event["consumer"] == "agent"
    assert event["memory"] > 0
    assert event["total"] == sum(
        event[name] for name in ("summary", "profile", "facts", "state", "memory", "page")
    )


def test_building_the_context_without_a_consumer_measures_nothing():
    """`build_brain_input` compune aceleași blocuri pentru contractul `BrainInput`, dar promptul
    care pleacă e cel din `agent_stage`: a le număra de acolo ar raporta un context trimis de două
    ori când el pleacă o dată."""
    ctx = _ctx_with_memory()
    context_blocks(ctx)
    assert _events(ctx, "context_bytes") == []


# ── Triajul iese de pe drumul sincron (Card 2) ───────────────────────────────


class _CountingTriageLLM:
    """Numără apelurile de clasificare. Un fake care ARUNCĂ n-ar dovedi nimic: `classify_message`
    degradează corect la orice excepție, deci testul ar trece și dacă apelul chiar s-a făcut."""

    model_triage = "nano-de-test"

    def __init__(self):
        self.calls = 0

    async def classify_json(self, system, user, **kw):
        self.calls += 1
        return {"route": "sales", "category_key": None, "reply": None, "confidence": "high"}


async def _run_triage(monkeypatch, *, flag: bool):
    monkeypatch.setattr(get_settings(), "triage_sync_shadow_enabled", flag, raising=False)

    async def _no_categories(conn, business_id):
        return []

    monkeypatch.setattr(triage_mod, "list_category_slugs", _no_categories)
    ctx, llm = _ctx(), _CountingTriageLLM()
    await triage_mod.triage_stage(ctx, PipelineDeps(conn=None, llm=llm))
    return ctx, llm


async def test_no_small_model_runs_before_the_brain(monkeypatch):
    """D1, verificat pe apeluri, nu pe intenții: sub flag nu se mai cheltuie nicio clasificare
    între client și răspuns."""
    ctx, llm = await _run_triage(monkeypatch, flag=True)

    assert llm.calls == 0
    assert ctx.route is None  # proprietarul rutei devine `agent_stage`
    assert _events(ctx, "triage_deferred")[0].properties["reason"] == "sync_shadow"


async def test_with_the_flag_off_triage_still_classifies(monkeypatch):
    """Contra-testul care ține flagul onest: stins = comportamentul de azi, neatins."""
    ctx, llm = await _run_triage(monkeypatch, flag=False)

    assert llm.calls == 1
    assert ctx.route is not None and ctx.route.route is Route.SALES
    assert _events(ctx, "triage_deferred") == []


def test_moving_triage_off_the_path_without_a_brain_is_refused_at_boot(monkeypatch):
    """Combinația e imposibilă, nu degradată: nimeni n-ar mai decide ruta, deci fiecare mesaj ar
    cădea în fallback-ul generic. Un proces care pornește așa ar arăta sănătos și ar răspunde
    prostii."""
    for key, value in {
        "SUPABASE_DB_URL": "postgresql://u:p@host:5432/db",
        "OPENAI_API_KEY": "sk-test",
        "TRIAGE_SYNC_SHADOW_ENABLED": "true",
        "SINGLE_BRAIN_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="SINGLE_BRAIN_ENABLED"):
        Settings(_env_file=None)


# ── Shadow-ul post-tur compară VERDICTE, nu texte ────────────────────────────


def test_what_the_turn_did_is_read_from_artefacts_not_reinterpreted():
    ctx = _ctx()
    ctx.route = RouteDecision(route=Route.SALES)
    assert aftercare_mod._brain_outcome(ctx) == "other"

    ctx.set_reply("text")
    ctx.reply.pending_question = {"field": "budget_max", "attempts": 1}
    assert aftercare_mod._brain_outcome(ctx) == "clarify"


def test_the_agreement_map_is_strict():
    """O hartă indulgentă („clarify e ok și dacă a răspuns") ar coborî rata de dezacord exact
    acolo unde vrem s-o vedem, iar shadow-ul ar valida promovarea din construcție."""
    assert aftercare_mod._SHADOW_AGREEMENT["clarify"] == "clarify"
    assert aftercare_mod._SHADOW_AGREEMENT["order"] == "order"
    assert "handoff" not in aftercare_mod._SHADOW_AGREEMENT  # ruta nu mai există
