"""D14 — graniță fail-closed pentru text trimis către un serviciu extern."""

from __future__ import annotations

import re

from src.domain.normalize import normalize

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)\d{1,3}(?:[ .-]?\d){8,11}|0(?:[ .-]?\d){9})(?!\d)")
_CARD_OR_CNP_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_ADDRESS_RE = re.compile(
    r"\b(?:strada|str\.?|bulevardul|bd\.?|bloc|scara|apartament|ap\.?|sector(?:ul)?|judet(?:ul)?)\b"
)
_DECLARED_NAME_RE = re.compile(
    r"\b(?:ma numesc|numele meu (?:este|e)|sunt)\s+[a-z]{2,}\s+[a-z]{2,}\b"
)
_HEALTH_RE = re.compile(
    r"\b(?:diabet(?:ic[aă]?)?|insarcinat[aă]?|sarcina|pregnant|cancer|hipertensiune|"
    r"astm|epilepsie|boala|afectiune|alergie (?:severa|medicala)|anemie|tiroida)\b"
)


def external_query_text(normalized_query: str) -> str | None:
    """Întoarce singurul text de query permis către embedding/reranking extern.

    `raw_query` nu este argument al acestei funcții. La orice indiciu de PII, adresă, nume
    declarat sau health data, nu încercăm să păstrăm fragmente „aproape sigure”: oprim exportul
    complet și apelantul cade pe retrieval intern. Astfel nu există o cale prin care un text
    liber sensibil să fie serializat accidental către un provider.
    """
    text = normalize(normalized_query).strip()
    if not text:
        return None
    forbidden = (
        _EMAIL_RE,
        _IBAN_RE,
        _PHONE_RE,
        _CARD_OR_CNP_RE,
        _ADDRESS_RE,
        _DECLARED_NAME_RE,
        _HEALTH_RE,
    )
    return None if any(pattern.search(text) for pattern in forbidden) else text
