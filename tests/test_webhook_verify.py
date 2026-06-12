"""Teste pentru GET /webhook — handshake-ul de verificare Meta."""

import pytest
from fastapi.testclient import TestClient

from src.webhook.app import app

VERIFY_TOKEN = "test-verify-token-12345"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("META_VERIFY_TOKEN", VERIFY_TOKEN)
    return TestClient(app)


def test_verify_correct_token_returns_challenge(client):
    """Token corect → challenge întors ca text brut, status 200."""
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1234567890",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "1234567890"
    assert resp.headers["content-type"].startswith("text/plain")


def test_verify_special_chars_challenge_returned_identical(client):
    """Edge: challenge cu caractere speciale → întors identic."""
    challenge = "a-b_c.d~e123"
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": challenge,
        },
    )
    assert resp.status_code == 200
    assert resp.text == challenge


def test_verify_wrong_token_returns_403(client):
    """Failure: token greșit → 403, fără challenge."""
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "1234567890",
        },
    )
    assert resp.status_code == 403


def test_verify_wrong_mode_returns_403(client):
    """Edge: mode != subscribe → 403 chiar cu token corect."""
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1234567890",
        },
    )
    assert resp.status_code == 403


def test_verify_token_not_configured_returns_403(monkeypatch):
    """Failure: META_VERIFY_TOKEN nesetat → 403 (nu acceptă orice)."""
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    client = TestClient(app)
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "",
            "hub.challenge": "x",
        },
    )
    assert resp.status_code == 403
