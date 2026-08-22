"""NX-251 — journey-uri adversariale pe pipeline-ul REAL, cu triajul scos de pe calea sincronă.

Un test de tur verifică un RĂSPUNS; problemele pe care le închide cardul apar între tururi. De
aceea aici totul e multi-tur, pe `run_pipeline` cu stagiile adevărate, iar starea se cară de la un
tur la altul exact cum o cară processorul: prin reducer.

ZERO OpenAI / ZERO DB: LLM scriptat + port de retrieval fals.
"""

from __future__ import annotations

from src.agent import brain as brain_mod
from src.agent.brain_models import obligations_from_ctx
from src.config import get_settings
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, reduce_all
from src.conversation.state_v2 import ConversationStateV2
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
from src.worker.runner import PipelineDeps, fallback_stage, run_pipeline
from src.worker.stages import agent as agent_stage_mod
from src.worker.stages import triage as triage_mod
from src.worker.stages.agent import agent_stage
from src.worker.stages.clarify import clarify_resume_stage
from src.worker.stages.triage import triage_stage
from tests.test_single_brain import _FakePort, _plan_dict

_PRODUCT = {
    "id": "p1",
    "business_id": "b1",
    "name": "Ser LumaDerm",
    "price": 89.0,
    "availability": "in_stock",
    "product_url": "https://demo.example/p1",
}

_STAGES = [clarify_resume_stage, triage_stage, agent_stage, fallback_stage]

POLICY = ReducerPolicy(vocabulary=NeedVocabulary.from_pack(None))


class _BrainOnlyLLM:
    """Doar creierul e scriptat. `classify_json` NU trebuie chemat niciodată în journey-urile
    astea — dacă e, cardul și-a ratat scopul, deci îl facem să pice zgomotos."""

    model_agent = "model-de-test"
    model_triage = "nano-de-test"

    def __init__(self, plans: list[dict], *, search: bool = True):
        self.plans = list(plans)
        self.search = search
        self.captured_user: str | None = None
        #: Obligațiile pe care le-a extras CODUL pentru turul curent (vezi `_Conversation.turn`).
        #: Un model care se poartă bine le declară exact pe astea; le injectăm ca testele să
        #: exerseze memoria și clarificarea, nu contabilitatea obligațiilor (acoperită în
        #: `test_single_brain`). Fără injecție, un plan scriptat cade la validare și testul ar
        #: trece din motivul greșit: nicio propunere de state nu s-ar mai aplica.
        self.obligations: list[dict] | None = None

    async def classify_json(self, system, user, *, model=None):  # pragma: no cover
        raise AssertionError("un model mic a rulat pe drumul sincron")

    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        self.captured_user = user
        rounds = 0
        if self.search:
            await execute("search_products", {"query": "ser"})
            rounds = 1
        plan = dict(self.plans.pop(0))
        if self.obligations is not None:
            plan["obligations"] = self.obligations
        return plan, rounds

    async def complete_schema(self, system, user, schema, **kw):  # pragma: no cover
        raise RuntimeError("repair neprogramat în acest scenariu")


def _wire(monkeypatch, port: _FakePort | None = None) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "single_brain_enabled", True, raising=False)
    monkeypatch.setattr(settings, "triage_sync_shadow_enabled", True, raising=False)
    monkeypatch.setattr(settings, "conversation_state_v2_enabled", True, raising=False)

    async def _empty(conn, business_id):
        return []

    monkeypatch.setattr(triage_mod, "list_category_slugs", _empty)
    monkeypatch.setattr(agent_stage_mod, "list_category_names", _empty)
    monkeypatch.setattr(agent_stage_mod, "list_routing_aliases", _empty)
    monkeypatch.setattr(brain_mod, "build_port", lambda ctx, deps, sel, **kw: port or _FakePort())


class _Conversation:
    """Starea care supraviețuiește între tururi, redusă exact ca în processor.

    Nu simulăm persistența: chemăm ACELAȘI reducer pe aceleași propuneri. Un fake de state ar
    testa fake-ul."""

    def __init__(self, state: ConversationStateV2 | None = None):
        self.state_v2 = state or ConversationStateV2()
        self.history: list[Message] = []
        self.pending_question: dict | None = None
        self.rejected: list = []

    async def turn(self, body: str, llm, monkeypatch, port=None) -> TurnContext:
        _wire(monkeypatch, port)
        ctx = TurnContext(
            turn_id=f"t{len(self.history)}",
            business=BusinessConfig(id="b1", slug="demo", name="Demo", vertical="beauty"),
            contact=Contact(id="c1", business_id="b1"),
            message=InboundMessage(provider_msg_id=f"m{len(self.history)}", body=body),
            conversation_id="conv1",
            state=ConversationState(pending_question=self.pending_question),
            history=[*self.history, Message(Direction.INBOUND, Author.CONTACT, body)],
        )
        ctx.state_v2 = self.state_v2
        llm.obligations = [{"kind": o.kind, "key": o.key} for o in obligations_from_ctx(ctx)]
        await run_pipeline(ctx, PipelineDeps(conn=None, llm=llm), _STAGES)

        reduced = reduce_all(self.state_v2, ctx.state_proposals, POLICY)
        self.state_v2 = reduced.state
        self.rejected = list(reduced.rejected)
        self.pending_question = ctx.reply.pending_question if ctx.reply else None
        self.history.append(Message(Direction.INBOUND, Author.CONTACT, body))
        if ctx.reply is not None:
            self.history.append(Message(Direction.OUTBOUND, Author.BOT, ctx.reply.text))
        return ctx


def _answer_plan(**kw) -> dict:
    """Plan valid; obligațiile sunt injectate de harness din ce a extras codul pentru mesaj."""
    return _plan_dict(**kw)


# ── „Nu vreau Sony" → „de fapt accept Sony" ──────────────────────────────────


async def test_a_rejected_brand_is_remembered_then_released_when_the_client_says_so(monkeypatch):
    """Scenariul emblematic, cap-coadă. Ce apără cardul nu e memoria in general, ci AUTORITATEA:
    modelul poate retrage ce a dedus tot el, fiindcă un fapt dedus e `soft` prin naștere (D7)."""
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _answer_plan(
                state_update_proposals=[{"op": "set_need", "key": "brand", "value": "sony"}]
            ),
            _answer_plan(state_update_proposals=[{"op": "revoke", "key": "brand", "value": None}]),
        ]
    )

    await conv.turn("nu vreau Sony", llm, monkeypatch)
    assert conv.state_v2.need_for("brand").normalized_value == "sony"

    await conv.turn("de fapt accept si Sony", llm, monkeypatch)
    assert conv.state_v2.need_for("brand") is None
    assert "brand" in conv.state_v2.revoked_keys()


async def test_the_model_cannot_quietly_drop_a_limit_the_client_set(monkeypatch):
    """Contra-journey-ul: un plafon DECLARAT de client (coroborat → `user_explicit` → `hard`) nu
    poate fi șters de o inferență la turul următor. Respingerea e vizibilă, nu tăcută."""
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _answer_plan(
                state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 200}]
            ),
            _answer_plan(
                state_update_proposals=[{"op": "revoke", "key": "budget_max", "value": None}]
            ),
        ]
    )

    await conv.turn("vreau ceva sub 200 de lei", llm, monkeypatch)
    need = conv.state_v2.need_for("budget_max")
    assert need.strength == "hard" and need.source == "user_explicit"

    await conv.turn("arata-mi mai multe optiuni", llm, monkeypatch)
    assert conv.state_v2.need_for("budget_max").normalized_value == 200.0
    assert [r.reason for r in conv.rejected] == ["unsupported_revoke"]


# ── Bugetul se schimbă (corecție, nu ștergere) ───────────────────────────────


async def test_a_new_budget_supersedes_the_old_one_and_leaves_a_tombstone(monkeypatch):
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _answer_plan(
                state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 200}]
            ),
            _answer_plan(
                state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 350}]
            ),
        ]
    )

    await conv.turn("vreau ceva sub 200 de lei", llm, monkeypatch)
    await conv.turn("de fapt pot da pana in 350", llm, monkeypatch)

    assert conv.state_v2.need_for("budget_max").normalized_value == 350.0
    assert [n.normalized_value for n in conv.state_v2.needs if n.status == "superseded"] == [200.0]


# ── Clarificarea pusă de brain se consumă determinist la turul următor ───────


async def test_the_answer_to_the_brains_question_is_consumed_not_re_guessed(monkeypatch):
    """Bucla completă: brain întreabă → slotul se persistă → `clarify_resume_stage` consumă
    răspunsul scurt și rutează determinist, fără niciun clasificator."""
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _plan_dict(
                obligations=[{"kind": "recommend", "key": "recommend_0"}],
                clarification={
                    "question": "Ce buget ai în minte?",
                    "target_need": "budget_max",
                    "reason": "missing_required",
                    "options": [],
                },
            ),
            _answer_plan(),
        ]
    )

    first = await conv.turn("vreau un ser", llm, monkeypatch)
    assert first.reply.pending_question["field"] == "budget_max"
    assert conv.state_v2.pending_clarification.target_key == "budget_max"

    second = await conv.turn("sub 200", llm, monkeypatch)
    # slotul s-a închis, iar răspunsul clientului a devenit fapt AL LUI (nu inferență)
    assert conv.state_v2.pending_clarification is None
    assert conv.state_v2.need_for("budget_max").source == "user_explicit"
    assert second.route is not None  # rutat determinist de clarify_resume, nu de un model mic


# ── Conversație lungă: rezumatul nu poate reînvia o corecție ────────────────


async def test_a_stale_summary_cannot_resurrect_what_the_client_took_back(monkeypatch):
    """Rezumatul e un artefact DERIVAT și neautoritativ. Chiar dacă mai conține bugetul retras,
    tombstone-ul îl ține afară — iar promptul o spune explicit, ca modelul să nu-l „recitească"."""
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _answer_plan(
                state_update_proposals=[{"op": "set_need", "key": "budget_max", "value": 200}]
            ),
            _answer_plan(),
        ]
    )
    await conv.turn("vreau ceva sub 200 de lei", llm, monkeypatch)
    # Ancora testului: faptul CHIAR a intrat în memorie. Fără verificarea asta, un plan care ar
    # cădea la validare ar face restul testului să treacă din motivul greșit — n-ar mai exista
    # nimic de reînviat, deci „rezumatul nu l-a readus" ar fi o afirmație goală.
    assert conv.state_v2.need_for("budget_max").normalized_value == 200.0

    await conv.turn("nu mai conteaza bugetul, sterge-l", llm, monkeypatch)

    # clientul retrage explicit: aici o facem prin reducer, ca la `clarify_resume` (user_explicit)
    from src.conversation.state_reducer import StateUpdateProposal

    conv.state_v2 = reduce_all(
        conv.state_v2,
        [StateUpdateProposal("revoke", key="budget_max", source="user_explicit")],
        POLICY,
    ).state
    assert "budget_max" in conv.state_v2.revoked_keys()

    ctx = await conv.turn("si ceva pentru par?", llm, monkeypatch)
    assert conv.state_v2.need_for("budget_max") is None
    assert "budget_max" in (llm.captured_user or "")  # revocarea e DECLARATĂ în prompt
    assert "NU le reintroduce" in (llm.captured_user or "")
    assert ctx.reply.text


# ── P6: turul livrează chiar dacă modelul cade ──────────────────────────────


class _DeadLLM(_BrainOnlyLLM):
    async def run_tool_loop_structured(self, system, user, tools, execute, schema, **kw):
        raise TimeoutError("provider indisponibil")

    async def complete_schema(self, system, user, schema, **kw):
        raise TimeoutError("provider indisponibil")


async def test_the_client_still_gets_an_answer_when_the_model_is_down(monkeypatch):
    conv = _Conversation()
    ctx = await conv.turn("vreau un ser pentru ten uscat", _DeadLLM([]), monkeypatch)

    assert ctx.reply is not None and ctx.reply.text.strip()
    assert ctx.reply.cacheable is False  # un răspuns de degradare nu otrăvește cache-ul


async def test_a_retrieval_outage_does_not_silence_the_turn(monkeypatch):
    conv = _Conversation()
    llm = _BrainOnlyLLM(
        [
            _plan_dict(
                obligations=[{"kind": "recommend", "key": "recommend_0"}],
                selected_products=[],
                recommendations=[],
                no_results={
                    "reason_class": "dependency_unavailable",
                    "criteria": [],
                    "alternatives": [],
                },
            )
        ]
    )
    port = _FakePort(raise_error=TimeoutError("retrieval jos"))
    ctx = await conv.turn("vreau un ser pentru ten uscat", llm, monkeypatch, port=port)

    assert ctx.reply is not None and ctx.reply.text.strip()
    # taxonomia ONESTĂ: „nu pot verifica acum" nu se dă drept „nu există" (D7)
    assert "disponibil" in ctx.reply.text.lower()
