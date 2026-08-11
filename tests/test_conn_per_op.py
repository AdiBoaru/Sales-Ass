"""NX-231 — invariantul conn-per-op, verificat prin CONSTRUCȚIE, nu prin citit cod.

Un test care doar numără `deps.conn` verifică forma. Ce trebuie apărat e comportamentul: nicio
conexiune nu are voie să existe cât rulează un apel extern, iar o conexiune eliberată nu are voie
să mai fie folosită. Ambele se prind cu un PROVIDER FALS care instrumentează checkout-ul:

  • `GuardedProvider` marchează conexiunea drept închisă la ieșirea din `async with`. Orice query
    de după ridică `UseAfterRelease` — deci un `conn` scăpat într-o closure sau reținut într-un
    câmp pică zgomotos, nu tăcut.
  • Un LLM/HTTP fals raportează providerului că intră într-un apel extern. Dacă în acel moment
    există un checkout deschis, providerul ridică `ExternalAwaitInsideConn` — exact bug-ul pe care
    cardul îl scoate din sistem.

Restul acoperă contabilitatea (`db_checkout_ms`/`db_hold_ms` pe operație), presiunea de pool
(mai multe tururi lente decât `pool.max_size`), anularea la mijloc de fază și resetul de context
între tenanți.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.db import op_metrics
from src.db.provider import db_tx, static_db
from src.worker.runner import PipelineDeps


class UseAfterRelease(AssertionError):
    """Un query pe o conexiune deja întoarsă în pool."""


class ExternalAwaitInsideConn(AssertionError):
    """Un apel extern (LLM/HTTP/backoff) cu o conexiune încă checkout-uită."""


class GuardedConn:
    """Conexiune falsă care refuză să lucreze după release și numără query-urile."""

    def __init__(self, owner: GuardedProvider, operation: str) -> None:
        self._owner = owner
        self.operation = operation
        self.closed = False
        self.queries: list[str] = []

    def _check(self, sql: str) -> None:
        if self.closed:
            raise UseAfterRelease(
                f"query pe o conexiune eliberată (operație={self.operation!r}): {sql[:40]!r}"
            )

    async def fetch(self, sql, *args):
        self._check(sql)
        self.queries.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        self._check(sql)
        self.queries.append(sql)
        return None

    async def fetchval(self, sql, *args):
        self._check(sql)
        self.queries.append(sql)
        return None

    async def execute(self, sql, *args):
        self._check(sql)
        self.queries.append(sql)
        return "OK"

    @asynccontextmanager
    async def transaction(self):
        self._check("BEGIN")
        yield self


class GuardedProvider:
    """Provider fals: urmărește checkout-urile deschise + toate conexiunile emise."""

    def __init__(self) -> None:
        self.open_count = 0
        self.max_open = 0
        self.issued: list[GuardedConn] = []
        self.operations: list[str] = []

    def __call__(self, operation: str = "unlabeled"):
        return self._cm(operation)

    @asynccontextmanager
    async def _cm(self, operation: str):
        conn = GuardedConn(self, operation)
        self.issued.append(conn)
        self.operations.append(operation)
        self.open_count += 1
        self.max_open = max(self.max_open, self.open_count)
        try:
            yield conn
        finally:
            self.open_count -= 1
            conn.closed = True

    async def external_call(self, name: str = "llm"):
        """Simulează un apel extern. Pică dacă cineva ține o conexiune în acest moment."""
        if self.open_count:
            held = [c.operation for c in self.issued if not c.closed]
            raise ExternalAwaitInsideConn(f"await {name}() cu checkout deschis: {held}")
        await asyncio.sleep(0)
        return f"{name}-ok"


# --------------------------------------------------------------------------- #
# 1. Providerul fals prinde exact ce trebuie să prindă (testăm instrumentul întâi)
# --------------------------------------------------------------------------- #


async def test_guard_detects_use_after_release():
    db = GuardedProvider()
    async with db("op") as conn:
        await conn.execute("select 1")
    with pytest.raises(UseAfterRelease):
        await conn.execute("select 2")  # conn scăpat în afara checkout-ului


async def test_guard_detects_external_await_inside_checkout():
    db = GuardedProvider()
    with pytest.raises(ExternalAwaitInsideConn):
        async with db("op"):
            await db.external_call("embed")


async def test_guard_allows_external_await_between_checkouts():
    db = GuardedProvider()
    async with db("read") as conn:
        await conn.fetch("select 1")
    assert await db.external_call("embed") == "embed-ok"
    async with db("write") as conn:
        await conn.execute("insert 1")
    assert db.max_open == 1  # niciodată două deodată


# --------------------------------------------------------------------------- #
# 2. Stagiile reale, pe providerul păzit: read scurt → extern → write scurt
# --------------------------------------------------------------------------- #


async def test_cache_stage_releases_conn_before_embedding(monkeypatch):
    """Cache-ul rulează pe TOT traficul și avea un embed între două query-uri. Dacă ăla se face
    cu conexiunea în mână, stratul cel mai ieftin devine cel mai scump sub concurență."""
    from src.worker.stages import cache as cache_stage_mod

    db = GuardedProvider()

    class _LLM:
        async def embed(self, texts):
            await db.external_call("embed")
            return [[0.0] * 4]

    async def _exact(conn, *a, **k):
        await conn.fetchrow("select exact")
        return None

    async def _semantic(conn, *a, **k):
        await conn.fetchrow("select semantic")
        return None

    monkeypatch.setattr(cache_stage_mod, "exact_lookup", _exact)
    monkeypatch.setattr(cache_stage_mod, "semantic_lookup", _semantic)

    ctx = _ctx()
    deps = PipelineDeps(db=db, llm=_LLM())
    await cache_stage_mod.cache_stage(ctx, deps)  # nu trebuie să ridice

    assert db.max_open == 1
    assert db.operations == ["cache_exact_lookup", "cache_semantic_lookup"]
    assert all(c.closed for c in db.issued)


async def test_search_tool_releases_conn_before_embedding(monkeypatch):
    """`search_products` e calea scumpă: verificare de embeddings → embed → scara de relaxare.
    Embed-ul (rețea, cu buget de timp propriu) nu are voie să stea peste un checkout."""
    from src.tools import catalog_tools as ct

    db = GuardedProvider()

    class _LLM:
        async def embed(self, texts):
            await db.external_call("embed")
            return [[0.1] * 4]

    async def _has_embeddings(conn, business_id):
        await conn.fetchval("select has_embeddings")
        return True

    async def _lexical(conn, business_id, **k):
        await conn.fetch("select lexical")
        return [{"id": "p1", "name": "P1", "price": 10.0}]

    async def _semantic(conn, business_id, vec, **k):
        await conn.fetch("select semantic")
        return []

    async def _by_ids(conn, business_id, ids, **k):
        await conn.fetch("select by_ids")
        return [{"id": "p1", "name": "P1", "price": 10.0}]

    monkeypatch.setattr(ct, "has_embeddings", _has_embeddings)
    monkeypatch.setattr(ct, "search_products_lexical", _lexical)
    monkeypatch.setattr(ct, "search_products_semantic", _semantic)
    monkeypatch.setattr(ct, "get_products_by_ids", _by_ids)

    ctx = _ctx()
    deps = PipelineDeps(db=db, llm=_LLM())
    res = await ct.search_products_tool(ctx, deps, {"query": "cremă"})

    assert res.ok
    assert db.max_open == 1
    assert "has_embeddings" in db.operations and "search_products_ladder" in db.operations


async def test_handoff_tool_releases_conn_before_operator_http(monkeypatch):
    """Escaladarea scrie în DB și apoi face un POST către operator. POST-ul e rețeaua altcuiva."""
    from src.tools import handoff_tools as ht

    db = GuardedProvider()

    async def _set_handoff(conn, *a, **k):
        await conn.execute("update conversations")

    async def _notify(ctx, reason):
        await db.external_call("notify_operator")

    monkeypatch.setattr(ht, "set_handoff", _set_handoff)
    monkeypatch.setattr(ht, "notify_operator", _notify)
    monkeypatch.setattr(ht, "handoff_enabled_for", lambda kind: True)

    ctx = _ctx()
    res = await ht.request_human_tool(ctx, PipelineDeps(db=db), {"reason": "vreau om"})
    assert res.ok
    assert db.max_open == 1


# --------------------------------------------------------------------------- #
# 3. Unit of Work — load/commit iau checkout-uri proprii, pipeline-ul niciunul
# --------------------------------------------------------------------------- #


async def test_turn_uow_load_and_commit_are_separate_short_checkouts(monkeypatch):
    from src.worker import turn_uow as uow

    db = GuardedProvider()
    anoop = _anoop

    monkeypatch.setattr(uow, "claim_inbound", _true)
    monkeypatch.setattr(uow, "get_or_create_contact", _contact)
    monkeypatch.setattr(uow, "get_or_create_conversation", _conv)
    monkeypatch.setattr(uow, "insert_message", _msg_id)
    monkeypatch.setattr(uow, "touch_last_inbound", anoop)
    monkeypatch.setattr(uow, "get_recent_messages", _empty_list)
    monkeypatch.setattr(uow, "get_summary_for_context", anoop)

    snap = await uow.load_turn(
        db,
        SimpleNamespace(id="biz-1", default_locale="ro"),
        "chan-1",
        {"provider_msg_id": "m1", "body": "salut", "sender_external_id": "u1"},
        turn_id="t1",
        safe_body="salut",
        identity_external_id="u1",
        verified_customer_ref=None,
        load_facts=False,
    )
    assert not snap.deduped and snap.conversation_id == "conv-1"
    assert db.operations == ["turn_load"]
    assert db.issued[0].closed  # eliberată înainte de a se întoarce snapshotul

    monkeypatch.setattr(uow, "enqueue_outbox", _outbox_id)
    monkeypatch.setattr(uow, "patch_conversation_state", anoop)
    monkeypatch.setattr(uow, "mark_inbound_completed", anoop)

    commit = uow.TurnCommit(
        business_id="biz-1",
        conversation_id="conv-1",
        contact_id="c1",
        fragments=[uow.OutboundFragment("salut", {}, {}, {"type": "text"}, "t1:0")],
        new_state={},
        expected_version=0,
        provider_msg_id="m1",
        deliver=True,
    )
    result = await uow.commit_turn(db, commit, rebuild_state=lambda fresh: fresh)
    assert result.first_outbox_id == "ob-1"
    assert db.operations == ["turn_load", "turn_commit"]
    assert db.max_open == 1  # niciodată două checkout-uri simultan


async def test_turn_commit_rolls_back_as_one_unit(monkeypatch):
    """Commitul e atomic: dacă patch-ul de stare crapă, tranzacția cade cu tot cu inserturi."""
    from src.worker import turn_uow as uow

    calls: list[str] = []

    class _TxConn(GuardedConn):
        @asynccontextmanager
        async def transaction(self):
            calls.append("begin")
            try:
                yield self
            except Exception:
                calls.append("rollback")
                raise
            else:
                calls.append("commit")

    class _P(GuardedProvider):
        @asynccontextmanager
        async def _cm(self, operation: str):
            conn = _TxConn(self, operation)
            self.issued.append(conn)
            self.open_count += 1
            try:
                yield conn
            finally:
                self.open_count -= 1
                conn.closed = True

    db = _P()

    async def _boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(uow, "insert_message", _msg_id)
    monkeypatch.setattr(uow, "enqueue_outbox", _outbox_id)
    monkeypatch.setattr(uow, "patch_conversation_state", _boom)

    commit = uow.TurnCommit(
        business_id="biz-1",
        conversation_id="conv-1",
        contact_id="c1",
        fragments=[uow.OutboundFragment("salut", {}, {}, None, "")],
        new_state={},
        expected_version=0,
        provider_msg_id=None,
        deliver=False,
    )
    with pytest.raises(RuntimeError):
        await uow.commit_turn(db, commit, rebuild_state=lambda fresh: fresh)
    assert calls == ["begin", "rollback"]
    assert db.open_count == 0  # conexiunea s-a întors în pool chiar și pe eroare


# --------------------------------------------------------------------------- #
# 4. Contabilitatea per operație (db_checkout_ms / db_hold_ms)
# --------------------------------------------------------------------------- #


async def test_op_metrics_accumulate_per_operation():
    acc, token = op_metrics.push()
    try:
        op_metrics.record("faq_lookup", checkout_ms=1.0, hold_ms=4.0, query_ms=3.0, queries=2)
        op_metrics.record("faq_lookup", checkout_ms=2.0, hold_ms=6.0, query_ms=5.0, queries=1)
        op_metrics.record("turn_commit", checkout_ms=0.5, hold_ms=9.0, query_ms=8.0, queries=4)
    finally:
        op_metrics.pop(token)
    assert acc.checkouts == 3
    assert acc.by_op["faq_lookup"].n == 2
    assert acc.hold_ms == pytest.approx(19.0)
    assert acc.checkout_ms == pytest.approx(3.5)
    props = acc.as_event_props()
    assert props["db_checkout_ms"] == pytest.approx(3.5)
    assert props["db_hold_ms"] == pytest.approx(19.0)
    assert set(props["by_operation"]) == {"faq_lookup", "turn_commit"}


async def test_idle_held_is_unknown_without_timing():
    acc, token = op_metrics.push()
    try:
        op_metrics.record("x", checkout_ms=1.0, hold_ms=100.0, query_ms=0.0, queries=0)
    finally:
        op_metrics.pop(token)
    # Fără proxy de timing NU inventăm un 0 liniștitor: `None` = „nu știm".
    assert acc.idle_held_ms is None
    assert "db_idle_held_ms" not in acc.as_event_props()
    acc.timed = True
    assert acc.idle_held_ms == pytest.approx(100.0)


async def test_op_metrics_cap_operation_cardinality():
    acc, token = op_metrics.push()
    try:
        for i in range(op_metrics.MAX_OPERATIONS + 5):
            op_metrics.record(f"op{i}", checkout_ms=1.0, hold_ms=1.0)
    finally:
        op_metrics.pop(token)
    assert len(acc.by_op) == op_metrics.MAX_OPERATIONS
    assert acc.as_event_props()["operations_dropped"] == 5


async def test_record_outside_a_turn_is_a_noop():
    # Joburi/scripturi iau checkout-uri fără acumulator deschis — nu ținem un registru global.
    op_metrics.record("job", checkout_ms=1.0, hold_ms=1.0)
    assert op_metrics.current() is None


# --------------------------------------------------------------------------- #
# 5. Presiune de pool: mai multe tururi lente decât `max_size`
# --------------------------------------------------------------------------- #


class _TinyPool:
    """Pool cu N locuri. `held_peak` = câte au fost ocupate simultan."""

    def __init__(self, size: int) -> None:
        self.sem = asyncio.Semaphore(size)
        self.in_use = 0
        self.held_peak = 0
        self.waits = 0

    @asynccontextmanager
    async def checkout(self):
        if self.sem.locked():
            self.waits += 1
        await self.sem.acquire()
        self.in_use += 1
        self.held_peak = max(self.held_peak, self.in_use)
        try:
            yield object()
        finally:
            self.in_use -= 1
            self.sem.release()


async def test_slow_turns_do_not_pin_the_pool():
    """Contra-proba cardului: 2× mai multe tururi „lente" decât locurile din pool. Cu conn-per-op
    toate termină; cu o conexiune ținută peste apelul lent, jumătate ar aștepta după cealaltă."""
    pool = _TinyPool(4)

    def provider():
        @asynccontextmanager
        async def _cm(operation="op"):
            async with pool.checkout() as conn:
                yield conn

        return _cm

    async def turn(db):
        async with db("load"):
            await asyncio.sleep(0)  # muncă de DB: scurtă
        await asyncio.sleep(0.02)  # „LLM": lung, FĂRĂ conexiune
        async with db("commit"):
            await asyncio.sleep(0)

    db = provider()
    await asyncio.wait_for(asyncio.gather(*(turn(db) for _ in range(8))), timeout=2.0)
    assert pool.held_peak <= 4
    # 8 tururi × 2 checkout-uri au trecut printr-un pool de 4 fără să se blocheze reciproc.


async def test_health_query_answers_while_turns_are_in_their_llm_phase():
    """În timpul „apelului lent" trebuie să existe locuri libere: un health-check (sau alt tenant)
    răspunde, nu stă la coadă după LLM-ul altcuiva."""
    pool = _TinyPool(2)

    @asynccontextmanager
    async def db(operation="op"):
        async with pool.checkout() as conn:
            yield conn

    started = asyncio.Event()

    async def slow_turn():
        async with db("load"):
            await asyncio.sleep(0)
        started.set()
        await asyncio.sleep(0.05)  # faza externă
        async with db("commit"):
            await asyncio.sleep(0)

    turns = [asyncio.create_task(slow_turn()) for _ in range(4)]
    await started.wait()
    await asyncio.sleep(0.005)  # toate sunt în faza externă
    async with asyncio.timeout(0.02):  # health-ul NU are voie să aștepte după LLM
        async with db("health") as conn:
            assert conn is not None
    await asyncio.gather(*turns)


# --------------------------------------------------------------------------- #
# 6. Anulare + izolare de tenant
# --------------------------------------------------------------------------- #


async def test_cancellation_returns_the_connection():
    """Anulare în mijlocul unei faze: `finally`-ul providerului trebuie să întoarcă conexiunea,
    altfel un client care închide tabul scurge un slot de pool la fiecare abandon."""
    pool = _TinyPool(1)

    @asynccontextmanager
    async def db(operation="op"):
        async with pool.checkout() as conn:
            yield conn

    async def hangs():
        async with db("slow_query"):
            await asyncio.sleep(10)

    task = asyncio.create_task(hangs())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.in_use == 0
    async with asyncio.timeout(0.1):  # singurul loc din pool e liber
        async with db("after") as conn:
            assert conn is not None


async def test_each_checkout_is_tenant_scoped_by_construction():
    """`business_id` e legat la construcția providerului, nu pasat la fiecare `db()`: un tool
    controlat de model n-are cum să ceară altă conexiune (P7 — server-owned)."""
    seen: list[str] = []

    def tenant_db_fake(business_id: str):
        @asynccontextmanager
        async def _cm(operation="op"):
            seen.append(business_id)  # fiecare checkout re-declară tenantul
            yield SimpleNamespace(business_id=business_id)

        return _cm

    db_a, db_b = tenant_db_fake("biz-a"), tenant_db_fake("biz-b")
    async with db_a("read") as conn:
        assert conn.business_id == "biz-a"
    async with db_b("read") as conn:
        assert conn.business_id == "biz-b"
    async with db_a("write") as conn:
        assert conn.business_id == "biz-a"
    assert seen == ["biz-a", "biz-b", "biz-a"]
    # `db()` nu primește business_id → niciun apelant nu-l poate schimba.
    import inspect

    assert "business_id" not in inspect.signature(db_a).parameters


async def test_db_tx_opens_one_checkout_with_one_transaction():
    db = GuardedProvider()
    async with db_tx(db, "atomic_pair") as conn:
        await conn.execute("insert a")
        await conn.execute("insert b")
    assert db.operations == ["atomic_pair"]  # UN checkout pentru toată perechea atomică
    assert db.issued[0].queries == ["insert a", "insert b"]
    assert db.issued[0].closed  # eliberată la ieșirea din tranzacție


async def test_db_tx_tolerates_a_fake_without_transaction():
    # Multe teste injectează `object()` ca `conn`; `db_tx` nu trebuie să crape pe el.
    async with db_tx(static_db(object()), "op") as conn:
        assert conn is not None


async def test_tenant_db_reasserts_isolation_on_every_checkout(monkeypatch):
    """Un tur face acum N checkout-uri. Fiecare trebuie să treacă prin `tenant_conn` — adică prin
    `set_config('app.business_id')` + assertul NX-04 + reset la release. Izolarea nu se
    „moștenește" de la checkout-ul anterior."""
    from src.db import provider as prov

    events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _fake_tenant_conn(business_id):
        events.append(("set", business_id))
        try:
            yield SimpleNamespace(business_id=business_id)
        finally:
            events.append(("reset", business_id))

    monkeypatch.setattr(prov, "tenant_conn", _fake_tenant_conn)
    db = prov.tenant_db("biz-1")
    async with db("load"):
        pass
    async with db("commit"):
        pass
    assert events == [
        ("set", "biz-1"),
        ("reset", "biz-1"),
        ("set", "biz-1"),
        ("reset", "biz-1"),
    ]


async def test_tenant_db_records_checkout_and_hold_per_operation(monkeypatch):
    from src.db import provider as prov

    @asynccontextmanager
    async def _fake_tenant_conn(business_id):
        yield SimpleNamespace(business_id=business_id)

    monkeypatch.setattr(prov, "tenant_conn", _fake_tenant_conn)
    db = prov.tenant_db("biz-1")
    acc, token = op_metrics.push()
    try:
        async with db("faq_semantic_topk"):
            await asyncio.sleep(0.001)
        async with db("turn_commit"):
            pass
    finally:
        op_metrics.pop(token)
    assert set(acc.by_op) == {"faq_semantic_topk", "turn_commit"}
    assert acc.by_op["faq_semantic_topk"].hold_ms > 0
    assert acc.checkouts == 2


def test_processor_emits_db_ops_and_pool_metrics():
    """P10: runner-ul/processor-ul măsoară, stagiile nu știu. `db_ops` dă defalcarea pe operație —
    fără ea, „poolul e plin" rămâne o observație fără vinovat."""
    from src.worker import processor as proc

    ctx = _ctx()
    acc = op_metrics.DbOpAccumulator()
    acc.record("turn_load", checkout_ms=1.0, hold_ms=3.0, query_ms=0.0, queries=0)
    acc.record("turn_commit", checkout_ms=2.0, hold_ms=5.0, query_ms=0.0, queries=0)
    proc._emit_db_metrics(ctx, acc)

    kinds = {e.type for e in ctx.events}
    assert {"pool_metrics", "db_ops"} <= kinds
    db_ops = next(e for e in ctx.events if e.type == "db_ops")
    assert db_ops.properties["checkouts"] == 2
    assert db_ops.properties["db_hold_ms"] == pytest.approx(8.0)
    assert set(db_ops.properties["by_operation"]) == {"turn_load", "turn_commit"}


def test_processor_skips_db_ops_when_nothing_was_checked_out():
    from src.worker import processor as proc

    ctx = _ctx()
    proc._emit_db_metrics(ctx, op_metrics.DbOpAccumulator())
    assert not any(e.type == "db_ops" for e in ctx.events)  # zero-rows n-au ce căuta în rollup


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ctx():
    from src.models import BusinessConfig, Contact, InboundMessage, TurnContext

    return TurnContext(
        turn_id="t1",
        business=BusinessConfig(id="biz-1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="biz-1"),
        message=InboundMessage(provider_msg_id="m1", body="caut o cremă"),
        conversation_id="conv-1",
    )


async def _anoop(*a, **k):
    return None


async def _true(*a, **k):
    return True


async def _empty_list(*a, **k):
    return []


async def _contact(conn, *a, **k):
    from src.models import Contact

    await conn.fetchrow("select contact")
    return Contact(id="c1", business_id="biz-1")


async def _conv(conn, *a, **k):
    await conn.fetchrow("select conv")
    return {
        "id": "conv-1",
        "state": {},
        "state_version": 0,
        "locale": "ro",
        "bot_active": True,
        "handoff_until": None,
        "shadow_mode": False,
    }


async def _msg_id(conn, *a, **k):
    await conn.execute("insert message")
    return "msg-1"


async def _outbox_id(conn, *a, **k):
    await conn.execute("insert outbox")
    return "ob-1"
