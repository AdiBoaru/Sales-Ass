"""G6-2 felia 2 — summarizer. Generare (ScriptedLLM) + redactare PII + orchestrarea hook-ului
post-tur (query-uri monkeypatch-uite). ZERO OpenAI/DB real."""

from datetime import UTC, datetime, timedelta

from src.models import Author, Direction, Message
from src.worker import processor as proc
from src.worker import summarizer as sm


def _msg(direction: Direction, body: str, *, mins: int = 0) -> Message:
    author = Author.CONTACT if direction == Direction.INBOUND else Author.BOT
    return Message(
        direction=direction,
        author=author,
        body=body,
        created_at=datetime(2026, 6, 16, 10, 0, tzinfo=UTC) + timedelta(minutes=mins),
    )


# --- redactare PII + prompt ---------------------------------------------------


def test_redact_pii_masks_phone_like():
    assert sm._redact_pii("sună-mă la 0712 345 678 te rog") == "sună-mă la *** te rog"
    assert sm._redact_pii("nr +40712345678 acum") == "nr *** acum"
    assert sm._redact_pii("preț 82.99 lei") == "preț 82.99 lei"  # prețul NU e telefon


def test_build_summary_prompt_includes_prev_and_messages():
    system, user = sm.build_summary_prompt(
        [_msg(Direction.INBOUND, "caut cremă")], prev_summary="Rezumat vechi", language="ro"
    )
    assert "telefon" in system.lower()  # instrucțiunea anti-PII
    assert "Rezumat de până acum:\nRezumat vechi" in user
    assert "Client: caut cremă" in user


# --- generate_summary (ScriptedLLM) ------------------------------------------


class _ScriptedLLM:
    model_triage = "nano"
    model_agent = "mini"

    def __init__(self, out="REZUMAT", *, boom=False):
        self._out = out
        self.boom = boom
        self.calls: list[dict] = []

    async def complete(self, system, user, *, model=None):
        self.calls.append({"model": model, "user": user})
        if self.boom:
            raise RuntimeError("api down")
        return self._out


async def test_generate_summary_uses_nano_and_redacts():
    llm = _ScriptedLLM(out="Clientul a cerut numărul 0712345678")
    out = await sm.generate_summary(llm, [_msg(Direction.INBOUND, "salut")], None, "ro")
    assert out == "Clientul a cerut numărul ***"  # PII redactat în output
    assert llm.calls[0]["model"] == "nano"  # FORȚEAZĂ model_triage (nano), nu mini


async def test_generate_summary_empty_messages_is_none():
    llm = _ScriptedLLM()
    assert await sm.generate_summary(llm, [], None, "ro") is None
    assert llm.calls == []  # nici nu cheamă modelul


# --- _summarize_if_needed (orchestrare hook post-tur) ------------------------


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _Tx()


class _Ctx:
    language = "ro"


def _patch(monkeypatch, *, total, prev, to_summarize, gen="REZUMAT"):
    sink: dict = {}

    async def f_count(conn, b, c):
        return total

    async def f_latest(conn, b, c):
        return prev

    async def f_window(conn, b, c, *, after, tail=8):
        sink["after"] = after
        return to_summarize

    async def f_generate(llm, msgs, prev_summary, language):
        sink["gen_args"] = {"n": len(msgs), "prev": prev_summary, "lang": language}
        return gen

    async def f_insert(conn, b, c, watermark, summary):
        sink["insert"] = {"watermark": watermark, "summary": summary}
        return "sid"

    async def f_cost(redis, b, amount):
        sink["cost"] = amount

    async def f_events(conn, b, events, *, conversation_id=None, contact_id=None):
        sink["events"] = [e.type for e in events]

    monkeypatch.setattr(proc, "count_messages", f_count)
    monkeypatch.setattr(proc, "get_latest_summary", f_latest)
    monkeypatch.setattr(proc, "get_messages_for_summary", f_window)
    monkeypatch.setattr(proc, "generate_summary", f_generate)
    monkeypatch.setattr(proc, "insert_conversation_summary", f_insert)
    monkeypatch.setattr(proc, "cost_add", f_cost)
    monkeypatch.setattr(proc, "insert_events", f_events)
    return sink


async def test_under_threshold_skips(monkeypatch):
    sink = _patch(monkeypatch, total=19, prev=None, to_summarize=[_msg(Direction.INBOUND, "x")])
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), object())
    assert "insert" not in sink and "gen_args" not in sink  # nici generare, nici scriere


async def test_first_summary_triggers_with_honest_watermark(monkeypatch):
    window = [_msg(Direction.INBOUND, "m1", mins=1), _msg(Direction.OUTBOUND, "m2", mins=2)]
    sink = _patch(monkeypatch, total=20, prev=None, to_summarize=window)
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), object())
    assert sink["insert"]["summary"] == "REZUMAT"
    assert sink["insert"]["watermark"] == window[-1].created_at  # cel mai nou INCLUS, nu MAX global
    assert sink["after"] is None  # prima generare: de la început
    assert sink["cost"] > 0 and sink["events"] == ["summarizer_run"]  # G2c + analytics


async def test_regen_skipped_when_too_few_new(monkeypatch):
    prev = {"summary": "vechi", "upto_message_at": datetime(2026, 6, 16, 9, tzinfo=UTC)}
    window = [_msg(Direction.INBOUND, f"m{i}", mins=i) for i in range(5)]  # 5 < delta(12)
    sink = _patch(monkeypatch, total=40, prev=prev, to_summarize=window)
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), object())
    assert "insert" not in sink  # sub delta → nu ardem un apel nano


async def test_regen_triggers_with_prev_summary_fed(monkeypatch):
    prev = {"summary": "vechi", "upto_message_at": datetime(2026, 6, 16, 9, tzinfo=UTC)}
    window = [_msg(Direction.INBOUND, f"m{i}", mins=i) for i in range(12)]  # >= delta
    sink = _patch(monkeypatch, total=40, prev=prev, to_summarize=window)
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), object())
    assert sink["gen_args"]["prev"] == "vechi"  # rezumatul anterior e dat generatorului
    assert sink["after"] == prev["upto_message_at"]  # fereastra ia de la watermark
    assert "insert" in sink


async def test_llm_none_skips(monkeypatch):
    sink = _patch(monkeypatch, total=50, prev=None, to_summarize=[_msg(Direction.INBOUND, "x")])
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), None)
    assert "gen_args" not in sink and "insert" not in sink  # cost guard / fără cheie → skip


async def test_best_effort_on_failure(monkeypatch):
    sink = _patch(monkeypatch, total=20, prev=None, to_summarize=[_msg(Direction.INBOUND, "x")])

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(proc, "insert_conversation_summary", boom)
    # nu trebuie să propage (turul a răspuns deja)
    await proc._summarize_if_needed(_FakeConn(), object(), "b", "conv", _Ctx(), object())
    assert "cost" not in sink  # n-a ajuns la cost_add după eșecul de insert
