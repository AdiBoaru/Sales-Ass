"""D14 — graniță fail-closed pentru text trimis către un serviciu extern.

Tiparele sunt împărțite în DOUĂ familii, pentru că nu au aceeași natură și nu au aceiași
consumatori:

- **IDENTIFICATORI** (`PII_PATTERNS`) — email, telefon, IBAN, card/CNP, adresă, nume declarat.
  Sunt PII în sens strict: identifică o persoană. Orice text care îi conține e nepublicabil,
  oriunde ar merge.
- **SUBIECTE SENSIBILE** (`SENSITIVE_TOPIC_PATTERNS`) — date de sănătate. NU identifică pe nimeni;
  sunt o categorie specială de date. Separate ca familie pentru că un consumator poate avea nevoie
  să verifice doar identificatorii (ex. validarea qrels: „ten sensibil" e conținut legitim de
  benchmark, un număr de telefon nu e).

Un singur loc unde sunt definite: înainte existau două regex-uri de PII în branch
(`src/evals/retrieval/validation.py` avea al doilea, deja divergent), adică două răspunsuri
posibile la aceeași întrebare.
"""

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

#: Identifică o PERSOANĂ. Verificat de oricine exportă text în afara sistemului.
PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    _EMAIL_RE,
    _IBAN_RE,
    _PHONE_RE,
    _CARD_OR_CNP_RE,
    _ADDRESS_RE,
    _DECLARED_NAME_RE,
)

#: Categorie specială de date (sănătate). Nu identifică pe nimeni.
SENSITIVE_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = (_HEALTH_RE,)


def contains_pii(text: str) -> bool:
    """True dacă textul conține un IDENTIFICATOR de persoană. Normalizează întâi (fără diacritice,
    lower) — altfel „Bulevardul" și „bulevardul" ar fi întrebări diferite."""
    return any(pattern.search(normalize(text)) for pattern in PII_PATTERNS)


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
    forbidden = (*PII_PATTERNS, *SENSITIVE_TOPIC_PATTERNS)
    return None if any(pattern.search(text) for pattern in forbidden) else text
