"""NX-20a — sesiune web semnată (HMAC) + cache control-plane + resolve_web_session.

ZERO DB/rețea reală: crypto pur + fake conn + clock monkeypatch-uit. Acoperă: round-trip
issue/verify, respingerea unei semnături falsificate/secret greșit/câmp gol, cache hit/TTL/
negative/evict, verify_web_session (cache + HMAC), query resolve_web_session.
"""

from src.db.queries import channels as channels_q
from src.web import session as sess
from src.web.session import SessionSecretCache, WebSession, issue_visitor, verify_sig

TOKEN = "pub_abc"
SECRET = "s3cr3t-de-test"


# --- crypto (issue / verify) -------------------------------------------------


def test_issue_then_verify_roundtrip():
    visitor_id, sig = issue_visitor(TOKEN, SECRET)
    assert visitor_id.startswith("web_")
    assert verify_sig(TOKEN, visitor_id, sig, SECRET) is True


def test_verify_rejects_tampered_visitor():
    _, sig = issue_visitor(TOKEN, SECRET)
    # semnătura emisă pentru un visitor_id, prezentată pentru ALTUL → invalid
    assert verify_sig(TOKEN, "web_altcineva", sig, SECRET) is False


def test_verify_rejects_wrong_secret():
    visitor_id, sig = issue_visitor(TOKEN, SECRET)
    assert verify_sig(TOKEN, visitor_id, sig, "alt-secret") is False


def test_verify_rejects_empty_fields():
    visitor_id, sig = issue_visitor(TOKEN, SECRET)
    assert verify_sig(TOKEN, visitor_id, "", SECRET) is False
    assert verify_sig("", visitor_id, sig, SECRET) is False
    assert verify_sig(TOKEN, visitor_id, sig, "") is False


# --- SessionSecretCache (clock controlat) ------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _patch_resolve(monkeypatch, results):
    calls = []

    async def fake(conn, token):
        calls.append(token)
        return results.get(token)

    monkeypatch.setattr(sess, "resolve_web_session", fake)
    return calls


async def test_cache_hit_avoids_second_query(monkeypatch):
    monkeypatch.setattr(sess.time, "monotonic", _Clock())
    calls = _patch_resolve(monkeypatch, {TOKEN: {"business_id": "b", "session_secret": SECRET}})
    cache = SessionSecretCache(ttl_s=60.0)
    r1 = await cache.get(None, TOKEN)
    r2 = await cache.get(None, TOKEN)
    assert r1 == r2 == {"business_id": "b", "session_secret": SECRET}
    assert calls == [TOKEN]  # al doilea get e servit din cache


async def test_cache_ttl_expiry_requeries(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(sess.time, "monotonic", clock)
    calls = _patch_resolve(monkeypatch, {TOKEN: {"business_id": "b", "session_secret": SECRET}})
    cache = SessionSecretCache(ttl_s=60.0)
    await cache.get(None, TOKEN)
    clock.t += 61  # peste TTL
    await cache.get(None, TOKEN)
    assert calls == [TOKEN, TOKEN]  # re-query după expirare


async def test_cache_negative_caches_miss(monkeypatch):
    monkeypatch.setattr(sess.time, "monotonic", _Clock())
    calls = _patch_resolve(monkeypatch, {})  # token necunoscut → None
    cache = SessionSecretCache(ttl_s=60.0)
    assert await cache.get(None, "pub_x") is None
    assert await cache.get(None, "pub_x") is None
    assert calls == ["pub_x"]  # miss-ul e cache-uit (anti-flood pe endpoint public)


async def test_cache_evicts_oldest_at_maxsize(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(sess.time, "monotonic", clock)
    _patch_resolve(monkeypatch, {})
    cache = SessionSecretCache(ttl_s=600.0, maxsize=2)
    await cache.get(None, "a")
    clock.t += 1
    await cache.get(None, "b")
    clock.t += 1
    await cache.get(None, "c")  # peste maxsize → evict cel mai vechi (a)
    assert "a" not in cache._store and "b" in cache._store and "c" in cache._store


# --- verify_web_session (cache + HMAC) ---------------------------------------


async def test_verify_web_session_valid(monkeypatch):
    monkeypatch.setattr(sess.time, "monotonic", _Clock())
    _patch_resolve(monkeypatch, {TOKEN: {"business_id": "biz-1", "session_secret": SECRET}})
    sess.get_session_cache.cache_clear()  # singleton proaspăt (TTL din settings)
    visitor_id, sig = issue_visitor(TOKEN, SECRET)
    out = await sess.verify_web_session(None, TOKEN, visitor_id, sig)
    assert isinstance(out, WebSession)
    assert out.business_id == "biz-1" and out.visitor_id == visitor_id and out.token == TOKEN


async def test_verify_web_session_unknown_token(monkeypatch):
    monkeypatch.setattr(sess.time, "monotonic", _Clock())
    _patch_resolve(monkeypatch, {})
    sess.get_session_cache.cache_clear()
    assert await sess.verify_web_session(None, "pub_x", "web_1", "sig") is None


async def test_verify_web_session_bad_sig(monkeypatch):
    monkeypatch.setattr(sess.time, "monotonic", _Clock())
    _patch_resolve(monkeypatch, {TOKEN: {"business_id": "biz-1", "session_secret": SECRET}})
    sess.get_session_cache.cache_clear()
    # token valid, dar semnătură care nu corespunde → None (fără oracol 403 vs 401)
    assert await sess.verify_web_session(None, TOKEN, "web_1", "deadbeef") is None


# --- resolve_web_session (query, fake conn) ----------------------------------


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.captured = None

    async def fetchrow(self, sql, *args):
        self.captured = args
        return self._row


async def test_resolve_web_session_returns_secret():
    conn = _FakeConn({"business_id": "biz-1", "session_secret": "sek", "identity_secret": "idk"})
    out = await channels_q.resolve_web_session(conn, "pub_abc")
    assert out == {"business_id": "biz-1", "session_secret": "sek", "identity_secret": "idk"}
    assert conn.captured == ("pub_abc",)  # public_token = $1 (P7: derivă tenantul)


async def test_resolve_web_session_identity_secret_optional():
    # NX-129: canal cu session_secret dar FĂRĂ identity_secret (login passthrough inactiv pe tenant)
    # → sesiune anonimă validă, identity_secret None (nu invalidează sesiunea).
    conn = _FakeConn({"business_id": "biz-1", "session_secret": "sek", "identity_secret": None})
    out = await channels_q.resolve_web_session(conn, "pub_abc")
    assert out == {"business_id": "biz-1", "session_secret": "sek", "identity_secret": None}


async def test_resolve_web_session_none_on_no_row():
    assert await channels_q.resolve_web_session(_FakeConn(None), "pub_x") is None


async def test_resolve_web_session_none_when_secret_missing():
    # canal seedat incomplet (fără session_secret) → miss grațios, nu o sesiune fără secret
    conn = _FakeConn({"business_id": "biz-1", "session_secret": None, "identity_secret": None})
    assert await channels_q.resolve_web_session(conn, "pub_abc") is None
