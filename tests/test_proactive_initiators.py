"""PL-1 — inițiatorii proactivi (sweeper-e care CREEAZĂ proactive_jobs). Fakes, ZERO DB/rețea.

Sweeper-ele sunt cod pur peste query-uri: monkeypatch-uim query-urile în namespace-ul `initiators`
și capturăm apelurile de `create_proactive_job`. Orchestrarea (control plane → per tenant) e testată
cu admin_conn/tenant_conn fabricate (model test_proactive). Query-urile SQL reale rămân pe
integration; aici testăm logica de inițiere + idempotența + izolarea per-tenant.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from src.proactive import initiators


class FakeTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def transaction(self):
        return FakeTxn()


def _settings(**kw):
    base = dict(
        proactive_initiators_batch=200,
        abandoned_cart_after_seconds=3600,
        abandoned_cart_max_age_seconds=604800,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# sweep_abandoned_cart — un job per coș eligibil, idempotent, skip fără conversație
# --------------------------------------------------------------------------- #


async def test_sweep_abandoned_cart_creates_jobs(monkeypatch):
    carts = [
        {"id": "ck1", "contact_id": "c1", "conversation_id": "v1"},
        {"id": "ck2", "contact_id": "c2", "conversation_id": "v2"},
    ]
    created: list[dict] = []

    async def f_find(conn, biz, **kw):
        return carts

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return f"job-{kw['dedupe_key']}"

    monkeypatch.setattr(initiators, "find_abandoned_carts", f_find)
    monkeypatch.setattr(initiators, "create_proactive_job", f_create)

    n = await initiators.sweep_abandoned_cart(
        FakeConn(), "b1", older_than_seconds=3600, max_age_seconds=604800, limit=200
    )
    assert n == 2
    assert [c["kind"] for c in created] == ["abandoned_cart", "abandoned_cart"]
    assert created[0]["dedupe_key"] == "abandoned_cart:ck1"
    assert created[0]["contact_id"] == "c1" and created[0]["conversation_id"] == "v1"


async def test_sweep_abandoned_cart_skips_without_conversation(monkeypatch):
    carts = [{"id": "ck1", "contact_id": "c1", "conversation_id": None}]
    created: list[dict] = []

    async def f_find(conn, biz, **kw):
        return carts

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return "x"

    monkeypatch.setattr(initiators, "find_abandoned_carts", f_find)
    monkeypatch.setattr(initiators, "create_proactive_job", f_create)

    n = await initiators.sweep_abandoned_cart(
        FakeConn(), "b1", older_than_seconds=1, max_age_seconds=2, limit=10
    )
    assert n == 0 and created == []  # fără conversație nu rutăm


async def test_sweep_abandoned_cart_dedup_not_counted(monkeypatch):
    """ON CONFLICT (dedupe_key) → create întoarce None (jobul exista deja) → nu se contorizează."""
    carts = [{"id": "ck1", "contact_id": "c1", "conversation_id": "v1"}]

    async def f_find(conn, biz, **kw):
        return carts

    async def f_create(conn, biz, **kw):
        return None

    monkeypatch.setattr(initiators, "find_abandoned_carts", f_find)
    monkeypatch.setattr(initiators, "create_proactive_job", f_create)

    n = await initiators.sweep_abandoned_cart(
        FakeConn(), "b1", older_than_seconds=1, max_age_seconds=2, limit=10
    )
    assert n == 0


# --------------------------------------------------------------------------- #
# sweep_back_in_stock — job + mark notified; fără dedupe (gardat de notified_at)
# --------------------------------------------------------------------------- #


async def test_sweep_back_in_stock_creates_and_marks(monkeypatch):
    subs = [{"id": "s1", "contact_id": "c1", "product_id": "p1", "conversation_id": "v1"}]
    created: list[dict] = []
    marked: list[str] = []

    async def f_find(conn, biz, **kw):
        return subs

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return "job1"

    async def f_mark(conn, biz, sid):
        marked.append(sid)

    monkeypatch.setattr(initiators, "find_restocked_subscriptions", f_find)
    monkeypatch.setattr(initiators, "create_proactive_job", f_create)
    monkeypatch.setattr(initiators, "mark_subscription_notified", f_mark)

    n = await initiators.sweep_back_in_stock(FakeConn(), "b1", limit=200)
    assert n == 1
    assert created[0]["kind"] == "back_in_stock"
    assert created[0]["payload"] == {"product_id": "p1"}
    assert created[0].get("dedupe_key") is None  # re-subscribe re-armează → fără dedupe_key
    assert marked == ["s1"]  # iese din candidați


async def test_sweep_back_in_stock_no_conversation_marks_only(monkeypatch):
    subs = [{"id": "s1", "contact_id": "c1", "product_id": "p1", "conversation_id": None}]
    created: list[dict] = []
    marked: list[str] = []

    async def f_find(conn, biz, **kw):
        return subs

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return "job1"

    async def f_mark(conn, biz, sid):
        marked.append(sid)

    monkeypatch.setattr(initiators, "find_restocked_subscriptions", f_find)
    monkeypatch.setattr(initiators, "create_proactive_job", f_create)
    monkeypatch.setattr(initiators, "mark_subscription_notified", f_mark)

    n = await initiators.sweep_back_in_stock(FakeConn(), "b1", limit=10)
    assert n == 0 and created == [] and marked == ["s1"]  # marcat ca să nu re-scaneze la infinit


# --------------------------------------------------------------------------- #
# inițiatori pe EVENIMENT — schedule_awb_update / schedule_follow_up
# --------------------------------------------------------------------------- #


async def test_schedule_awb_update(monkeypatch):
    created: list[dict] = []

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return "job1"

    monkeypatch.setattr(initiators, "create_proactive_job", f_create)
    out = await initiators.schedule_awb_update(
        FakeConn(), "b1", contact_id="c1", conversation_id="v1", order_id="o1",
        awb="AWB9", carrier="FAN",
    )
    assert out == "job1"
    assert created[0]["kind"] == "awb_update"
    assert created[0]["dedupe_key"] == "awb_update:o1"  # o singură notificare per comandă
    assert created[0]["payload"] == {"order_id": "o1", "awb": "AWB9", "carrier": "FAN"}


async def test_schedule_follow_up(monkeypatch):
    created: list[dict] = []

    async def f_create(conn, biz, **kw):
        created.append(kw)
        return "job1"

    monkeypatch.setattr(initiators, "create_proactive_job", f_create)
    await initiators.schedule_follow_up(
        FakeConn(), "b1", contact_id="c1", conversation_id="v1", body="salut",
        scheduled_at=None, variables={"x": "1"},
    )
    assert created[0]["kind"] == "follow_up"
    assert created[0]["payload"] == {"body": "salut", "variables": {"x": "1"}}


# --------------------------------------------------------------------------- #
# run_initiators — control plane → per tenant; un tenant stricat nu oprește restul
# --------------------------------------------------------------------------- #


async def test_run_initiators_iterates_tenants_and_isolates_errors(monkeypatch):
    @asynccontextmanager
    async def fake_admin(pool):
        yield FakeConn()

    @asynccontextmanager
    async def fake_tenant(business_id):
        yield FakeConn()

    async def f_cart_tenants(conn, **kw):
        return ["b1", "b2"]

    async def f_restock_tenants(conn, **kw):
        return ["b3"]

    swept: list[tuple] = []

    async def f_sweep_cart(conn, business_id, **kw):
        swept.append(("cart", business_id))
        if business_id == "b2":
            raise RuntimeError("boom")  # tenant stricat
        return 2

    async def f_sweep_restock(conn, business_id, **kw):
        swept.append(("restock", business_id))
        return 1

    monkeypatch.setattr(initiators, "admin_conn", fake_admin)
    monkeypatch.setattr(initiators, "tenant_conn", fake_tenant)
    monkeypatch.setattr(initiators, "business_ids_with_abandoned_carts", f_cart_tenants)
    monkeypatch.setattr(initiators, "business_ids_with_restocks", f_restock_tenants)
    monkeypatch.setattr(initiators, "sweep_abandoned_cart", f_sweep_cart)
    monkeypatch.setattr(initiators, "sweep_back_in_stock", f_sweep_restock)

    counts = await initiators.run_initiators(None, settings=_settings())

    # b1=2 + b2 crashed(0) + b3 restock=1 — tenantul stricat nu a oprit restul
    assert counts == {"abandoned_cart": 2, "back_in_stock": 1}
    assert ("cart", "b1") in swept
    assert ("cart", "b2") in swept  # a fost încercat
    assert ("restock", "b3") in swept


# --------------------------------------------------------------------------- #
# P5 + P7 — verificări în sursă
# --------------------------------------------------------------------------- #


def test_no_direct_channel_send_in_initiators():
    src = Path("src/proactive/initiators.py").read_text(encoding="utf-8")
    assert "MetaClient" not in src  # P5: inițiatorii NU trimit, doar inserează joburi
    assert "TelegramClient" not in src
    assert "import httpx" not in src


def test_proactive_queries_have_idempotency_and_isolation():
    src = Path("src/db/queries/proactive.py").read_text(encoding="utf-8")
    assert "on conflict (business_id, dedupe_key)" in src  # idempotența inițiatorilor (019)
    # find_* + mark_subscription_notified tenant-scoped (P7); control-plane e excepția documentată
    assert "make_interval(secs => $2)" in src  # praguri coș (after/max_age)
