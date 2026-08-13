"""NX-236 — sigiliul: round-trip, determinism, tamper pe fiecare segment, rotație, expirare.

Regula pe care o verifică fișierul: un token care nu a ieșit EXACT de la noi, cu cheia noastră,
pentru audiența noastră și în fereastra lui de timp, nu se deschide — iar motivul respingerii nu
spune atacatorului nimic mai mult decât „nu".
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from src.web.action_crypto import (
    MAX_TOKEN_CHARS,
    KeyRingError,
    OpenedToken,
    OpenFailure,
    derive_action_id,
    open_token,
    parse_key_ring,
    pseudonym,
    redact_token,
    seal,
)
from src.web.action_models import ActionArgs, ActionEnvelope

NOW = 1_700_000_000
PID = "11111111-1111-4111-8111-111111111111"


def _key_spec(key_id: str, seed: bytes = b"\x01") -> str:
    return f"{key_id}:{base64.b64encode(seed * 32).decode()}"


def _ring(*specs: str):
    return parse_key_ring(",".join(specs))


def _envelope(**overrides) -> ActionEnvelope:
    base = {
        "version": "a1",
        "action_id": "a" * 32,
        "kind": "request_details",
        "args": ActionArgs(product_ref=PID),
        "policy": "one_shot",
        "audience": "web-widget-v2",
        "tenant_ref": "t" * 32,
        "session_ref": "s" * 32,
        "conversation_ref": "c" * 32,
        "source_turn_id": "33333333-3333-4333-8333-333333333333",
        "source_revision": 5,
        "issued_at": NOW,
        "expires_at": NOW + 1800,
    }
    base.update(overrides)
    return ActionEnvelope(**base)  # type: ignore[arg-type]


# ── Key ring ────────────────────────────────────────────────────────────────────────────────
def test_key_ring_requires_at_least_one_key():
    with pytest.raises(KeyRingError):
        parse_key_ring("")
    with pytest.raises(KeyRingError):
        parse_key_ring(None)


def test_key_ring_rejects_short_material():
    short = base64.b64encode(os.urandom(16)).decode()
    with pytest.raises(KeyRingError):
        parse_key_ring(f"k1:{short}")


def test_key_ring_rejects_duplicate_ids_and_bad_base64():
    duplicate = _key_spec("k1") + "," + _key_spec("k1", b"\x02")
    with pytest.raises(KeyRingError):
        parse_key_ring(duplicate)
    with pytest.raises(KeyRingError):
        parse_key_ring("k1:not-base64!!")


def test_key_ring_rejects_bad_key_ids():
    material = base64.b64encode(os.urandom(32)).decode()
    for bad in ("k 1", "k/1", "k" * 17, ""):
        with pytest.raises(KeyRingError):
            parse_key_ring(f"{bad}:{material}")


def test_first_key_is_current_and_all_verify():
    ring = _ring(_key_spec("k2", b"\x02"), _key_spec("k1", b"\x01"))
    assert ring.current.key_id == "k2"
    assert ring.slot("k2") == "current"
    assert ring.slot("k1") == "previous"
    assert ring.slot("k9") == "unknown"


def test_subkeys_are_domain_separated():
    key = _ring(_key_spec("k1")).current
    assert len({key.seal, key.id_key, key.ref_key}) == 3


# ── Round-trip + determinism ────────────────────────────────────────────────────────────────
def test_seal_open_roundtrip():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    opened = open_token(token, ring, now=NOW + 10)
    assert isinstance(opened, OpenedToken)
    assert opened.envelope.kind == "request_details"
    assert opened.envelope.args.product_ref == PID
    assert opened.slot == "current"


def test_sealing_is_deterministic():
    """Fără asta, fiecare GET ar produce alt token pentru același buton (replay ne-identic)."""
    ring = _ring(_key_spec("k1"))
    assert seal(ring.current, _envelope()) == seal(ring.current, _envelope())


def test_token_is_opaque_no_readable_claims():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    assert PID not in token
    assert "request_details" not in token
    # Corpul nu se decodează în JSON lizibil (e ciphertext, nu Base64 de claims).
    body = token.split(".")[2]
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError, ValueError)):
        json.loads(raw.decode("utf-8"))


def test_token_stays_within_the_contract_cap():
    ring = _ring(_key_spec("k1"))
    token = seal(
        ring.current,
        _envelope(
            kind="compare_selection",
            args=ActionArgs(product_refs=(PID, "22222222-2222-4222-8222-222222222222")),
        ),
    )
    assert len(token) <= MAX_TOKEN_CHARS


# ── Tamper ──────────────────────────────────────────────────────────────────────────────────
def test_flipping_any_byte_of_the_body_is_rejected():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    version, key_id, body = token.split(".")
    for index in range(0, len(body), 7):  # eșantion dens, nu exhaustiv (timp de rulare)
        swapped = "A" if body[index] != "A" else "B"
        tampered = f"{version}.{key_id}.{body[:index]}{swapped}{body[index + 1 :]}"
        result = open_token(tampered, ring, now=NOW)
        assert isinstance(result, OpenFailure), f"byte {index} acceptat"


def test_rewriting_the_prefix_is_rejected():
    """Versiunea și `key_id` sunt în clar, deci trebuie legate criptografic (AAD)."""
    ring = _ring(_key_spec("k1"), _key_spec("k2", b"\x02"))
    token = seal(ring.current, _envelope())
    _, _, body = token.split(".")
    assert isinstance(open_token(f"a1.k2.{body}", ring, now=NOW), OpenFailure)
    assert isinstance(open_token(f"a2.k1.{body}", ring, now=NOW), OpenFailure)


def test_token_from_another_key_ring_is_rejected():
    mine = _ring(_key_spec("k1", b"\x01"))
    theirs = _ring(_key_spec("k1", b"\x09"))  # același id, alt material
    token = seal(theirs.current, _envelope())
    result = open_token(token, mine, now=NOW)
    assert isinstance(result, OpenFailure) and result.reason == "bad_seal"


def test_unknown_key_fails_closed():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    other = _ring(_key_spec("k7", b"\x07"))
    result = open_token(token, other, now=NOW)
    assert isinstance(result, OpenFailure) and result.reason == "unknown_key"


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "a1",
        "a1.k1",
        "a1.k1.",
        "a1.k1.####",
        "a1.k1." + "A" * 6000,  # peste capul de contract
        "../../etc/passwd",
        "a1.k1.AAAA\x00AAAA",
        "a1.k1." + base64.urlsafe_b64encode(b"{}" * 900).decode(),
    ],
)
def test_garbage_is_rejected_without_raising(garbage):
    ring = _ring(_key_spec("k1"))
    assert isinstance(open_token(garbage, ring, now=NOW), OpenFailure)


def test_non_string_token_is_rejected():
    ring = _ring(_key_spec("k1"))
    assert isinstance(open_token(None, ring, now=NOW), OpenFailure)  # type: ignore[arg-type]


# ── Timp + audiență ─────────────────────────────────────────────────────────────────────────
def test_expired_token_reports_expired_not_invalid():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    result = open_token(token, ring, now=NOW + 1801)
    assert isinstance(result, OpenFailure) and result.reason == "expired"


def test_clock_skew_is_tolerated_on_both_ends():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    assert isinstance(open_token(token, ring, now=NOW + 1830, skew_s=60), OpenedToken)
    assert isinstance(open_token(token, ring, now=NOW - 30, skew_s=60), OpenedToken)


def test_token_from_the_future_is_rejected():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    result = open_token(token, ring, now=NOW - 3600)
    assert isinstance(result, OpenFailure) and result.reason == "not_yet_valid"


def test_wrong_audience_is_rejected():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope(audience="web-widget-v1"))
    result = open_token(token, ring, now=NOW)
    assert isinstance(result, OpenFailure) and result.reason == "audience"


def test_unknown_kind_inside_a_valid_seal_is_rejected():
    """Sigiliul e al nostru, conținutul nu e din vocabular ⇒ tot respingere (fail-closed)."""
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope(kind="execute_tool"))
    result = open_token(token, ring, now=NOW)
    assert isinstance(result, OpenFailure) and result.reason == "claims"


# ── Rotație ─────────────────────────────────────────────────────────────────────────────────
def test_rotation_keeps_old_tokens_valid_during_overlap():
    old = _ring(_key_spec("k1", b"\x01"))
    token = seal(old.current, _envelope())
    rotated = _ring(_key_spec("k2", b"\x02"), _key_spec("k1", b"\x01"))
    opened = open_token(token, rotated, now=NOW + 10)
    assert isinstance(opened, OpenedToken) and opened.slot == "previous"


def test_dropping_the_old_key_invalidates_its_tokens():
    old = _ring(_key_spec("k1", b"\x01"))
    token = seal(old.current, _envelope())
    only_new = _ring(_key_spec("k2", b"\x02"))
    assert isinstance(open_token(token, only_new, now=NOW + 10), OpenFailure)


# ── Derivări ────────────────────────────────────────────────────────────────────────────────
def test_action_id_depends_on_source_kind_and_args():
    key = _ring(_key_spec("k1")).current
    base = derive_action_id(
        key, source_turn_id="t1", kind="request_details", args={"product_ref": PID}
    )
    assert base == derive_action_id(
        key, source_turn_id="t1", kind="request_details", args={"product_ref": PID}
    )
    assert base != derive_action_id(
        key, source_turn_id="t2", kind="request_details", args={"product_ref": PID}
    )
    assert base != derive_action_id(
        key, source_turn_id="t1", kind="request_reviews", args={"product_ref": PID}
    )
    assert base != derive_action_id(key, source_turn_id="t1", kind="request_details", args={})


def test_action_id_differs_between_keys():
    a = _ring(_key_spec("k1", b"\x01")).current
    b = _ring(_key_spec("k1", b"\x02")).current
    args = {"product_ref": PID}
    assert derive_action_id(a, source_turn_id="t", kind="x", args=args) != derive_action_id(
        b, source_turn_id="t", kind="x", args=args
    )


def test_pseudonyms_are_scoped_and_not_the_raw_value():
    key = _ring(_key_spec("k1")).current
    tenant = pseudonym(key, "tenant", "biz-1")
    assert tenant != "biz-1" and len(tenant) == 32
    assert tenant != pseudonym(key, "session", "biz-1")  # scope separă domeniile
    assert tenant == pseudonym(key, "tenant", "biz-1")  # stabil


# ── Redactare ───────────────────────────────────────────────────────────────────────────────
def test_redaction_never_leaks_the_token():
    ring = _ring(_key_spec("k1"))
    token = seal(ring.current, _envelope())
    redacted = redact_token(token)
    assert token[:16] not in redacted
    assert redacted == f"tok:len={len(token)}"
