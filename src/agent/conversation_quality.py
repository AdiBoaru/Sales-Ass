"""NX-239 — checks OBIECTIVE de calitate conversațională. PURE: fără LLM, fără DB, fără ceas.

Nu măsoară „frumusețea" (aia e treaba pairwise-ului uman NX-246) — măsoară ce se poate verifica
mecanic din contractul de calitate umană:

  1. lead DIRECT: primul bloc răspunde, nu recapitulează/salută/își cere scuze șablonard;
  2. ne-REPETITIV: deschiderea nu e amprenta turului anterior (n-gram fingerprint);
  3. maximum O clarificare (structural în plan + numărul de întrebări din proză);
  4. motive CONCRETE: o recomandare fără evidence sau cu frază generică e semnalată;
  5. no-results ONEST: clasa e din taxonomia închisă.

Rezultatele ies ca `QualityCheck(check, outcome)` — vocabular închis, direct emis ca
`conversation_quality{check,outcome}` (P10/P12: zero text de client în telemetrie).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: Deschideri ȘABLON care amână răspunsul (RO/EN, fără diacritice, lowercase).
_TEMPLATE_OPENERS: tuple[str, ...] = (
    "sigur!",
    "sigur,",
    "desigur",
    "buna!",
    "buna,",
    "buna ziua",
    "salut",
    "multumesc ca",
    "multumim ca",
    "imi pare rau",
    "ne pare rau",
    "as an ai",
    "ca asistent",
    "sunt un asistent",
    "of course",
    "sure!",
    "certainly",
)

#: Motive GENERICE care ar putea descrie orice produs — un „de ce recomand" fără conținut.
_GENERIC_REASON_RE = re.compile(
    r"^(?:un\s+)?produs\s+(?:excelent|foarte\s+bun|de\s+calitate)\b"
    r"|\bideal\s+pentru\s+tine\b|\bperfect\s+pentru\s+(?:tine|oricine)\b"
    r"|\bo\s+alegere\s+(?:buna|excelenta|inspirata)\b|\bcalitate\s+superioara\b"
    r"|\bgreat\s+product\b|\bperfect\s+choice\b",
    re.IGNORECASE,
)

_NO_RESULTS_CLASSES: frozenset[str] = frozenset(
    {"no_match", "insufficient_data", "dependency_unavailable"}
)


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """Un check + rezultatul lui. `check`/`outcome` = vocabular închis (labels de metrică)."""

    check: str
    outcome: str  # "pass" | "fail"


def direct_lead(text: str) -> bool:
    """True dacă primul bloc RĂSPUNDE (nu începe cu salut/șablon/disclaimer). Obiectiv, nu
    estetic: verifică doar deschiderile din lista închisă."""
    lead = _norm(text).strip()
    if not lead:
        return False
    return not any(lead.startswith(opener) for opener in _TEMPLATE_OPENERS)


def opening_fingerprint(text: str, *, ngram: int = 4) -> str:
    """Primele `ngram` cuvinte normalizate — amprenta deschiderii (detecție de șablon repetat)."""
    words = _norm(text).split()
    return " ".join(words[:ngram])


def repeated_opening(text: str, previous_bot_texts: list[str] | tuple[str, ...]) -> bool:
    """True dacă deschiderea turului REPETĂ amprenta unui răspuns anterior al botului din aceeași
    conversație. O amprentă goală/prea scurtă nu acuză pe nimeni."""
    fp = opening_fingerprint(text)
    if len(fp.split()) < 3:
        return False
    return any(fp == opening_fingerprint(prev) for prev in previous_bot_texts if prev)


def question_count(text: str) -> int:
    """Numărul de propoziții-întrebare din proză (mecanic: `?`-uri)."""
    return _norm(text).count("?")


def generic_reason(reason: str) -> bool:
    """True dacă motivul unei recomandări e o frază generică (ar putea descrie orice produs)."""
    return bool(_GENERIC_REASON_RE.search(_norm(reason)))


def no_results_class_valid(reason_class: str) -> bool:
    return reason_class in _NO_RESULTS_CLASSES


def evaluate_reply(
    text: str,
    *,
    plan: Any = None,
    previous_bot_texts: tuple[str, ...] = (),
) -> tuple[QualityCheck, ...]:
    """Toate checkurile obiective pe un răspuns + planul lui (dacă există). Nu blochează turul —
    apelantul (brain-ul) emite rezultatele; NX-246 le folosește ca semnal, nu ca gate."""
    checks: list[QualityCheck] = [
        QualityCheck("direct_lead", "pass" if direct_lead(text) else "fail"),
        QualityCheck(
            "repeated_opening",
            "fail" if repeated_opening(text, list(previous_bot_texts)) else "pass",
        ),
        QualityCheck("max_one_clarification", "pass" if question_count(text) <= 1 else "fail"),
    ]
    if plan is not None:
        recommendations = getattr(plan, "recommendations", ()) or ()
        has_generic = any(
            generic_reason(getattr(rec, "reason", "")) or not getattr(rec, "evidence_ids", ())
            for rec in recommendations
        )
        checks.append(QualityCheck("concrete_reasons", "fail" if has_generic else "pass"))
        no_results = getattr(plan, "no_results", None)
        if no_results is not None:
            checks.append(
                QualityCheck(
                    "honest_no_results",
                    "pass"
                    if no_results_class_valid(getattr(no_results, "reason_class", ""))
                    else "fail",
                )
            )
        clarification = getattr(plan, "clarification", None)
        if clarification is not None and question_count(text) > 1:
            checks.append(QualityCheck("clarification_focus", "fail"))
    return tuple(checks)


__all__ = [
    "QualityCheck",
    "direct_lead",
    "evaluate_reply",
    "generic_reason",
    "no_results_class_valid",
    "opening_fingerprint",
    "question_count",
    "repeated_opening",
]
