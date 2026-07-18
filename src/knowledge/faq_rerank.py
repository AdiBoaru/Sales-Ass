"""NX-175 — selecția FAQ corectă: rerank determinist conștient de calificatori + marjă → clarify.

Problema (măsurată live): `semantic_lookup` e top-1 orb pe cosine. La „Cum pot face un retur?" cei
mai apropiați 2 candidați sunt la 0.026 distanță, iar top-1 e EXCEPȚIA („produs desfăcut" → «Nu.»),
nu procedura generală („Cum returnez un produs?"). Marja mică ESTE semnalul de coliziune.

Trei straturi, generice (nu specifice returului — orice tenant/cluster/limbă):
  1. rerank pe MARKERI de excepție: un FAQ a cărui întrebare conține un calificator restrictiv
     (desfăcut/desigilat/deschis/folosit...) pe care întrebarea clientului NU-l are răspunde la o
     întrebare mai îngustă decât cea pusă → se DEMOTEAZĂ. Invers (client are markerul, FAQ nu) →
     penalizare ușoară (clientul vrea excepția). Normalizare de text, NU semantică → determinist.
  2. marjă de încredere: dacă după rerank #1 și #2 sunt sub ε ȘI au răspunsuri DIFERITE ȘI markerii
     n-au separat → nu ghicim, cerem CLARIFICARE (chips = întrebările candidate).
  3. pragul de servire rămâne la caller (stagiul gratuit vs tool au praguri diferite) și se aplică
     pe cosine-ul ORIGINAL al FAQ-ului ales (rerank decide ORDINEA, cosine-ul dă ÎNCREDEREA).

Pur, fără DB/LLM: primește candidații (deja retrievați) + query canonic. Testabil pe dict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def _norm(s: str) -> str:
    """lower + fără diacritice — paritate cu `cache.canonical.canonicalize` și cu embeddingul."""
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


# Markeri CANONICI de excepție/restricție (fără diacritice). O întrebare de FAQ care îi conține e o
# SPECIALIZARE (răspunde la un caz mai îngust). Vocabular de NORMALIZARE, nu wordlist semantic:
# extensibil per limbă, dar principiul (specializare lexicală) e generic. RO acum; en/hu la nevoie.
_EXCEPTION_MARKERS: tuple[str, ...] = (
    "desfacut",
    "desigilat",
    "deschis",
    "folosit",
    "uzat",
    "sigiliu rupt",
    "sigiliul rupt",
    "rupt",
    "deteriorat",
    "gresit",  # „produsul greșit" = caz special vs retur general
)


@dataclass(frozen=True)
class FaqCandidate:
    """Un candidat retrievat (cosine deja calculat de DB). `similarity` = cosine ORIGINAL."""

    id: str
    question: str
    answer: str
    similarity: float


@dataclass(frozen=True)
class FaqDecision:
    """Rezultatul selecției. `action`:
    - `serve`  → răspunde cu `answer` (FAQ ales; `confidence` = cosine ORIGINAL, pt pragul caller);
    - `clarify`→ ambiguu: cere alegere cu `clarify_options` (chips = întrebările candidate);
    - `miss`   → niciun candidat (caller cade pe fallback)."""

    action: str
    faq_id: str | None = None
    question: str | None = None
    answer: str | None = None
    confidence: float = 0.0  # cosine ORIGINAL al FAQ-ului ales (NU scorul ajustat)
    clarify_options: list[tuple[str, str]] = field(default_factory=list)  # (faq_id, question)
    # observabilitate: ordinea ajustată (faq_id, scor_ajustat) — pt event, fără PII
    ranking: list[tuple[str, float]] = field(default_factory=list)


def _has_marker(text_norm: str) -> bool:
    return any(re.search(rf"\b{re.escape(m)}", text_norm) for m in _EXCEPTION_MARKERS)


def _adjust(query_norm: str, cand: FaqCandidate, *, demote: float, mild: float) -> float:
    """Scorul de RANKING (nu de încredere). Reguli generice de specializare lexicală."""
    q_marker = _has_marker(query_norm)
    f_marker = _has_marker(_norm(cand.question))
    score = cand.similarity
    if f_marker and not q_marker:
        score -= demote  # FAQ mai îngust decât întrebarea → coboară
    elif q_marker and not f_marker:
        score -= mild  # clientul vrea excepția, FAQ-ul e general → coboară ușor
    return score


def rerank(
    query_canon: str,
    candidates: list[FaqCandidate],
    *,
    demote: float = 0.15,
    mild: float = 0.05,
    margin_eps: float = 0.03,
) -> FaqDecision:
    """Reordonează candidații (calificatori) + decide serve/clarify/miss. Pur.

    `demote`/`mild` = penalizările de ranking; `margin_eps` = pragul sub care #1↔#2 (răspunsuri
    diferite) declanșează clarificarea. Pragul de SERVIRE (tau) rămâne la caller — aici doar
    ORDONĂM și semnalăm ambiguitatea reală.

    `margin_eps` e mic (0.03) DELIBERAT: rerank-ul pe calificatori rezolvă deja coliziunea măsurată
    (excepția demotată sub general → separare ~0.12), deci clarify e o plasă pt DEAD-HEAT-uri reale
    (două răspunsuri diferite la sub 0.03 unul de altul), NU pt sub-topicuri înrudite dar distincte
    („cum returnez" vs „când primesc banii" la 0.055 — ăsta se SERVEȘTE cu top-1, nu se întreabă).
    Peste-clarificarea e la fel de proastă ca răspunsul greșit: un tur inutil care enervează."""
    if not candidates:
        return FaqDecision(action="miss")

    q = _norm(query_canon)
    scored = sorted(
        ((_adjust(q, c, demote=demote, mild=mild), c) for c in candidates),
        key=lambda t: t[0],
        reverse=True,
    )
    ranking = [(c.id, round(s, 4)) for s, c in scored]
    top_score, top = scored[0]

    # Ambiguitate reală: #1 și #2 aproape la egalitate DUPĂ rerank, cu răspunsuri DIFERITE (dacă
    # răspunsul e identic — duplicate-uri, ex. „Cum plătesc?" ≡ „Ce metode de plată" — nu are rost
    # să întrebăm, servim). Markerii n-au separat (altfel marja ar fi > eps). → clarificare onestă.
    if len(scored) >= 2:
        second_score, second = scored[1]
        if (top_score - second_score) < margin_eps and _norm(top.answer) != _norm(second.answer):
            return FaqDecision(
                action="clarify",
                clarify_options=[(top.id, top.question), (second.id, second.question)],
                confidence=top.similarity,
                ranking=ranking,
            )

    return FaqDecision(
        action="serve",
        faq_id=top.id,
        question=top.question,
        answer=top.answer,
        confidence=top.similarity,  # cosine ORIGINAL → caller aplică tau pe el
        ranking=ranking,
    )
