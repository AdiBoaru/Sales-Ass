"""NX-221 — serializare ture pe `/web/chat` + conflictul de stare nu mai pierde reply-ul.

REPRODUCERE (comportamentul VECHI, documentat — pasul 0 din card): fără lock, două
`web_chat` concurente pe aceeași conversație rulează două pipeline-uri pe ACELAȘI snapshot
de stare; turul care pierde optimistic lock-ul (`state_version`) arunca `StateConflict`
din interiorul tranzacției care îi scrisese deja reply-ul → rollback: clientul al doilea
primea excepție (500) și NICIUN răspuns. Documentat de
`test_lock_off_concurrent_conflict_documents_old_behavior` (kill-switch OFF = azi) și de
`test_conflict_retried_reply_kept` (roșu pe main: StateConflict propaga din handle_turn).

ZERO Redis/DB/OpenAI real: fake redis (SET NX PX + eval compare-del, pattern
test_conversation_lock) + stub-uri DB (pattern test_processor_state / test_web_gateway).
"""

import asyncio
import time
from types import SimpleNamespace

from redis.exceptions import RedisError

from src.config import get_settings
from src.db.provider import static_db
from src.db.queries.conversations import StateConflict
from src.models import BusinessConfig, Contact, Reply
from src.web import app as wa
from src.web.app import WebChatIn
from src.web.session import WebSession
from src.worker import processor as proc
from src.worker import turn_uow as uow
from src.worker.processor import TurnResult, handle_turn
from src.worker.turn_lock import acquire_turn_lock, release_turn_lock, turn_lock_key

BIZ = "biz-1"


class FakeLockRedis:
    """Fake Redis pt lock (SET NX PX cu expirare reală pe monotonic + eval compare-del) +
    metodele minime cerute de web_chat (rate limit incr, cost guard get/incrbyfloat)."""

    def __init__(self, fail_set=False):
        self.store: dict = {}  # key -> (value, expires_at | None)
        self.fail_set = fail_set  # injectează „Redis jos" DOAR pe SET (lock-ul)
        self.counters: dict = {}
        self.kv: dict = {}

    def alive(self, key):
        v = self.store.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and time.monotonic() >= exp:
            del self.store[key]
            return None
        return val

    async def set(self, key, value, nx=False, px=None, ex=None):
        if self.fail_set:
            raise RedisError("redis down")
        if nx and self.alive(key) is not None:
            return None
        ttl_s = px / 1000.0 if px else ex
        self.store[key] = (value, time.monotonic() + ttl_s if ttl_s else None)
        return True

    async def eval(self, script, numkeys, key, arg):
        if self.alive(key) == arg:
            del self.store[key]
            return 1
        return 0

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        v = self.kv.get(key)
        return str(v) if v is not None else None

    async def incrbyfloat(self, key, amount):
        self.kv[key] = float(self.kv.get(key, 0)) + float(amount)
        return self.kv[key]


# --- primitive acquire/release (unit) ----------------------------------------


async def test_acquire_free_lock_instant_no_wait():
    r = FakeLockRedis()
    lock = await acquire_turn_lock(r, BIZ, "webchat:t:v", ttl_ms=15000, wait_max_ms=1000)
    assert lock.acquired is True
    assert lock.waited_ms == 0.0  # prima încercare = zero AȘTEPTARE (nu emitem turn_lock_wait)
    assert r.alive(turn_lock_key(BIZ, "webchat:t:v")) == lock.token


async def test_second_acquire_waits_until_release():
    r = FakeLockRedis()
    first = await acquire_turn_lock(r, BIZ, "k", ttl_ms=15000, wait_max_ms=1000)

    async def release_soon():
        await asyncio.sleep(0.08)
        await release_turn_lock(r, first)

    task = asyncio.create_task(release_soon())
    second = await acquire_turn_lock(r, BIZ, "k", ttl_ms=15000, wait_max_ms=2000)
    await task
    assert second.acquired is True
    assert second.waited_ms > 0  # a stat după primul → telemetrie reală de contenție


async def test_wait_max_exceeded_bypass_keeps_holder_lock():
    r = FakeLockRedis()
    holder = await acquire_turn_lock(r, BIZ, "k", ttl_ms=60000, wait_max_ms=1000)
    second = await acquire_turn_lock(r, BIZ, "k", ttl_ms=60000, wait_max_ms=120)
    assert second.acquired is False  # bypass: procesăm oricum (principiul 6)
    assert second.waited_ms >= 100  # a așteptat fereastra întreagă
    # release pe bypass = no-op — NU șterge lock-ul deținătorului
    await release_turn_lock(r, second)
    assert r.alive(turn_lock_key(BIZ, "k")) == holder.token


async def test_ttl_expiry_unblocks_next_turn():
    # proces mort cu lock-ul luat → TTL-ul (plasa anti-deadlock) expiră → turul următor intră
    r = FakeLockRedis()
    await acquire_turn_lock(r, BIZ, "k", ttl_ms=60, wait_max_ms=1000)  # deținător „mort"
    second = await acquire_turn_lock(r, BIZ, "k", ttl_ms=15000, wait_max_ms=1000)
    assert second.acquired is True


async def test_redis_error_bypass_never_raises():
    r = FakeLockRedis(fail_set=True)
    lock = await acquire_turn_lock(r, BIZ, "k", ttl_ms=15000, wait_max_ms=1000)
    assert lock.acquired is False  # Redis jos → bypass, NU excepție spre client


async def test_no_contention_across_conversations_or_tenants():
    r = FakeLockRedis()
    l1 = await acquire_turn_lock(r, BIZ, "webchat:t:v1", ttl_ms=15000, wait_max_ms=1000)
    l2 = await acquire_turn_lock(r, BIZ, "webchat:t:v2", ttl_ms=15000, wait_max_ms=1000)
    l3 = await acquire_turn_lock(r, "biz-2", "webchat:t:v1", ttl_ms=15000, wait_max_ms=1000)
    assert all(lk.acquired and lk.waited_ms == 0.0 for lk in (l1, l2, l3))


# --- integrare /web/chat (serializare + bypass + kill-switch) -----------------


class _Req:
    def __init__(self, host="1.2.3.4", body=b"{}"):
        self.client = SimpleNamespace(host=host)
        self._body = body
        self.headers = {"content-length": str(len(body))}

    async def stream(self):
        yield self._body


async def _coro(value):
    return value


class _FakeCm:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class ConvStore:
    """Conversația partajată: state + state_version (optimistic lock, ca în DB)."""

    def __init__(self):
        self.state: dict = {}
        self.version = 0


def _wire_web_chat(monkeypatch, redis, store, *, work_s=0.05):
    """Montează web_chat pe stub-uri + un handle_turn care REPRODUCE semantica reală:
    citește snapshot (state, version) la început, „lucrează" (sleep = fereastra de cursă),
    apoi scrie cu optimistic lock — versiune schimbată → StateConflict (comportamentul de
    pe main, unde conflictul omora tot turul). Întoarce jurnalele (seen/replies/events)."""
    seen: list = []  # (body, displayed_products văzute la începutul turului)
    replies: list = []
    events: list = []  # envelope-urile primite de handle_turn (telemetria de lock)

    async def fake_verify(token, vid, sig):
        return WebSession(business_id=BIZ, token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": BIZ}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None)

    async def fake_handle_turn(
        conn, business, channel_id, event, *, redis=None, deliver=True, defer_aftercare=False
    ):
        events.append(event)
        snap_state, snap_version = dict(store.state), store.version
        seen.append((event["body"], snap_state.get("displayed_products")))
        await asyncio.sleep(work_s)  # pipeline-ul rulează → fereastra de cursă
        if store.version != snap_version:  # optimistic lock (patch_conversation_state)
            raise StateConflict(f"state_version != {snap_version}")
        store.state = {**snap_state, "displayed_products": [event["body"]]}
        store.version += 1
        reply = Reply(text=f"raspuns:{event['body']}")
        replies.append(reply.text)
        return TurnResult("conv", "ct", "turn", reply.text, None, reply=reply, language="ro")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _FakeCm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _FakeCm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)
    return seen, replies, events


def _chat(body):
    return WebChatIn(token="tok", visitor_id="web_1", sig="s", message=body)


async def test_lock_off_concurrent_conflict_documents_old_behavior(monkeypatch):
    """REPRO pasul 0 (kill-switch OFF = comportamentul de pe main, byte-identic): două
    cereri concurente pe aceeași conversație → al doilea tur moare cu StateConflict
    (rollback → reply pierdut → 500 la client). Exact bug-ul pe care NX-221 îl închide."""
    monkeypatch.setattr(get_settings(), "web_turn_lock_enabled", False)
    store = ConvStore()
    r = FakeLockRedis()
    seen, replies, _ = _wire_web_chat(monkeypatch, r, store)

    results = await asyncio.gather(
        wa.web_chat(_chat("serum ten gras"), _Req()),
        wa.web_chat(_chat("sub 100 lei"), _Req()),
        return_exceptions=True,
    )

    errors = [x for x in results if isinstance(x, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], StateConflict)  # un client FĂRĂ răspuns
    assert len(replies) == 1  # doar un tur a apucat să scrie
    assert store.version == 1  # starea celuilalt s-a pierdut
    assert not any(k.startswith("turnlock:") for k in r.store)  # OFF → zero chei de lock


async def test_lock_on_serializes_both_replies(monkeypatch):
    """DoD: cu lock ON, rafala se serializează — ambele tururi primesc reply,
    state_version avansează de 2×, al doilea tur VEDE ce a scris primul."""
    store = ConvStore()
    r = FakeLockRedis()
    seen, replies, events = _wire_web_chat(monkeypatch, r, store)

    res1, res2 = await asyncio.gather(
        wa.web_chat(_chat("serum ten gras"), _Req()),
        wa.web_chat(_chat("sub 100 lei"), _Req()),
    )

    assert "raspuns:" in res1["content"] and "raspuns:" in res2["content"]  # ambele au reply
    assert store.version == 2  # ambele patch-uri au prins (serializat, nu conflict)
    first_body, second_body = seen[0][0], seen[1][0]
    assert seen[0][1] is None  # primul tur pornește pe stare goală
    assert seen[1][1] == [first_body]  # al doilea VEDE displayed_products scrise de primul
    assert second_body != first_body
    # telemetria: exact turul care a stat după primul are turn_lock_wait_ms > 0
    waits = [e.get("turn_lock_wait_ms") for e in events]
    assert sum(1 for w in waits if w) == 1
    assert not any(e.get("turn_lock_bypass") for e in events)
    assert not any(k.startswith("turnlock:") for k in r.store)  # release în finally


async def test_lock_timeout_bypass_reply_still_sent(monkeypatch):
    """Lock ținut peste wait_max (proces străin) → bypass cu event, reply-ul pleacă."""
    monkeypatch.setattr(get_settings(), "turn_lock_wait_max_ms", 100)
    store = ConvStore()
    r = FakeLockRedis()
    # lock deja deținut de un proces străin, cu TTL mare
    await r.set(turn_lock_key(BIZ, "webchat:tok:web_1"), "alt-proces", nx=True, px=60000)
    _, replies, events = _wire_web_chat(monkeypatch, r, store, work_s=0.0)

    res = await wa.web_chat(_chat("salut"), _Req())

    assert "raspuns:salut" in res["content"]  # niciodată tăcere: reply-ul pleacă
    assert events[0].get("turn_lock_bypass") is True
    assert r.alive(turn_lock_key(BIZ, "webchat:tok:web_1")) == "alt-proces"  # nu i-am șters lock-ul


async def test_lock_redis_down_bypass_reply_still_sent(monkeypatch):
    """Redis indisponibil la acquire (DOAR pe SET — rate limit/cost guard funcționează) →
    bypass cu event + procesare, NU excepție spre client (principiul 6)."""
    store = ConvStore()
    r = FakeLockRedis(fail_set=True)
    _, replies, events = _wire_web_chat(monkeypatch, r, store, work_s=0.0)

    res = await wa.web_chat(_chat("salut"), _Req())

    assert "raspuns:salut" in res["content"]
    assert events[0].get("turn_lock_bypass") is True


# --- processor: StateConflict nu mai omoară reply-ul (fix 2) ------------------


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()


async def _run_processor(
    monkeypatch, *, conflicts: int, fresh_state: dict | None = None, event_extra: dict | None = None
):
    """handle_turn cu DB stubbed (pattern test_processor_state); patch_conversation_state
    aruncă StateConflict la primele `conflicts` apeluri, apoi reușește. Întoarce
    (TurnResult, apelurile de patch, evenimentele persistate)."""
    patch_calls: list = []
    persisted: list = []

    async def fake_conv(*a, **k):
        return {
            "id": "conv",
            "state": {},
            "state_version": 0,
            "locale": "ro",
            "bot_active": True,
        }

    async def fake_patch(conn, business_id, conv_id, new_state, version, **k):
        patch_calls.append((new_state, version))
        if len(patch_calls) <= conflicts:
            raise StateConflict(f"state_version != {version}")
        return version + 1

    async def fake_fresh(conn, business_id, conv_id):
        return (dict(fresh_state or {}), 7)

    async def fake_persist(conn, business_id, conv_id, contact_id, events):
        persisted.extend(events)

    async def anoop(*a, **k):
        return None

    async def fake_contact(*a, **k):
        return Contact(id="c", business_id=BIZ)

    async def fake_claim(*a, **k):
        return True

    async def fake_insert_msg(*a, **k):
        return "msg-id"

    async def fake_outbox(*a, **k):
        return "outbox-1"

    async def stage(ctx, deps):
        ctx.state.constraints["budget_max"] = "100"
        ctx.set_reply("raspunsul turului")

    monkeypatch.setattr(uow, "claim_inbound", fake_claim)
    monkeypatch.setattr(uow, "mark_inbound_completed", anoop)
    monkeypatch.setattr(uow, "get_or_create_contact", fake_contact)
    monkeypatch.setattr(uow, "get_or_create_conversation", fake_conv)
    monkeypatch.setattr(uow, "insert_message", fake_insert_msg)
    monkeypatch.setattr(uow, "touch_last_inbound", anoop)
    monkeypatch.setattr(uow, "get_recent_messages", anoop)
    monkeypatch.setattr(uow, "get_summary_for_context", anoop)
    monkeypatch.setattr(uow, "fetch_relevant_facts", anoop)
    monkeypatch.setattr(uow, "enqueue_outbox", fake_outbox)
    monkeypatch.setattr(uow, "patch_conversation_state", fake_patch)
    # raising=False: pe main funcția nu există → repro-ul rămâne rulabil pe codul vechi
    monkeypatch.setattr(uow, "get_state_and_version", fake_fresh, raising=False)
    monkeypatch.setattr(proc, "persist_events", fake_persist)
    monkeypatch.setattr(proc, "_record_turn_cost", anoop)
    monkeypatch.setattr(proc, "_llm_within_budget", anoop)
    monkeypatch.setattr(proc, "run_aftercare", anoop)

    business = BusinessConfig(id=BIZ, slug="s", name="n")
    event = {
        "channel_kind": "webchat",
        "sender_external_id": "web_1",
        "provider_msg_id": "m1",
        "content_type": "text",
        "body": "salut",
        **(event_extra or {}),
    }
    result = await handle_turn(static_db(_FakeConn()), business, "chan-1", event, stages=[stage])
    return result, patch_calls, persisted


def _types(persisted):
    return [e.type for e in persisted]


async def test_conflict_retried_reply_kept(monkeypatch):
    """REGRESIA fixului 2 (roșu pe main: StateConflict propaga din TX → reply pierdut):
    conflict la primul patch → re-read stare proaspătă + retry 1× → reply-ul turului
    supraviețuiește, patch-ul se re-aplică pe versiunea/starea PROASPĂTĂ (nu pe snapshot)."""
    result, patch_calls, persisted = await _run_processor(
        monkeypatch, conflicts=1, fresh_state={"cheia_castigatorului": "ramane"}
    )
    assert result.reply_text == "raspunsul turului"  # reply-ul NU s-a pierdut
    assert len(patch_calls) == 2  # original + exact UN retry
    retry_state, retry_version = patch_calls[1]
    assert retry_version == 7  # versiunea RE-CITITĂ, nu cea stale (0)
    assert retry_state["cheia_castigatorului"] == "ramane"  # construit pe starea proaspătă
    assert retry_state["constraints"]["budget_max"] == "100"  # deltele turului re-aplicate
    assert "state_conflict_retried" in _types(persisted)
    assert "state_conflict_dropped" not in _types(persisted)


async def test_double_conflict_drops_patch_keeps_reply(monkeypatch):
    """Conflict și la retry → pierdem PATCH-ul de stare, NU răspunsul: reply intact,
    `state_conflict_dropped` emis, nicio excepție spre apelant (web ar da 500)."""
    result, patch_calls, persisted = await _run_processor(monkeypatch, conflicts=2)
    assert result.reply_text == "raspunsul turului"  # textul răspunsului nealterat
    assert len(patch_calls) == 2  # UN singur retry, nu buclă
    assert "state_conflict_dropped" in _types(persisted)
    assert "state_conflict_retried" not in _types(persisted)


async def test_no_conflict_no_conflict_events(monkeypatch):
    """Calea fericită rămâne neschimbată: un singur patch, zero evenimente de conflict."""
    result, patch_calls, persisted = await _run_processor(monkeypatch, conflicts=0)
    assert result.reply_text == "raspunsul turului"
    assert len(patch_calls) == 1
    assert "state_conflict_retried" not in _types(persisted)
    assert "state_conflict_dropped" not in _types(persisted)


async def test_turn_lock_wait_event_from_envelope(monkeypatch):
    """Telemetria de lock măsurată la margine → emisă pe traiectoria turului (P10)."""
    _, _, persisted = await _run_processor(
        monkeypatch, conflicts=0, event_extra={"turn_lock_wait_ms": 42.0}
    )
    waits = [e for e in persisted if e.type == "turn_lock_wait"]
    assert len(waits) == 1 and waits[0].properties["wait_ms"] == 42.0


async def test_turn_lock_bypass_event_from_envelope(monkeypatch):
    _, _, persisted = await _run_processor(
        monkeypatch,
        conflicts=0,
        event_extra={"turn_lock_bypass": True, "turn_lock_wait_ms": 120.0},
    )
    bypasses = [e for e in persisted if e.type == "turn_lock_bypass"]
    assert len(bypasses) == 1 and bypasses[0].properties["waited_ms"] == 120.0
    assert not [e for e in persisted if e.type == "turn_lock_wait"]  # bypass, nu wait


async def test_no_lock_telemetry_no_events(monkeypatch):
    _, _, persisted = await _run_processor(monkeypatch, conflicts=0)
    assert not [e for e in persisted if e.type in ("turn_lock_wait", "turn_lock_bypass")]
