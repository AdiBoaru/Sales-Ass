"""NX-161 F1 — DoD deferred: aftercare-ul folosește checkout-uri SCURTE, NU ține un conn peste LLM.

Un `SpyDb` (provider fals) numără checkout-urile și urmărește câte conexiuni sunt DESCHISE în orice
moment; LLM-urile (embed / generate_summary) înregistrează starea `open` la momentul apelului. Cele
două aserții cheie (regula 1 + fresh checkouts):
  • LLM-ul rulează cu ZERO conn deschis (`db_open_at_llm == 0`);
  • read și write sunt checkout-uri SEPARATE (`checkouts >= 2`, niciodată 2 simultan).
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from src.models import BusinessConfig, Contact, InboundMessage, Reply, TurnContext
from src.worker import aftercare as ac


class _NoopTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _SpyConn:
    def transaction(self):
        return _NoopTx()


class SpyDb:
    """Provider fals: fiecare `db()` = un checkout NOU. `open` = câte conexiuni sunt deschise ACUM
    (trebuie să rămână ≤1 și 0 pe durata LLM)."""

    def __init__(self):
        self.checkouts = 0
        self.open = 0
        self.max_concurrent = 0

    def __call__(self):
        return self._cm()

    @asynccontextmanager
    async def _cm(self):
        self.checkouts += 1
        self.open += 1
        self.max_concurrent = max(self.max_concurrent, self.open)
        try:
            yield _SpyConn()
        finally:
            self.open -= 1


class SpyLLM:
    """Înregistrează câte conexiuni erau DESCHISE când s-a chemat `embed` (trebuie 0 — regula 1)."""

    def __init__(self, spy_db: SpyDb):
        self._db = spy_db
        self.db_open_at_llm = None

    async def embed(self, texts):
        self.db_open_at_llm = self._db.open
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _ctx_dynamic() -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="biz", slug="s", name="n"),
        contact=Contact(id="c", business_id="biz"),
        message=InboundMessage(provider_msg_id="m", body="caut crema sub 80 lei"),  # dynamic
        conversation_id="conv",
        language="ro",
    )
    ctx.reply = Reply(
        text="Îți recomand crema X la 49.90 lei.",
        products=[{"product_id": "p1", "price": 49.9, "name": "Crema X"}],  # → tier dynamic
    )
    return ctx


async def test_cache_writeback_deferred_releases_conn_across_embed(monkeypatch):
    async def fake_dv(conn, bid):
        return 3

    async def fake_upsert(conn, *a, **k):
        pass

    monkeypatch.setattr(ac, "get_data_version", fake_dv)
    monkeypatch.setattr(ac, "upsert_entry", fake_upsert)

    spy_db = SpyDb()
    spy_llm = SpyLLM(spy_db)
    await ac._cache_writeback(spy_db, spy_llm, "biz", "ro", "caut crema sub 80 lei", _ctx_dynamic())

    # read (data_version) + write (upsert) = checkout-uri SEPARATE, niciodată 2 simultan.
    assert spy_db.checkouts == 2
    assert spy_db.max_concurrent == 1
    # embed-ul (LLM) a rulat cu ZERO conn deschis (regula 1 — nu ținem db() peste LLM).
    assert spy_llm.db_open_at_llm == 0


async def test_summarize_deferred_releases_conn_across_generate(monkeypatch):
    spy_db = SpyDb()
    recorded = {}

    async def fake_count(conn, b, c):
        return 50  # peste prag

    async def fake_latest(conn, b, c):
        return None

    async def fake_window(conn, b, c, *, after, tail=8):
        return [SimpleNamespace(created_at="2026-06-16T10:00:00Z")]

    async def fake_generate(llm, msgs, prev, lang):
        recorded["db_open_at_llm"] = spy_db.open  # trebuie 0 (reads închise ÎNAINTE de generare)
        return "REZUMAT"

    async def fake_insert_sum(conn, *a):
        return "sid"

    async def fake_events(conn, *a, **k):
        pass

    monkeypatch.setattr(ac, "count_messages", fake_count)
    monkeypatch.setattr(ac, "get_latest_summary", fake_latest)
    monkeypatch.setattr(ac, "get_messages_for_summary", fake_window)
    monkeypatch.setattr(ac, "generate_summary", fake_generate)
    monkeypatch.setattr(ac, "insert_conversation_summary", fake_insert_sum)
    monkeypatch.setattr(ac, "insert_events", fake_events)

    ctx = SimpleNamespace(language="ro", turn_id="t")
    await ac._summarize_if_needed(spy_db, None, "b", "conv", ctx, object())

    # reads (count/latest/window) + writes (summary/events) = checkout-uri SEPARATE.
    assert spy_db.checkouts == 2
    assert spy_db.max_concurrent == 1
    # generate_summary (LLM) a rulat cu conn ELIBERAT (regula 1).
    assert recorded["db_open_at_llm"] == 0
