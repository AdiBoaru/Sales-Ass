"""NX-120 — DoS hardening: body-size cap, fail-CLOSED pe calea care cheltuie LLM (/web/chat),
cost-cap per business + per vizitator, verificare Origin la bootstrap. Fără rețea/DB reală:
fake redis (poate arunca RedisError) + seam-uri monkeypatch-uite."""

import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import time as _time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from src.web import app as wa
from src.web.app import WebChatIn, WebMessageIn
from src.web.security import (
    check_origin,
    normalize_allowlist,
    normalize_origin,
    origin_bucket,
    redact_secret,
    verify_demo_access,
    visitor_bucket,
)
from src.web.session import WebSession
from src.webhook.body_limit import enforce_body_cap
from src.worker.processor import TurnResult


async def _coro(value):
    return value


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


class _Req:
    def __init__(self, host="1.2.3.4", body=b"{}", headers=None):
        self.client = SimpleNamespace(host=host)
        self._body = body
        self.headers = headers if headers is not None else {"content-length": str(len(body))}

    async def stream(self):
        yield self._body


class _BodyReq:
    def __init__(self, *, content_length, chunks):
        self.headers = {} if content_length is None else {"content-length": str(content_length)}
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield c


class FakeRedis:
    async def incr(self, key):
        return 1  # sub orice prag

    async def expire(self, *a):
        return True

    async def get(self, key):
        return None

    async def incrbyfloat(self, key, amount):
        return amount


class RaisingRedis(FakeRedis):
    async def incr(self, key):
        raise RedisError("down")

    async def get(self, key):
        raise RedisError("down")


class OverBudgetRedis(FakeRedis):
    async def get(self, key):
        return "999"  # orice cheie de cost → peste plafon


# --- enforce_body_cap --------------------------------------------------------


async def test_body_cap_content_length_over_max():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=100, chunks=[b"x" * 100]), 50)
    assert ei.value.status_code == 413


async def test_body_cap_content_length_absent_rejected():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=None, chunks=[b"x"]), 50)
    assert ei.value.status_code == 413


async def test_body_cap_lying_content_length_caught_by_stream():
    # declară 10 (≤ max 20), dar trimite 30 → stream-limit prinde depășirea
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=10, chunks=[b"x" * 15, b"x" * 15]), 20)
    assert ei.value.status_code == 413


async def test_body_cap_negative_content_length_400():
    with pytest.raises(HTTPException) as ei:
        await enforce_body_cap(_BodyReq(content_length=-1, chunks=[b""]), 1000)
    assert ei.value.status_code == 400  # CL negativ = invalid (RFC), poate desincroniza proxy


async def test_body_cap_ok_returns_bytes():
    assert await enforce_body_cap(_BodyReq(content_length=5, chunks=[b"hello"]), 50) == b"hello"


async def test_body_cap_exactly_at_max_passes():
    assert await enforce_body_cap(_BodyReq(content_length=5, chunks=[b"hello"]), 5) == b"hello"


# --- web_rate_limited fail-closed vs fail-open -------------------------------


async def test_rate_limited_fail_closed_on_redis_error():
    assert await wa.web_rate_limited(RaisingRedis(), "t", "ip", "v", fail_closed=True) is True


async def test_rate_limited_fail_open_on_redis_error():
    assert await wa.web_rate_limited(RaisingRedis(), "t", "ip", "v", fail_closed=False) is False


# --- /web/chat: fail-CLOSED + cost-cap (handle_turn NEapelat) ----------------


def _setup_chat(monkeypatch, redis):
    called = {"handle_turn": False}

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None)

    async def fake_handle_turn(*a, **k):
        called["handle_turn"] = True
        return TurnResult("c", "ct", "t", "hi", None, reply=None, language="ro")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "handle_turn", fake_handle_turn)
    return called


def _chat_req():
    return WebChatIn(token="tok", visitor_id="web_1", sig="s", message="x")


async def test_web_chat_fail_closed_when_redis_down(monkeypatch):
    called = _setup_chat(monkeypatch, RaisingRedis())
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_chat_req(), _Req())
    assert ei.value.status_code == 429
    assert called["handle_turn"] is False  # pipeline-ul NU rulează (zero LLM)


async def test_web_chat_over_budget_429(monkeypatch):
    called = _setup_chat(monkeypatch, OverBudgetRedis())
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(_chat_req(), _Req())
    assert ei.value.status_code == 429
    assert called["handle_turn"] is False  # peste cost-cap → fără handle_turn


async def test_web_chat_under_budget_runs(monkeypatch):
    called = _setup_chat(monkeypatch, FakeRedis())
    await wa.web_chat(_chat_req(), _Req())
    assert called["handle_turn"] is True  # sub praguri + redis OK → pipeline rulează


# --- /web/messages: fail-OPEN păstrat ---------------------------------------


async def test_web_message_fail_open_on_redis_error(monkeypatch):
    async def fake_verify(*a):
        return WebSession(business_id="b", token="t", visitor_id="v")

    async def fake_enqueue(redis, event):
        return "x"

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "enqueue_inbound", fake_enqueue)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(RaisingRedis()))
    res = await wa.web_message(WebMessageIn(token="t", visitor_id="v", sig="s", text="hi"), _Req())
    assert res["accepted"] is True  # fail-OPEN: redis jos nu blochează ingestia ieftină


# --- /web/bootstrap: verificare Origin server-side --------------------------


def _settings_with_origins(origins, **over):
    """Stub de Settings pentru poarta de margine. Câmpurile NX-229 au valorile de producție
    (v2 OFF, poarta demo OFF) ca testele de origin să măsoare originul, nu altceva."""
    base = dict(
        web_cors_origins_list=origins,
        web_demo_access_enabled=False,
        web_rate_limit_window_s=60,
        web_bootstrap_rate_limit_max=10,
        web_session_v2_enabled=False,
        web_session_v2_required=False,
        web_session_ttl_s=43200,
        web_session_origin_binding=False,
        web_max_body_bytes=16384,
        web_demo_access_secret="",
        web_demo_access_issuer="",
        web_demo_access_audience="",
        web_demo_access_leeway_s=30,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _CountingRedis:
    """Redis minimal pentru limita de bootstrap: numără incrementele pe cheie."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, ttl):
        return True


async def test_bootstrap_origin_not_allowlisted_403(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))
    req = _origin_req("https://evil.example")
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", req)
    assert ei.value.status_code == 403  # respins înainte de a emite sesiunea


async def test_bootstrap_origin_allowlisted_ok(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_CountingRedis()))
    res = await wa.web_bootstrap("tok", _origin_req("https://shop.ro"))
    assert res["token"] == "tok"


async def test_bootstrap_no_origin_ok(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins(["https://shop.ro"]))

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_CountingRedis()))
    res = await wa.web_bootstrap("tok", _Req(headers={}))  # same-origin / non-browser → permis
    assert res["token"] == "tok"


async def test_bootstrap_cors_disabled_with_origin_403(monkeypatch):
    # NX-120 secure-by-default: allowlist GOL + Origin de browser → 403 (nu permitem orice origin).
    monkeypatch.setattr(wa, "get_settings", lambda: _settings_with_origins([]))
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", _origin_req("https://evil.example"))
    assert ei.value.status_code == 403


# --- middleware de body-size (respinge declarat-mare înainte de routing/parsing) ---


def test_request_size_middleware_rejects_oversized():
    from fastapi.testclient import TestClient

    from src.webhook.app import app as webhook_app

    client = TestClient(webhook_app)
    res = client.post("/webhook", content=b"x" * 262145)  # > 256KB cap grosier global
    assert res.status_code == 413  # respins de middleware, înainte de verificarea semnăturii


# ════════════════════════════════════════════════════════════════════════════════════════════
# NX-229 — normalizare de origin, poarta de acces demo, redactare.
# ════════════════════════════════════════════════════════════════════════════════════════════

_ALLOW = normalize_allowlist(["https://demo.nativextech.com"])


# --- normalizare -------------------------------------------------------------


def test_origin_default_port_is_canonical():
    """`https://x:443` si `https://x` sunt acelasi origin; un allowlist naiv le vede diferite."""
    assert normalize_origin("https://demo.nativextech.com:443") == "https://demo.nativextech.com"
    assert normalize_origin("http://localhost:80") == "http://localhost"


def test_origin_case_and_trailing_slash_normalized():
    assert normalize_origin("HTTPS://Demo.NativexTech.com/") == "https://demo.nativextech.com"


def test_origin_keeps_non_default_port():
    assert normalize_origin("http://localhost:5173") == "http://localhost:5173"


def test_origin_null_is_not_an_origin():
    """`null` e o valoare pe care browserele chiar o trimit (iframe sandboxed, file://)."""
    assert normalize_origin("null") is None
    assert normalize_origin("NULL") is None


def test_origin_with_path_or_query_is_rejected():
    """Un origin e scheme+host+port. Cu path ne uitam la un URL, iar un URL nu se compara cu un
    allowlist de origini."""
    assert normalize_origin("https://demo.nativextech.com/admin") is None
    assert normalize_origin("https://demo.nativextech.com?x=1") is None
    assert normalize_origin("https://demo.nativextech.com#frag") is None


def test_origin_with_userinfo_is_rejected():
    assert normalize_origin("https://user@demo.nativextech.com") is None


def test_origin_exotic_schemes_rejected():
    for raw in ["javascript://x", "data:text/html,x", "file:///etc/passwd", "ftp://x"]:
        assert normalize_origin(raw) is None, raw


def test_allowlist_drops_unparseable_entries():
    """Un origin scris gresit in config nu devine tacut «permite orice» — pur si simplu lipseste."""
    allow = normalize_allowlist(["https://ok.example", "nu e un origin", "null", ""])
    assert allow == frozenset({"https://ok.example"})


# --- policy ------------------------------------------------------------------


def test_absent_origin_is_allowed():
    """Requesturile non-browser n-au Origin; suprafata reala de abuz e browser-driven."""
    assert check_origin(None, _ALLOW) == (True, None)
    assert check_origin("   ", _ALLOW) == (True, None)


def test_exact_origin_allowed():
    assert check_origin("https://demo.nativextech.com", _ALLOW) == (True, None)


def test_equivalent_origin_allowed_after_normalization():
    assert check_origin("https://DEMO.nativextech.com:443/", _ALLOW) == (True, None)


def test_subdomain_is_not_the_same_origin():
    """`www.demo.x` != `demo.x`. Potrivirea e exacta, nu pe sufix."""
    ok, reason = check_origin("https://www.demo.nativextech.com", _ALLOW)
    assert not ok and reason == "origin_not_allowed"


def test_scheme_mismatch_rejected():
    ok, _ = check_origin("http://demo.nativextech.com", _ALLOW)
    assert not ok


def test_port_mismatch_rejected():
    ok, _ = check_origin("https://demo.nativextech.com:8443", _ALLOW)
    assert not ok


def test_null_origin_rejected_not_treated_as_absent():
    """Capcana: daca `null` s-ar normaliza la None si None ar insemna «absent», ar trece."""
    ok, reason = check_origin("null", _ALLOW)
    assert not ok and reason == "origin_malformed"


def test_empty_allowlist_rejects_every_browser_origin():
    ok, _ = check_origin("https://demo.nativextech.com", frozenset())
    assert not ok, "secure-by-default: fara allowlist nu se serveste niciun widget"


# --- poarta de acces demo ----------------------------------------------------

_DEMO_SECRET = "demo-secret"
_EQ = "=" * 1


def _seg(obj):
    return _b64.urlsafe_b64encode(_json.dumps(obj).encode()).decode().rstrip(_EQ)


def _jwt(payload, secret=_DEMO_SECRET, alg="HS256"):
    head, body = _seg({"alg": alg, "typ": "JWT"}), _seg(payload)
    mac = _hmac.new(secret.encode(), f"{head}.{body}".encode(), _hashlib.sha256).digest()
    return f"{head}.{body}.{_b64.urlsafe_b64encode(mac).decode().rstrip(_EQ)}"


def test_demo_access_valid():
    tok = _jwt({"sub": "user-1", "exp": int(_time.time()) + 600})
    assert verify_demo_access(f"Bearer {tok}", _DEMO_SECRET) == (True, None)


def test_demo_access_never_returns_a_subject():
    """Nucleul deciziei: poarta spune DACA treci, niciodata CINE esti.

    In v1 headerul sosea si nimeni nu-l valida; distanta pana la «userul logat e X» era o linie
    de cod. Semnatura tipului (bool, nu str) e ce face acea linie imposibil de scris."""
    tok = _jwt({"sub": "user-secret-ref", "exp": int(_time.time()) + 600})
    result = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET)
    assert result == (True, None)
    assert "user-secret-ref" not in repr(result)
    assert all(not isinstance(x, dict) for x in result)


def test_demo_access_missing_header():
    assert verify_demo_access(None, _DEMO_SECRET) == (False, "missing")
    assert verify_demo_access("", _DEMO_SECRET) == (False, "missing")


def test_demo_access_requires_bearer_scheme():
    tok = _jwt({"sub": "u", "exp": int(_time.time()) + 600})
    assert verify_demo_access(tok, _DEMO_SECRET) == (False, "malformed")
    assert verify_demo_access(f"Basic {tok}", _DEMO_SECRET) == (False, "malformed")


def test_demo_access_alg_none_rejected():
    """Atacul clasic pe JWT: `alg=none`."""
    tok = _jwt({"sub": "u", "exp": 9_000_000_000}, alg="none")
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET)
    assert not ok and reason == "bad_alg"


def test_demo_access_algorithm_confusion_rejected():
    tok = _jwt({"sub": "u", "exp": 9_000_000_000}, alg="RS256")
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET)
    assert not ok and reason == "bad_alg"


def test_demo_access_wrong_secret_rejected():
    tok = _jwt({"sub": "u", "exp": int(_time.time()) + 600}, secret="alt")
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET)
    assert not ok and reason == "bad_signature"


def test_demo_access_expired_rejected():
    tok = _jwt({"sub": "u", "exp": int(_time.time()) - 10_000})
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET)
    assert not ok and reason == "expired"


def test_demo_access_without_exp_rejected():
    """Fara `exp`, un token furat e valabil pe veci."""
    ok, reason = verify_demo_access(f"Bearer {_jwt({'sub': 'u'})}", _DEMO_SECRET)
    assert not ok and reason == "expired"


def test_demo_access_issuer_and_audience_enforced_when_configured():
    tok = _jwt({"sub": "u", "exp": int(_time.time()) + 600, "iss": "supabase", "aud": "web"})
    good = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET, issuer="supabase", audience="web")
    assert good == (True, None)
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET, issuer="altcineva")
    assert not ok and reason == "bad_issuer"
    ok, reason = verify_demo_access(f"Bearer {tok}", _DEMO_SECRET, audience="alt")
    assert not ok and reason == "bad_audience"


def test_demo_access_audience_accepts_list_form():
    """RFC 7519: `aud` poate fi string sau lista."""
    tok = _jwt({"sub": "u", "exp": int(_time.time()) + 600, "aud": ["web", "mobile"]})
    assert verify_demo_access(f"Bearer {tok}", _DEMO_SECRET, audience="web") == (True, None)


def test_demo_access_garbage_never_raises():
    for junk in ["Bearer", "Bearer ", "Bearer a.b", "Bearer !!!.???.###", "Bearer ..", "x y z"]:
        ok, reason = verify_demo_access(junk, _DEMO_SECRET)
        assert not ok and reason is not None


# --- redactare ---------------------------------------------------------------


def test_redact_never_echoes_the_secret():
    secret = "pub_830a2be74a2a60d48865de3bb7a6dc7a"
    out = redact_secret(secret)
    assert secret not in out and out.startswith("sha256:")
    assert redact_secret(secret) == out, "amprenta e stabila (corelabila intre aparitii)"


def test_origin_bucket_is_low_cardinality_and_opaque():
    """Multimea originilor RESPINSE e nemarginita si controlata de atacator: cruda intr-o metrica
    ar fi o explozie de cardinalitate, iar subdomeniul poate identifica tenantul."""
    bucket = origin_bucket("https://demo.nativextech.com")
    assert "nativextech" not in bucket
    assert origin_bucket("nu e origin") == "malformed"
    assert bucket == origin_bucket("https://DEMO.nativextech.com:443")


def test_visitor_bucket_hides_channel_pii():
    vid = "web_9f2c4e"
    assert vid not in visitor_bucket(vid)
    assert visitor_bucket(None) == "-"


# ════════════════════════════════════════════════════════════════════════════════════════════
# NX-229 — poarta aplicata UNIFORM pe endpointuri. In v1 originul se verifica DOAR la bootstrap,
# deci /chat (calea care cheltuie LLM), /messages si /stream erau descoperite.
# ════════════════════════════════════════════════════════════════════════════════════════════


def _origin_req(origin, **kw):
    """`_Req` cu Origin, PASTRAND `content-length` (altfel body-cap-ul raspunde 413 inaintea
    portii de origin si testul masoara alt lucru)."""
    return _Req(headers={"origin": origin, "content-length": "2"}, **kw)


def _edge_settings(**over):
    return _settings_with_origins(["https://demo.nativextech.com"], **over)


def _session_for(monkeypatch, business_id="biz-1", claims=None):
    async def fake_verify(token, vid, sig):
        return WebSession(business_id=business_id, token=token, visitor_id=vid, claims=claims)

    monkeypatch.setattr(wa, "_verify", fake_verify)


async def test_chat_rejects_disallowed_origin_before_spending(monkeypatch):
    """Cea mai importanta lacuna a v1: /chat n-avea NICIO verificare de origin, iar tokenul public
    e public prin definitie (traieste in bundle-ul site-ului)."""
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())
    _session_for(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(
            WebChatIn(token="t", visitor_id="v", sig="s", message="hi"),
            _origin_req("https://evil.example"),
        )
    assert ei.value.status_code == 403


async def test_messages_rejects_disallowed_origin(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())
    _session_for(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await wa.web_message(
            WebMessageIn(token="t", visitor_id="v", sig="s", text="hi"),
            _origin_req("https://evil.example"),
        )
    assert ei.value.status_code == 403


async def test_stream_rejects_disallowed_origin(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())
    _session_for(monkeypatch)
    req = SimpleNamespace(
        headers={"origin": "https://evil.example"}, client=SimpleNamespace(host="1.1.1.1")
    )
    with pytest.raises(HTTPException) as ei:
        await wa.web_stream("t", "v", "s", req, last_event_id=None)
    assert ei.value.status_code == 403


async def test_session_bound_to_origin_cannot_be_used_elsewhere(monkeypatch):
    """O sesiune v2 legata de o pagina nu poate fi refolosita de pe alta, chiar daca ambele
    origini sunt in allowlist."""
    from src.web.session import SessionClaims

    monkeypatch.setattr(
        wa,
        "get_settings",
        lambda: _settings_with_origins(
            ["https://demo.nativextech.com", "https://alt.nativextech.com"]
        ),
    )
    _session_for(
        monkeypatch,
        claims=SessionClaims(
            visitor_id="v",
            issued_at=0,
            expires_at=9_000_000_000,
            key_id="k",
            key_age="current",
            origin="https://demo.nativextech.com",
        ),
    )
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(
            WebChatIn(token="t", visitor_id="v", sig="s", message="hi"),
            _origin_req("https://alt.nativextech.com"),
        )
    assert ei.value.status_code == 403


async def test_bootstrap_rate_limited_after_burst(monkeypatch):
    """Emiterea de sesiuni era NELIMITATA: un atacator putea coase oricate visitor_id-uri
    proaspete ca sa ocoleasca limita per-visitor de pe /chat."""
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings(web_bootstrap_rate_limit_max=2))

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    redis = _CountingRedis()
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    for _ in range(2):
        await wa.web_bootstrap("tok", _Req())
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", _Req())
    assert ei.value.status_code == 429


async def test_bootstrap_rate_limit_key_has_no_raw_ip(monkeypatch):
    """IP-ul e PII de retea: intra hash-uit in cheie, nu in clar."""
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    redis = _CountingRedis()
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    await wa.web_bootstrap("tok", _Req())
    assert redis.counts, "limita trebuie sa fi incrementat ceva"
    assert all("1.1.1.1" not in k for k in redis.counts)


async def test_bootstrap_fails_closed_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())

    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(RaisingRedis()))
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", _Req())
    assert ei.value.status_code == 429


async def test_per_channel_allowlist_narrows_the_global_one(monkeypatch):
    """Originile unui tenant n-au ce cauta in poarta altuia."""
    monkeypatch.setattr(
        wa,
        "get_settings",
        lambda: _settings_with_origins(
            ["https://demo.nativextech.com", "https://alt.nativextech.com"]
        ),
    )

    async def fake_resolve(token):
        return {
            "business_id": "b",
            "session_secret": "sek",
            "allowed_origins": "https://demo.nativextech.com",
        }

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_CountingRedis()))
    # `alt` e in globalul procesului, dar NU e originul acestui canal.
    with pytest.raises(HTTPException) as ei:
        await wa.web_bootstrap("tok", _origin_req("https://alt.nativextech.com"))
    assert ei.value.status_code == 403


async def test_demo_access_gate_blocks_when_enabled(monkeypatch):
    monkeypatch.setattr(
        wa,
        "get_settings",
        lambda: _edge_settings(
            web_demo_access_enabled=True,
            web_demo_access_secret="demo-secret",
            web_demo_access_issuer="",
            web_demo_access_audience="",
            web_demo_access_leeway_s=30,
        ),
    )
    _session_for(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await wa.web_chat(
            WebChatIn(token="t", visitor_id="v", sig="s", message="hi"),
            _origin_req("https://demo.nativextech.com"),
        )
    assert ei.value.status_code == 401, "fara Authorization valid, poarta demo inchide"


async def test_demo_access_gate_is_off_by_default(monkeypatch):
    """OFF by default → comportament byte-identic cu v1 pana cand se aprinde deliberat."""
    monkeypatch.setattr(wa, "get_settings", lambda: _edge_settings())
    assert wa._enforce_demo_access(_Req(headers={})) is None


async def test_session_rejection_never_logs_the_token(monkeypatch, caplog):
    """Log capture: zero token/sig in clar. Un secret intr-un log e un secret pierdut."""
    import logging as _logging

    secret_token = "pub_830a2be74a2a60d48865de3bb7a6dc7a"

    class _Cache:
        async def get(self, conn, token):
            return None

    from src.web import session as ws

    monkeypatch.setattr(ws, "get_session_cache", lambda: _Cache())

    @asynccontextmanager
    async def fake_admin(pool):
        yield object()

    monkeypatch.setattr(wa, "admin_conn", fake_admin)
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(object()))
    with caplog.at_level(_logging.INFO):
        out = await wa._verify(secret_token, "web_1", "sig-secret")
    assert out is None
    assert secret_token not in caplog.text
    assert "sig-secret" not in caplog.text
    assert "web_session_rejected" in caplog.text
