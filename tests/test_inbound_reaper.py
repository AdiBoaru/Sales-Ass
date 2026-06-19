"""NX-86 — claim-or-resume + mark_completed + cleanup + reaper XAUTOCLAIM. Fără DB/Redis real:
`conn`/`redis`/`debouncer` false. Logica de filtrare orfani trăiește în SQL (verificat la deploy
cu apply_012.py); aici testăm contractul wrapper-elor + bucla reaper-ului."""

import json

from redis.exceptions import ResponseError

from src.db.queries import inbound_dedupe as dd
from src.worker import consumer as cons


class _Conn:
    def __init__(self, fetchval_ret=None):
        self._fetchval_ret = fetchval_ret
        self.sql = ""
        self.params = ()
        self.executed: list = []

    async def fetchval(self, sql, *args):
        self.sql = sql
        self.params = args
        return self._fetchval_ret

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "DELETE 3"


# --- claim-or-resume ---------------------------------------------------------


async def test_claim_new_or_orphan_returns_true():
    c = _Conn(fetchval_ret=1)  # rând întors = nou SAU orfan reclamat
    assert await dd.claim_inbound(c, "biz", "m1") is True
    assert (
        "do update" in c.sql and "completed_at is null" in c.sql
    )  # claim-or-resume, nu DO NOTHING


async def test_claim_finalized_or_recent_returns_false():
    c = _Conn(fetchval_ret=None)  # zero rânduri = finalizat sau revendicat recent → skip
    assert await dd.claim_inbound(c, "biz", "m1") is False


async def test_mark_completed_sets_watermark():
    c = _Conn()
    await dd.mark_inbound_completed(c, "biz", "m1")
    sql, args = c.executed[-1]
    assert "completed_at = now()" in sql and args == ("biz", "m1")


async def test_cleanup_purges_finalized_and_orphans():
    c = _Conn()
    n = await dd.cleanup_inbound_dedupe(c, older_than_hours=48, orphan_age_days=7)
    sql = c.executed[-1][0]
    assert "completed_at is not null" in sql  # criteriul 1: finalizate vechi
    assert "completed_at is null" in sql  # criteriul 2: orfani abandonați
    assert n == 3


# --- reaper XAUTOCLAIM -------------------------------------------------------


class _Redis:
    def __init__(self, xautoclaim_ret=None, raise_err=False):
        self._ret = xautoclaim_ret
        self._raise = raise_err
        self.acked: list = []

    async def xautoclaim(self, *a, **k):
        if self._raise:
            raise ResponseError("NOGROUP no such group")
        return self._ret

    async def xack(self, stream, group, *ids):
        self.acked.extend(ids)


class _Deb:
    def __init__(self):
        self.added: list = []

    async def add(self, event, msg_id=None):
        self.added.append((event, msg_id))


async def test_reap_reprocesses_message_via_debounce():
    evt = {"kind": "message", "channel_kind": "telegram", "body": "x", "provider_msg_id": "p"}
    r = _Redis(xautoclaim_ret=["0-0", [("1-0", {"data": json.dumps(evt)})], []])
    deb = _Deb()
    n = await cons.reap_pending(None, r, "worker-x", deb)
    assert n == 1
    assert deb.added == [(evt, "1-0")]  # mesaj → debounce (ACK delegat NX-87)
    assert r.acked == []  # NU ACK aici (Debouncer-ul ACK-uiește după flush)


async def test_reap_deleted_entry_ack_only():
    r = _Redis(xautoclaim_ret=["0-0", [("1-0", None)], []])  # fields None = intrare ștearsă
    n = await cons.reap_pending(None, r, "worker-x", _Deb())
    assert n == 1 and r.acked == ["1-0"]  # doar ACK, fără reprocesare


async def test_reap_xautoclaim_error_is_graceful():
    r = _Redis(raise_err=True)  # ex. NOGROUP → logat, nu oprește bucla
    assert await cons.reap_pending(None, r, "worker-x", _Deb()) == 0


async def test_reap_empty_pel_returns_zero():
    r = _Redis(xautoclaim_ret=["0-0", [], []])
    assert await cons.reap_pending(None, r, "worker-x", _Deb()) == 0
