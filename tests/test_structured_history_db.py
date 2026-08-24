"""NX-255 — seam-ul REAL: `messages.payload.shown` scris de Sender și citit înapoi de istoric.

Testele unit acoperă scrierea și citirea SEPARAT (dict în memorie). Între ele stă însă Postgres:
`insert_message` face `json.dumps`, coloana e `jsonb`, iar asyncpg întoarce jsonb ca `str` fiindcă
poolul n-are codec de tip. Un dus-întors care nu trece prin DB nu dovedește nimic despre exact
partea care poate să pice, iar `shown` e inutil dacă nu se întoarce.
"""

from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.messages import get_recent_messages
from src.models import Author, Direction
from src.worker.context import _shown_refs

REFS = [
    {
        "product_id": "a3f2c1d4-0000-4000-8000-000000000001",
        "name": "Ser hidratant X",
        "price": 89.0,
    },
    {
        "product_id": "b71e0055-0000-4000-8000-000000000002",
        "name": "Cremă de noapte Y",
        "price": 129.5,
    },
]


@pytest.fixture
async def scope():
    """Tenant throwaway + conversație, șterse la final (ca restul testelor de DB din repo)."""
    pool = await get_pool()
    bid = str(uuid4())
    async with admin_conn(pool) as conn:
        await conn.execute(
            "insert into businesses (id, slug, name, vertical, status, default_locale) "
            "values ($1, $2, 'NX-255 istoric', 'beauty_salon', 'active', 'ro')",
            bid,
            f"nx255-{uuid4().hex[:8]}",
        )
        channel_id = str(uuid4())
        await conn.execute(
            "insert into channels (id, business_id, kind, provider_account_id) "
            "values ($1, $2, 'webchat', $3)",
            channel_id,
            bid,
            f"tok-{uuid4().hex[:10]}",
        )
        contact_id = await conn.fetchval(
            "insert into contacts (business_id) values ($1) returning id", bid
        )
        conv_id = await conn.fetchval(
            "insert into conversations (business_id, contact_id, channel_id) "
            "values ($1, $2, $3) returning id",
            bid,
            str(contact_id),
            channel_id,
        )
    try:
        yield bid, str(contact_id), str(conv_id)
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _insert(conn, bid, conv, contact, direction, author, body, payload):
    from src.db.queries.messages import insert_message

    return await insert_message(
        conn, bid, conv, contact, direction, author, body=body, payload=payload
    )


async def test_shown_survives_the_round_trip_through_postgres(scope):
    """Scris ca `jsonb`, citit înapoi ca `str`, decodat, și recunoscut de `_shown_refs`."""
    bid, contact, conv = scope
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _insert(
            conn,
            bid,
            conv,
            contact,
            Direction.INBOUND,
            Author.CONTACT,
            "ce ai pentru ten uscat",
            None,
        )
        await _insert(
            conn,
            bid,
            conv,
            contact,
            Direction.OUTBOUND,
            Author.BOT,
            "Uite doua variante.",
            {"turn_id": "t1", "fragment_index": 0, "shown": REFS},
        )
        history = await get_recent_messages(conn, bid, conv)

    bot = next(m for m in history if m.direction == Direction.OUTBOUND)
    assert isinstance(bot.payload, dict), "jsonb a venit ca str și n-a fost decodat"
    assert bot.payload["turn_id"] == "t1"  # cheile vechi rămân intacte
    refs = _shown_refs(bot, 6)
    assert refs == REFS  # inclusiv prețul, ca număr, nu ca string


async def test_full_chain_from_sender_to_next_turns_prompt(scope, monkeypatch):
    """Lanțul ÎNTREG, fără LLM: Sender construiește fragmentul → Postgres → istoricul turului
    următor → transcriptul care pleacă în prompt.

    Testele unit verifică fiecare verigă, dar o verigă corectă nu dovedește un lanț: forma scrisă
    de `_build_fragment` și forma așteptată de `_shown_refs` sunt două decizii luate în fișiere
    diferite, iar între ele stă o serializare. Dacă se despart, `shown` devine tăcut inert și
    totul pare să meargă."""
    from src.config import get_settings
    from src.models import BusinessConfig, Contact, InboundMessage, Reply, RichItem, TurnContext
    from src.worker.compose import card_products
    from src.worker.context import conversation_transcript
    from src.worker.processor import _build_fragment

    monkeypatch.setenv("STRUCTURED_HISTORY_ENABLED", "true")
    get_settings.cache_clear()
    try:
        bid, contact, conv = scope
        ctx = TurnContext(
            turn_id="t1",
            business=BusinessConfig(id=bid, slug="d", name="D"),
            contact=Contact(id=contact, business_id=bid),
            message=InboundMessage(provider_msg_id="m", body="ce ai pentru ten uscat"),
            conversation_id=conv,
        )
        # Exact forma pe care o produce calea RICH (cea care servește majoritatea recomandărilor),
        # nu un dict scris de mână: `card_products` e chemat pe RichItem-uri reale, deci dacă
        # redenumește o cheie (`product_id` → `id`), lanțul se rupe AICI, nu tăcut în producție.
        cards = card_products(
            [
                RichItem(product_id=r["product_id"], name=r["name"], price=r["price"], url="/p/x")
                for r in REFS
            ]
        )
        assert {"product_id", "name", "price"} <= set(cards[0]), (
            "contractul card_products s-a mutat"
        )
        ctx.reply = Reply(text="Uite doua variante.", products=cards)
        frag = _build_fragment(
            ctx,
            "Uite doua variante.",
            index=0,
            turn_id="t1",
            to="v",
            deliver=False,
            is_rich=True,
            has_products=True,
        )

        pool = await get_pool()
        async with admin_conn(pool) as conn:
            await _insert(
                conn,
                bid,
                conv,
                contact,
                Direction.INBOUND,
                Author.CONTACT,
                "ce ai pentru ten uscat",
                {"turn_id": "t1", "fragment_index": 0},
            )
            await _insert(
                conn,
                bid,
                conv,
                contact,
                Direction.OUTBOUND,
                Author.BOT,
                "Uite doua variante.",
                frag.message_payload,
            )
            await _insert(
                conn,
                bid,
                conv,
                contact,
                Direction.INBOUND,
                Author.CONTACT,
                "al doilea e bun si pentru iarna?",
                {"turn_id": "t2", "fragment_index": 0},
            )
            history = await get_recent_messages(conn, bid, conv)

        transcript = conversation_transcript(history)
        # Turul următor vede ce s-a arătat, cu id-uri pe care tool-urile le pot rezolva.
        assert "Ser hidratant X" in transcript
        assert REFS[1]["product_id"] in transcript
        assert "Client: ce ai pentru ten uscat" in transcript  # întrebarea, verbatim
        assert "pentru iarna" not in transcript  # mesajul CURENT rămâne exclus
    finally:
        get_settings.cache_clear()


async def test_history_without_payload_still_loads(scope):
    """Rândurile scrise înainte de NX-255 (payload `{}` prin coalesce) nu rup citirea."""
    bid, contact, conv = scope
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await _insert(conn, bid, conv, contact, Direction.INBOUND, Author.CONTACT, "salut", None)
        history = await get_recent_messages(conn, bid, conv)

    assert history[0].payload == {}  # `coalesce($9, '{}')` din insert, decodat corect
    assert _shown_refs(history[0], 6) == []
