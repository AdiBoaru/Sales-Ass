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

from src.config import get_settings
from src.conversation.state_v2 import ConversationStateV2, Need
from src.models import Contact, ConversationState, Route
from src.worker.context import context_blocks
from src.worker.runner import PipelineDeps
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


# ── D1, verificat STRUCTURAL: niciun model mic între client și agent ─────────


def test_no_stage_classifies_with_a_model() -> None:
    """Invariantul D1, ca poartă pe FORMĂ — nu ca test pe un flag.

    Cât timp triajul exista, „nano nu mai rulează sincron" era o proprietate a unei SETĂRI, deci
    se putea pierde prin config. NX-297 l-a șters, iar atunci invariantul devine verificabil pe
    cod: niciun stagiu din pipeline n-are voie să cheme un clasificator de model. Un al doilea
    triaj adăugat mâine ar arăta exact ca primul — alt nume, același defect — și n-ar fi prins de
    niciun test de comportament, fiindcă ar trece.

    `agent_stage` e EXCLUS explicit: bucla lui de tool-calling e chiar agentul, nu un strat
    dinaintea lui. `classify_json` (un singur consumator: extracția de fundal, POST-tur) nu
    trăiește în `stages/`.
    """
    import ast
    from pathlib import Path

    offenders: list[str] = []
    for path in sorted(Path("src/worker/stages").glob("*.py")):
        if path.name == "agent.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"classify_json", "complete_schema", "run_tool_loop"}
            ):
                offenders.append(f"{path.as_posix()}:{node.lineno} → {node.func.attr}")
    assert not offenders, "stagiu care cheamă un model înaintea agentului (D1): " + ", ".join(
        offenders
    )


async def test_the_agent_owns_the_route_when_nobody_else_set_it():
    """Perechea: absența rutei nu mai e o excepție sub flag, e cazul NORMAL, iar `agent_stage` o
    tratează ca atare. Fără asta, ștergerea triajului ar fi lăsat fiecare mesaj în fallback."""
    from src.worker.stages.agent import agent_stage

    class _BoomLLM:
        model_agent = "model-de-test"

        async def run_tool_loop(self, *a, **kw):
            raise RuntimeError("oprim după ce ruta e decisă")

    ctx = _ctx()
    assert ctx.route is None
    await agent_stage(ctx, PipelineDeps(conn=None, llm=_BoomLLM()))

    assert ctx.route is not None and ctx.route.route is Route.SALES
    assert _events(ctx, "route_defaulted")[0].properties["reason"] == "no_triage"


# --- NX-275 felia 3: layoutul pentru prompt caching --------------------------


def test_layoutul_stins_e_byte_identic_cu_azi(monkeypatch):
    """Poarta feliei: cu flagul stins, mesajul compus trebuie să fie EXACT cel de azi.

    Nu „echivalent", nu „aceleași informații" — aceiași octeți. Un layout care se schimbă tăcut
    ar invalida prompt cachingul existent (prefixul de system) și ar schimba comportamentul
    modelului, iar amândouă ar fi puse pe seama altui card.

    Flagul se stinge EXPLICIT, nu se presupune: `.env`-ul de dezvoltare are stiva aprinsă, deci un
    test care citește configul ambiental măsoară mediul, nu invariantul."""
    from src.agent.brain import _compose_user
    from src.agent.brain_models import UserParts

    monkeypatch.setenv("PROMPT_CACHE_LAYOUT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        parts = UserParts(history="ISTORIC\n", per_turn="PERTUR\n", message="Mesaj client: bla")
        blocks = "OBLIGATII\nNEVOI\nSEMNALE\n"
        assert _compose_user(parts.legacy(), parts, blocks) == blocks + parts.legacy()
        assert _compose_user(parts.legacy(), None, blocks) == blocks + parts.legacy()
    finally:
        monkeypatch.delenv("PROMPT_CACHE_LAYOUT_ENABLED", raising=False)
        get_settings.cache_clear()


def test_layoutul_aprins_urca_istoricul_in_fata(monkeypatch):
    """Aprins: istoricul primul, mesajul ultimul, conținut neschimbat.

    Cele două aserții care contează: (a) niciun octet nu se pierde pe drum — reordonare, nu
    rescriere; (b) istoricul chiar e la POZIȚIA 0, fiindcă un cache se prinde pe prefix și orice
    octet variabil pus înaintea lui îl scoate din joc."""
    from src.agent.brain import _compose_user
    from src.agent.brain_models import UserParts

    monkeypatch.setenv("PROMPT_CACHE_LAYOUT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        parts = UserParts(history="ISTORIC\n", per_turn="PERTUR\n", message="Mesaj client: bla")
        blocks = "OBLIGATII\n"
        out = _compose_user(parts.legacy(), parts, blocks)
        assert out.startswith("ISTORIC\n")
        assert out.endswith("Mesaj client: bla")
        assert sorted(out) == sorted(blocks + parts.legacy())  # aceiași octeți, altă ordine
    finally:
        monkeypatch.delenv("PROMPT_CACHE_LAYOUT_ENABLED", raising=False)
        get_settings.cache_clear()


def test_cheia_de_cache_e_a_tenantului_si_a_versiunii_nu_a_conversatiei():
    """Cheia leagă turele care CHIAR au același prefix și le separă pe cele care n-au.

    `conversation_id` în cheie ar face fiecare conversație propria partiție — adică exact opusul
    scopului, cu aparența că s-a făcut ceva. Versiunea de prompt trebuie SĂ FIE acolo: altfel, la
    o schimbare de prompt, cererile ar căuta într-un cache al formei vechi."""
    from types import SimpleNamespace

    from src.agent.brain import BRAIN_PROMPT_VERSION, _cache_key

    ctx = SimpleNamespace(business=SimpleNamespace(id="biz-1"), conversation_id="conv-9")
    key = _cache_key(ctx)
    assert key == f"biz-1:{BRAIN_PROMPT_VERSION}"
    assert "conv-9" not in key


def test_cheia_de_cache_nu_pleaca_pe_sarma_cu_flagul_stins(monkeypatch):
    """OFF = byte-identic, inclusiv pe parametrii trimiși furnizorului.

    Un parametru în plus e nevinovat la OpenAI, dar nu și la un endpoint compatibil care refuză
    câmpuri necunoscute — iar felia asta nu are voie să schimbe nimic până nu e măsurată."""
    import asyncio
    from types import SimpleNamespace

    from src.agent import llm as llm_mod

    monkeypatch.setenv("PROMPT_CACHE_LAYOUT_ENABLED", "false")
    get_settings.cache_clear()
    sent: dict = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            raise RuntimeError("stop")  # nu ne interesează răspunsul, doar ce s-a trimis

    client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    try:
        with llm_mod.prompt_cache_scope("biz-1:main_brain.v1"):
            with pytest.raises(Exception):
                asyncio.run(client._chat(agent=True, model="gpt-5.6-luna", messages=[]))
        assert "prompt_cache_key" not in sent
    finally:
        monkeypatch.delenv("PROMPT_CACHE_LAYOUT_ENABLED", raising=False)
        get_settings.cache_clear()
