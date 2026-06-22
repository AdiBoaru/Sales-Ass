"""NX-112 — processor = singurul scriitor EXPLICIT al state-ului (P3).

Verifică write-back-ul: un slot NOU umplut de un stagiu pe `ctx.state.constraints`
ajunge în `new_state` și SUPRAVIEȚUIEȘTE turului următor (înainte se pierdea silențios
fiindcă `new_state` se construia doar din `conv["state"]` brut, nu din `ctx.state`).
Plus: `cart` (owner = Agent, via state_patch) nu e clobber-uit de merge-ul canonic.

ZERO OpenAI/DB real — stub conn + funcții monkeypatch-uite (pattern G8-1)."""

from src.models import BusinessConfig, Contact
from src.worker import processor as proc
from src.worker.processor import handle_turn
from src.worker.stages.clarify import clarify_resume_stage


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


async def _run(monkeypatch, stage, *, initial_state, body="salut"):
    """handle_turn cu DB stubbed; întoarce new_state-ul dat lui patch_conversation_state.
    `initial_state` devine `conv["state"]` (sursa din care se hidratează ctx.state)."""
    captured: dict = {}

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": initial_state,
            "state_version": 0,
            "locale": "ro",
            "bot_active": True,
            "handoff_until": None,
        }

    async def fake_patch(conn, business_id, conv_id, new_state, version, **k):
        captured["new_state"] = new_state

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="c", business_id="biz-1")

    async def fake_claim(*a, **k):
        return True

    async def fake_insert_msg(*a, **k):
        return "msg-id"

    async def fake_outbox(*a, **k):
        return "outbox-1"

    async def fake_budget(*a, **k):
        return None

    monkeypatch.setattr(proc, "claim_inbound", fake_claim)
    monkeypatch.setattr(proc, "mark_inbound_completed", anoop)
    monkeypatch.setattr(proc, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(proc, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(proc, "insert_message", fake_insert_msg)
    monkeypatch.setattr(proc, "touch_last_inbound", anoop)
    monkeypatch.setattr(proc, "get_recent_messages", anoop)
    monkeypatch.setattr(proc, "get_summary_for_context", anoop)
    monkeypatch.setattr(proc, "enqueue_outbox", fake_outbox)
    monkeypatch.setattr(proc, "patch_conversation_state", fake_patch)
    monkeypatch.setattr(proc, "_persist_events", anoop)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", fake_budget)
    monkeypatch.setattr(proc, "_cache_writeback", anoop)
    monkeypatch.setattr(proc, "_summarize_if_needed", anoop)

    business = BusinessConfig(id="biz-1", slug="s", name="n")
    event = {
        "channel_kind": "telegram",
        "sender_external_id": "u1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": body,
    }
    await handle_turn(_FakeConn(), business, "chan-1", event, stages=[stage])
    return captured["new_state"]


# --- slot NOU umplut de un stagiu pe ctx.state → persistat în new_state -------


async def test_new_constraint_persisted_to_new_state(monkeypatch):
    async def stage(ctx, deps):
        ctx.state.constraints["budget_max"] = "200"  # slot nou pe ctx.state (dict detașat)
        ctx.set_reply("ok")

    new_state = await _run(monkeypatch, stage, initial_state={})
    assert new_state["constraints"]["budget_max"] == "200"


async def test_constraint_survives_second_turn(monkeypatch):
    # Turul 1: umple slotul.
    async def fill(ctx, deps):
        ctx.state.constraints["budget_max"] = "200"
        ctx.set_reply("ok")

    s1 = await _run(monkeypatch, fill, initial_state={})
    assert s1["constraints"]["budget_max"] == "200"

    # Turul 2: state-ul rezultat devine conv["state"]; un stagiu care NU atinge constraints.
    async def noop_reply(ctx, deps):
        ctx.set_reply("alt raspuns")

    s2 = await _run(monkeypatch, noop_reply, initial_state=s1)
    assert s2["constraints"]["budget_max"] == "200"  # nu e stale/uitat (bug-ul NX-112)


async def test_preexisting_and_new_constraint_both_persist(monkeypatch):
    async def stage(ctx, deps):
        ctx.state.constraints["skin_type"] = "uscat"  # slot nou peste unul pre-existent
        ctx.set_reply("ok")

    new_state = await _run(monkeypatch, stage, initial_state={"constraints": {"x": 1}})
    assert new_state["constraints"] == {"x": 1, "skin_type": "uscat"}


# --- integrare cu clarify_resume_stage: constraint + asked_intents persistate -


async def test_clarify_resume_persists_constraint_and_asked_intent(monkeypatch):
    async def stage(ctx, deps):
        ctx.state.pending_question = {"field": "budget_max", "resume_route": "sales"}
        await clarify_resume_stage(ctx, deps)  # umple constraint + asked_intents
        ctx.set_reply("Iată ce am găsit")  # reply ca să se ajungă la write-back

    new_state = await _run(monkeypatch, stage, initial_state={}, body="200 lei")
    assert new_state["constraints"]["budget_max"] == "200 lei"
    assert "budget_max" in new_state["asked_intents"]


# --- cart (owner Agent, via state_patch) NU e clobber-uit de merge-ul canonic -


async def test_cart_via_state_patch_coexists_with_constraint(monkeypatch):
    async def stage(ctx, deps):
        ctx.state.constraints["budget_max"] = "200"  # merge canonic
        # cart_add scrie via state_patch (ctx.state.cart NU e sincronizat) → trebuie să câștige.
        ctx.state_patch["cart"] = [{"product_id": "p1", "name": "X", "price": 10.0, "quantity": 1}]
        ctx.set_reply("ok")

    new_state = await _run(monkeypatch, stage, initial_state={"cart": []})
    assert new_state["constraints"]["budget_max"] == "200"  # constraint persistat
    assert new_state["cart"][0]["product_id"] == "p1"  # cart-ul nou NU e suprascris cu cel vechi


# --- NX-119b: sesiunea de căutare se resetează la reply non-căutare (fără zombi) -


async def test_active_search_reset_on_non_search_reply(monkeypatch):
    # reply FĂRĂ produse → sesiunea de căutare veche se șterge (un „mai arată-mi" ulterior n-o reia)
    async def stage(ctx, deps):
        ctx.set_reply("Salut! Cu ce te ajut?")  # niciun produs

    new_state = await _run(
        monkeypatch,
        stage,
        initial_state={"active_search": {"pool": ["p1"], "cursor": 6, "fp": "x"}},
    )
    assert new_state["active_search"] is None


async def test_active_search_kept_on_product_reply(monkeypatch):
    # reply CU produse + state_patch active_search → sesiunea persistă (state_patch are întâietate)
    async def stage(ctx, deps):
        ctx.state_patch["active_search"] = {"pool": ["p2"], "cursor": 6, "fp": "y", "page": 0}
        ctx.set_reply("Uite", products=[{"product_id": "p2", "name": "P2", "price": 9.0}])

    new_state = await _run(monkeypatch, stage, initial_state={})
    assert new_state["active_search"]["fp"] == "y"
