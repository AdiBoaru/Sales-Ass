"""NX-53 — izolarea multi-tenant SUB CONCURENȚĂ (50 tururi paralele, 2 tenanți).

Dovada executabilă pentru NX-50: testul secvențial existent nu atinge scenariul
real — 50 de `tenant_conn` lansate cu `asyncio.gather`, alternând A/B, pe un pool
cu conexiuni intens refolosite între tenanți. Dacă `app.business_id` s-ar scurge
între checkout-uri (bug-ul P0-A), un task ar vedea datele altui tenant.

Integration + slow → exclus din CI normal (`-m "not integration"`); rulează în
jobul nightly `isolation-concurrent` (vezi .github/workflows/ci.yml) și local:
    pytest -m "integration and slow"

NX-254 — CELE 50 DE TURURI NU CER 50 DE SESIUNI, și confuzia asta a ținut `CI`
roșu pe `main` de pe 21 august. Task-urile se așteaptă la coadă pe `bot_pool`,
care are `max_size` sesiuni; concurența logică rămâne 50, bugetul de sesiuni e
al poolului. Ce depășea plafonul poolerului (15 clienți în session mode, ÎMPĂRȚIT
cu producția) era suma celor două pooluri ale procesului: 10 admin + 10 bot = 20.

Deci fixul nu e mai puține tururi — ar fi fost teatru, fiindcă REFOLOSIREA
agresivă a conexiunilor între tenanți ESTE scenariul pe care testul îl dovedește.
Fixul e bugetul: `DB_ADMIN_POOL_MAX` + `DB_BOT_POOL_MAX` sub plafon, cu loc lăsat
producției. Cu mai puține sloturi, aceleași 50 de tururi refolosesc conexiunile
MAI des, deci testul e mai strict, nu mai slab.

Fixture-ul creează 2 businesses throwaway (COMMIT — trebuie vizibile între
conexiuni concurente, deci NU rollback) și le curăță la final.
"""

import asyncio
from uuid import uuid4

import pytest

from src.config import get_settings
from src.db.connection import (
    admin_conn,
    close_pool,
    get_bot_pool,
    get_pool,
    tenant_conn,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

N_PER_TENANT = 25


@pytest.fixture
async def two_tenants():
    """Două businesses reale, throwaway. Insert (commit) → vizibile între
    conexiunile concurente din bot_pool; șterse la teardown (cu contacts-probe)."""
    pool = await get_pool()
    a, b = str(uuid4()), str(uuid4())
    async with admin_conn(pool) as conn:
        for bid in (a, b):
            await conn.execute(
                "insert into businesses (id, slug, name, vertical, status, default_locale) "
                "values ($1, $2, $3, 'ecommerce', 'active', 'ro')",
                bid,
                f"nx53-{uuid4().hex[:8]}",
                "NX-53 isolation test",
            )
    try:
        yield a, b
    finally:
        async with admin_conn(pool) as conn:
            await conn.execute("delete from contacts where business_id = any($1::uuid[])", [a, b])
            await conn.execute("delete from businesses where id = any($1::uuid[])", [a, b])
        await close_pool()


async def _probe(bid: str, run_id: str, idx: str) -> dict:
    """Un tur: checkout tenant-scoped → INSERT marker → citește ce e vizibil.
    Întoarce rolul, GUC-ul efectiv și business_id-urile distincte văzute."""
    async with tenant_conn(bid) as conn:
        user = await conn.fetchval("select current_user")
        guc = await conn.fetchval("select current_setting('app.business_id', true)")
        await conn.execute(
            "insert into contacts (business_id, display_name) values ($1, $2)",
            bid,
            f"nx53-{run_id}-{idx}",
        )
        visible = await conn.fetch("select distinct business_id::text as bid from contacts")
    return {"user": user, "guc": guc, "visible": sorted(r["bid"] for r in visible)}


def test_bugetul_de_sesiuni_incape_sub_plafonul_poolerului():
    """NX-254 — garda care transformă `(EMAXCONNSESSION)` într-un mesaj care spune ce s-a întâmplat.

    Fără ea, depășirea bugetului iese ca eșec al testului de IZOLARE: gate-ul de securitate arată
    roșu din motive de capacitate, iar cine se uită caută o scurgere de tenant care nu există. A
    durat cinci zile prima oară.

    Se verifică doar acolo unde bugetul e DECLARAT (`DB_SESSION_BUDGET`). Nedeclarat înseamnă „nu
    cunoaștem plafonul mediului ăstuia", iar o gardă care inventează un plafon ar fi al doilea fel
    de a minți. NU e un skip pe eșec de capacitate — e absența unei constrângeri de verificat.

    Verifică o proprietate a CONFIGURĂRII, deci nu atinge DB-ul și rulează în milisecunde."""
    settings = get_settings()
    if settings.db_session_budget is None:
        pytest.skip("DB_SESSION_BUDGET nedeclarat: nu există plafon de verificat în mediul ăsta")
    total = settings.db_admin_pool_max + settings.db_bot_pool_max
    assert total <= settings.db_session_budget, (
        f"procesul poate cere {total} sesiuni (admin {settings.db_admin_pool_max} + bot "
        f"{settings.db_bot_pool_max}), dar bugetul declarat e {settings.db_session_budget}. "
        "Coboară DB_ADMIN_POOL_MAX / DB_BOT_POOL_MAX. NU reduce N_PER_TENANT: cele 50 de tururi "
        "se așteaptă la coadă pe pool, deci nu cer 50 de sesiuni, iar reducerea lor ar slăbi exact "
        "refolosirea de conexiuni pe care testul o dovedește."
    )


async def test_concurrent_isolation_holds(two_tenants):
    """Happy: 50 task-uri paralele (25 A / 25 B) → fiecare vede DOAR datele lui."""
    a, b = two_tenants
    run_id = uuid4().hex[:8]
    await get_bot_pool()  # warm pool înainte de gather (evită race la lazy-init)

    specs = []
    for i in range(N_PER_TENANT):
        specs.append((a, f"a{i}"))
        specs.append((b, f"b{i}"))
    results = await asyncio.gather(*[_probe(bid, run_id, idx) for bid, idx in specs])

    for (bid, idx), r in zip(specs, results):
        assert r["user"] == "bot_runtime", f"task {idx}: rol {r['user']}"
        assert r["guc"] == bid, f"task {idx}: GUC {r['guc']} ≠ {bid} (scurgere!)"
        assert r["visible"] == [bid], f"task {idx}: vede {r['visible']}, aștept doar {bid}"

    # Numărătoare globală ca admin: 25 + 25, zero încrucișări.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        ca = await conn.fetchval("select count(*) from contacts where business_id = $1::uuid", a)
        cb = await conn.fetchval("select count(*) from contacts where business_id = $1::uuid", b)
    assert ca == N_PER_TENANT
    assert cb == N_PER_TENANT


async def test_empty_guc_is_fail_closed(two_tenants):
    """Failure mode: o conexiune cu GUC gol nu vede NIMIC (fail-closed), niciodată
    datele altui tenant — exact ce ține izolarea când ceva merge prost."""
    a, _ = two_tenants
    async with tenant_conn(a) as conn:
        await conn.execute(
            "insert into contacts (business_id, display_name) values ($1, 'nx53-fc')", a
        )
    bot = await get_bot_pool()
    async with bot.acquire() as conn:
        await conn.execute("select set_config('app.business_id', '', false)")
        n = await conn.fetchval("select count(*) from contacts")
    assert n == 0
