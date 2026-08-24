"""NX-256 — captura FULL a turului (`conversation_traces`).

Trei lucruri se verifică, fiecare legat de incidentul din 24 aug (rich degradat pe
membership fără nicio urmă despre ce emisese modelul):

  1. dropul de membership NU mai e mut: `rich_membership_dropped` (event cu id-urile
     respinse) + `ctx.trace` populat, iar itemii legitimi supraviețuiesc;
  2. `_persist_trace`: flag OFF = zero scrieri (absorbant); ON = un insert cu clientul
     în forma SAFE (NX-230), Reply-ul complet serializat, ref-urile recomandării și
     diagnosticele; un eșec de insert NU propagă (best-effort);
  3. gărzile de schemă: cleanup + GDPR delete sunt no-op pe o DB fără migrarea 045.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from src.db.queries import traces as traces_q
from src.models import (
    BusinessConfig,
    Chip,
    Contact,
    InboundMessage,
    Reply,
    RichItem,
    RichReply,
    TurnContext,
    TurnUsage,
)
from src.worker import aftercare as ac
from src.worker import compose

BIZ = BusinessConfig(id="biz", slug="s", name="n")


def _ctx(reply: Reply | None = None) -> TurnContext:
    ctx = TurnContext(
        turn_id="t-1",
        business=BIZ,
        contact=Contact(id="c-1", business_id="biz"),
        message=InboundMessage(
            provider_msg_id="m-1",
            body="ai si altele ?",
            safe_body="ai si altele ?",
            channel_kind="webchat",
        ),
        conversation_id="conv-1",
        language="ro",
    )
    ctx.reply = reply
    return ctx


# --- 1. membership drop → event + trace, itemii legitimi rămân ---------------------------


def test_assemble_records_foreign_ids_and_keeps_members() -> None:
    ctx = _ctx()
    retrieved = [
        {"id": "p-real", "name": "Ser A", "price": 10.0, "product_url": "u", "stock_total": 3}
    ]
    j = {
        "items": [
            {"product_id": "p-real", "fit_clause": "pentru ten gras"},
            {"product_id": "p-strain-1"},
            {"product_id": "p-strain-2"},
        ]
    }
    rich = compose.assemble(ctx, j, retrieved)
    # itemul legitim supraviețuiește; străinii sunt DROP (poarta nu se relaxează)
    assert [it.product_id for it in rich.items] == ["p-real"]
    assert ctx.trace["rich_membership_dropped"] == ["p-strain-1", "p-strain-2"]
    ev = next(e for e in ctx.events if e.type == "rich_membership_dropped")
    assert ev.properties["n"] == 2
    assert ev.properties["product_ids"] == ["p-strain-1", "p-strain-2"]


def test_assemble_all_members_stays_silent() -> None:
    ctx = _ctx()
    retrieved = [{"id": "p1", "name": "A", "price": 5.0}]
    compose.assemble(ctx, {"items": [{"product_id": "p1"}]}, retrieved)
    assert "rich_membership_dropped" not in ctx.trace
    assert all(e.type != "rich_membership_dropped" for e in ctx.events)


# --- 2. _persist_trace: flag OFF absorbant / ON = un insert complet ----------------------


class _SpyDb:
    """Provider fals: numără checkout-urile și înregistrează argumentele insertului."""

    def __init__(self):
        self.checkouts = 0
        self.executed: list[tuple] = []

    def __call__(self, *a):
        return self._cm()

    @asynccontextmanager
    async def _cm(self):
        self.checkouts += 1
        db = self

        class _Conn:
            async def execute(self, sql, *args):
                db.executed.append((sql, args))

        yield _Conn()


def _work(ctx: TurnContext) -> ac.AftercareWork:
    return ac.AftercareWork(
        business=BIZ,
        conversation_id="conv-1",
        contact_id="c-1",
        ctx=ctx,
        inbound_msg_id="m-1",
        shadow_mode=False,
        llm=None,
        language="ro",
    )


async def test_persist_trace_flag_off_is_absorbing(monkeypatch) -> None:
    monkeypatch.setattr(
        ac, "get_settings", lambda: SimpleNamespace(conversation_trace_enabled=False)
    )
    db = _SpyDb()
    await ac._persist_trace(db, _work(_ctx(Reply(text="salut"))))
    assert db.checkouts == 0  # OFF = zero I/O, byte-identic


async def test_persist_trace_writes_full_row(monkeypatch) -> None:
    monkeypatch.setattr(
        ac, "get_settings", lambda: SimpleNamespace(conversation_trace_enabled=True)
    )
    rich = RichReply(
        intro="Am găsit",
        items=[RichItem(product_id="p1", name="Ser A", price=10.0, reason="pentru ten gras")],
        pick=None,
        education=None,
        chips=[Chip(label="Mai ieftin", payload="Mai ieftin")],
        disclaimer="d",
    )
    ctx = _ctx(Reply(text="Am găsit un ser.", rich=rich))
    ctx.message.body = "textul BRUT, pre-redactare"  # safe_body rămâne forma persistabilă
    ctx.trace["rich_raw"] = {"items": [{"product_id": "p1"}]}
    ctx.usage = TurnUsage(models=["gpt-5.4-nano", "gpt-5.6-luna"])
    db = _SpyDb()
    await ac._persist_trace(db, _work(ctx))
    assert db.checkouts == 1
    (sql, args) = db.executed[0]
    assert "conversation_traces" in sql
    # ordinea parametrilor din insert_trace: biz, conv, contact, turn, canal, limbă, modele,
    # client, bot, reply, recommended, diagnostics
    assert args[0:5] == ("biz", "conv-1", "c-1", "t-1", "webchat")
    assert args[6] == "gpt-5.4-nano,gpt-5.6-luna"
    assert args[7] == "ai si altele ?"  # forma SAFE (NX-230), nu body-ul brut
    assert args[8] == "Am găsit un ser."
    assert '"reason": "pentru ten gras"' in args[9]  # Reply-ul COMPLET, cu tot cardul
    assert '"product_id": "p1"' in args[10]  # recommended = ref-uri
    assert '"rich_raw"' in args[11]  # diagnosticele intermediare


async def test_persist_trace_failure_does_not_propagate(monkeypatch) -> None:
    monkeypatch.setattr(
        ac, "get_settings", lambda: SimpleNamespace(conversation_trace_enabled=True)
    )

    class _BoomDb:
        def __call__(self, *a):
            raise RuntimeError("db down")

    await ac._persist_trace(_BoomDb(), _work(_ctx(Reply(text="x"))))  # nu ridică


def test_recommended_refs_priority() -> None:
    rich = RichReply(
        intro=None,
        items=[RichItem(product_id="p1", name="A", price=1.0)],
        pick=None,
        education=None,
        chips=[],
        disclaimer="d",
    )
    r = ac._recommended_refs(Reply(text="t", rich=rich, products=[{"product_id": "px"}]))
    assert r == [{"product_id": "p1", "name": "A", "price": 1.0}]  # rich bate products
    r2 = ac._recommended_refs(Reply(text="t", products=[{"product_id": "px", "name": "X"}]))
    assert r2 is not None and r2[0]["product_id"] == "px"
    assert ac._recommended_refs(Reply(text="t")) is None
    assert ac._recommended_refs(None) is None


# --- 3. gărzile de schemă: no-op fără migrarea 045 ---------------------------------------


class _NoTableConn:
    """Conexiune falsă pe o DB FĂRĂ migrarea 045: `to_regclass` → NULL, deci fals."""

    def __init__(self):
        self.deletes = 0

    async def fetchval(self, sql, *a):
        assert "to_regclass" in sql
        return False

    async def execute(self, sql, *a):
        self.deletes += 1
        return "DELETE 0"


async def test_cleanup_and_gdpr_are_noop_without_migration() -> None:
    conn = _NoTableConn()
    assert await traces_q.cleanup_conversation_traces(conn) == 0
    assert await traces_q.delete_traces_for_contact(conn, "biz", "c-1") == 0
    assert conn.deletes == 0  # gărzile opresc ÎNAINTE de delete
