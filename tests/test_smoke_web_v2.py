"""NX-248 — smoke-ul de release alege contractul pe care îl SERVEȘTE instanța.

Scriptul n-avea niciun test, iar defectul pe care îl repară testele astea l-a găsit o promovare
reală: smoke-ul cerea `POST /web/v2/turns` → 202, dar v2 e în spatele unui flag stins în producție
(măsurat 2026-08-19: 404 cu parametri, 422 fără). Deployul reușea, smoke-ul pica, iar releaseul
raporta „picat" pentru o promovare care de fapt mersese — cel mai prost moment posibil pentru un
fals negativ, fiindcă imaginea era deja schimbată.

Testele nu ating rețeaua: `_request` e înlocuit cu un fals care întoarce răspunsuri scriptate.
"""

import json

import pytest

from scripts.release import smoke_web_v2 as smoke

BASE = "https://bot.example.com"
TOKEN = "public-token"

HEALTH = (200, {"status": "ok", "release": "abc123"}, '{"status":"ok"}')
BOOTSTRAP = (200, {"visitor_id": "web_1", "sig": "s"}, '{"visitor_id":"web_1"}')


def _wire(monkeypatch, routes):
    """`routes` = listă de (predicat_pe_url, metodă, răspuns). Prima potrivire câștigă."""
    seen: list[tuple[str, str]] = []

    def fake_request(url, *, method="GET", body=None, timeout=15.0):
        seen.append((method, url))
        for match, want_method, response in routes:
            if want_method == method and match in url:
                return response
        raise AssertionError(f"cerere neașteptată: {method} {url}")

    monkeypatch.setattr(smoke, "_request", fake_request)
    return seen


def _v1_routes(chat_response):
    return [
        ("/health/ready", "GET", HEALTH),
        ("/web/bootstrap", "GET", BOOTSTRAP),
        # v2 stins: gate-ul răspunde 404 ÎNAINTE de verificarea sesiunii.
        ("/web/v2/turns", "POST", (404, {"detail": "not found"}, '{"detail":"not found"}')),
        ("/web/chat", "POST", chat_response),
    ]


def test_v2_dark_falls_back_to_the_v1_contract_and_says_so(monkeypatch):
    """404 pe v2 ⇒ verifică v1 și DECLARĂ profilul. Un verde fără profil ar fi de necitit."""
    raw = json.dumps({"content": "Salut! Cu ce te pot ajuta?", "products": [], "suggestions": []})
    chat = (200, json.loads(raw), raw)
    seen = _wire(monkeypatch, _v1_routes(chat))

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is True
    assert report["profile"] == "v1"
    assert [s["step"] for s in report["steps"]][-2:] == ["chat_v1", "raspuns_v1_nu_e_gol"]
    # Turul sincron a fost chiar exersat, nu doar declarat.
    assert ("POST", f"{BASE}/web/chat") in seen


def test_v1_empty_answer_fails_because_silence_is_a_bug(monkeypatch):
    """P6: niciodată tăcere. Un 200 cu `content` gol e exact defectul care trebuie prins."""
    raw = json.dumps({"content": "   ", "products": []})
    seen = _wire(monkeypatch, _v1_routes((200, json.loads(raw), raw)))

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "raspuns_v1_nu_e_gol"
    assert report["profile"] == "v1"
    assert ("POST", f"{BASE}/web/chat") in seen


def test_expecting_v2_makes_a_silent_fallback_to_v1_a_failure(monkeypatch):
    """După cutover, `--expect-profile v2` nu lasă un flag stins din greșeală să treacă verde."""
    raw = json.dumps({"content": "ok", "products": []})
    _wire(monkeypatch, _v1_routes((200, json.loads(raw), raw)))

    report = smoke.run_smoke(BASE, TOKEN, expect_profile="v2")

    assert report["ok"] is False
    assert report["failed_step"] == "profil_detectat"
    assert report["profile"] == "v1"


def test_v2_alive_runs_the_durable_chain_including_replay(monkeypatch):
    """Cu v2 pornit, contractul verificat e cel asincron — fără să atingă nimeni fișierul."""
    terminal = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "view": {"blocks": []}})
    routes = [
        ("/health/ready", "GET", HEALTH),
        ("/web/bootstrap", "GET", BOOTSTRAP),
        ("/web/v2/turns?", "POST", (202, {"turn": {"id": "t-1"}}, "{}")),
        ("/web/v2/turns/t-1", "GET", (200, json.loads(terminal), terminal)),
    ]
    _wire(monkeypatch, routes)

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is True
    assert report["profile"] == "v2"
    steps = [s["step"] for s in report["steps"]]
    assert "replay_byte_identic" in steps
    assert "idempotenta" in steps


def test_replay_that_differs_is_caught(monkeypatch):
    """Dacă a doua citire diferă, „refresh-ul paginii" schimbă un răspuns deja dat clientului."""
    first = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "n": 1})
    second = json.dumps({"turn": {"id": "t-1", "status": "completed"}, "n": 2})
    responses = iter([(200, json.loads(first), first), (200, json.loads(second), second)])

    def fake_request(url, *, method="GET", body=None, timeout=15.0):
        if "/health/ready" in url:
            return HEALTH
        if "/web/bootstrap" in url:
            return BOOTSTRAP
        if method == "POST":
            return (202, {"turn": {"id": "t-1"}}, "{}")
        return next(responses)

    monkeypatch.setattr(smoke, "_request", fake_request)

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "replay_byte_identic"


@pytest.mark.parametrize("status", [500, 503])
def test_unready_instance_stops_before_sending_traffic(monkeypatch, status):
    """Dacă `/health/ready` nu e 200, nu are rost să trimitem un tur (și să-l plătim)."""
    seen = _wire(monkeypatch, [("/health/ready", "GET", (status, {}, "{}"))])

    report = smoke.run_smoke(BASE, TOKEN)

    assert report["ok"] is False
    assert report["failed_step"] == "health_ready"
    assert report["profile"] == "unknown"
    assert len(seen) == 1  # nimic după poarta de readiness
