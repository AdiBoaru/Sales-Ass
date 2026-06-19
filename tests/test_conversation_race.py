"""NX-87 — get_or_create_conversation race-safe (ON CONFLICT pe indexul parțial one-open).

Unit (fără DB): `conn` fals + monkeypatch pe `get_open_conversation`/`_row_to_dict` → exersăm cele
două ramuri: (1) INSERT reușit → conversație nouă; (2) ON CONFLICT DO NOTHING (zero rânduri, am
pierdut cursa) → re-citim conversația câștigătoare. Corectitudinea sub concurență REALĂ e impusă
de indexul `uq_conversations_one_open` (migrația 010), verificat la deploy cu apply_010.py.
"""

from src.db.queries import conversations as cq


class _Conn:
    """`fetchrow` întoarce rezultatul INSERT-ului (un rând la succes, None la ON CONFLICT)."""

    def __init__(self, insert_row):
        self._insert_row = insert_row
        self.insert_calls = 0

    async def fetchrow(self, sql, *args):
        self.insert_calls += 1
        return self._insert_row


async def test_creates_new_conversation_when_none_open(monkeypatch):
    async def no_open(conn, b, c, ch):
        return None

    monkeypatch.setattr(cq, "get_open_conversation", no_open)
    monkeypatch.setattr(cq, "_row_to_dict", lambda r: r)
    conn = _Conn(insert_row={"id": "conv-new"})

    res = await cq.get_or_create_conversation(conn, "b", "c", "ch")
    assert res == {"id": "conv-new"} and conn.insert_calls == 1


async def test_conflict_loss_reselects_winner(monkeypatch):
    # get_open: None (nicio conv) la prima chemare, apoi conversația câștigătoare la re-SELECT.
    seq = [None, {"id": "conv-winner"}]

    async def open_seq(conn, b, c, ch):
        return seq.pop(0)

    monkeypatch.setattr(cq, "get_open_conversation", open_seq)
    conn = _Conn(insert_row=None)  # INSERT → ON CONFLICT DO NOTHING → zero rânduri

    res = await cq.get_or_create_conversation(conn, "b", "c", "ch")
    assert res == {"id": "conv-winner"}  # am pierdut cursa → re-citit câștigătorul
    assert conn.insert_calls == 1  # un singur INSERT (nu retry orb)
