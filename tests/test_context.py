"""Teste unit pentru context builder (transcript + search_query + profil/state bugetate)."""

from src.models import (
    Author,
    BusinessConfig,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    ProductRef,
    TurnContext,
)
from src.worker.context import (
    context_blocks,
    conversation_transcript,
    customer_profile_block,
    search_query,
    state_block,
    summary_block,
)


def _msg(direction: Direction, body: str) -> Message:
    author = Author.CONTACT if direction == Direction.INBOUND else Author.BOT
    return Message(direction=direction, author=author, body=body)


def test_transcript_excludes_current_and_labels_roles():
    history = [
        _msg(Direction.INBOUND, "caut o cremă"),
        _msg(Direction.OUTBOUND, "Îți recomand X"),
        _msg(Direction.INBOUND, "mai ieftin"),  # mesajul CURENT (ultimul) → exclus
    ]
    t = conversation_transcript(history)
    assert "Client: caut o cremă" in t
    assert "Asistent: Îți recomand X" in t
    assert "mai ieftin" not in t


def test_transcript_empty_for_first_message():
    assert conversation_transcript([_msg(Direction.INBOUND, "salut")]) == ""
    assert conversation_transcript([]) == ""


def test_transcript_budget_max_turns():
    history = [_msg(Direction.INBOUND, f"m{i}") for i in range(20)]
    t = conversation_transcript(history, max_turns=3)
    assert "m18" in t and "m16" in t  # ultimele 3 din prior (m16,m17,m18)
    assert "m15" not in t


def test_search_query_joins_recent_user_messages():
    history = [
        _msg(Direction.INBOUND, "cremă hidratantă ten uscat"),
        _msg(Direction.OUTBOUND, "uite X"),
        _msg(Direction.INBOUND, "mai ieftin"),
    ]
    q = search_query(history, "mai ieftin", n=2)
    assert "cremă hidratantă ten uscat" in q
    assert "mai ieftin" in q
    assert "uite X" not in q  # doar mesajele CLIENTULUI


def test_search_query_fallback_to_current():
    assert search_query([], "salut") == "salut"


# --- G6-2: hidratare state + blocuri profil/state ---------------------------


def test_state_from_jsonb_hydrates_and_is_defensive():
    raw = {
        "displayed_products": [
            {"product_id": "p1", "name": "Crema", "price": 82.99, "url": "x"},
            {"id": "p2", "name": "Ser", "price": 120.5},  # cheie `id` în loc de product_id
            {"name": "fără id"},  # incomplet → sărit
        ],
        "constraints": {"buget_max": 100},
        "state_version": 3,
    }
    s = ConversationState.from_jsonb(raw)
    assert [p.product_id for p in s.displayed_products] == ["p1", "p2"]
    assert s.displayed_products[0].price == 82.99
    assert s.constraints == {"buget_max": 100}
    assert s.state_version == 3


def test_state_from_jsonb_empty_is_valid():
    s = ConversationState.from_jsonb(None)
    assert s.displayed_products == [] and s.constraints == {} and s.state_version == 0


def test_customer_profile_block_compact_and_skips_empty():
    c = Contact(
        id="c",
        business_id="b",
        profile={"tip_ten": "uscat", "concerns": ["riduri", "pete"], "gol": ""},
        lifecycle="returning",
    )
    block = customer_profile_block(c)
    assert "tip_ten: uscat" in block
    assert "concerns: riduri, pete" in block
    assert "gol" not in block  # valoare goală sărită
    assert "stadiu: returning" in block


def test_customer_profile_block_empty_when_no_profile():
    assert customer_profile_block(Contact(id="c", business_id="b")) == ""


def test_customer_profile_block_respects_budget():
    c = Contact(id="c", business_id="b", profile={"x": "y" * 500})
    assert len(customer_profile_block(c, max_chars=80)) <= 80


def test_state_block_shows_products_and_constraints():
    s = ConversationState(
        displayed_products=[
            ProductRef("p1", "Crema", 82.99),
            ProductRef("p2", "Ser", 120.5),
        ],
        constraints={"buget_max": 100, "gol": ""},
    )
    block = state_block(s)
    assert "Crema (82.99 lei)" in block and "Ser (120.50 lei)" in block
    # R3: id-ul (UUID) e expus → agentul poate chema tools pe produsele afișate fără re-căutare.
    assert "[p1]" in block and "[p2]" in block
    assert "buget_max: 100" in block
    assert "gol" not in block


def test_state_block_empty_state_is_blank():
    assert state_block(ConversationState()) == ""


def test_state_block_exposes_full_uuids_without_truncation():
    # R3: 3 produse cu UUID-uri reale (36 char) + nume lungi (cazul cel mai rău de buget) →
    # fiecare id COMPLET în bloc. O trunchiere care ar tăia un UUID l-ar corupe → DataError la cast.
    ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    s = ConversationState(
        displayed_products=[
            ProductRef(i, "Mira Atelier Balance Crema pentru hidratare 214", 97.99) for i in ids
        ]
    )
    block = state_block(s)
    for i in ids:
        assert f"[{i}]" in block


def _ctx(*, profile=None, products=None) -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b", profile=profile or {}),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
        state=ConversationState(displayed_products=products or []),
    )


def test_context_blocks_joins_nonempty():
    ctx = _ctx(profile={"tip_ten": "uscat"}, products=[ProductRef("p1", "Crema", 82.99)])
    blocks = context_blocks(ctx)
    assert "Profil client:" in blocks and "[p1] Crema (82.99 lei)" in blocks


def test_context_blocks_empty_when_nothing():
    assert context_blocks(_ctx()) == ""


def test_summary_block_empty_when_none():
    assert summary_block(_ctx()) == ""  # ctx.summary None → ""


def test_summary_block_formatted_and_capped():
    ctx = _ctx()
    ctx.summary = "Clientul caută cremă hidratantă pentru ten uscat, buget sub 100 lei. " * 20
    block = summary_block(ctx, max_chars=80)
    assert block.startswith("Rezumat conversație anterioară: ")
    assert len(block) <= 80


def test_context_blocks_summary_first():
    ctx = _ctx(profile={"tip_ten": "uscat"}, products=[ProductRef("p1", "Crema", 82.99)])
    ctx.summary = "Discuție anterioară despre creme."
    blocks = context_blocks(ctx)
    # ordine cronologică: rezumatul (fundal vechi) ÎNAINTEA profilului și state-ului
    assert blocks.index("Rezumat conversație") < blocks.index("Profil client")
    assert blocks.index("Profil client") < blocks.index("Produse arătate recent")


async def test_agent_prompt_includes_context_blocks():
    """Wiring: blocurile de profil/state ajung în mesajul USER al agentului."""
    from src.models import Route, RouteDecision
    from src.worker.runner import PipelineDeps
    from src.worker.stages.agent import agent_stage

    captured: dict[str, str] = {}

    class _CapLLM:
        async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
            captured["user"] = user
            return "Salut! Cu ce te ajut?"

        async def complete(self, *a, **k):
            return ""

        async def embed(self, texts, *, model=None):
            return [[0.0] * 8 for _ in texts]

    ctx = _ctx(profile={"tip_ten": "uscat"}, products=[ProductRef("p1", "Crema", 82.99)])
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.message = InboundMessage(provider_msg_id="m", body="recomandă-mi ceva")
    await agent_stage(ctx, PipelineDeps(conn=object(), redis=None, llm=_CapLLM()))

    assert "Profil client:" in captured["user"]
    assert "[p1] Crema (82.99 lei)" in captured["user"]  # R3: id-ul produsului afișat în context
