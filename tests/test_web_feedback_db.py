"""NX-246 (felia 2) — feedback pe Postgres REAL: unique, concurență, revizii, RLS.

Exclus din CI fast (`-m "not integration"`). Există pentru garanțiile care NU se pot dovedi cu un
provider fals, fiindcă sunt proprietăți ale SCHEMEI, nu ale codului:

  • un singur rând per prompt, chiar sub 20 de cereri CONCURENTE (unique + ON CONFLICT);
  • retry identic nu incrementează `revision` (clauza `where` din upsert, nu un `if` în Python);
  • plafonul de revizii ține și când cursa e reală;
  • `business_id = $1` + RLS: voturile altui tenant nu intră în agregat.

Cere migrarea 042 aplicată — altfel modulul se SKIP-uie cu mesaj explicit.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries.feedback import get_feedback, tally_feedback, upsert_feedback
from src.observability.feedback_stats import Tally, build_report
from src.web.action_models import FEEDBACK_TAXONOMY_VERSION

pytestmark = [pytest.mark.integration]

MAX_REVISIONS = 5


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-246 feedback', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx246-{uuid4().hex[:8]}",
    )
    return bid


async def _make_conversation(conn, bid: str) -> str:
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
    return str(
        await conn.fetchval(
            "insert into conversations (business_id, contact_id, channel_id) "
            "values ($1, $2, $3) returning id",
            bid,
            str(contact_id),
            channel_id,
        )
    )


@pytest.fixture(autouse=True)
async def _require_migration():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        exists = await conn.fetchval("select to_regclass('public.web_feedback') is not null")
    if not exists:
        # Închidem poolul ÎNAINTE de skip, și nu e o precauție teoretică: pytest-asyncio creează o
        # buclă per funcție, iar `get_pool()` memorează poolul legat de bucla curentă. Un skip fără
        # close lasă în cache un pool legat de o buclă MOARTĂ, pe care îl primește modulul următor
        # — care crapă cu `AttributeError` în fixture, nu în test, deci cu un mesaj care nu spune
        # nimic despre cauză. (Aici e vizibil fiindcă migrarea 042 e încă neaplicată: TOATE testele
        # din modul sar, deci `shop` — care ar fi închis poolul — nu rulează niciodată.)
        await close_pool()
        pytest.skip("migrarea 042_web_feedback nu e aplicată (rulează scripts/migrate.py)")


@pytest.fixture
async def shop():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a, b = await _make_business(conn), await _make_business(conn)
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            for bid in (a, b):
                await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _vote(conn, bid, conv, *, prompt, rating="positive", reason=None, action_id="a1"):
    return await upsert_feedback(
        conn,
        bid,
        conversation_id=conv,
        turn_id=str(uuid4()),
        feedback_prompt_id=prompt,
        rating=rating,
        reason_code=reason,
        taxonomy_version=FEEDBACK_TAXONOMY_VERSION,
        schema_version="web-feedback.v1",
        release_sha="deadbeef",
        release_track="candidate",
        pipeline_version="web-chat.v1",
        action_id=action_id,
        max_revisions=MAX_REVISIONS,
    )


# ── Unicitate + revizii ─────────────────────────────────────────────────────────────────────


async def test_un_singur_rand_per_prompt(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)
        await _vote(conn, bid, conv, prompt="p1", action_id="a1")
        await _vote(conn, bid, conv, prompt="p1", rating="negative", action_id="a2")
        n = await conn.fetchval(
            "select count(*) from web_feedback "
            "where business_id = $1 and feedback_prompt_id = 'p1'",
            bid,
        )
    assert n == 1, "o corecție a creat un al doilea rând (agregatele ar dubla votul)"


async def test_retry_identic_nu_incrementeaza_revizia(shop):
    """Clauza `where` din upsert, nu un `if` în Python: două cereri identice nu se pot suprapune."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)
        first = await _vote(conn, bid, conv, prompt="p1", action_id="a1")
        again = await _vote(conn, bid, conv, prompt="p1", action_id="a1")
        row = await get_feedback(conn, bid, "p1")
    assert first.revision == 1
    assert again is None, "retry-ul identic a scris (ar fi mutat `updated_at`)"
    assert row.revision == 1


async def test_corectia_creste_revizia(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)
        await _vote(conn, bid, conv, prompt="p1", action_id="a1")
        second = await _vote(
            conn, bid, conv, prompt="p1", rating="negative", reason="too_long", action_id="a2"
        )
    assert second.revision == 2
    assert second.rating == "negative" and second.reason_code == "too_long"


async def test_plafonul_de_revizii_opreste_scrierea(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)
        for i in range(10):
            await _vote(
                conn,
                bid,
                conv,
                prompt="p1",
                rating="negative" if i % 2 else "positive",
                action_id=f"a{i}",
            )
        row = await get_feedback(conn, bid, "p1")
    assert row.revision == MAX_REVISIONS


async def test_douazeci_de_cereri_concurente_produc_un_singur_rand(shop):
    """Failure matrix: „20 submissions feedback concurente ⇒ un singur rând/revision canonic"."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)

    async def _one(i: int):
        async with admin_conn(pool) as c:
            # ACELAȘI action_id: e retry-ul aceluiași buton, trimis de 20 de ori (retry de rețea,
            # double-click, două taburi). Rezultatul canonic e „un rând, revizia 1".
            return await _vote(c, bid, conv, prompt="p-conc", action_id="same")

    await asyncio.gather(*[_one(i) for i in range(20)], return_exceptions=True)
    async with admin_conn(pool) as conn:
        n = await conn.fetchval(
            "select count(*) from web_feedback where business_id = $1 and feedback_prompt_id = $2",
            bid,
            "p-conc",
        )
        row = await get_feedback(conn, bid, "p-conc")
    assert n == 1
    assert row.revision == 1, f"revizia a crescut la {row.revision} dintr-un retry identic"


async def test_voturi_opuse_concurente_lasa_o_singura_revizie_activa(shop):
    """Două voturi OPUSE simultan: o singură stare activă, nu două rânduri."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)

    async def _one(i: int):
        async with admin_conn(pool) as c:
            return await _vote(
                c,
                bid,
                conv,
                prompt="p-race",
                rating="positive" if i % 2 else "negative",
                action_id=f"a{i}",
            )

    await asyncio.gather(*[_one(i) for i in range(2)], return_exceptions=True)
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(
            "select rating, revision from web_feedback "
            "where business_id = $1 and feedback_prompt_id = $2",
            bid,
            "p-race",
        )
    assert len(rows) == 1
    assert rows[0]["rating"] in ("positive", "negative")


# ── Izolare de tenant ───────────────────────────────────────────────────────────────────────


async def test_agregatul_nu_vede_alt_tenant(shop):
    a, b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv_a = await _make_conversation(conn, a)
        conv_b = await _make_conversation(conn, b)
        for i in range(3):
            await _vote(conn, a, conv_a, prompt=f"a{i}", action_id=f"x{i}")
        for i in range(7):
            await _vote(conn, b, conv_b, prompt=f"b{i}", rating="negative", action_id=f"y{i}")
        now = datetime.now(UTC)
        window = (now - timedelta(hours=1), now + timedelta(minutes=5))
        tally_a = await tally_feedback(conn, a, window_from=window[0], window_to=window[1])
        tally_b = await tally_feedback(conn, b, window_from=window[0], window_to=window[1])

    assert sum(t.n for t in tally_a) == 3
    assert sum(t.n for t in tally_b) == 7
    assert all(t.rating == "positive" for t in tally_a)


async def test_acelasi_prompt_id_pe_doi_tenanti_e_legal(shop):
    """Unicitatea e per TENANT: doi clienți pot avea prompturi identice fără să se ciocnească."""
    a, b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv_a, conv_b = await _make_conversation(conn, a), await _make_conversation(conn, b)
        row_a = await _vote(conn, a, conv_a, prompt="same-prompt", action_id="a")
        row_b = await _vote(conn, b, conv_b, prompt="same-prompt", action_id="b")
    assert row_a is not None and row_b is not None
    assert row_a.id != row_b.id


# ── Raportul, pe date reale ─────────────────────────────────────────────────────────────────


async def test_raportul_sub_prag_nu_emite_procent(shop):
    """Cardul: „la volum insuficient raportează `insufficient_sample`" — nu 0%, nu 100%."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        conv = await _make_conversation(conn, bid)
        for i in range(4):
            await _vote(conn, bid, conv, prompt=f"p{i}", action_id=f"a{i}")
        now = datetime.now(UTC)
        rows = await tally_feedback(
            conn, bid, window_from=now - timedelta(hours=1), window_to=now + timedelta(minutes=5)
        )
    report = build_report(
        [Tally(r.rating, r.reason_code, r.release_track, r.n) for r in rows],
        window_from=datetime.now(UTC) - timedelta(hours=1),
        window_to=datetime.now(UTC),
        business_id=bid,
        taxonomy_version=FEEDBACK_TAXONOMY_VERSION,
    )
    payload = report.as_dict()
    assert payload["verdict"] == "insufficient_sample"
    assert payload["positive_feedback_rate"] is None, "un procent din 4 voturi"
    assert "csat" not in str(payload).lower(), "raportul nu are voie să numească asta CSAT"


async def test_randurile_nu_contin_text_liber(shop):
    """P12 pe schemă: coloanele de text liber pur și simplu NU există."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "select column_name from information_schema.columns "
                "where table_name = 'web_feedback'"
            )
        }
    interzise = {"comment", "body", "text", "ip", "user_agent", "token", "visitor_id", "contact_id"}
    assert not (cols & interzise), f"coloană interzisă în web_feedback: {cols & interzise}"
