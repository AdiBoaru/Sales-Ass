"""NX-161 Felia 0B — provider DB tenant-scoped + puntea de compat pe PipelineDeps.

Pur, fără DB reală: `static_db` yield-uiește un conn injectat; `tenant_db` e testat cu `tenant_conn`
monkeypatch-uit (verificăm doar delegarea). Puntea de compat (`__post_init__`) e miezul: cele 114
`PipelineDeps(conn=...)` din teste trebuie să primească automat un provider, fără rescriere.
"""

from contextlib import asynccontextmanager

from src.db import provider as prov
from src.db.provider import static_db, tenant_db
from src.worker.runner import PipelineDeps

# --------------------------------------------------------------------------- #
# 1. static_db — yield-uiește conn-ul injectat, fără checkout nou
# --------------------------------------------------------------------------- #


async def test_static_db_yields_injected_conn():
    fake = object()
    db = static_db(fake)
    async with db() as c:
        assert c is fake


async def test_static_db_reusable_across_calls():
    fake = object()
    db = static_db(fake)
    async with db() as c1:
        assert c1 is fake
    async with db() as c2:  # provider reutilizabil (mai multe operații într-un tur)
        assert c2 is fake


# --------------------------------------------------------------------------- #
# 2. tenant_db — delegă la tenant_conn (checkout scurt real în prod)
# --------------------------------------------------------------------------- #


async def test_tenant_db_delegates_to_tenant_conn(monkeypatch):
    sentinel = object()
    seen = {}

    @asynccontextmanager
    async def _fake_tenant_conn(business_id):
        seen["biz"] = business_id
        yield sentinel

    monkeypatch.setattr(prov, "tenant_conn", _fake_tenant_conn)
    db = tenant_db("biz-1")
    async with db() as c:
        assert c is sentinel
    assert seen["biz"] == "biz-1"  # business_id legat la construcție


# --------------------------------------------------------------------------- #
# 3. PipelineDeps — puntea de compat (__post_init__)
# --------------------------------------------------------------------------- #


async def test_conn_creates_static_provider():
    # cele 114 `PipelineDeps(conn=...)` din teste → primesc automat un provider static.
    fake = object()
    deps = PipelineDeps(conn=fake, redis=None, llm=None)
    assert deps.db is not None
    async with deps.db() as c:
        assert c is fake


def test_conn_none_leaves_db_none():
    # `conn=None` (multe teste de stagiu) → db rămâne None (stagiul nu-l atinge oricum).
    deps = PipelineDeps(conn=None, llm=None)
    assert deps.db is None


def test_no_args_is_valid():
    # toate câmpurile au default → construcție goală validă (loosening inofensiv).
    deps = PipelineDeps()
    assert deps.conn is None and deps.db is None


async def test_explicit_db_wins_over_conn():
    # un `db` explicit NU e suprascris de static(conn) — feliile migrate pasează tenant_db real
    # ALĂTURI de conn (compat), iar providerul real trebuie păstrat.
    explicit_conn = object()
    explicit = static_db(explicit_conn)
    other_conn = object()
    deps = PipelineDeps(conn=other_conn, db=explicit)
    assert deps.db is explicit
    async with deps.db() as c:
        assert c is explicit_conn  # providerul explicit, nu static(other_conn)


async def test_legacy_construction_shape_still_works():
    # forma exactă din cele 114 usage-uri (conn=object(), redis, llm) rămâne validă + capătă db.
    deps = PipelineDeps(conn=object(), redis=None, llm=None)
    assert deps.conn is not None and deps.db is not None
