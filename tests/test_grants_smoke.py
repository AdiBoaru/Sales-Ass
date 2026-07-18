"""NX-123 — smoke-test grant/policy: rulează ca `bot_runtime` cu `app.business_id`
setat (exact ca runtime-ul) și verifică, pentru fiecare tabel runtime:
  • pe tabelele CITITE: SELECT trivial NU aruncă (grant lipsă → InsufficientPrivilege;
    politică `FOR ALL TO public` care subselectează business_users → „permission denied");
  • pe tabelele APPEND-ONLY: INSERT merge, dar SELECT e REFUZAT (NX-177 — privilegiul care
    NU trebuie să existe e la fel de important ca cel care trebuie);
  • nicio politică `FOR ALL TO public` pe tabelele runtime (clasa de bug 003→011).

Mută regresia „grant/politică greșit" de la «prod, primul client nou» la «CI».
@integration + @slow → rulează pe main/nightly (atinge DB real), NU pe PR.

INSERT-grant-urile runtime sunt acoperite de test_inbound_dedupe / test_tenant_isolation
(care fac scrieri reale ca bot_runtime); aici țintim grant-urile + politici public-ALL."""

import asyncpg
import pytest

from src.db.connection import close_pool, get_pool

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Tabelele pe care calea runtime (bot_runtime) le CITEȘTE, derivate din src/db/queries/*.
# Sursă explicită (nu din date de tenant) → generic pe orice vertical.
READABLE_TABLES = [
    "businesses",
    "contacts",
    "channel_identities",
    "conversations",
    "conversation_summaries",
    "messages",
    "inbound_dedupe",
    "outbox",
    "intent_aliases",
    "faqs",
    "semantic_cache",
    "usage_daily",
    "orders",
    "order_items",
    "products",
    "product_variants",
    "product_embeddings",
    "categories",
    "brands",
]

# NX-177: `analytics_events` e APPEND-ONLY prin design — bot_runtime are INSERT, NU SELECT
# (docs/003_bot_runtime_role.sql:66 `grant insert on analytics_events`; CLAUDE.md „botul are doar
# INSERT"; src/db/queries/demand.py „se citește pe conn ADMIN — bot_runtime n-are SELECT").
# Era în lista de SELECT → testul cerea un grant care NU trebuie să existe. Fix-ul „adaugă grantul"
# ar fi slăbit o graniță de izolare deliberată ca să facă testul verde — exact invers.
# Aici verificăm invariantul REAL: INSERT merge, SELECT e refuzat.
APPEND_ONLY_TABLES = ["analytics_events"]
RUNTIME_TABLES = READABLE_TABLES + APPEND_ONLY_TABLES  # pt auditul de politici (vezi mai jos)


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@pytest.mark.parametrize("table", READABLE_TABLES)
async def test_bot_runtime_can_select(pool, table):
    """Un SELECT trivial ca bot_runtime nu trebuie să arunce (0 rânduri e OK).
    RLS filtrează la app.business_id; un grant lipsă sau o politică public-ALL ar arunca."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO_BIZ)
            await conn.execute(f"select 1 from {table} limit 1")  # noqa: S608 — tabel din allow-list
        finally:
            await tr.rollback()


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_bot_runtime_cannot_select_append_only(pool, table):
    """Append-only: SELECT-ul lui bot_runtime TREBUIE să fie refuzat.

    Assert pe absența privilegiului, nu pe prezența lui: dacă cineva „repară" testul de mai sus
    adăugând `grant select`, ăsta pică și spune de ce. Citirile de analytics (rollup, raport de
    cerere, replay) merg pe conn ADMIN — vezi src/db/queries/demand.py."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO_BIZ)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(f"select 1 from {table} limit 1")  # noqa: S608 — allow-list
        finally:
            await tr.rollback()


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_bot_runtime_can_insert_append_only(pool, table):
    """...dar INSERT-ul merge (altfel n-am avea observabilitate). Rollback → zero reziduu."""
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set local role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO_BIZ)
            await conn.execute(
                f"insert into {table} (business_id, event_type) values ($1, $2)",  # noqa: S608
                DEMO_BIZ,
                "grants_smoke",
            )
        finally:
            await tr.rollback()


async def test_no_public_all_policies_on_runtime_tables(pool):
    """Nicio politică `FOR ALL TO public` pe tabele runtime — se evaluează ȘI pe SELECT-ul
    lui bot_runtime și crapă dacă subselectează business_users (regresia 003→011)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select tablename, policyname from pg_policies "
            "where schemaname = 'public' and tablename = any($1::text[]) "
            "and cmd = 'ALL' and roles @> array['public']::name[]",
            RUNTIME_TABLES,
        )
    bad = [f"{r['tablename']}.{r['policyname']}" for r in rows]
    assert not bad, "politici FOR ALL TO public pe tabele runtime (clasa 003→011): " + ", ".join(
        bad
    )
