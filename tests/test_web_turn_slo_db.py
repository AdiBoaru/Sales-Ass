"""NX-246 — query-ul de SLI pe Postgres REAL: `renderable` în SQL + izolare de tenant.

Exclus din CI fast (`-m "not integration"`). Există pentru exact două lucruri care nu se pot
dovedi fără DB:

  1. **`renderable` calculat în SQL oglindește `turn_service.renderable`.** Expresia din
     `db/queries/web_turn_slo.py` e o traducere de mână a semanticii „pythonice" de adevăr
     (`{"products": []}` e FALSY). Dacă cele două diverg, indicatorul P6 devine verde peste
     terminale goale — adică fix bug-ul pe care ar trebui să-l prindă. Testul rulează AMBELE pe
     aceleași payload-uri și compară verdict cu verdict.
  2. **`business_id = $1` chiar izolează** (P7 + RLS pe `bot_runtime`): rândurile altui tenant nu
     intră în numitorul nostru. Un raport de SLO contaminat cross-tenant e mai rău decât niciunul.

Cere migrarea 040 aplicată — altfel modulul se SKIP-uie cu mesaj explicit (ca `test_web_turns_db`).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.db.connection import admin_conn, close_pool, get_pool
from src.db.queries import web_turns as wt
from src.db.queries.web_turn_slo import load_turn_facts
from src.observability import slo
from src.web import turn_service as ts

pytestmark = [pytest.mark.integration]


async def _make_business(conn) -> str:
    bid = str(uuid4())
    await conn.execute(
        "insert into businesses (id, slug, name, vertical, status, default_locale) "
        "values ($1, $2, 'NX-246 slo', 'beauty_salon', 'active', 'ro')",
        bid,
        f"nx246-{uuid4().hex[:8]}",
    )
    return bid


async def _make_scope(conn, bid: str) -> tuple[str, str]:
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
    return str(contact_id), str(conversation_id)


@pytest.fixture(autouse=True)
async def _require_migration():
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
                await conn.execute("delete from businesses where id = $1", bid)
        await close_pool()


async def _turn(conn, bid, conv, contact, *, status="completed", view=None, code=None):
    row = await wt.insert_turn(
        conn, bid, conv, contact, str(uuid4()), f"fp-{uuid4().hex[:8]}", session_ref_hash="h"
    )
    claim = await wt.claim_turn(conn, bid, row.id, owner="t", lease_ttl_s=60)
    if status == "completed":
        await wt.complete_turn(conn, bid, row.id, lease_epoch=claim.lease_epoch, response_json=view)
    elif status == "failed":
        await wt.fail_turn(
            conn,
            bid,
            row.id,
            lease_epoch=claim.lease_epoch,
            error_view=view,
            safe_error_code=code or "processing_error",
        )
    return row.id


def _window():
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + timedelta(minutes=5)


# ── 1. `renderable` din SQL == `turn_service.renderable` din Python ─────────────────────────

#: Payload-uri alese ca să lovească exact granițele semanticii de adevăr, nu cazul fericit.
PAYLOADS = [
    {"content": "Salut! Uite serul potrivit.", "products": []},
    {"content": "", "products": [{"name": "Ser X"}]},
    {"content": "", "products": []},  # terminal GOL — P6 trebuie să-l vadă
    {"content": "", "products": [], "comparison": {"rows": []}},
    {"content": "", "products": [], "comparison": None},  # jsonb null ≠ prezent
    {"content": "", "products": [], "offer": {"url": "x"}},
    {"products": []},  # cheie lipsă
    {"content": "text", "products": [{"name": "A"}], "offer": {"url": "y"}},
]


async def test_renderable_din_sql_oglindeste_python(shop):
    """Dacă expresia SQL diverge de `turn_service.renderable`, P6 devine verde peste goluri."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact, conv = await _make_scope(conn, bid)
        for payload in PAYLOADS:
            # `complete_turn` are CHECK de non-NULL, dar nu de conținut — exact de asta există
            # indicatorul. Inserăm direct ca să putem stoca și payload-uri goale.
            await _turn(conn, bid, conv, contact, view=payload)
        page = await load_turn_facts(conn, bid, window_from=_window()[0], window_to=_window()[1])

    assert len(page.facts) == len(PAYLOADS)
    asteptat = [ts.renderable(p) for p in PAYLOADS]
    obtinut = [f.renderable for f in page.facts]
    assert obtinut == asteptat, f"SQL {obtinut} != Python {asteptat} pe {PAYLOADS}"


async def test_terminal_gol_produce_fail_pe_p6(shop):
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact, conv = await _make_scope(conn, bid)
        for _ in range(5):
            await _turn(conn, bid, conv, contact, view={"content": "ok", "products": []})
        await _turn(conn, bid, conv, contact, view={"content": "", "products": []})
        page = await load_turn_facts(conn, bid, window_from=_window()[0], window_to=_window()[1])

    wf, wt_ = _window()
    report = slo.evaluate(page.facts, window_from=wf, window_to=wt_, business_id=bid)
    p6 = next(s for s in report.slis if s.name == "non_empty_terminal")
    assert p6.verdict == slo.VERDICT_FAIL
    assert report.verdict == slo.VERDICT_FAIL


# ── 2. Izolare de tenant ────────────────────────────────────────────────────────────────────


async def test_query_ul_nu_vede_alt_tenant(shop):
    """P7: un raport de SLO contaminat cross-tenant e mai rău decât niciun raport."""
    a, b = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact_a, conv_a = await _make_scope(conn, a)
        contact_b, conv_b = await _make_scope(conn, b)
        for _ in range(3):
            await _turn(conn, a, conv_a, contact_a, view={"content": "A", "products": []})
        for _ in range(7):
            await _turn(conn, b, conv_b, contact_b, view={"content": "B", "products": []})
        wf, wt_ = _window()
        page_a = await load_turn_facts(conn, a, window_from=wf, window_to=wt_)
        page_b = await load_turn_facts(conn, b, window_from=wf, window_to=wt_)

    assert (page_a.total, len(page_a.facts)) == (3, 3)
    assert (page_b.total, len(page_b.facts)) == (7, 7)


async def test_fereastra_filtreaza_pe_accepted_at(shop):
    """Filtrul e pe momentul PROMISIUNII, nu al finalizării: altfel turele lente ar dispărea din
    numitor exact când sistemul e lent, iar disponibilitatea ar crește în incident."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact, conv = await _make_scope(conn, bid)
        await _turn(conn, bid, conv, contact, view={"content": "ok", "products": []})
        now = datetime.now(UTC)
        page = await load_turn_facts(
            conn, bid, window_from=now - timedelta(days=2), window_to=now - timedelta(days=1)
        )
    assert page.facts == [] and page.total == 0


async def test_trunchierea_e_raportata(shop):
    """No silent caps: un set tăiat de `limit` se DECLARĂ, ca verdictele să nu poată fi PASS."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact, conv = await _make_scope(conn, bid)
        for _ in range(4):
            await _turn(conn, bid, conv, contact, view={"content": "ok", "products": []})
        wf, wt_ = _window()
        page = await load_turn_facts(conn, bid, window_from=wf, window_to=wt_, limit=2)

    assert page.truncated is True and page.total == 4 and len(page.facts) == 2
    report = slo.evaluate(
        page.facts, window_from=wf, window_to=wt_, business_id=bid, truncated=page.truncated
    )
    assert all(s.verdict != slo.VERDICT_PASS for s in report.slis)


async def test_faptele_nu_contin_continut(shop):
    """`TurnFact` e proiectat fără `response_json`: raportul nu are nevoie de conținut ca să
    numere, iar ce nu iese din DB nu poate ajunge într-un artefact de CI."""
    bid, _ = shop
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        contact, conv = await _make_scope(conn, bid)
        await _turn(
            conn, bid, conv, contact, view={"content": "secretul clientului", "products": []}
        )
        page = await load_turn_facts(conn, bid, window_from=_window()[0], window_to=_window()[1])

    assert "secretul clientului" not in repr(page.facts)
    assert not hasattr(page.facts[0], "response_json")
