"""NX-160 felia 4 — safety gate pentru memorie (classify → visibility).

Al doilea pas din pipeline-ul de memorie (`capture broad → CLASSIFY SAFETY → canonicalize →
inject safe`). Modelul capturează LARG; codul decide ce are voie să ajungă în promptul agentului.
PUR (fără DB/LLM) — testabil izolat.

Regula critică (P0-safety, răspundere juridică): o CONDIȚIE medicală („sunt diabetic",
„însărcinată", „am cancer") NU se injectează niciodată automat — o marcăm `health`/`candidate`
(semnal intern, nefolosit pentru recomandări). DAR o PREFERINȚĂ comercială derivată din ea („fără
zahăr", „fără gluten") formulată ca restricție de produs e `safe`/`inject`. Diferența = *ce a spus
clientul*, nu *ce am dedus despre sănătatea lui*.

`safety_class`: safe | pii | health | financial | sensitive | unknown
`visibility`  : inject (poate ajunge în prompt) | candidate (stocat, nefolosit) | drop (nu stocăm)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Telefon E.164-ish (aceeași formă ca profile._PHONE_RE / summarizer). PII (P12).
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{6,}\d")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# IBAN RO / card 13-19 cifre / CNP RO (13 cifre) — financiar (drop).
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", re.IGNORECASE)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# --- chei care semnalează categorii periculoase (match pe raw_key ȘI canonical_key) ---
_PII_KEYS = frozenset(
    {"phone", "telefon", "email", "e_mail", "address", "adresa", "name", "full_name", "nume", "cnp"}
)
_FINANCIAL_KEYS = frozenset(
    {"card", "credit_card", "iban", "account_number", "cvv", "password", "parola", "pin"}
)
_HEALTH_KEYS = frozenset(
    {
        "health_condition",
        "medical_condition",
        "diagnosis",
        "disease",
        "condition",
        "pregnancy",
        "medication",
        "treatment",
        "afectiune",
        "boala",
        "diagnostic",
    }
)

# Termeni CLARI de condiție medicală în VALOARE — chiar sub o cheie inocentă, un diagnostic nu se
# injectează. Focalizat pe condiții, NU pe preferințe („fără zahăr" NU e aici → rămâne safe).
_HEALTH_VALUE_TERMS = frozenset(
    {
        "diabet",
        "diabetic",
        "diabetica",
        "insarcinat",
        "insarcinata",
        "pregnant",
        "sarcina",
        "cancer",
        "hipertensiune",
        "tensiune",
        "astm",
        "epilepsie",
        "boala",
        "afectiune",
        "alergie severa",
        "alergie medicala",
        "anemie",
        "tiroida",
    }
)


@dataclass(frozen=True)
class SafetyVerdict:
    """Rezultatul clasării: clasa + vizibilitatea derivată. `visibility` decide dacă faptul ajunge
    vreodată în prompt (`inject`), e stocat ca semnal intern (`candidate`) sau nici nu se persistă
    (`drop`)."""

    safety_class: str
    visibility: str


def _strip_diacritics(text: str) -> str:
    """Normalizare best-effort a diacriticelor RO pentru match pe termeni (ă→a, î→i, ș→s, ț→t)."""
    table = str.maketrans("ăâîșşțţĂÂÎȘŞȚŢ", "aaissttAAISSTT")
    return text.translate(table)


def _value_text(value: Any) -> str:
    """Aplatizează valoarea (str/list/dict/scalar) în text de scanat, lower + fără diacritice."""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (list, tuple)):
        raw = " ".join(_value_text(v) for v in value)
    elif isinstance(value, dict):
        raw = " ".join(_value_text(v) for v in value.values())
    else:
        raw = str(value) if value is not None else ""
    return _strip_diacritics(raw).lower()


def _looks_like_pii_value(text: str) -> bool:
    return bool(_PHONE_RE.search(text) or _EMAIL_RE.search(text))


def _looks_like_financial_value(text: str) -> bool:
    return bool(_IBAN_RE.search(text) or _CARD_RE.search(text))


def classify(raw_key: str, canonical_key: str | None, value: Any) -> SafetyVerdict:
    """Clasează un fact candidat → `SafetyVerdict`. Prioritate (cea mai periculoasă câștigă):
    financial > pii > health > safe. Cheile se testează normalizate; valoarea se scanează pt
    pattern-uri (telefon/email/IBAN/card) ȘI termeni de condiție medicală.

    - financiar (card/IBAN/CNP/parolă în cheie sau valoare) → `financial` / `drop`.
    - PII de contact (telefon/email/adresă/nume în cheie sau valoare) → `pii` / `drop`.
    - condiție medicală (cheie de sănătate sau termen de diagnostic în valoare) → `health` /
      `candidate` (stocat ca semnal, NU injectat).
    - restul → `safe` / `inject`.
    """
    keys = {_norm(raw_key), _norm(canonical_key or "")}
    text = _value_text(value)

    if keys & _FINANCIAL_KEYS or _looks_like_financial_value(text):
        return SafetyVerdict("financial", "drop")
    if keys & _PII_KEYS or _looks_like_pii_value(text):
        return SafetyVerdict("pii", "drop")
    if keys & _HEALTH_KEYS or _has_health_term(text):
        return SafetyVerdict("health", "candidate")
    return SafetyVerdict("safe", "inject")


def _norm(key: str) -> str:
    return "_".join((key or "").strip().lower().replace("-", " ").split())


def _has_health_term(text: str) -> bool:
    """Termen de condiție medicală în valoare. `_HEALTH_VALUE_TERMS` sunt termeni de DIAGNOSTIC,
    nu de preferință — „fara zahar"/„fara gluten" nu sunt aici (rămân safe)."""
    return any(term in text for term in _HEALTH_VALUE_TERMS)
