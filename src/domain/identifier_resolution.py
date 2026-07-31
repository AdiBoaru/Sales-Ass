"""NX-209 — rezolvare exactă/fuzzy de produs, pură și folosită doar în shadow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from rapidfuzz.fuzz import ratio

from src.domain.normalize import normalize

ResolutionStatus = Literal["resolve", "clarify", "not_found"]
_RESOLVE_THRESHOLD = 92.0
_CLARIFY_THRESHOLD = 75.0
_AMBIGUITY_MARGIN = 4.0

# Cât din identificatorul candidat trebuie să acopere cererea ca potrivirea fuzzy să conteze.
#
# Fără asta, `ratio` pe şiruri fără spaţii tratează „cererea e un FRAGMENT al numelui" drept
# similaritate mare: `'cremahidratanta'` (15) vs `'petalarichcremahidratanta'` (25) → exact 75.0,
# adică pragul de `clarify`. Efectul măsurat: „cremă hidratantă" întorcea UN produs, sărind peste
# FTS şi semantic, deşi 9 produse din catalog au „hidratant" în nume. O cerere de categorie
# deturnată într-un identificator.
#
# Potrivirea fuzzy de identificator trebuie să fie despre un TYPO ÎNTR-UN IDENTIFICATOR ÎNTREG, nu
# despre un fragment care se întâmplă să fie conţinut în el.
#
# Nu e un parametru de tuning: măturat pe catalogul real (300 de produse), orice valoare între 0.7
# şi 0.9 dă exact acelaşi comportament — 0 cereri de categorie deturnate din 20, şi 40/40 nume
# regăsite, atât exacte cât şi cu typo. Sub 0.7 deturnarea reapare. Deci pragul separă două regimuri
# calitative, nu reglează fin un compromis.
_MIN_COVERAGE = 0.7


@dataclass(frozen=True)
class IdentifierCandidate:
    product_id: str
    name: str
    skus: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentifierResolution:
    status: ResolutionStatus
    product_id: str | None
    score: float
    candidate_ids: tuple[str, ...]


def _norm(value: str) -> str:
    return normalize(value).replace(" ", "")


def _best_ratio(query_normalized: str, candidate: IdentifierCandidate) -> float:
    """Cel mai bun scor peste nume + SKU-uri + aliasuri aprobate.

    `max` şi nu media: identificatorii sunt alternative pentru acelaşi produs, nu dovezi care se
    cumulează. Un SKU potrivit perfect nu trebuie diluat de un nume lung care nu seamănă.

    Acoperirea se verifică PER VALOARE, nu pe candidat: o cerere care se potriveşte perfect cu un
    SKU scurt are acoperire ~1.0 faţă de SKU chiar dacă numele produsului e lung."""
    scores = [
        float(ratio(query_normalized, normalized))
        for value in (candidate.name, *candidate.skus, *candidate.aliases)
        if value
        for normalized in (_norm(value),)
        if normalized and len(query_normalized) / len(normalized) >= _MIN_COVERAGE
    ]
    return max(scores) if scores else 0.0


def resolve_identifier(
    query: str, candidates: Sequence[IdentifierCandidate]
) -> IdentifierResolution:
    """Exact înainte de fuzzy; scor apropiat sau mediu cere clarify, nu alege arbitrar."""
    query_normalized = _norm(query)
    if not query_normalized:
        return IdentifierResolution("not_found", None, 0.0, ())

    exact = [
        candidate
        for candidate in candidates
        if query_normalized
        in {_norm(value) for value in (candidate.name, *candidate.skus, *candidate.aliases)}
    ]
    if len(exact) == 1:
        return IdentifierResolution("resolve", exact[0].product_id, 100.0, (exact[0].product_id,))
    if len(exact) > 1:
        return IdentifierResolution(
            "clarify", None, 100.0, tuple(item.product_id for item in exact)
        )

    # Fuzzy pe TOATE identificatorii, nu doar pe nume. Înainte se scora exclusiv `candidate.name`,
    # deci un SKU tastat greşit („ABC-1234" → „ABC-1235") sau un alias aproximat nu se potrivea
    # niciodată — într-un modul al cărui scop declarat e rezolvarea de nume/SKU/alias. Potrivirea
    # exactă le acoperea, fuzzy-ul nu; adică exact cazul pentru care există fuzzy (tastare umană).
    scored = sorted(
        ((_best_ratio(query_normalized, candidate), candidate) for candidate in candidates),
        key=lambda item: (-item[0], item[1].product_id),
    )
    if not scored or scored[0][0] < _CLARIFY_THRESHOLD:
        return IdentifierResolution("not_found", None, scored[0][0] if scored else 0.0, ())

    best_score, best = scored[0]
    close = tuple(
        candidate.product_id
        for score, candidate in scored
        if score >= _CLARIFY_THRESHOLD and best_score - score <= _AMBIGUITY_MARGIN
    )
    if best_score < _RESOLVE_THRESHOLD or len(close) > 1:
        return IdentifierResolution("clarify", None, best_score, close)
    return IdentifierResolution("resolve", best.product_id, best_score, (best.product_id,))
