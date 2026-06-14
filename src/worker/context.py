"""Stagiul 6 (logică) — Context builder: pregătește contextul conversației pentru
prompturile LLM (triaj + agent), cu BUGET impus în cod (principiul 4).

Istoricul e deja încărcat în `ctx.history` de processor (max 8 mesaje, cel mai
recent ultimul — INCLUSIV mesajul curent). Aici doar îl formatăm compact și
bugetăm. Profil client + state compact + summarizer pentru conversații lungi =
adăugiri ulterioare (rămân în acest modul).
"""

from src.models import Direction, Message


def conversation_transcript(
    history: list[Message], *, max_turns: int = 6, max_chars: int = 1200
) -> str:
    """Transcript compact „Client/Asistent" al mesajelor ANTERIOARE (fără cel curent
    — ultimul din `history` e mesajul în curs de procesare). Gol dacă nu există
    context anterior. Bugetat: ultimele `max_turns` mesaje, tăiat la `max_chars`."""
    prior = history[:-1] if history else []
    lines: list[str] = []
    for m in prior[-max_turns:]:
        body = (m.body or "").strip()
        if not body:
            continue
        role = "Client" if m.direction == Direction.INBOUND else "Asistent"
        lines.append(f"{role}: {body}")
    return "\n".join(lines)[-max_chars:]


def search_query(history: list[Message], current: str, *, n: int = 2) -> str:
    """Textul pentru căutare = ultimele `n` mesaje ale CLIENTULUI (inclusiv cel
    curent), ca follow-up-urile scurte („ceva mai ieftin", „și pentru păr") să
    caute în contextul corect, nu izolat. Fallback: mesajul curent."""
    users = [
        (m.body or "").strip()
        for m in history
        if m.direction == Direction.INBOUND and (m.body or "").strip()
    ]
    if not users:
        return current.strip()
    return " ".join(users[-n:])
