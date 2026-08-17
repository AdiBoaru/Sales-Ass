"""NX-233 — executorul async pe Postgres REAL (tenanți throwaway, cleanup complet).

Exclus din CI fast (`-m "not integration"`). Cere migrarea 040 aplicată (skip explicit altfel).
Acoperă exact ce nu se poate dovedi fără DB:
  • acceptul async persistă ATOMIC rândul de ledger + inputul SAFE (recovery integral din DB);
  • `claimable_turns` vede accepted + lease expirat, NU lease viu / terminale (fair, bounded);
  • doi revendicatori CONCURENȚI pe același turn → un singur câștigător (CAS real);
  • sweeperul terminalizează pe DB real: accepted overdue → cancelled RANDABIL,
    running expirat overdue → claim(epoch+1) + failed; zombie-ul scrie 0 rânduri;
  • autorizarea de sesiune (hash): alt vizitator → None, fără existence leak;
  • PII: niciun body brut / visitor / token pe rândul de ledger (inputul safe stă în messages).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.provider import static_db
from src.db.queries import web_turns as wt
from src.web import turn_recovery as tr
from src.web import turn_service as ts

pytestmark = [pytest.mark.integration]


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-233 executor', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx233-{uuid4().hex[:8]}",
    )
    return bid


async def _make_channel(conn, bid: str) -> tuple[str, str]:
    channel_id = str(uuid4())
    token = f"tok-{uuid4().hex[:10]}"
    await conn.execute(
        "insert into channels (id, business_id, kind, provider_account_id) "
        "values ($1, $2, 'webchat', $3)",
        channel_id,
        bid,
        token,
    )
    return channel_id, token


@pytest.fixture(autouse=True)
async def _require_migration():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        exists = await conn.fetchval("select to_regclass('public.web_turns') is not null")
    if not exists:
        pytest.skip("migrarea 040_web_turns nu e aplicată pe DB (rulează scripts/migrate.py)")


@pytest.fixture
async def shop():
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        bid = await _make_business(conn)
    try:
        yield bid
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _accept_async(
    conn,
    bid,
    channel_id,
    token,
    *,
    visitor=None,
    text="vreau un ser bun",
    page_context=None,
    action_payload=None,
    content_type="text",
):
    """Acceptul de pe calea v2 (persist_inbound + deadline + session_ref), pe conn real."""
    visitor = visitor or f"web_{uuid4().hex[:10]}"
    fp = ts.request_fingerprint("sek", business_id=bid, channel_token=token, text=text)
    outcome = await ts.accept_web_turn(
        static_db(conn),
        business_id=bid,
        channel_id=channel_id,
        channel_kind="webchat",
        channel_token=token,
        sender_external_id=visitor,
        client_turn_id=str(uuid4()),
        fingerprint=fp,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        session_ref=ts.session_ref_hash(token, visitor),
        persist_inbound=True,
        safe_body=text,
        content_type=content_type,
        page_context=page_context,
        action_payload=action_payload,
    )
    return outcome, visitor


async def test_async_accept_persists_ledger_and_safe_input_atomically(shop):
    bid = shop
    pool = await get_pool()
    secret_text = "vreau un ser, telefonul meu e 0722123456"
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        outcome, visitor = await _accept_async(conn, bid, channel_id, token, text=secret_text)
        assert isinstance(outcome, ts.Accepted) and outcome.inbound_msg_id is not None
        row = outcome.row
        assert row.deadline_at is not None
        # Recovery-ul integral din DB: refs de execuție complete, fără Redis.
        refs = await wt.load_execution_refs(conn, bid, row.id)
        assert refs is not None
        assert refs.channel_id == channel_id
        assert refs.channel_account_id == token
        assert refs.sender_external_id == visitor
        assert refs.inbound_msg_id == outcome.inbound_msg_id
        assert refs.safe_body == secret_text  # forma SAFE persistată la accept (aici = input)
        # PII pe LEDGER: rândul nu conține body/visitor/token în clar (inputul stă în messages).
        rec = await conn.fetchrow(
            "select * from web_turns where business_id = $1 and id = $2", bid, row.id
        )
        flat = " ".join(str(v) for v in dict(rec).values())
        for needle in (secret_text, "0722123456", visitor, token):
            assert needle not in flat
        # izolare: alt tenant nu vede refs
        assert await wt.load_execution_refs(conn, str(uuid4()), row.id) is None


async def test_execution_refs_return_page_context_and_action(shop):
    """REGRESIE (defect găsit de gate-ul E2E NX-247): `load_execution_refs` citea `payload` din
    Record, dar proiecția EXTERIOARĂ a query-ului nu-l selecta — coloana exista doar în subqueryul
    lateral. Rezultat: `page_context` și `action` ieșeau MEREU None, deci ancora de pagină (NX-234)
    nu ajungea niciodată la execuție, iar un tur de acțiune reluat își pierdea comanda (NX-236).

    Persistarea era corectă tot timpul — de asta niciun test de accept nu a prins-o. Testul de mai
    sus (`..._persists_ledger_and_safe_input_atomically`) asertează `safe_body`, dar nu cele două
    câmpuri care veneau din `payload`. Aici se închide exact acea gaură.
    """
    bid = shop
    pool = await get_pool()
    page_context = {"v": 1, "surface": "product", "product": {"id": "ext-42", "kind": "external"}}
    action = {"kind": "cart_add", "args": {"product_ref": "ext-42"}}
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        outcome, _visitor = await _accept_async(
            conn,
            bid,
            channel_id,
            token,
            page_context=page_context,
            action_payload=action,
            content_type="action",
            text="",
        )
        assert isinstance(outcome, ts.Accepted)
        refs = await wt.load_execution_refs(conn, bid, outcome.row.id)
        assert refs is not None
        assert refs.page_context == page_context, "contextul de pagină nu ajunge la execuție"
        assert refs.action == action, "comanda de acțiune nu se rehidratează"
        assert refs.content_type == "action"


async def test_claimable_scan_sees_only_claimable_ordered(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        # 1) accepted (cel mai vechi) — revendicabil
        first, _ = await _accept_async(conn, bid, channel_id, token)
        await asyncio.sleep(0.01)
        # 2) running cu lease VIU — NU revendicabil
        live, _ = await _accept_async(conn, bid, channel_id, token)
        await wt.claim_turn(conn, bid, live.row.id, owner="wA", lease_ttl_s=300)
        # 3) running cu lease EXPIRAT — revendicabil
        expired, _ = await _accept_async(conn, bid, channel_id, token)
        claim = await wt.claim_turn(conn, bid, expired.row.id, owner="wB", lease_ttl_s=300)
        await conn.execute(
            "update web_turns set lease_expires_at = now() - interval '1 second' "
            "where business_id = $1 and id = $2",
            bid,
            expired.row.id,
        )
        # 4) terminal — NU revendicabil
        done, _ = await _accept_async(conn, bid, channel_id, token)
        c = await wt.claim_turn(conn, bid, done.row.id, owner="wC", lease_ttl_s=300)
        await wt.complete_turn(
            conn, bid, done.row.id, lease_epoch=c.lease_epoch, response_json={"content": "ok"}
        )
        rows = await wt.claimable_turns(conn, limit=50)
        ours = [r for r in rows if r.business_id == bid]
        ids = [r.turn_id for r in ours]
        assert first.row.id in ids and expired.row.id in ids
        assert live.row.id not in ids and done.row.id not in ids
        assert ids.index(first.row.id) < ids.index(expired.row.id)  # fair: cel mai vechi primul
        assert claim is not None


async def test_two_concurrent_claimers_one_winner(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        outcome, _ = await _accept_async(conn, bid, channel_id, token)
    turn_id = outcome.row.id

    async def one_claim(owner):
        async with admin_conn(pool) as conn:
            return await wt.claim_turn(conn, bid, turn_id, owner=owner, lease_ttl_s=300)

    results = await asyncio.gather(one_claim("wA"), one_claim("wB"))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and winners[0].lease_epoch == 1


async def test_sweeper_terminalizes_on_real_db(shop, monkeypatch):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        # accepted overdue → cancelled; running expirat overdue → failed cu epoch nou
        overdue_accepted, _ = await _accept_async(conn, bid, channel_id, token)
        overdue_running, _ = await _accept_async(conn, bid, channel_id, token)
        old_claim = await wt.claim_turn(
            conn, bid, overdue_running.row.id, owner="zombie", lease_ttl_s=300
        )
        await conn.execute(
            "update web_turns set deadline_at = now() - interval '5 seconds' "
            "where business_id = $1 and id in ($2, $3)",
            bid,
            overdue_accepted.row.id,
            overdue_running.row.id,
        )
        await conn.execute(
            "update web_turns set lease_expires_at = now() - interval '1 second' "
            "where business_id = $1 and id = $2",
            bid,
            overdue_running.row.id,
        )

    # Sweeperul scrie tenant-scoped; în test rulăm scrierile pe admin pool (fără bot pool).
    def fake_tenant_db(business_id):
        def provider(op=None):
            return admin_conn(pool)

        return provider

    monkeypatch.setattr(tr, "tenant_db", fake_tenant_db)

    class _NoRedis:
        async def lpush(self, *a):
            return 1

        def pipeline(self):
            raise RuntimeError("wake pierdut — sweeperul nu depinde de Redis")

    report = await tr.sweep_once(_NoRedis(), limit=200)
    assert report.cancelled >= 1 and report.failed >= 1

    async with admin_conn(pool) as conn:
        cancelled = await wt.get_turn_by_id(conn, bid, overdue_accepted.row.id)
        failed = await wt.get_turn_by_id(conn, bid, overdue_running.row.id)
        # P6 pe DB real: ambele terminale au payload RANDABIL, cu cod stabil.
        assert cancelled.status == "cancelled"
        assert ts.renderable(cancelled.response_json)
        assert cancelled.safe_error_code == "deadline_exceeded"
        assert failed.status == "failed"
        assert ts.renderable(failed.response_json)
        assert failed.lease_epoch == old_claim.lease_epoch + 1  # zombie-ul a fost deposedat
        # zombie-ul se trezește și încearcă să scrie cu epoch-ul VECHI → 0 rânduri
        assert not await wt.complete_turn(
            conn,
            bid,
            overdue_running.row.id,
            lease_epoch=old_claim.lease_epoch,
            response_json={"content": "rezultat zombie"},
        )
        after = await wt.get_turn_by_id(conn, bid, overdue_running.row.id)
        assert after.status == "failed" and "zombie" not in str(after.response_json)


async def test_session_authorization_on_real_rows(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        outcome, visitor = await _accept_async(conn, bid, channel_id, token)
        db = static_db(conn)
        mine = await ts.get_turn_for_session(
            db, business_id=bid, turn_id=outcome.row.id, channel_token=token, visitor_id=visitor
        )
        assert mine is not None and mine.id == outcome.row.id
        # alt vizitator pe același tenant → None, indistinct de inexistent
        other = await ts.get_turn_for_session(
            db, business_id=bid, turn_id=outcome.row.id, channel_token=token, visitor_id="web_alt"
        )
        assert other is None


async def test_second_accept_conflict_references_active_turn(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        channel_id, token = await _make_channel(conn, bid)
        visitor = f"web_{uuid4().hex[:10]}"
        first, _ = await _accept_async(conn, bid, channel_id, token, visitor=visitor)
        second, _ = await _accept_async(
            conn, bid, channel_id, token, visitor=visitor, text="alt mesaj"
        )
        assert isinstance(second, ts.ActiveTurnConflict)
        assert second.active is not None and second.active.id == first.row.id
