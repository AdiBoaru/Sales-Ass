"""NX-236 — consumul one-shot pe Postgres REAL: cine câștigă cursa și ce vede pierzătorul.

Garanțiile de aici nu se pot dovedi fără DB, fiindcă arbitrul lor NU e cod Python, ci indecșii
ledgerului: `unique (business_id, conversation_id, client_turn_id)` (idempotency) și indexul
parțial „un singur turn activ per conversație" (single-flight). Consumul unei acțiuni e chiar
rândul turului care o folosește — cheia lui de request e HMAC peste `action_id` — deci:

  • același buton + același `client_turn_id` → un singur rând (retry ⇒ replay, nu a doua execuție);
  • același buton + ALT `client_turn_id` → al doilea găsește primul consumator (`already_consumed`);
  • N accepturi CONCURENTE pe același buton → exact unul inserează.

Exclus din CI fast (`-m "not integration"`). Cere migrarea 040 aplicată.
"""

import asyncio
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.provider import static_db
from src.db.queries import web_turns as wt
from src.web import action_service as svc
from src.web import turn_service as ts
from src.web.action_crypto import parse_key_ring
from src.web.action_models import ActionArgs, ActionPlan

pytestmark = [pytest.mark.integration]

SECRET = "nx236-test-secret"
PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"
KEY_SPEC = "k1:bngyMzYtdGVzdC1rZXktb25lLS0tLS0tLS0tLS0tLS0="  # 32B ASCII, base64


def _ring():
    return parse_key_ring(KEY_SPEC)


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-236 actions', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx236-{uuid4().hex[:8]}",
    )
    return bid


async def _make_scope(conn, bid: str) -> tuple[str, str, str]:
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


PLAN = (
    ActionPlan("request_details", ActionArgs(product_ref=PID_A)),
    ActionPlan("request_reviews", ActionArgs(product_ref=PID_A)),
)
VIEW = svc.merge_actions_into_view(
    {"content": "Uite serul.", "products": [{"product_id": PID_A, "name": "Ser X"}]}, PLAN
)


async def _completed_source(conn, bid, conv, contact, session_hash="h") -> wt.WebTurnRow:
    """Un turn TERMINAL cu plan de acțiuni persistat — dovada de emitere, scrisă ca în producție."""
    row = await wt.insert_turn(
        conn,
        bid,
        conv,
        contact,
        str(uuid4()),
        "fp-source",
        session_ref_hash=session_hash,
        conversation_revision=3,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
    )
    claim = await wt.claim_turn(conn, bid, row.id, owner="o1", lease_ttl_s=60)
    await wt.complete_turn(conn, bid, row.id, lease_epoch=claim.lease_epoch, response_json=VIEW)
    return await wt.get_turn_by_id(conn, bid, row.id)


async def _consume(conn, bid, conv, contact, fingerprint, client_turn_id):
    return await wt.insert_turn(
        conn,
        bid,
        conv,
        contact,
        client_turn_id,
        fingerprint,
        session_ref_hash="h",
        conversation_revision=4,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
    )


# `authorize_action` are `skew_s=0` ca default de BIBLIOTECĂ, dar niciun apelant real nu-l
# folosește: ruta din `src/web/app.py` trimite `WEB_ACTION_CLOCK_SKEW_S` (60s). Aici contează,
# fiindcă `issued_at` e `completed_at`-ul rândului — deci CEASUL BAZEI — iar verificarea folosește
# ceasul procesului. Cu toleranță zero, testul măsoară de fapt diferența dintre cele două ceasuri
# (baza noastră e cu ~0,5s înaintea mașinii locale ⇒ `not_yet_valid`), nu autorizarea pe care o
# are de verificat.
PROD_SKEW_S = 60


def _fingerprint(bid: str, action_id: str) -> str:
    return svc.action_fingerprint(SECRET, business_id=bid, channel_token="tok", action_id=action_id)


# ── Emiterea se re-derivă din rândul persistat ─────────────────────────────────────────────
async def test_actions_are_reissued_identically_from_the_stored_row(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
    first = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)
    second = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)
    assert [a.token for a in first] == [a.token for a in second]
    assert len(first) == 2


async def test_authorize_finds_the_source_row_on_a_real_db(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact, session_hash="sess-hash")
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        verdict = await svc.authorize_action(
            static_db(conn),
            token=issued.token,
            business_id=bid,
            channel_token="tok",
            visitor_id="vis",
            client_turn_id=str(uuid4()),
            ring=_ring(),
            fingerprint_secret=SECRET,
            skew_s=PROD_SKEW_S,
        )
    # Sesiunea reală (token+visitor) nu produce `sess-hash` → refuz, fără existence leak.
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.code == "action_not_found"


async def test_authorize_succeeds_when_the_session_matches(shop):
    bid = shop
    pool = await get_pool()
    session_hash = ts.session_ref_hash("tok", "vis")
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact, session_hash=session_hash)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        verdict = await svc.authorize_action(
            static_db(conn),
            token=issued.token,
            business_id=bid,
            channel_token="tok",
            visitor_id="vis",
            client_turn_id=str(uuid4()),
            ring=_ring(),
            fingerprint_secret=SECRET,
            skew_s=PROD_SKEW_S,
        )
    assert isinstance(verdict, svc.AuthorizedAction)
    assert verdict.command.args.product_ref == PID_A


# ── Consum one-shot ─────────────────────────────────────────────────────────────────────────
async def test_same_action_same_turn_is_one_row(shop):
    """Retry-ul aceluiași turn: idempotency-ul ledgerului îl rezolvă — replay, nu a doua rulare."""
    bid = shop
    pool = await get_pool()
    client_turn_id = str(uuid4())
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        fingerprint = _fingerprint(bid, issued.action_id)
        first = await _consume(conn, bid, conv, contact, fingerprint, client_turn_id)
        second = await _consume(conn, bid, conv, contact, fingerprint, client_turn_id)
        found = await wt.find_turn_by_fingerprint(conn, bid, conv, fingerprint)
    assert first is not None and second is None
    assert found.id == first.id
    assert svc.consumption_conflict(found, client_turn_id) is None


async def test_same_action_different_turn_is_already_consumed(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        fingerprint = _fingerprint(bid, issued.action_id)
        first = await _consume(conn, bid, conv, contact, fingerprint, str(uuid4()))
        # Primul consumator trebuie să fie TERMINAL, altfel single-flight-ul respinge oricum.
        claim = await wt.claim_turn(conn, bid, first.id, owner="o", lease_ttl_s=60)
        await wt.complete_turn(
            conn, bid, first.id, lease_epoch=claim.lease_epoch, response_json={"content": "ok"}
        )
        found = await wt.find_turn_by_fingerprint(conn, bid, conv, fingerprint)
    conflict = svc.consumption_conflict(found, str(uuid4()))
    assert conflict is not None and conflict.code == "action_already_consumed"


async def test_twenty_concurrent_consumptions_yield_one_winner(shop):
    """Cursa reală: 20 de taburi apasă simultan. Single-flight-ul lasă exact unul să insereze."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
    fingerprint = _fingerprint(bid, issued.action_id)

    async def one():
        async with admin_conn(pool) as conn:
            return await _consume(conn, bid, conv, contact, fingerprint, str(uuid4()))

    results = await asyncio.gather(*[one() for _ in range(20)], return_exceptions=True)
    winners = [r for r in results if isinstance(r, wt.WebTurnRow)]
    assert len(winners) == 1
    async with admin_conn(pool) as conn:
        count = await conn.fetchval(
            "select count(*) from web_turns where business_id = $1 and request_fingerprint = $2",
            bid,
            fingerprint,
        )
    assert count == 1


async def test_two_different_actions_do_not_collide(shop):
    """Butoane diferite = chei de consum diferite: al doilea nu e „deja consumat"."""
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)
        fp_a = _fingerprint(bid, issued[0].action_id)
        fp_b = _fingerprint(bid, issued[1].action_id)
        assert fp_a != fp_b
        first = await _consume(conn, bid, conv, contact, fp_a, str(uuid4()))
        claim = await wt.claim_turn(conn, bid, first.id, owner="o", lease_ttl_s=60)
        await wt.complete_turn(
            conn, bid, first.id, lease_epoch=claim.lease_epoch, response_json={"content": "ok"}
        )
        assert await wt.find_turn_by_fingerprint(conn, bid, conv, fp_b) is None


# ── Retenție / GDPR: sursa dispare ⇒ butonul moare ─────────────────────────────────────────
async def test_deleting_the_source_turn_kills_its_actions(shop):
    bid = shop
    pool = await get_pool()
    session_hash = ts.session_ref_hash("tok", "vis")
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact, session_hash=session_hash)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        await conn.execute("delete from web_turns where id = $1", source.id)
        verdict = await svc.authorize_action(
            static_db(conn),
            token=issued.token,
            business_id=bid,
            channel_token="tok",
            visitor_id="vis",
            client_turn_id=str(uuid4()),
            ring=_ring(),
            fingerprint_secret=SECRET,
            skew_s=PROD_SKEW_S,
        )
    assert isinstance(verdict, svc.ActionRejected)
    assert verdict.reason == "source_missing"


# ── PII: nimic sensibil nu ajunge pe rând ──────────────────────────────────────────────────
async def test_no_token_or_action_id_is_stored_on_the_row(shop):
    bid = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        _, contact, conv = await _make_scope(conn, bid)
        source = await _completed_source(conn, bid, conv, contact)
        issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_ring(), ttl_s=1800)[0]
        fingerprint = _fingerprint(bid, issued.action_id)
        row = await _consume(conn, bid, conv, contact, fingerprint, str(uuid4()))
        dump = await conn.fetchval(
            "select to_jsonb(w)::text from web_turns w where id = $1", row.id
        )
    assert issued.token not in dump
    assert issued.action_id not in dump  # doar HMAC-ul lui (fingerprint), niciodată id-ul brut
