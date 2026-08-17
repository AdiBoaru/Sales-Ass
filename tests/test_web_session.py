"""NX-20a — sesiune web semnată (HMAC) + cache control-plane + resolve_web_session.

ZERO DB/rețea reală: crypto pur + fake conn + clock monkeypatch-uit. Acoperă: round-trip
issue/verify, respingerea unei semnături falsificate/secret greșit/câmp gol, cache hit/TTL/
negative/evict, verify_web_session (cache + HMAC), query resolve_web_session.
"""

import time as _time

from src.db.queries import channels as channels_q
from src.web import session as sess
from src.web.session import (
    SessionSecretCache,
    WebSession,
    is_v2_sig,
    issue_session_v2,
    issue_visitor,
    key_id_for,
    sign_session_v2,
    verify_session_v2,
    verify_sig,
)

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
    conn = _FakeConn(
        {
            "business_id": "biz-1",
            "session_secret": "sek",
            "session_secret_prev": None,
            "identity_secret": "idk",
            "allowed_origins": None,
            "default_locale": "ro",
        }
    )
    out = await channels_q.resolve_web_session(conn, "pub_abc")
    assert out == {
        "business_id": "biz-1",
        "session_secret": "sek",
        "session_secret_prev": None,
        "identity_secret": "idk",
        "allowed_origins": None,
        # NX-244: limba tenantului, pentru copy-ul de shell servit la bootstrap (D3).
        "default_locale": "ro",
    }
    assert conn.captured == ("pub_abc",)  # public_token = $1 (P7: derivă tenantul)


async def test_resolve_web_session_carries_rotation_and_origins():
    """NX-229: cheia precedentă și allowlistul per-canal ajung la marginea web.

    Fără ele, rotația n-are overlap (toată lumea deconectată deodată) și allowlistul rămâne
    global — adică originile unui tenant s-ar aplica altuia."""
    conn = _FakeConn(
        {
            "business_id": "biz-1",
            "session_secret": "new",
            "session_secret_prev": "old",
            "identity_secret": None,
            "allowed_origins": "https://demo.nativextech.com",
            "default_locale": "ro",
        }
    )
    out = await channels_q.resolve_web_session(conn, "pub_abc")
    assert out["session_secret_prev"] == "old"
    assert out["allowed_origins"] == "https://demo.nativextech.com"


async def test_resolve_web_session_identity_secret_optional():
    # NX-129: canal cu session_secret dar FĂRĂ identity_secret (login passthrough inactiv pe tenant)
    # → sesiune anonimă validă, identity_secret None (nu invalidează sesiunea).
    conn = _FakeConn(
        {
            "business_id": "biz-1",
            "session_secret": "sek",
            "session_secret_prev": None,
            "identity_secret": None,
            "allowed_origins": None,
            "default_locale": "ro",
        }
    )
    out = await channels_q.resolve_web_session(conn, "pub_abc")
    assert out["identity_secret"] is None
    assert out["session_secret"] == "sek"


async def test_resolve_web_session_none_on_no_row():
    assert await channels_q.resolve_web_session(_FakeConn(None), "pub_x") is None


async def test_resolve_web_session_none_when_secret_missing():
    # canal seedat incomplet (fără session_secret) → miss grațios, nu o sesiune fără secret
    conn = _FakeConn({"business_id": "biz-1", "session_secret": None, "identity_secret": None})
    assert await channels_q.resolve_web_session(conn, "pub_abc") is None


# ════════════════════════════════════════════════════════════════════════════════════════════
# NX-229 — sesiune v2: claims semnate, expirare, key id, rotatie dual-key, origin binding.
# ════════════════════════════════════════════════════════════════════════════════════════════

_SEC = "secret-curent"
_OLD = "secret-vechi"
_KEYS = {"current": _SEC, "previous": _OLD}


def _ok(sig, token="tok", vid=None, **kw):
    return verify_session_v2(token, vid, sig, secrets=_KEYS, **kw)


async def test_v2_roundtrip_is_valid():
    vid, sig = issue_session_v2("tok", _SEC, ttl_s=3600)
    claims, reason = _ok(sig, vid=vid)
    assert reason is None
    assert claims.visitor_id == vid
    assert claims.key_age == "current"
    assert claims.expires_at > claims.issued_at


async def test_v2_sig_is_detectable_and_v1_is_not():
    _, v2 = issue_session_v2("tok", _SEC, ttl_s=60)
    assert is_v2_sig(v2)
    _, v1 = issue_visitor("tok", _SEC)
    assert not is_v2_sig(v1)


async def test_v2_expires():
    """v1 nu expira NICIODATA. Asta e intreg motivul cardului."""
    vid = "web_x"
    sig = sign_session_v2("tok", vid, _SEC, ttl_s=10, now=int(_time.time()) - 10_000)
    claims, reason = _ok(sig, vid=vid)
    assert claims is None and reason == "expired"


async def test_v2_rejects_issued_in_future():
    """Un `iat` din viitor inseamna ceas stricat sau claims fabricate; nici una nu e acceptabila."""
    vid = "web_x"
    sig = sign_session_v2("tok", vid, _SEC, ttl_s=3600, now=int(_time.time()) + 10_000)
    claims, reason = _ok(sig, vid=vid)
    assert claims is None and reason == "issued_in_future"


async def test_v2_tolerates_small_clock_skew():
    vid = "web_x"
    sig = sign_session_v2("tok", vid, _SEC, ttl_s=1, now=int(_time.time()) - 30)
    claims, reason = _ok(sig, vid=vid)
    assert reason is None, "30s de drift intre doua masini nu e un atac"


async def test_v2_wrong_key_rejected():
    vid, sig = issue_session_v2("tok", "alt-secret-complet", ttl_s=3600)
    claims, reason = _ok(sig, vid=vid)
    assert claims is None and reason == "unknown_key"


async def test_v2_previous_key_still_verifies_during_overlap():
    """Rotatie: cheia noua semneaza, noua+vechea verifica. Altfel rotatia deconecteaza pe toti."""
    vid, sig = issue_session_v2("tok", _OLD, ttl_s=3600)
    claims, reason = _ok(sig, vid=vid)
    assert reason is None
    assert claims.key_age == "previous"
    assert claims.key_id == key_id_for(_OLD)


async def test_v2_key_id_identifies_the_secret():
    assert key_id_for(_SEC) != key_id_for(_OLD)
    assert key_id_for(_SEC) == key_id_for(_SEC)
    assert _SEC not in key_id_for(_SEC), "key id-ul nu are voie sa expuna secretul"


async def test_v2_tampered_claims_rejected():
    """Modificarea claims-urilor invalideaza MAC-ul: nu se poate prelungi expirarea."""
    import base64 as _b64
    import json as _json

    vid, sig = issue_session_v2("tok", _SEC, ttl_s=1)
    _, claims_b64, mac = sig.split(".")
    claims = _json.loads(_b64.urlsafe_b64decode(claims_b64 + "=" * (-len(claims_b64) % 4)))
    claims["exp"] = int(_time.time()) + 999_999
    forged_b64 = (
        _b64.urlsafe_b64encode(_json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    got, reason = _ok(f"v2.{forged_b64}.{mac}", vid=vid)
    assert got is None and reason in {"bad_signature", "unknown_key"}


async def test_v2_cross_tenant_token_swap_rejected():
    """Codex attack #1: refoloseste sesiunea cu ALT public token.

    Chiar daca atacatorul are o semnatura valida pentru tenantul A, amprenta tokenului din claims
    n-o lasa sa treaca prezentata cu tokenul tenantului B."""
    vid, sig = issue_session_v2("tok-tenant-A", _SEC, ttl_s=3600)
    claims, reason = verify_session_v2("tok-tenant-B", vid, sig, secrets=_KEYS)
    assert claims is None and reason == "token_mismatch"


async def test_v2_visitor_swap_rejected():
    vid, sig = issue_session_v2("tok", _SEC, ttl_s=3600)
    claims, reason = _ok(sig, vid="web_altcineva")
    assert claims is None and reason == "visitor_mismatch"


async def test_v2_origin_binding_rejects_other_page():
    vid, sig = issue_session_v2("tok", _SEC, ttl_s=3600, origin="https://demo.nativextech.com")
    claims, reason = _ok(sig, vid=vid, origin="https://evil.example")
    assert claims is None and reason == "origin_mismatch"


async def test_v2_origin_binding_accepts_same_page():
    vid, sig = issue_session_v2("tok", _SEC, ttl_s=3600, origin="https://demo.nativextech.com")
    claims, reason = _ok(sig, vid=vid, origin="https://demo.nativextech.com")
    assert reason is None and claims.origin == "https://demo.nativextech.com"


async def test_v2_unbound_session_refused_when_binding_required():
    vid, sig = issue_session_v2("tok", _SEC, ttl_s=3600)  # fara origin
    claims, reason = _ok(
        sig, vid=vid, origin="https://demo.nativextech.com", require_origin_match=True
    )
    assert claims is None and reason == "origin_unbound"


async def test_v1_sig_is_not_accepted_by_v2_verifier():
    vid, v1 = issue_visitor("tok", _SEC)
    claims, reason = _ok(v1, vid=vid)
    assert claims is None and reason == "not_v2"


async def test_v2_garbage_never_raises():
    """Input ostil nu are voie sa produca o exceptie: ar fi un DoS de o linie."""
    for junk in ["v2.", "v2.a.b.c", "v2.!!!.???", "v2..", "v2.e30.", "v2.bm90anNvbg.x"]:
        claims, reason = _ok(junk, vid="web_x")
        assert claims is None and reason is not None
