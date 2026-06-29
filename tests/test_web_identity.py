"""NX-129 — verificarea JWT host-signed (HS256) pt login passthrough web + wiring-ul de margine.

Crypto pur (stdlib), zero DB/rețea. Acoperă: token valid → sub, expirat, `exp`/`sub` lipsă,
secret greșit, `alg=none` (pinning), malformat, leeway de ceas, și `_apply_identity` (marginea web).
"""

import base64
import hashlib
import hmac
import json
import time

from src.config import get_settings
from src.web.identity import verify_identity_token

SECRET = "identity-secret-de-test"
_FUTURE = 9999999999  # exp departe în viitor
_PAST = 1  # exp în 1970


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint(payload: dict, *, secret: str = SECRET, alg: str = "HS256") -> str:
    """Semnează un JWT HS256 ca backend-ul gazdei (stdlib, oglindă a verificatorului)."""
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


# --- verify_identity_token ---------------------------------------------------


def test_valid_token_returns_sub():
    ref, reject = verify_identity_token(_mint({"sub": "cust_42", "exp": _FUTURE}), SECRET)
    assert ref == "cust_42" and reject is None


def test_expired_token_rejected():
    ref, reject = verify_identity_token(_mint({"sub": "cust_42", "exp": _PAST}), SECRET)
    assert ref is None and reject == "expired"


def test_missing_exp_rejected():
    # `exp` obligatoriu: un token fără expirare = replay infinit
    ref, reject = verify_identity_token(_mint({"sub": "cust_42"}), SECRET)
    assert ref is None and reject == "expired"


def test_missing_sub_rejected():
    ref, reject = verify_identity_token(_mint({"exp": _FUTURE}), SECRET)
    assert ref is None and reject == "no_sub"


def test_wrong_secret_rejected():
    ref, reject = verify_identity_token(_mint({"sub": "x", "exp": _FUTURE}), "alt-secret")
    assert ref is None and reject == "bad_signature"


def test_alg_none_rejected():
    # atac clasic JWT: alg=none → respins DUR pe pinning, înainte de orice verificare de semnătură
    ref, reject = verify_identity_token(_mint({"sub": "x", "exp": _FUTURE}, alg="none"), SECRET)
    assert ref is None and reject == "bad_alg"


def test_malformed_rejected():
    assert verify_identity_token("nu.e.un.jwt", SECRET) == (None, "malformed")  # 4 segmente
    assert verify_identity_token("", SECRET) == (None, "malformed")
    assert verify_identity_token("doar-un-segment", SECRET) == (None, "malformed")
    assert verify_identity_token("a.b.c", SECRET)[1] == "malformed"  # 3 segmente, dar nu JSON


def test_leeway_allows_small_clock_drift():
    # exp în trecutul RECENT, dar în fereastra de leeway → acceptat (drift de ceas gazdă↔bot)
    ref, reject = verify_identity_token(
        _mint({"sub": "cust_9", "exp": int(time.time()) - 5}), SECRET, leeway_s=30
    )
    assert ref == "cust_9" and reject is None


# --- _apply_identity (marginea web) ------------------------------------------


def test_apply_identity_marks_envelope(monkeypatch):
    from src.channels.base import InboundEvent
    from src.web import app as webapp
    from src.web.session import WebSession

    monkeypatch.setattr(get_settings(), "web_identity_enabled", True)
    session = WebSession(business_id="b", token="t", visitor_id="web_1", identity_secret=SECRET)
    ev = InboundEvent(
        channel_kind="webchat",
        channel_account_id="t",
        sender_external_id="web_1",
        provider_msg_id="m",
        content_type="text",
        body="vreau retur",
    )
    webapp._apply_identity(ev, session, _mint({"sub": "cust_7", "exp": _FUTURE}))
    assert ev.verified_customer_ref == "cust_7"
    assert "identity_rejected" not in ev.payload


def test_apply_identity_expired_marks_rejected_not_blocking(monkeypatch):
    from src.channels.base import InboundEvent
    from src.web import app as webapp
    from src.web.session import WebSession

    monkeypatch.setattr(get_settings(), "web_identity_enabled", True)
    session = WebSession(business_id="b", token="t", visitor_id="web_1", identity_secret=SECRET)
    ev = InboundEvent(
        channel_kind="webchat",
        channel_account_id="t",
        sender_external_id="web_1",
        provider_msg_id="m",
        content_type="text",
        body="hi",
    )
    webapp._apply_identity(ev, session, _mint({"sub": "x", "exp": _PAST}))
    assert ev.verified_customer_ref is None  # rămâne anonim (nu blochează chat-ul)
    assert ev.payload["identity_rejected"] == "expired"  # observabilitate


def test_apply_identity_noop_when_feature_off(monkeypatch):
    from src.channels.base import InboundEvent
    from src.web import app as webapp
    from src.web.session import WebSession

    monkeypatch.setattr(get_settings(), "web_identity_enabled", False)
    session = WebSession(business_id="b", token="t", visitor_id="web_1", identity_secret=SECRET)
    ev = InboundEvent(
        channel_kind="webchat",
        channel_account_id="t",
        sender_external_id="web_1",
        provider_msg_id="m",
        content_type="text",
        body="hi",
    )
    webapp._apply_identity(ev, session, _mint({"sub": "cust_7", "exp": _FUTURE}))
    assert ev.verified_customer_ref is None  # feature off → anonim, indiferent de token
