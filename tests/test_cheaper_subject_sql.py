"""NX-314 — SQL-ul «mai ieftin» ordonat după subiect, fără DB.

Aceeași poartă ca la NX-313: fiecare `$n` legat apare în SQL (un parametru legat și nefolosit
crăpa căutarea pe `main`), ordinea clauzelor e cea decisă, iar cu flagul stins SQL-ul e cel de
dinainte, byte cu byte. Ordinea pe catalogul real e în `test_cheaper_subject_db.py`.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.agent.planner import resolve_cheaper_followup
from src.config import get_settings
from src.conversation.subject import SUBJECT_KEY, ConversationSubject
from src.db.queries.catalog import cheaper_by_subject_sql, search_cheaper_than
from src.models import BusinessConfig, Contact, InboundMessage, ProductRef, TurnContext

CREMA = "crema de fata"
_REFS = ["00000000-0000-0000-0000-000000000001"]


class _Conn:
    """Înregistrează SQL-ul și argumentele; întoarce rândurile date."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [dict(r) for r in self.rows]


def _placeholders(sql: str) -> set[int]:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


def _where(sql: str) -> str:
    """Clauza WHERE a query-ului EXTERIOR: `_SELECT` are propriile `order by` în subinterogări."""
    return sql.split(" where p.business_id", 1)[1].rsplit(" order by ", 1)[0]


def _order(sql: str) -> str:
    return sql.rsplit(" order by ", 1)[1]


def _assert_every_bound_param_is_used(sql: str, n_args: int) -> None:
    assert _placeholders(sql) == set(range(1, n_args + 1)), (
        f"legate {n_args}, folosite {sorted(_placeholders(sql))}"
    )


# ── SQL-ul pur ──────────────────────────────────────────────────────────────────────────────────


def test_every_bound_parameter_appears_in_the_sql():
    for subject in (
        ConversationSubject(product_type=CREMA),
        ConversationSubject(product_type=CREMA, needs=(("concerns", "hydration"),)),
        ConversationSubject(shelf_key="ten", product_type=CREMA),
        ConversationSubject(
            shelf_key="ten", product_type=CREMA, needs=(("concerns", "hydration"),)
        ),
    ):
        sql, extra = cheaper_by_subject_sql(subject, content_status=None)
        _assert_every_bound_param_is_used(sql, 4 + len(extra))


def test_the_order_is_type_then_needs_then_rating_then_closest_price():
    sql, _ = cheaper_by_subject_sql(
        ConversationSubject(product_type=CREMA, needs=(("concerns", "hydration"),)),
        content_status=None,
    )
    order = _order(sql)
    positions = [
        order.index("'product_type') is not distinct from"),
        order.index("unnest("),
        order.index("* 4.0)"),  # ratingul shrunk
        order.index(") desc, p.id"),  # prețul efectiv, DESCRESCĂTOR, apoi tie-break
    ]
    assert positions == sorted(positions)
    assert " asc" not in order, "«mai ieftin» nu mai înseamnă «cel mai ieftin din raft»"
    assert sql.rstrip().endswith("limit $4")


def test_the_hard_gates_are_the_same_as_today():
    """Ordinea se schimbă, porțile nu: tenant, activ, în stoc, strict sub prag, fără afișate."""
    sql, _ = cheaper_by_subject_sql(ConversationSubject(product_type=CREMA), content_status=None)
    where = _where(sql)
    for gate in (
        " = $1 and p.status = 'active'",
        "p.availability in ('in_stock', 'low_stock')",
        "p.id <> all($2::uuid[])",
        "< $3",
        "p.primary_category_id in (",
    ):
        assert gate in where
    assert "product_type" not in where, "tipul ORDONEAZĂ, nu exclude (enforce_ready: false)"


def test_the_shelf_widens_the_category_gate_and_never_replaces_it():
    """Raftul persistat e cel CĂUTAT de model, deci poate fi o ghicitură (NX-313). Ca reuniune,
    pool-ul de azi (categoria setului afișat) rămâne mereu inclus."""
    sql, extra = cheaper_by_subject_sql(
        ConversationSubject(shelf_key="ten", product_type=CREMA), content_status=None
    )
    where = _where(sql)
    assert "(p.primary_category_id in (" in where
    assert " or exists (select 1 from categories reqc" in where or " or (lower(c.slug)" in where
    assert extra[0] == ["ten"]


def test_no_needs_means_no_needs_clause():
    sql, extra = cheaper_by_subject_sql(
        ConversationSubject(product_type=CREMA), content_status=None
    )
    assert "unnest(" not in sql and extra == [CREMA]


def test_the_content_status_gate_is_kept():
    sql, _ = cheaper_by_subject_sql(ConversationSubject(product_type=CREMA), content_status="X_CS")
    assert " and X_CS order by " in sql


# ── search_cheaper_than: byte-identic fără tip ──────────────────────────────────────────────────


async def _sql_of(**kw: Any) -> str:
    conn = _Conn()
    await search_cheaper_than(conn, "b1", _REFS, 110.0, **kw)
    return conn.calls[0][0]


async def test_without_a_typed_subject_the_sql_is_byte_identical():
    legacy = await _sql_of()
    assert await _sql_of(subject=None) == legacy
    assert await _sql_of(subject=ConversationSubject(shelf_key="ten")) == legacy
    assert "order by coalesce(vp.price" in legacy and " asc, " in legacy


async def test_rows_are_marked_as_matches_or_fillers():
    """Doar 2 creme mai ieftine ⇒ 2 potriviri, apoi completări marcate `subject_match=false`."""
    rows = [
        {"id": "a", "name": "Crema A", "price": 90.0, "attributes": {"product_type": CREMA}},
        {"id": "b", "name": "Crema B", "price": 70.0, "attributes": {"product_type": CREMA}},
        {"id": "c", "name": "Masca C", "price": 10.0, "attributes": {"product_type": "masca"}},
        {"id": "d", "name": "Plasture", "price": 5.0, "attributes": {}},
    ]
    conn = _Conn(rows)
    got = await search_cheaper_than(
        conn, "b1", _REFS, 110.0, subject=ConversationSubject(product_type=CREMA)
    )
    assert [p["subject_match"] for p in got] == [True, True, False, False]
    sql, args = conn.calls[0]
    _assert_every_bound_param_is_used(sql, len(args))
    assert args[:4] == ("b1", _REFS, 110.0, 6)


# ── resolve_cheaper_followup: cine trimite subiectul ────────────────────────────────────────────


class _Policy:
    def gate(self, ctx, products, purpose):  # noqa: ARG002
        return (products,)


class _Deps:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def _cm(self):
        yield self.conn

    def db(self, op: str):  # noqa: ARG002
        return self._cm()


def _ctx() -> TurnContext:
    ctx = TurnContext(
        turn_id="t4",
        business=BusinessConfig(id="b1", slug="demo", name="Demo"),
        contact=Contact(id="c1", business_id="b1"),
        message=InboundMessage(provider_msg_id="m4", body="si ceva mai ieftin"),
        conversation_id="conv1",
    )
    ctx.state.displayed_products = [
        ProductRef(product_id=_REFS[0], name="SOME BY MI Yuja Niacin", price=110.0)
    ]
    ctx.state.search_constraints = {
        SUBJECT_KEY: {"type": CREMA, "needs": [["concerns", "hydration"]]}
    }
    return ctx


@pytest.fixture
def _flag(monkeypatch):
    def _set(on: bool) -> None:
        monkeypatch.setattr(get_settings(), "conversation_subject_enabled", on)

    return _set


async def test_flag_off_sends_no_subject_and_the_sql_is_the_old_one(_flag):
    _flag(False)
    conn = _Conn()
    ctx = _ctx()
    await resolve_cheaper_followup(ctx, _Deps(conn), policy=_Policy())
    assert conn.calls[0][0] == await _sql_of()
    ev = next(e for e in ctx.events if e.type == "cheaper_followup")
    assert set(ev.properties) - {"turn_id"} == {"baseline", "found"}


async def test_flag_on_orders_by_the_stored_subject(_flag):
    _flag(True)
    rows = [{"id": "a", "name": "Crema", "price": 70.0, "attributes": {"product_type": CREMA}}]
    conn = _Conn(rows)
    ctx = _ctx()
    outcome = await resolve_cheaper_followup(ctx, _Deps(conn), policy=_Policy())
    sql, args = conn.calls[0]
    assert "is not distinct from" in sql
    assert CREMA in args and ["concerns"] in args and ["hydration"] in args
    assert outcome.products[0]["subject_match"] is True
    ev = next(e for e in ctx.events if e.type == "cheaper_followup")
    assert ev.properties["subject_type_known"] is True
    assert ev.properties["type_matched"] == 1


async def test_nothing_cheaper_is_still_the_honest_reply_and_a_price_gap(_flag):
    _flag(True)
    ctx = _ctx()
    outcome = await resolve_cheaper_followup(ctx, _Deps(_Conn([])), policy=_Policy())
    assert outcome.handled and ctx.reply is not None
    gap = [e for e in ctx.events if e.type == "unmet_query"]
    assert gap and gap[0].properties["reason"] == "price_gap"
