"""NX-233 — rutele v2 (accept/status/SSE) + proiecția `web-view.v2` (fără DB/rețea reală).

Garanțiile verificate AICI:
  • POST /web/v2/turns NU cheamă pipeline/LLM/tool — doar acceptă durabil și notifică;
  • replay: același turn terminal → 200 cu proiecția DETERMINISTĂ a payload-ului persistat;
  • conflicte tipizate: `idempotency_conflict` și `conversation_turn_in_progress` (cu
    referința AUTORIZATĂ la turnul activ), erori structurate ÎNAINTE de accept (schema/action);
  • GET reautorizează sesiunea (hash de sesiune): alt vizitator → 404 indistinct;
  • proiecția v1→v2: validată de contract (parse_view), byte-deterministă, `running` nu iese
    NICIODATĂ pe sârmă, terminalele au mereu ceva randabil (P6), reducerea se calculează server;
  • SSE: id-uri monotonice pe lifecycle, `Last-Event-ID` reia fără dubluri, rezultatul terminal
    o singură dată, zero tokeni/draft.
"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import get_settings
from src.db.queries.web_turns import WebTurnRow
from src.web import app as wa
from src.web import turn_events as tev
from src.web import turn_service as ts
from src.web.contracts_v2 import parse_view

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _row(**over) -> WebTurnRow:
    base = dict(
        id=str(uuid4()),
        business_id="b1",
        conversation_id="c1",
        contact_id="ct1",
        session_ref_hash=ts.session_ref_hash("tok", "web_1"),
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="accepted",
        attempt=0,
        lease_owner=None,
        lease_epoch=0,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=3,
        pipeline_version=ts.RESPONSE_CONTRACT_SYNC_V1,
        response_json=None,
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    base.update(over)
    return WebTurnRow(**base)


COMPLETED_PAYLOAD = {
    "content": "Uite serul potrivit pentru tenul tău.",
    "products": [
        {
            "product_id": "p-1",
            "name": "Ser Niacinamidă",
            "price": 89.0,
            "list_price": 109.0,
            "url": "https://shop.example.com/p/ser",
            "image_url": "https://shop.example.com/i/ser.jpg",
            "rating": 4.8,
            "review_count": 120,
            "reason": "Reduce porii vizibil.",
            "badges": [{"label": "Reducere", "tone": "info"}],
        }
    ],
    "suggestions": ["Vezi alternative"],
}


# ── Proiecția v1 → web-view.v2 ────────────────────────────────────────────────


def test_terminal_view_completed_is_valid_and_display_ready():
    row = _row(status="completed", response_json=COMPLETED_PAYLOAD, completed_at=NOW)
    view = tev.terminal_view(row, "ro")
    parse_view(view)  # contractul NX-228 e poarta
    assert view["turn"]["status"] == "completed"
    blocks = view["messages"][0]["blocks"]
    assert blocks[0]["type"] == "text"
    products = [b for b in blocks if b["type"] == "product_list"][0]["items"]
    item = products[0]
    # Cifrele rămân în backend: prețul, reducerea și ratingul sunt TEXT localizat, gata.
    assert item["price"]["current"] == "89,00 lei"
    assert item["price"]["previous"] == "109,00 lei"
    assert item["price"]["discount"] == "-18%"
    assert item["rating"] == "4,8 din 5 (120 recenzii)"
    # CTA-ul e navigate către URL-ul deja validat; niciun submit fără token semnat (NX-236).
    assert item["actions"][0]["activation"] == {
        "type": "navigate",
        "href": "https://shop.example.com/p/ser",
        "target": "_blank",
    }
    # `view_id`, nu `product_id`: id-ul de catalog nu pleacă în browser.
    dumped = json.dumps(view)
    assert "p-1" not in dumped and "fp" not in dumped


def test_terminal_view_is_byte_deterministic():
    row = _row(status="completed", response_json=COMPLETED_PAYLOAD, completed_at=NOW)
    a = json.dumps(tev.terminal_view(row, "ro"), sort_keys=True)
    b = json.dumps(tev.terminal_view(row, "ro"), sort_keys=True)
    assert a == b  # replay byte-echivalent: aceeași intrare → aceiași bytes


def test_terminal_view_failed_has_error_and_notice():
    payload = ts.error_view("deadline_exceeded", "ro")
    row = _row(
        status="failed",
        response_json=payload,
        safe_error_code="deadline_exceeded",
        completed_at=NOW,
    )
    view = tev.terminal_view(row, "ro")
    parse_view(view)
    assert view["error"]["code"] == "deadline_exceeded" and view["error"]["retryable"]
    assert view["messages"][0]["blocks"][0]["type"] == "notice"


def test_terminal_view_cancelled_is_renderable_without_error():
    row = _row(
        status="cancelled",
        response_json=ts.error_view("cancelled", "ro"),
        safe_error_code="cancelled",
        completed_at=NOW,
    )
    view = tev.terminal_view(row, "ro")
    parse_view(view)
    assert "error" not in view  # `error` doar pe failed (contract)
    assert view["messages"][0]["blocks"][0]["type"] == "notice"  # dar NU e gol (P6)


def test_terminal_view_unrenderable_falls_back_to_safe_failed():
    row = _row(status="completed", response_json={"content": "", "products": []}, completed_at=NOW)
    view = tev.terminal_view(row, "ro")
    parse_view(view)
    assert view["error"]["code"] == "projection_error"  # niciodată tăcere pe terminal


def test_terminal_view_comparison_maps_to_table():
    payload = {
        "content": "Comparăm cele două seruri.",
        "products": [],
        "suggestions": [],
        "comparison": {
            "columns": [{"name": "Ser A", "price": 10.0}, {"name": "Ser B", "price": 20.0}],
            "rows": [{"label": "Textură", "values": ["gel", None]}],
        },
    }
    row = _row(status="completed", response_json=payload, completed_at=NOW)
    view = tev.terminal_view(row, "ro")
    parse_view(view)
    cmp_block = [b for m in view["messages"] for b in m["blocks"] if b["type"] == "comparison"][0]
    assert cmp_block["headers"] == ["Ser A", "Ser B"]
    assert cmp_block["rows"][0]["cells"][1]["text"] is None  # necunoscut ≠ gol


def test_terminal_view_rejects_dangerous_urls():
    payload = {
        "content": "x",
        "products": [{"name": "P", "price": 1.0, "url": "javascript:alert(1)"}],
        "suggestions": [],
    }
    row = _row(status="completed", response_json=payload, completed_at=NOW)
    view = tev.terminal_view(row, "ro")
    parse_view(view)
    dumped = json.dumps(view)
    assert "javascript:" not in dumped


def test_status_projection_never_leaks_running():
    for status, phase, expected in [
        ("accepted", None, "accepted"),
        ("running", None, "working"),
        ("running", "validating", "validating"),
    ]:
        payload = tev.status_payload(_row(status=status), phase=phase, poll_after_ms=500)
        assert payload["turn"]["status"] == expected
        assert payload["poll_after_ms"] == 500
    assert tev.STATUS_ORDINAL["accepted"] < tev.STATUS_ORDINAL["working"]
    assert tev.STATUS_ORDINAL["working"] < tev.STATUS_ORDINAL["validating"]
    assert tev.STATUS_ORDINAL["validating"] < tev.STATUS_ORDINAL["completed"]


# ── Seams comune pentru endpointuri ──────────────────────────────────────────


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return None

    async def incrbyfloat(self, key, amount):
        return float(amount)


class _Req:
    def __init__(self, body: bytes = b"{}"):
        self.client = SimpleNamespace(host="1.2.3.4")
        self._body = body
        self.headers = {"content-length": str(len(body))}
        self._disconnected = False

    async def stream(self):
        yield self._body

    async def is_disconnected(self):
        return self._disconnected


@asynccontextmanager
async def _fake_cm(*a, **k):
    yield None


async def _coro(value):
    return value


def _body(client_turn_id, text="hei", input_type="text"):
    inp = (
        {"type": "text", "text": text}
        if input_type == "text"
        else {"type": "action", "action_token": "tok-opaque"}
    )
    return json.dumps(
        {"schema_version": "web-turn.v2", "client_turn_id": str(client_turn_id), "input": inp}
    ).encode()


def _wire_v2(monkeypatch, *, accept=None, session_row=None, refreshed=None):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_turn_v2_enabled", True)
    monkeypatch.setattr(settings, "web_turn_sse_enabled", True)

    async def fake_verify(token, vid, sig):
        return wa.WebSession(business_id="b1", token=token, visitor_id=vid)

    async def fake_resolve_channel(conn, kind, token):
        return {"channel_id": "chan", "business_id": "b1"}

    async def fake_load_business(conn, bid):
        return SimpleNamespace(id=bid, daily_cost_cap_usd=None, default_locale="ro")

    events: list = []

    async def fake_persist(db, business_id, conversation_id, contact_id, evs, **kw):
        events.extend(evs)

    woken: list = []

    async def fake_wake(redis, business_id, turn_id):
        woken.append(turn_id)

    async def fake_get_for_session(db, **kw):
        return session_row

    async def fake_get_by_id(conn, business_id, turn_id):
        return refreshed

    async def fail_handle(*a, **k):
        raise AssertionError("request handlerul NU cheamă pipeline/LLM/tool (NX-233)")

    monkeypatch.setattr(wa, "_verify", fake_verify)
    monkeypatch.setattr(wa, "get_redis", lambda: _coro(_FakeRedis()))
    monkeypatch.setattr(wa, "get_pool", lambda: _coro(None))
    monkeypatch.setattr(wa, "admin_conn", _fake_cm)
    monkeypatch.setattr(wa, "tenant_db", lambda business_id: _fake_cm)
    monkeypatch.setattr(wa, "resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(wa, "load_business", fake_load_business)
    monkeypatch.setattr(wa, "persist_events", fake_persist)
    monkeypatch.setattr(wa, "wake_executor", fake_wake)
    monkeypatch.setattr(wa, "get_turn_for_session", fake_get_for_session)
    monkeypatch.setattr(wa, "get_turn_by_id", fake_get_by_id)
    monkeypatch.setattr(wa, "handle_turn", fail_handle)
    if accept is not None:
        monkeypatch.setattr(wa, "accept_web_turn", accept)
    return events, woken


# ── POST /web/v2/turns ────────────────────────────────────────────────────────


async def test_accept_new_returns_202_and_never_runs_pipeline(monkeypatch):
    row = _row()
    captured = {}

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(row, inbound_msg_id="m1")

    events, woken = _wire_v2(monkeypatch, accept=fake_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 202
    payload = json.loads(res.body)
    assert payload["turn"]["id"] == row.id
    assert payload["turn"]["status"] == "accepted"
    assert payload["status_url"].endswith(row.id)
    assert payload["poll_after_ms"] > 0
    # acceptul e DURABIL: inputul safe se persistă, deadline-ul se fixează, wake-ul pleacă
    assert captured["persist_inbound"] is True
    assert captured["deadline_at"] is not None
    assert captured["session_ref"] == ts.session_ref_hash("tok", "web_1")
    assert woken == [row.id]
    assert {e.type for e in events} >= {"web_turn_accepted"}


async def test_accept_existing_terminal_replays_projection(monkeypatch):
    row = _row(
        status="completed",
        response_json=COMPLETED_PAYLOAD,
        completed_at=NOW,
        request_fingerprint=ts.request_fingerprint(
            get_settings().web_turn_fingerprint_secret,
            business_id="b1",
            channel_token="tok",
            text="hei",
        ),
    )

    async def fake_accept(db, **kw):
        return ts.ExistingCompleted(row)

    _wire_v2(monkeypatch, accept=fake_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 200
    view = json.loads(res.body)
    assert view == tev.terminal_view(row, "ro")  # replay = proiecția aceluiași rând, exact


async def test_accept_idempotency_conflict_409(monkeypatch):
    async def fake_accept(db, **kw):
        return ts.IdempotencyConflict(_row(status="completed", response_json={"content": "x"}))

    _wire_v2(monkeypatch, accept=fake_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_body(uuid4())), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 409
    assert json.loads(res.body)["error"]["code"] == "idempotency_conflict"


async def test_accept_active_turn_conflict_references_active(monkeypatch):
    active = _row(status="running", lease_owner="w1", lease_epoch=1)

    async def fake_accept(db, **kw):
        return ts.ActiveTurnConflict(active_client_turn_id=active.client_turn_id, active=active)

    _wire_v2(monkeypatch, accept=fake_accept)
    res = await wa.web_turn_accept_v2(
        _Req(_body(uuid4())), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 409
    body = json.loads(res.body)
    assert body["error"]["code"] == "conversation_turn_in_progress"
    # referință AUTORIZATĂ la turnul activ: clientul știe CE așteaptă, fără să pornească altul
    assert body["active_turn"]["turn"]["id"] == active.id
    assert body["active_turn"]["turn"]["status"] == "working"  # `running` nu iese pe sârmă


async def test_accept_schema_invalid_and_action_are_structured_errors(monkeypatch):
    _wire_v2(monkeypatch)
    res = await wa.web_turn_accept_v2(
        _Req(b'{"schema_version": "web-turn.v2"}'), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 422
    assert json.loads(res.body)["error"]["code"] == "schema_invalid"
    res = await wa.web_turn_accept_v2(
        _Req(_body(uuid4(), input_type="action")), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 422
    assert json.loads(res.body)["error"]["code"] == "action_not_supported"
    res = await wa.web_turn_accept_v2(_Req(b"nu-e-json"), token="tok", visitor_id="web_1", sig="s")
    assert res.status_code == 400


async def test_v2_routes_404_when_flag_off(monkeypatch):
    _wire_v2(monkeypatch)
    monkeypatch.setattr(get_settings(), "web_turn_v2_enabled", False)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_turn_accept_v2(_Req(_body(uuid4())), token="tok", visitor_id="web_1", sig="s")
    assert ei.value.status_code == 404


# ── GET /web/v2/turns/{id} ────────────────────────────────────────────────────


async def test_get_returns_202_status_for_active_turn(monkeypatch):
    row = _row(status="running", lease_owner="w1", lease_epoch=1)
    _wire_v2(monkeypatch, session_row=row)
    res = await wa.web_turn_status_v2(
        row.id, token="tok", visitor_id="web_1", sig="s", request=_Req()
    )
    assert res.status_code == 202
    assert json.loads(res.body)["turn"]["status"] == "working"


async def test_get_returns_200_terminal_projection(monkeypatch):
    row = _row(status="completed", response_json=COMPLETED_PAYLOAD, completed_at=NOW)
    _wire_v2(monkeypatch, session_row=row)
    res = await wa.web_turn_status_v2(
        row.id, token="tok", visitor_id="web_1", sig="s", request=_Req()
    )
    assert res.status_code == 200
    assert json.loads(res.body) == tev.terminal_view(row, "ro")


async def test_get_unknown_or_foreign_turn_is_404(monkeypatch):
    _wire_v2(monkeypatch, session_row=None)  # alt vizitator/tenant → serviciul întoarce None
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_turn_status_v2(
            str(uuid4()), token="tok", visitor_id="web_1", sig="s", request=_Req()
        )
    assert ei.value.status_code == 404
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_turn_status_v2(
            "nu-e-uuid", token="tok", visitor_id="web_1", sig="s", request=_Req()
        )
    assert ei.value.status_code == 404


def test_session_authorization_is_hash_based():
    """Autorizarea GET/SSE: hash-ul sesiunii care a creat turnul; alt vizitator nu trece."""
    row = _row()
    assert row.session_ref_hash == ts.session_ref_hash("tok", "web_1")
    assert row.session_ref_hash != ts.session_ref_hash("tok", "web_2")
    assert row.session_ref_hash != ts.session_ref_hash("alt-tok", "web_1")


# ── SSE ───────────────────────────────────────────────────────────────────────


async def _collect_sse(response) -> list[str]:
    frames = []
    async for chunk in response.body_iterator:
        frames.append(chunk)
    return frames


async def test_sse_emits_monotonic_statuses_then_result_once(monkeypatch):
    first = _row(status="accepted")
    running = _row(
        id=first.id,
        client_turn_id=first.client_turn_id,
        status="running",
        lease_owner="w1",
        lease_epoch=1,
    )
    done = _row(
        id=first.id,
        client_turn_id=first.client_turn_id,
        status="completed",
        response_json=COMPLETED_PAYLOAD,
        completed_at=NOW,
    )
    sequence = iter([running, done])

    async def fake_get_by_id(conn, business_id, turn_id):
        return next(sequence)

    _wire_v2(monkeypatch, session_row=first)
    monkeypatch.setattr(wa, "get_turn_by_id", fake_get_by_id)
    monkeypatch.setattr(get_settings(), "web_turn_sse_poll_ms", 1)
    res = await wa.web_turn_events_v2(
        first.id,
        token="tok",
        visitor_id="web_1",
        sig="s",
        request=_Req(),
        last_event_id=None,
    )
    frames = await _collect_sse(res)
    joined = "".join(frames)
    assert "event: status" in joined and "event: result" in joined
    assert joined.count("event: result") == 1
    assert '"running"' not in joined  # enumul intern nu iese pe sârmă
    ids = [int(line.split(": ")[1]) for line in joined.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)  # monotonic — Last-Event-ID poate relua fără dubluri
    # rezultatul e proiecția terminală completă (deja comisă), nu un draft
    assert "Ser Niacinamid" in joined


async def test_sse_last_event_id_resumes_without_duplicates(monkeypatch):
    done = _row(status="completed", response_json=COMPLETED_PAYLOAD, completed_at=NOW)
    _wire_v2(monkeypatch, session_row=done, refreshed=done)
    res = await wa.web_turn_events_v2(
        done.id,
        token="tok",
        visitor_id="web_1",
        sig="s",
        request=_Req(),
        last_event_id="3",
    )
    frames = await _collect_sse(res)
    assert frames == []  # clientul avea deja terminalul → zero dubluri; GET rămâne fallback


async def test_sse_404_when_flag_off(monkeypatch):
    row = _row()
    _wire_v2(monkeypatch, session_row=row)
    monkeypatch.setattr(get_settings(), "web_turn_sse_enabled", False)
    with pytest.raises(wa.HTTPException) as ei:
        await wa.web_turn_events_v2(
            row.id, token="tok", visitor_id="web_1", sig="s", request=_Req(), last_event_id=None
        )
    assert ei.value.status_code == 404


# ── Config: relațiile parametrilor se validează la boot ───────────────────────


def test_config_rejects_heartbeat_that_cannot_fit_in_lease():
    from pydantic import ValidationError

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            SUPABASE_DB_URL="postgresql://u:p@localhost/db",
            WEB_TURN_HEARTBEAT_S="200",
            WEB_TURN_LEASE_TTL_S="300",
        )


# ── NX-236: ingressul acțiunilor opace (flag WEB_ACTIONS_ENABLED) ─────────────


def _action_ring():
    """Inel de test (32B ASCII). Valorile nu au voie să ajungă în repo pentru prod — aici sunt
    material de test, generat determinist ca să nu depindem de random."""
    from src.web.action_crypto import parse_key_ring

    return parse_key_ring("k1:bngyMzYtdGVzdC1rZXktb25lLS0tLS0tLS0tLS0tLS0=")


def _action_source(**over) -> WebTurnRow:
    """Un turn terminal cu PLANUL persistat — dovada de emitere, exact ca în tranzacția reală."""
    from src.web.action_models import ActionArgs, ActionPlan
    from src.web.action_service import merge_actions_into_view

    plans = (ActionPlan("request_details", ActionArgs(product_ref="p-1")),)
    view = merge_actions_into_view(
        {"content": "ok", "products": [{"product_id": "p-1"}], "suggestions": []}, plans
    )
    # `completed_at` REAL, nu `NOW`: expirarea unui token e ancorată în el, iar un terminal
    # datat în trecut ar produce tokenuri deja expirate (exact ce trebuie să se întâmple în
    # producție, dar aici ar masca testul).
    return _row(status="completed", response_json=view, completed_at=datetime.now(UTC), **over)


def _wire_actions(monkeypatch, source: WebTurnRow, consumer: WebTurnRow | None = None):
    from src.web import action_service as svc

    settings = get_settings()
    monkeypatch.setattr(settings, "web_actions_enabled", True)
    monkeypatch.setattr(settings, "web_action_ttl_s", 1800)
    monkeypatch.setattr(
        settings, "web_action_keys", "k1:bngyMzYtdGVzdC1rZXktb25lLS0tLS0tLS0tLS0tLS0="
    )

    async def fake_get_turn_by_id(conn, business_id, turn_id):
        return source if (source and turn_id == source.id and business_id == "b1") else None

    async def fake_find(conn, business_id, conversation_id, fingerprint):
        return consumer

    monkeypatch.setattr(svc, "get_turn_by_id", fake_get_turn_by_id)
    monkeypatch.setattr(svc, "find_turn_by_fingerprint", fake_find)
    issued = svc.issue_actions(source, svc.plans_from_row(source), ring=_action_ring(), ttl_s=1800)
    return issued[0]


def _action_body(client_turn_id, token: str) -> bytes:
    return json.dumps(
        {
            "schema_version": "web-turn.v2",
            "client_turn_id": str(client_turn_id),
            "input": {"type": "action", "action_token": token},
        }
    ).encode()


async def test_action_accept_persists_the_typed_command_not_the_token(monkeypatch):
    source = _action_source()
    captured = {}
    accepted = _row()

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(accepted, inbound_msg_id="m1")

    events, _ = _wire_v2(monkeypatch, accept=fake_accept)
    issued = _wire_actions(monkeypatch, source)
    res = await wa.web_turn_accept_v2(
        _Req(_action_body(accepted.client_turn_id, issued.token)),
        token="tok",
        visitor_id="web_1",
        sig="s",
    )
    assert res.status_code == 202
    # Comanda TYPED se persistă; tokenul NU atinge discul (P12).
    assert captured["action_payload"]["kind"] == "request_details"
    assert captured["content_type"] == "action"
    assert captured["safe_body"] == ""  # eticheta butonului nu devine mesaj de client
    assert issued.token not in json.dumps(captured["action_payload"])
    assert {e.type for e in events} >= {"web_action_verified", "web_action_key_age"}


async def test_action_with_a_tampered_token_is_rejected_before_accept(monkeypatch):
    source = _action_source()

    async def fake_accept(db, **kw):
        raise AssertionError("un token stricat NU are voie să ajungă la accept")

    _wire_v2(monkeypatch, accept=fake_accept)
    issued = _wire_actions(monkeypatch, source)
    tampered = issued.token[:-2] + ("AB" if not issued.token.endswith("AB") else "CD")
    res = await wa.web_turn_accept_v2(
        _Req(_action_body(uuid4(), tampered)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 400
    assert json.loads(res.body)["error"]["code"] == "action_invalid"


async def test_action_from_another_visitor_is_404_without_existence_leak(monkeypatch):
    source = _action_source()

    async def fake_accept(db, **kw):
        raise AssertionError("un token al altei sesiuni NU ajunge la accept")

    _wire_v2(monkeypatch, accept=fake_accept)
    issued = _wire_actions(monkeypatch, source)
    res = await wa.web_turn_accept_v2(
        _Req(_action_body(uuid4(), issued.token)), token="tok", visitor_id="web_ALTUL", sig="s"
    )
    assert res.status_code == 404
    body = json.loads(res.body)
    assert body["error"]["code"] == "action_not_found"
    # Motivul fin (session_mismatch) rămâne în log, nu pe sârmă.
    assert "session" not in body["error"]["message"].lower()


async def test_action_already_consumed_by_another_turn_is_409(monkeypatch):
    source = _action_source()

    async def fake_accept(db, **kw):
        raise AssertionError("un buton deja consumat NU ajunge la accept")

    _wire_v2(monkeypatch, accept=fake_accept)
    consumer = _row(client_turn_id=str(uuid4()))
    issued = _wire_actions(monkeypatch, source, consumer=consumer)
    res = await wa.web_turn_accept_v2(
        _Req(_action_body(uuid4(), issued.token)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 409
    assert json.loads(res.body)["error"]["code"] == "action_already_consumed"


async def test_action_is_refused_when_the_kill_switch_is_off(monkeypatch):
    source = _action_source()
    _wire_v2(monkeypatch)
    issued = _wire_actions(monkeypatch, source)
    monkeypatch.setattr(get_settings(), "web_actions_enabled", False)
    res = await wa.web_turn_accept_v2(
        _Req(_action_body(uuid4(), issued.token)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 422
    assert json.loads(res.body)["error"]["code"] == "action_not_supported"


def test_projection_emits_submit_actions_only_with_the_flag_on(monkeypatch):
    source = _action_source()
    settings = get_settings()
    monkeypatch.setattr(settings, "web_actions_enabled", False)
    tev._ring.cache_clear()
    off = tev.terminal_view(source, "ro")
    parse_view(off)
    assert "submit" not in json.dumps(off)

    monkeypatch.setattr(settings, "web_actions_enabled", True)
    monkeypatch.setattr(
        settings, "web_action_keys", "k1:bngyMzYtdGVzdC1rZXktb25lLS0tLS0tLS0tLS0tLS0="
    )
    monkeypatch.setattr(settings, "web_action_ttl_s", 1800)
    tev._ring.cache_clear()
    on = tev.terminal_view(source, "ro")
    parse_view(on)
    dumped = json.dumps(on)
    assert '"type": "submit"' in dumped
    # Tokenul e opac: nici kind-ul, nici id-ul de catalog nu se citesc din el.
    assert "request_details" not in dumped and "p-1" not in dumped


def test_projection_of_actions_is_byte_deterministic(monkeypatch):
    source = _action_source()
    settings = get_settings()
    monkeypatch.setattr(settings, "web_actions_enabled", True)
    monkeypatch.setattr(
        settings, "web_action_keys", "k1:bngyMzYtdGVzdC1rZXktb25lLS0tLS0tLS0tLS0tLS0="
    )
    monkeypatch.setattr(settings, "web_action_ttl_s", 1800)
    tev._ring.cache_clear()
    a = json.dumps(tev.terminal_view(source, "ro"), sort_keys=True)
    b = json.dumps(tev.terminal_view(source, "ro"), sort_keys=True)
    assert a == b


# ── NX-249: asignarea de release la marginea de accept ────────────────────────


def _release_policy(*, mode="canary", percent=100, stage=6, rollback_compatible=False):
    from src.release.models import ReleasePolicy

    return ReleasePolicy(
        policy_id="nx249-api",
        revision=2,
        environment="test",
        created_at="2026-08-13T09:00:00+00:00",
        not_before="2026-08-13T10:00:00+00:00",
        expires_at="2026-09-13T10:00:00+00:00",
        control_release_sha="c0ntr0l1234567",
        control_pipeline_version="web-chat.v1",
        candidate_release_sha="cand1date7654321",
        candidate_pipeline_version="web-view.v2",
        mode=mode,
        percent=percent,
        stage=stage,
        eligible_business_ids=("b1",),
        stable_salt_id="salt-api",
        quality_packet_hash="sha256:q",
        e2e_packet_hash="sha256:e",
        deploy_manifest_hash="sha256:d",
        slo_policy_version="slo_policy.v1",
        quality_policy_version="nx246-gate-v1",
        rollback_compatible=rollback_compatible,
        approved_by="adi",
        approved_at="2026-08-13T09:30:00+00:00",
        change_ticket="NX-249",
    )


def _wire_release(monkeypatch, *, enabled=True, policy=None, available=True, code="ok"):
    """Storeul de policy, fără DB. `policy=None` + `available=False` = store căzut."""
    from src.release.policy_store import PolicyView

    settings = get_settings()
    monkeypatch.setattr(settings, "release_controller_enabled", enabled)
    monkeypatch.setattr(settings, "release_assignment_salt", "salt-de-test")

    async def fake_current(conn, environment, **kw):
        return PolicyView(
            policy=policy, revision=2 if policy else None, code=code, available=available
        )

    monkeypatch.setattr(wa.policy_store, "current", fake_current)


async def test_controllerul_stins_nu_trimite_context_de_release(monkeypatch):
    """OFF = byte-identic: `accept_web_turn` primește `release=None`, deci nu capturează nimic."""
    row = _row()
    captured = {}

    async def fake_accept(db, **kw):
        captured.update(kw)
        return ts.Accepted(row)

    _wire_v2(monkeypatch, accept=fake_accept)
    _wire_release(monkeypatch, enabled=False)
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 202
    assert captured["release"] is None


async def test_asignarea_ajunge_la_accept_si_se_emite_evenimentul(monkeypatch):
    """Contextul se construiește la MARGINE (unde se poate deschide control plane) și se
    consumă în checkout-ul de accept (unde nu se mai poate — NX-231)."""
    row = _row(release_track="candidate", release_policy_id="nx249-api", release_policy_revision=2)
    captured = {}

    async def fake_accept(db, **kw):
        captured.update(kw)
        assignment = kw["release"].decide("b1", "c1", None)
        return ts.Accepted(row, assignment=assignment)

    events, woken = _wire_v2(monkeypatch, accept=fake_accept)
    _wire_release(monkeypatch, policy=_release_policy())
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 202
    assert captured["release"] is not None
    assert captured["release"].mode == "canary"
    assigned = next(e for e in events if e.type == "release_assigned")
    assert assigned.properties["decision"] == "candidate"
    assert assigned.properties["policy_revision"] == 2
    # P12: `policy_id` nu e etichetă de metrică, dar nici ID-uri de tenant/conversație în event.
    assert "business_id" not in assigned.properties
    assert woken == [row.id]


async def test_conversatia_drenata_primeste_503_si_nu_creeaza_rand(monkeypatch):
    """Kill-switch fără compatibilitate dovedită: nu acceptăm, nu convertim, nu abandonăm."""
    from src.release.models import DECISION_DRAIN, Assignment

    row = _row()

    async def fake_accept(db, **kw):
        return ts.ReleaseDrained(
            Assignment(
                decision=DECISION_DRAIN,
                reason="rollback_incompatible",
                track=None,
                policy_id="nx249-api",
                policy_revision=2,
            )
        )

    events, woken = _wire_v2(monkeypatch, accept=fake_accept)
    _wire_release(monkeypatch, policy=_release_policy(mode="force_control", stage=6))
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 503
    payload = json.loads(res.body)
    assert payload["error"]["code"] == "release_draining"
    assert woken == [], "un turn drenat nu trezește niciun executor"
    assert not any(e.type == "web_turn_accepted" for e in events)


async def test_storeul_cazut_nu_rupe_acceptul_ci_da_control(monkeypatch):
    """Fail-closed: un control plane jos nu are voie să oprească traficul, doar canaryul."""
    row = _row()
    captured = {}

    async def fake_accept(db, **kw):
        captured["assignment"] = kw["release"].decide("b1", "c1", None)
        return ts.Accepted(row, assignment=captured["assignment"])

    _wire_v2(monkeypatch, accept=fake_accept)
    _wire_release(monkeypatch, policy=None, available=False, code="store_down")
    res = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    assert res.status_code == 202
    assert captured["assignment"].decision == "control"
    assert captured["assignment"].reason == "store_unavailable"


RELEASE_VOCABULARY = (
    "release_track",
    "release_policy",
    "policy_id",
    "candidate",
    "champion",
    "canary",
    "bucket",
    "percent",
    "force_control",
    "stable_salt",
)


async def test_frontendul_nu_afla_nimic_despre_canary(monkeypatch):
    """Boundary NENEGOCIABIL: frontendul nu știe procentul, bucketul, champion/candidate,
    kill-switchul sau motivul de rollback. El bootstrap-uiește contractul pe care backendul l-a
    ASIGNAT, trimite inputul și afișează ViewModelul.

    Testul scanează TEXTUL răspunsurilor (accept 202 + terminal), nu o listă de chei cunoscute: o
    gardă pe chei ar trece pe lângă un câmp nou adăugat mâine într-un sub-obiect.
    """
    row = _row(release_track="candidate", release_policy_id="nx249-api", release_policy_revision=2)

    async def fake_accept(db, **kw):
        return ts.Accepted(row, assignment=kw["release"].decide("b1", "c1", None))

    _wire_v2(monkeypatch, accept=fake_accept)
    _wire_release(monkeypatch, policy=_release_policy())
    accepted = await wa.web_turn_accept_v2(
        _Req(_body(row.client_turn_id)), token="tok", visitor_id="web_1", sig="s"
    )
    terminal = tev.terminal_view(
        _row(
            status="completed",
            response_json=COMPLETED_PAYLOAD,
            completed_at=NOW,
            release_track="candidate",
            release_policy_id="nx249-api",
            release_policy_revision=2,
        ),
        "ro",
    )
    for payload in (accepted.body.decode(), json.dumps(terminal)):
        lowered = payload.lower()
        for word in RELEASE_VOCABULARY:
            assert word not in lowered, f"vocabular de release pe sârmă: {word!r}"
