"""Summarizer conversații lungi (G6-2 felia 2) — generarea rezumatului rolling.

Logică izolată de processor (testabilă unit fără DB). Rulează POST-TUR async (vezi
processor._summarize_if_needed): comprimă mesajele care ies din fereastra de 8 într-un rezumat
de fundal, ca prompturile triaj/agent să țină firul pe conversații lungi FĂRĂ să trimită zeci
de mesaje vechi.

LLM = modelul AGENTULUI (NX-297: nano a ieșit din proiect), chemat POST-TUR — NU un al treilea
punct LLM în pipeline-ul sincron (principiul 2). PII (principiul 12):
redactare defensivă a secvențelor tip telefon, ÎN PLUS de instrucțiunea din system prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.models import Direction

if TYPE_CHECKING:
    from src.models import Message

# Secvențe tip telefon (E.164-ish): +? urmat de cifre/spații/cratime, ≥8 cifre. Redactare
# defensivă — telefonul trăiește în channel_identities (P12), nu trebuie să ajungă în rezumat.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{6,}\d")

_SYSTEM = (
    "Ești un asistent care rezumă o conversație de vânzări pentru un magazin online. "
    "Scrii un rezumat SCURT și factual, în limba clientului, care păstrează: ce caută clientul, "
    "produsele și prețurile discutate, deciziile, constrângerile (buget, tip de ten, preferințe) "
    "și obiecțiile. NU include numere de telefon sau date personale de contact. "
    "Maxim ~5 propoziții, fără markdown."
)


def _redact_pii(text: str) -> str:
    """Înlocuiește secvențele tip telefon cu „***" (best-effort, P12)."""
    return _PHONE_RE.sub("***", text or "")


def _transcript(messages: list[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        body = _redact_pii((m.body or "").strip())
        if not body:
            continue
        role = "Client" if m.direction == Direction.INBOUND else "Asistent"
        lines.append(f"{role}: {body}")
    return "\n".join(lines)


def build_summary_prompt(
    messages: list[Message], prev_summary: str | None, language: str
) -> tuple[str, str]:
    """(system, user) pentru apelul de sumarizare. System static; user = rezumat anterior +
    mesajele noi de integrat (cu PII redactat)."""
    prev_block = f"Rezumat de până acum:\n{prev_summary}\n\n" if prev_summary else ""
    user = (
        f"Limba clientului: {language}\n{prev_block}"
        f"Mesaje noi de integrat în rezumat:\n{_transcript(messages)}\n\n"
        "Întoarce rezumatul ACTUALIZAT (îl înlocuiește pe cel anterior), concis și factual."
    )
    return _SYSTEM, user


async def generate_summary(
    llm, messages: list[Message], prev_summary: str | None, language: str
) -> str | None:
    """Produce rezumatul actualizat. None dacă nu e nimic de sumarizat sau modelul întoarce gol.
    Ridică la eroare de API — caller-ul (hook) prinde.

    NX-297: rulează pe modelul AGENTULUI, nu pe nano. Rezumatul e memoria conversației — ce scrie
    prost aici se propagă în toate turele următoare, iar diferența de cost e zero (vezi
    `profile.py`). Un model mai bun pe același preț nu e o alegere, e o corecție."""
    if not messages:
        return None
    system, user = build_summary_prompt(messages, prev_summary, language)
    text = await llm.complete(system, user, model=llm.model_agent)
    return _redact_pii((text or "").strip()) or None
