"""Teste unit pentru context builder (transcript + search_query bugetate)."""

from src.models import Author, Direction, Message
from src.worker.context import conversation_transcript, search_query


def _msg(direction: Direction, body: str) -> Message:
    author = Author.CONTACT if direction == Direction.INBOUND else Author.BOT
    return Message(direction=direction, author=author, body=body)


def test_transcript_excludes_current_and_labels_roles():
    history = [
        _msg(Direction.INBOUND, "caut o cremă"),
        _msg(Direction.OUTBOUND, "Îți recomand X"),
        _msg(Direction.INBOUND, "mai ieftin"),  # mesajul CURENT (ultimul) → exclus
    ]
    t = conversation_transcript(history)
    assert "Client: caut o cremă" in t
    assert "Asistent: Îți recomand X" in t
    assert "mai ieftin" not in t


def test_transcript_empty_for_first_message():
    assert conversation_transcript([_msg(Direction.INBOUND, "salut")]) == ""
    assert conversation_transcript([]) == ""


def test_transcript_budget_max_turns():
    history = [_msg(Direction.INBOUND, f"m{i}") for i in range(20)]
    t = conversation_transcript(history, max_turns=3)
    assert "m18" in t and "m16" in t  # ultimele 3 din prior (m16,m17,m18)
    assert "m15" not in t


def test_search_query_joins_recent_user_messages():
    history = [
        _msg(Direction.INBOUND, "cremă hidratantă ten uscat"),
        _msg(Direction.OUTBOUND, "uite X"),
        _msg(Direction.INBOUND, "mai ieftin"),
    ]
    q = search_query(history, "mai ieftin", n=2)
    assert "cremă hidratantă ten uscat" in q
    assert "mai ieftin" in q
    assert "uite X" not in q  # doar mesajele CLIENTULUI


def test_search_query_fallback_to_current():
    assert search_query([], "salut") == "salut"
