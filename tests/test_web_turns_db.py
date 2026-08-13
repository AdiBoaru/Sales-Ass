"""NX-232 — `web_turns` pe Postgres REAL (tenanți throwaway, cleanup complet).

Exclus din CI fast (`-m "not integration"`). Acoperă exact garanțiile care nu se pot dovedi
fără DB: unique/partial unique sub INSERT real, claim/reclaim/fencing ca UPDATE-uri
condiționate concurente, CHECK-ul de terminal non-empty, atomicitatea tranzacției de commit,
RLS-ul pe `bot_runtime`, GDPR, retenția bounded și absența PII pe rânduri.

Cere migrarea 040 aplicată — altfel modulul se SKIP-uie cu mesaj explicit.
"""

import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.provider import static_db
from src.db.queries import web_turns as wt
from src.web import turn_service as ts

pytestmark = [pytest.mark.integration]


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-232 ledger', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx232-{uuid4().hex[:8]}",
    )
    return bid


async def _make_scope(conn, bid: str) -> tuple[str, str, str]:
    """(channel_id, contact_id, conversation_id) throwaway pentru un business."""
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
    conversation_id = await conn.fetchval(
        "insert into conversations (business_id, contact_id, channel_id) "
        "values ($1, $2, $3) returning id",
        bid,
        str(contact_id),
        channel_id,
    )
    return channel_id, str(contact_id), str(conversation_id)


@pytest.fixture(autouse=True)
async def _require_migration():
    """Function-scoped DELIBERAT (nu module): un fixture async module-scoped ar crea poolul
    în ALT event loop decât testele (pytest-asyncio, loop per funcție) → InterfaceError."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        exists = await conn.fetchval("select to_regclass('public.web_turns') is not null")
    if not exists:
        pytest.skip("migrarea 040_web_turns nu e aplicată pe DB (rulează scripts/migrate.py)")


@pytest.fixture
async def shop():
    """Două businessuri throwaway (al doilea = martorul de izolare), cu cleanup complet."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a, b = await _make_business(conn), await _make_business(conn)
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            for bid in (a, b):
                # web_turns/conversations/contacts/channels cad prin ON DELETE CASCADE
                await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _insert(conn, bid, conv, contact, client_id=None, fp="fp-1", **kw):
    return await wt.insert_turn(
        conn,
        bid,
        conv,
        contact,
        client_id or str(uuid4()),
        fp,
        session_ref_hash="h",
        conversation_revision=0,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        **kw,
    )


VIEW = {"content": "Salut! Uite serul potrivit.", "products": [{"name": "Ser X"}]}


# ── Idempotency + single-flight ───────────────────────────────────────────────


async def test_same_client_turn_id_yields_single_row(shop):
    bid, _ = shop
    pool = await get_pool()
    cid = str(uuid4())
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        first = await _insert(conn, bid, conv, contact, client_id=cid)
        second = await _insert(conn, bid, conv, contact, client_id=cid, fp="fp-2")
        row = await wt.get_turn(conn, bid, conv, cid)
    assert first is not None and second is None
    assert row.id == first.id
    assert row.request_fingerprint == "fp-1"  # primul request e adevărul; fp-2 → conflict


async def test_twenty_concurrent_accepts_one_row(shop):
    """Două taburi × retry agresiv: 20 de accepturi simultane pe același ID → UN rând."""
    bid, _ = shop
    pool = await get_pool()
    cid = str(uuid4())
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)

    async def one_accept():
        async with admin_conn(pool) as conn:
            return await _insert(conn, bid, conv, contact, client_id=cid)

    results = await asyncio.gather(*[one_accept() for _ in range(20)])
    winners = [r for r in results if r is not None]
    async with admin_conn(pool) as conn:
        count = await conn.fetchval(
            "select count(*) from web_turns where business_id = $1 and conversation_id = $2",
            bid,
            conv,
        )
    assert len(winners) == 1 and count == 1


async def test_single_flight_partial_unique_per_conversation(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        first = await _insert(conn, bid, conv, contact)
        blocked = await _insert(conn, bid, conv, contact)  # ALT client_turn_id, aceeași conv
        assert first is not None and blocked is None
        # terminăm primul turn → al doilea accept trece
        claim = await wt.claim_turn(conn, bid, first.id, owner="w1", lease_ttl_s=60)
        assert await wt.complete_turn(
            conn, bid, first.id, lease_epoch=claim.lease_epoch, response_json=VIEW
        )
        after = await _insert(conn, bid, conv, contact)
        assert after is not None


# ── Claim / renew / reclaim / fencing ─────────────────────────────────────────


async def test_lease_lifecycle_and_epoch_fencing(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row = await _insert(conn, bid, conv, contact)

        claim1 = await wt.claim_turn(conn, bid, row.id, owner="worker-A", lease_ttl_s=300)
        assert claim1 is not None and not claim1.reclaimed and claim1.attempt == 1
        # lease viu → al doilea claim e refuzat (single-flight)
        assert await wt.claim_turn(conn, bid, row.id, owner="worker-B", lease_ttl_s=300) is None
        # renew: doar owner-ul + epoch-ul curent
        assert await wt.renew_lease(
            conn, bid, row.id, owner="worker-A", lease_epoch=claim1.lease_epoch, lease_ttl_s=300
        )
        assert not await wt.renew_lease(
            conn, bid, row.id, owner="worker-A", lease_epoch=99, lease_ttl_s=300
        )

        # workerul moare cu lease-ul în mână → după expiry, B reclamă cu epoch NOU
        await conn.execute(
            "update web_turns set lease_expires_at = now() - interval '1 second' "
            "where business_id = $1 and id = $2",
            bid,
            row.id,
        )
        claim2 = await wt.claim_turn(conn, bid, row.id, owner="worker-B", lease_ttl_s=300)
        assert claim2 is not None and claim2.reclaimed
        assert claim2.lease_epoch == claim1.lease_epoch + 1 and claim2.attempt == 2

        # workerul VECHI se trezește și încearcă să finalizeze → update 0 rows, respins
        assert not await wt.complete_turn(
            conn, bid, row.id, lease_epoch=claim1.lease_epoch, response_json=VIEW
        )
        fresh = await wt.get_turn_by_id(conn, bid, row.id)
        assert fresh.status == "running" and fresh.response_json is None

        # owner-ul CURENT finalizează; replay-ul e canonic byte-echivalent
        assert await wt.complete_turn(
            conn, bid, row.id, lease_epoch=claim2.lease_epoch, response_json=VIEW
        )
        done = await wt.get_turn_by_id(conn, bid, row.id)
        assert done.status == "completed" and done.completed_at is not None
        assert json.dumps(done.response_json, sort_keys=True) == json.dumps(VIEW, sort_keys=True)

        # terminal = final: nici claim, nici o a doua finalizare
        assert await wt.claim_turn(conn, bid, row.id, owner="worker-C", lease_ttl_s=300) is None
        assert not await wt.complete_turn(
            conn, bid, row.id, lease_epoch=claim2.lease_epoch, response_json={"content": "alt"}
        )


async def test_terminal_without_response_is_rejected_by_db(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row = await _insert(conn, bid, conv, contact)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "update web_turns set status = 'completed', completed_at = now() "
                "where business_id = $1 and id = $2",
                bid,
                row.id,
            )


async def test_commit_transaction_is_atomic(shop):
    """Rezultat + mesaj în ACEEAȘI tranzacție: dacă orice ridică după complete_turn,
    NIMIC nu rămâne scris (crash point după complete, înainte de COMMIT)."""
    bid, _ = shop
    pool = await get_pool()

    class _Boom(Exception):
        pass

    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        row = await _insert(conn, bid, conv, contact)
        claim = await wt.claim_turn(conn, bid, row.id, owner="w1", lease_ttl_s=300)
        with pytest.raises(_Boom):
            async with conn.transaction():
                ok = await wt.complete_turn(
                    conn, bid, row.id, lease_epoch=claim.lease_epoch, response_json=VIEW
                )
                assert ok
                raise _Boom
        fresh = await wt.get_turn_by_id(conn, bid, row.id)
        assert fresh.status == "running" and fresh.response_json is None  # rollback complet


async def test_retention_is_noop_when_table_is_missing(shop, monkeypatch):
    """Jobul e înregistrat în scheduler NEcondiționat de flag → poate rula pe un deployment
    fără migrarea 040. Acolo trebuie să fie no-op TĂCUT (0), nu o eroare la fiecare interval."""
    bid, _ = shop
    pool = await get_pool()

    class _NoTable:
        """Conn care raportează tabelul absent; orice DELETE ar fi o eroare de test."""

        async def fetchval(self, sql, *a):
            return False

        async def execute(self, sql, *a):
            raise AssertionError("cleanup nu are voie să atingă tabelul dacă lipsește")

    assert await wt.cleanup_web_turns(_NoTable()) == 0
    # iar pe DB-ul REAL (tabel prezent) rămâne funcțional, nu blocat de guard
    async with admin_conn(pool) as conn:
        assert await wt.cleanup_web_turns(conn, batch_size=1) >= 0
        assert bid  # tenantul throwaway există; guard-ul nu depinde de el


# ── Izolare tenant + RLS ──────────────────────────────────────────────────────


async def test_cross_tenant_read_returns_nothing(shop):
    bid_a, bid_b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid_a)
        row = await _insert(conn, bid_a, conv, contact)
        # alt tenant cere ID-ul → None, indistinct de „nu există" (fără existence leak)
        assert await wt.get_turn_by_id(conn, bid_b, row.id) is None
        assert await wt.get_turn(conn, bid_b, conv, row.client_turn_id) is None


async def test_rls_bot_runtime_sees_only_its_tenant(shop):
    bid_a, bid_b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact_a, conv_a = await _make_scope(conn, bid_a)
        # conv_b rămâne FĂRĂ turn: încercarea de insert de mai jos trebuie să lovească RLS-ul,
        # nu unique-ul de single-flight (care ar fi mascat eroarea de privilegiu).
        _, contact_b, conv_b = await _make_scope(conn, bid_b)
        await _insert(conn, bid_a, conv_a, contact_a)
        try:
            await conn.execute("set role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, false)", bid_a)
            visible = await conn.fetch("select business_id from web_turns")
            assert {str(r["business_id"]) for r in visible} == {bid_a}
            # WITH CHECK: bot_runtime nu poate scrie în alt tenant nici cu business_id explicit
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await _insert(conn, bid_b, conv_b, contact_b, client_id=str(uuid4()))
            await conn.execute("select set_config('app.business_id', '', false)")
            assert await conn.fetchval("select count(*) from web_turns") == 0
        finally:
            await conn.execute("reset role")


# ── GDPR + retenție + PII ─────────────────────────────────────────────────────


async def test_gdpr_deletes_only_that_contacts_turns(shop):
    bid_a, bid_b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact_a, conv_a = await _make_scope(conn, bid_a)
        _, contact_b, conv_b = await _make_scope(conn, bid_b)
        await _insert(conn, bid_a, conv_a, contact_a)
        await _insert(conn, bid_b, conv_b, contact_b)
        deleted = await wt.delete_turns_for_contact(conn, bid_a, contact_a)
        assert deleted == 1
        remaining = await conn.fetchval(
            "select count(*) from web_turns where business_id = $1", bid_b
        )
        assert remaining == 1  # martorul de izolare rămâne neatins


async def test_retention_is_bounded_and_keeps_fresh_rows(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        _, contact2, conv2 = await _make_scope(conn, bid)
        old_done = await _insert(conn, bid, conv, contact)
        c = await wt.claim_turn(conn, bid, old_done.id, owner="w", lease_ttl_s=1)
        await wt.complete_turn(
            conn, bid, old_done.id, lease_epoch=c.lease_epoch, response_json=VIEW
        )
        fresh = await _insert(conn, bid, conv, contact)  # activ, proaspăt → NU se șterge
        old_stale = await _insert(conn, bid, conv2, contact2)  # accept fără follow-through
        await conn.execute(
            "update web_turns set completed_at = now() - interval '30 days' "
            "where business_id = $1 and id = $2",
            bid,
            old_done.id,
        )
        await conn.execute(
            "update web_turns set accepted_at = now() - interval '30 days' "
            "where business_id = $1 and id = $2",
            bid,
            old_stale.id,
        )
        # bounded: batch_size=1 → o rulare șterge exact UN rând, jobul repetă până la 0
        assert (
            await wt.cleanup_web_turns(
                conn, terminal_older_than_hours=168, stale_older_than_days=7, batch_size=1
            )
            == 1
        )
        assert (
            await wt.cleanup_web_turns(
                conn, terminal_older_than_hours=168, stale_older_than_days=7, batch_size=100
            )
            == 1
        )
        left = await conn.fetch("select id from web_turns where business_id = $1", bid)
        assert {str(r["id"]) for r in left} == {fresh.id}


async def test_no_raw_body_token_or_visitor_on_any_column(shop):
    """PII scan: acceptul REAL (serviciul, nu doar SQL-ul) nu lasă body/visitor/token în clar
    pe niciun rând al ledgerului."""
    bid, _ = shop
    pool = await get_pool()
    secret_text = "telefonul meu e 0722123456 și emailul adi@example.com"
    visitor = f"web_{uuid4().hex}"
    token = f"tok-{uuid4().hex[:10]}"
    async with admin_conn(pool) as conn:
        channel_id = str(uuid4())
        await conn.execute(
            "insert into channels (id, business_id, kind, provider_account_id) "
            "values ($1, $2, 'webchat', $3)",
            channel_id,
            bid,
            token,
        )
        fp = ts.request_fingerprint("sek", business_id=bid, channel_token=token, text=secret_text)
        outcome = await ts.accept_web_turn(
            static_db(conn),
            business_id=bid,
            channel_id=channel_id,
            channel_kind="webchat",
            channel_token=token,
            sender_external_id=visitor,
            client_turn_id=str(uuid4()),
            fingerprint=fp,
        )
        assert isinstance(outcome, ts.Accepted)
        rec = await conn.fetchrow(
            "select * from web_turns where business_id = $1 and id = $2", bid, outcome.row.id
        )
    flat = " ".join(str(v) for v in dict(rec).values())
    for needle in (secret_text, "0722123456", "adi@example.com", visitor, token):
        assert needle not in flat
