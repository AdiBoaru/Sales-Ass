"""NX-123 — smoke-test grant/policy: rulează ca `bot_runtime` cu `app.business_id`
setat (exact ca runtime-ul) și verifică, pentru fiecare tabel runtime:
  • SELECT trivial NU aruncă (grant lipsă → InsufficientPrivilege; politică
    `FOR ALL TO public` care subselectează business_users → „permission denied");
  • nicio politică `FOR ALL TO public` pe tabelele runtime (clasa de bug 003→011).

Mută regresia „grant/politică greșit" de la «prod, primul client nou» la «CI».
@integration + @slow → rulează pe main/nightly (atinge DB real), NU pe PR.

INSERT-grant-urile runtime sunt acoperite de test_inbound_dedupe / test_tenant_isolation
(care fac scrieri reale ca bot_runtime); aici țintim SELECT-grant + politici public-ALL."""

import pytest

from src.db.connection import close_pool, get_pool

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Tabelele atinse de calea runtime (bot_runtime), derivate din src/db/queries/*.
# Sursă explicită (nu din date de tenant) → generic pe orice vertical.
RUNTIME_TABLES = [
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
    "analytics_events",
    "usage_daily",
    "orders",
    "order_items",
    "products",
    "product_variants",
    "product_embeddings",
    "categories",
    "brands",
]


@pytest.fixture
async def pool():
    p = await get_pool()
    yield p
    await close_pool()


@pytest.mark.parametrize("table", RUNTIME_TABLES)
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
