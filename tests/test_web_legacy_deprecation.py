"""NX-290 — retragerea transportului async v1 (`POST /web/messages` + `GET /web/stream`).

Ce verificăm aici nu e „merge flagul", ci cele patru afirmații care trebuie să spună același
lucru cât timp ruta e în retragere: headerul standard, corpul lui `410`, ce anunță bootstrapul și
ce ajunge în contor. Plus două gărzi mecanice care prind exact greșelile tăcute: să nu deprecăm
din greșeală succesorul, și ca registrul să nu putrezească după o redenumire de rută.
"""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

import pytest
from fastapi import Response

from scripts.legacy_route_report import evaluate_removal
from src.web import app as wa
from src.web import deprecation
from src.web.app import WebMessageIn
from src.web.session import WebSession


async def _coro(value):
    return value


class _Req:
    def __init__(self):
        self.client = SimpleNamespace(host="1.2.3.4")
        self.headers = {"content-length": "2"}

    async def stream(self):
        yield b"{}"


class _FakeRedis:
    """Minimal cât să treacă rate limitul și metering-ul; înregistrează abonările pubsub."""

    def __init__(self):
        self.subscribed: list = []

    async def incr(self, key):
        return 1

    async def expire(self, *a):
        return True

    def pubsub(self):
        redis = self

        class _PS:
            async def subscribe(self, channel):
                redis.subscribed.append(channel)

            async def unsubscribe(self, channel):
                return None

            async def get_message(self, **k):
                return None

        return _PS()


def _wire(monkeypatch, *, legacy_enabled: bool):
    """Sesiune validă + redis fals + contor capturat. Întoarce (redis, events, enqueued)."""
    events: list = []
    enqueued: list = []

    async def fake_verify(token, vid, sig):
        return WebSession(business_id="b", token=token, visitor_id=vid)

    async def fake_persist(db, business_id, conv_id, contact_id, evts, *, operation=""):
        events.extend(evts)

    async def fake_enqueue(redis, event):
        enqueued.append(event)
        return "x"

    redis = _FakeRedis()
    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "persist_events", fake_persist)
    monkeypatch.setattr(wa, "enqueue_inbound", fake_enqueue)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(redis))
    monkeypatch.setattr(wa.get_settings(), "web_legacy_async_enabled", legacy_enabled)
    return redis, events, enqueued


# --- formatul headerelor (RFC 9745 + RFC 8594) --------------------------------


def test_deprecation_header_is_structured_field_date():
    """RFC 9745 §2: sf-date = `@` + secunde epoch, întreg. Un ISO-8601 aici e sintaxă invalidă."""
    h = deprecation.deprecation_headers(deprecation.route("/web/messages"))
    assert h["Deprecation"].startswith("@")
    assert h["Deprecation"][1:].isdigit()


def test_sunset_header_is_imf_fixdate_and_round_trips():
    """RFC 8594 §3: HTTP-date. Verificăm prin PARSARE, nu prin potrivire de text — un
    `+0000` ar trece un regex naiv, dar nu e IMF-fixdate."""
    entry = deprecation.route("/web/stream")
    h = deprecation.deprecation_headers(entry)
    assert h["Sunset"].endswith(" GMT")
    assert parsedate_to_datetime(h["Sunset"]) == entry.sunset_at


def test_link_header_names_doc_and_successor():
    entry = deprecation.route("/web/messages")
    link = deprecation.deprecation_headers(entry)["Link"]
    assert 'rel="deprecation"' in link and deprecation.DOC_URL in link
    assert 'rel="successor-version"' in link and entry.successor in link


def test_gone_headers_forbid_caching():
    """Un `410` cache-uit de un proxy ar supraviețui rollback-ului flagului — adică ar face
    ireversibilă o decizie construită special ca să fie reversibilă."""
    assert deprecation.gone_headers(deprecation.route("/web/stream"))["Cache-Control"] == "no-store"


def test_sunset_passed_is_a_question_not_an_action():
    entry = deprecation.route("/web/messages")
    assert deprecation.sunset_passed(entry, datetime(2026, 1, 1, tzinfo=UTC)) is False
    assert deprecation.sunset_passed(entry, datetime(2027, 1, 1, tzinfo=UTC)) is True


# --- calea servită (flag ON = comportamentul de azi + anunț) -------------------


async def test_messages_still_works_and_announces(monkeypatch):
    _, events, enqueued = _wire(monkeypatch, legacy_enabled=True)
    response = Response()
    res = await wa.web_message(
        WebMessageIn(token="tok", visitor_id="web_1", sig="s", text="salut"), _Req(), response
    )
    assert res["accepted"] is True
    assert len(enqueued) == 1  # mesajul intră în pipeline exact ca înainte
    assert response.headers["Deprecation"].startswith("@")
    assert "successor-version" in response.headers["Link"]
    assert [e.properties["outcome"] for e in events] == ["served"]


# --- calea refuzată (flag OFF = 410, și nimic nu intră în sistem) --------------


async def test_messages_refused_returns_410_and_enqueues_nothing(monkeypatch):
    """Cel mai important invariant al flip-ului: un mesaj refuzat nu trebuie să ajungă pe stream.
    Altfel clientul primește `410` iar botul îi răspunde oricum, într-un canal pe care nu-l mai
    ascultă nimeni."""
    _, events, enqueued = _wire(monkeypatch, legacy_enabled=False)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_message(
            WebMessageIn(token="tok", visitor_id="web_1", sig="s", text="salut"),
            _Req(),
            Response(),
        )
    assert ei.value.status_code == 410
    assert enqueued == []
    assert ei.value.detail["successor"] == "/web/v2/turns"
    assert ei.value.detail["error"] == "gone"
    assert ei.value.headers["Sunset"].endswith(" GMT")
    assert [e.properties["outcome"] for e in events] == ["refused"]


async def test_stream_refused_never_subscribes(monkeypatch):
    redis, events, _ = _wire(monkeypatch, legacy_enabled=False)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_stream("tok", "web_1", "s", _Req(), last_event_id=None)
    assert ei.value.status_code == 410
    assert redis.subscribed == []  # nicio conexiune pubsub ținută degeaba
    assert [e.properties["outcome"] for e in events] == ["refused"]


async def test_invalid_session_stays_403_not_410(monkeypatch):
    """Retragerea se anunță clienților legitimi, nu suprafeței de scanare: fără sesiune validă,
    răspunsul rămâne cel dinainte."""
    _wire(monkeypatch, legacy_enabled=False)

    async def none_verify(*a):
        return None

    monkeypatch.setattr(wa, "_verify", none_verify)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_message(
            WebMessageIn(token="t", visitor_id="v", sig="bad", text="x"), _Req(), Response()
        )
    assert ei.value.status_code == 403


# --- bootstrapul nu mai face reclamă unei rute stinse -------------------------


async def _bootstrap(monkeypatch, *, legacy_enabled: bool):
    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek"}

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_FakeRedis()))
    monkeypatch.setattr(wa.get_settings(), "web_legacy_async_enabled", legacy_enabled)
    return await wa.web_bootstrap("tok", _Req())


async def test_bootstrap_advertises_sse_url_while_route_lives(monkeypatch):
    assert (await _bootstrap(monkeypatch, legacy_enabled=True))["sse_url"] == "/web/stream"


async def test_bootstrap_drops_sse_url_when_route_is_gone(monkeypatch):
    """Cheia DISPARE, nu devine `null`: un client care verifică prezența câmpului nu trebuie să
    creadă că transportul există și e temporar indisponibil."""
    assert "sse_url" not in await _bootstrap(monkeypatch, legacy_enabled=False)


# --- gărzi mecanice: registrul nu are voie să mintă ---------------------------


def _mounted() -> dict[str, object]:
    return {r.path: r for r in wa.router.routes if hasattr(r, "path")}


def _is_deprecated(route: object) -> bool:
    """FastAPI lasă `deprecated=None` pe rutele nemarcate (absent ≠ `False` în OpenAPI), deci
    comparația se face pe adevărul afirmației, nu pe identitatea valorii."""
    return getattr(route, "deprecated", None) is True


def test_every_deprecated_route_exists_and_is_marked_in_openapi():
    """Registrul putrezește tăcut la o redenumire de rută: headerele ar continua să iasă pe o
    cale care nu mai există, iar cea nouă n-ar mai anunța nimic. Legăm registrul de router."""
    mounted = _mounted()
    for entry in deprecation.LEGACY_ASYNC_ROUTES:
        assert entry.path in mounted, f"{entry.path} nu mai e montată"
        assert _is_deprecated(mounted[entry.path])


def test_successors_are_not_themselves_deprecated():
    """Greșeala catastrofală și tăcută: să marchezi drept depășit exact drumul spre care trimiți."""
    deprecated_paths = {e.path for e in deprecation.LEGACY_ASYNC_ROUTES}
    mounted = _mounted()
    for entry in deprecation.LEGACY_ASYNC_ROUTES:
        assert entry.successor not in deprecated_paths
        successor = mounted.get(entry.successor)
        assert successor is not None, f"succesorul {entry.successor} nu e montat"
        assert not _is_deprecated(successor)


def test_no_other_route_is_marked_deprecated():
    """Simetric: o rută marcată `deprecated=True` fără intrare în registru ar ieși fără headere,
    fără succesor și fără contor — adică un anunț pe care nu-l poate citi nimeni."""
    declared = {e.path for e in deprecation.LEGACY_ASYNC_ROUTES}
    marked = {p for p, r in _mounted().items() if _is_deprecated(r)}
    assert marked == declared


# --- verdictul de ștergere: tăcerea nu e dovadă ------------------------------


def _verdict(**over):
    base = dict(calls={}, observed_turns=500, window_hours=672.0, sunset_passed=True)
    base.update(over)
    return evaluate_removal(**base)["verdict"]


def test_calls_block_removal_even_after_sunset():
    assert _verdict(calls={"/web/stream": {"served": 1}}) == "BLOCKED"


def test_refused_calls_also_block():
    """După flip, un `410` înseamnă un client rupt. E un motiv să NU ștergem încă (mai întâi îl
    reparăm), nu o confirmare că nimeni nu mai folosește ruta."""
    assert _verdict(calls={"/web/messages": {"refused": 3}}) == "BLOCKED"


def test_zero_calls_on_zero_traffic_is_unknown_not_safe():
    """Starea de AZI a proiectului: zero conversații. Un raport care ar da `SAFE` aici ar fabrica
    o dovadă din faptul că nu rulează nimic."""
    assert _verdict(observed_turns=0) == "UNKNOWN"


def test_thin_window_is_insufficient():
    assert _verdict(observed_turns=3) == "INSUFFICIENT"


def test_before_sunset_is_insufficient():
    """Data anunțată public e o promisiune. Am putea șterge mai devreme fără să spargem nimic —
    dar atunci anunțul n-ar fi însemnat nimic, iar următorul n-ar mai fi crezut."""
    assert _verdict(sunset_passed=False) == "INSUFFICIENT"


def test_safe_only_when_observed_and_silent_and_past_sunset():
    assert _verdict() == "SAFE_TO_REMOVE"
