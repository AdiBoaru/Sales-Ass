"""NX-71 — poarta de gating proactiv. Stub pe `conn` (fetchval/fetchrow fabricate).

ZERO DB real, ZERO LLM/embeddings, ZERO rețea. Poarta e cod determinist: testăm
consent → fereastră → template + randarea variabilelor + propagarea erorii DB.
"""

from pathlib import Path

import pytest

from src.db.queries.wa_templates import get_approved_template
from src.models import Contact
from src.proactive.templates import (
    ProactiveDecision,
    _has_optin,
    decide_proactive,
    render_template,
)


class FakeConn:
    """Stub minimal: `fetchval` întoarce fereastra 24h, `fetchrow` întoarce template-ul."""

    def __init__(self, *, in_window=False, template_row=None, fetchrow_fail=None):
        self.in_window = in_window
        self.template_row = template_row
        self.fetchrow_fail = fetchrow_fail
        self.fetchval_calls: list[tuple] = []
        self.fetchrow_calls: list[tuple] = []

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.in_window

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if self.fetchrow_fail is not None:
            raise self.fetchrow_fail
        return self.template_row


def _contact(consent: dict) -> Contact:
    return Contact(id="contact-1", business_id="biz-1", consent=consent)


_CONV = {"id": "conv-1", "last_inbound_at": None}


async def _decide(conn, *, consent, kind="awb_update", values=None):
    return await decide_proactive(
        conn,
        business_id="biz-1",
        contact=_contact(consent),
        conversation=_CONV,
        channel_id="chan-1",
        kind=kind,
        locale="ro",
        template_name="awb_update",
        free_text="Comanda ta a fost expediată.",
        variables=values or {},
    )


# --------------------------------------------------------------------------- #
# _has_optin — convenția consent jsonb (override per-kind bate default-ul)
# --------------------------------------------------------------------------- #


def test_optin_default_proactive_for_transactional():
    assert _has_optin({"proactive": True}, "awb_update") is True
    assert _has_optin({}, "awb_update") is False
    assert _has_optin({"proactive": False}, "back_in_stock") is False


def test_optin_default_marketing_for_marketing_kinds():
    assert _has_optin({"marketing": True}, "abandoned_cart") is True
    assert _has_optin({"proactive": True}, "abandoned_cart") is False  # marketing ≠ proactive


def test_optin_per_kind_override():
    # opt-in fin: per-kind True bate absența default-ului de marketing
    assert _has_optin({"abandoned_cart": True}, "abandoned_cart") is True
    # opt-out fin: per-kind False bate default-ul de marketing True
    assert _has_optin({"marketing": True, "abandoned_cart": False}, "abandoned_cart") is False


# --------------------------------------------------------------------------- #
# render_template — placeholders poziționali {{n}}
# --------------------------------------------------------------------------- #


def test_render_template_positional():
    text = render_template(
        "AWB-ul tău {{1}} la {{2}}", ["awb", "courier"], {"awb": "123", "courier": "FAN"}
    )
    assert text == "AWB-ul tău 123 la FAN"


def test_render_template_missing_variable_is_empty():
    text = render_template("AWB {{1}} la {{2}}", ["awb", "courier"], {"awb": "123"})
    assert text == "AWB 123 la "  # {{2}} → string gol, nu crapă


# --------------------------------------------------------------------------- #
# decide_proactive — consent → fereastră → template
# --------------------------------------------------------------------------- #


async def test_no_optin_blocks_even_in_window():
    conn = FakeConn(in_window=True)
    dec = await _decide(conn, consent={})
    assert dec == ProactiveDecision(allowed=False, mode="blocked", reason="no_optin")
    assert conn.fetchval_calls == []  # nici fereastra nu se mai verifică


async def test_optin_false_blocks():
    dec = await _decide(FakeConn(in_window=True), consent={"proactive": False})
    assert dec.allowed is False and dec.reason == "no_optin"


async def test_in_window_returns_free_text_without_template_lookup():
    conn = FakeConn(in_window=True)
    dec = await _decide(conn, consent={"proactive": True})
    assert dec.allowed is True
    assert dec.mode == "free"
    assert dec.reason == "ok_free"
    assert dec.rendered_text == "Comanda ta a fost expediată."
    assert conn.fetchrow_calls == []  # în fereastră → ZERO lookup de template


async def test_out_of_window_renders_approved_template():
    template_row = {
        "id": "tmpl-1",
        "name": "awb_update",
        "language": "ro",
        "body": "AWB-ul tău {{1}} la {{2}}",
        "variables": ["awb", "courier"],
        "provider_template_id": "ptid-1",
    }
    conn = FakeConn(in_window=False, template_row=template_row)
    dec = await _decide(conn, consent={"proactive": True}, values={"awb": "123", "courier": "FAN"})
    assert dec.allowed is True
    assert dec.mode == "template"
    assert dec.reason == "ok_template"
    assert dec.rendered_text == "AWB-ul tău 123 la FAN"
    assert dec.template_id == "tmpl-1"
    assert dec.provider_template_id == "ptid-1"


async def test_out_of_window_no_template_blocks():
    conn = FakeConn(in_window=False, template_row=None)
    dec = await _decide(conn, consent={"proactive": True})
    assert dec.allowed is False
    assert dec.mode == "blocked"
    assert dec.reason == "no_window_no_template"


async def test_db_error_on_template_lookup_propagates():
    """DB jos la lookup template → excepția se propagă (NX-70 marchează jobul failed).
    Poarta NU întoarce allowed=True tăcut la incertitudine."""
    conn = FakeConn(in_window=False, fetchrow_fail=RuntimeError("db down"))
    with pytest.raises(RuntimeError, match="db down"):
        await _decide(conn, consent={"proactive": True})


# --------------------------------------------------------------------------- #
# get_approved_template — pasează corect filtrele (limbă = parte din cheie, P11)
# --------------------------------------------------------------------------- #


async def test_get_approved_template_passes_locale_and_returns_none():
    conn = FakeConn(template_row=None)
    res = await get_approved_template(
        conn, "biz-1", channel_id="chan-1", name="awb_update", locale="hu"
    )
    assert res is None
    # filtrele cheie ajung în query: business_id, channel, nume, limbă
    [(_query, args)] = conn.fetchrow_calls
    assert args == ("biz-1", "chan-1", "awb_update", "hu")


async def test_get_approved_template_deserializes_variables():
    row = {
        "id": "t1",
        "name": "awb_update",
        "language": "ro",
        "body": "x {{1}}",
        "variables": '["awb"]',  # vine ca str din jsonb → trebuie deserializat
        "provider_template_id": "p1",
    }
    res = await get_approved_template(
        FakeConn(template_row=row), "biz-1", channel_id="c", name="awb_update", locale="ro"
    )
    assert res["variables"] == ["awb"]


# --------------------------------------------------------------------------- #
# P7 + P11 — query-urile noi conțin filtrele obligatorii (verificare în sursă)
# --------------------------------------------------------------------------- #


def test_new_queries_filter_business_id_and_language():
    src = Path("src/db/queries")
    wa = (src / "wa_templates.py").read_text(encoding="utf-8")
    conv = (src / "conversations.py").read_text(encoding="utf-8")
    assert "business_id = $1" in wa
    assert "language = $4" in wa  # locale e parte din cheie (P11)
    assert "status = 'approved'" in wa
    assert "in_24h_window(c)" in conv
    assert "c.business_id = $1" in conv
