"""NX-113b — fuziune de candidați PURĂ (Reciprocal Rank Fusion + merge determinist).

ZERO DB, ZERO LLM (P2): primește două pool-uri deja ranguite (lexical + vector) și le combină
într-o singură listă ordonată. `relevance` → RRF (produsele în AMBELE liste urcă natural);
`price_asc`/`price_desc`/`rating_desc` → re-sort determinist pe cheia cerută (RRF ar strica
ordinea de preț construită determinist în SQL). Tot ce e aici e testabil fără infrastructură.
"""

from __future__ import annotations

import json
from typing import Any

# Constanta RRF standard (Cormack 2009). k mare → rangurile mici contează aproape egal, k mic →
# accentuează top-ul. 60 = valoarea uzuală din literatură; suficient de generic peste verticale.
RRF_K = 60

# Disponibilitate considerată „pe stoc" pentru boost-ul de rerank (oglindă a filtrului din catalog).
_IN_STOCK = frozenset({"in_stock", "low_stock"})


def _pid(item: Any) -> str:
    """Id-ul unui candidat: dict de produs (`id`) sau direct un id (string)."""
    return str(item["id"]) if isinstance(item, dict) else str(item)


def _norm_concern(c: Any) -> str:
    return str(c).strip().lower()


def _product_concerns(p: dict[str, Any]) -> set[str]:
    """Concerns-urile unui produs din `attributes->'concerns'`. DEFENSIV: asyncpg întoarce jsonb
    ca text (fără codec) SAU listă (cu codec); acceptăm ambele + None, fără să crăpăm."""
    raw = p.get("concerns")
    if raw is None:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    if isinstance(raw, (list, tuple)):
        return {_norm_concern(x) for x in raw}
    return set()


def _eff_price(p: dict[str, Any]) -> float:
    """Prețul efectiv întors de SELECT (`coalesce(vp.price, sale_price, price)`). Lipsă → +inf
    (sortat la coadă pe price_asc)."""
    v = p.get("price")
    return float(v) if v is not None else float("inf")


def _shrunk_rating(p: dict[str, Any]) -> float:
    """Rating Bayesian (oglindă a `_SHRUNK_RATING` din catalog.py): `(n*r + 30*4.0)/(n + 30)`.
    Re-calculat în Python ca re-sortul pe `rating_desc` să reproducă EXACT ordinea SQL."""
    n = p.get("review_count") or 0
    r = p.get("rating") or 0
    return (n * float(r) + 30 * 4.0) / (n + 30)


def rrf_scores(
    lexical_ranked: list[Any],
    vector_ranked: list[Any],
    *,
    k: int = RRF_K,
) -> dict[str, float]:
    """Scorul RRF per id: Σ 1/(k + rang) pe fiecare listă (rang de la 1). Un produs în AMBELE
    liste acumulează din amândouă. Pură."""
    scores: dict[str, float] = {}
    for ranked in (lexical_ranked, vector_ranked):
        for rank, item in enumerate(ranked, start=1):
            pid = _pid(item)
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank)
    return scores


def rrf_fuse(
    lexical_ranked: list[Any],
    vector_ranked: list[Any],
    *,
    k: int = RRF_K,
) -> list[str]:
    """Reciprocal Rank Fusion: id-uri ordonate descrescător după scorul RRF.

    Un produs prezent în AMBELE liste urcă peste unul prezent doar într-una, chiar dacă acolo
    are rang mai bun. Determinist: tie-break stabil pe id (crescător). Pură — testabilă fără DB.
    """
    scores = rrf_scores(lexical_ranked, vector_ranked, k=k)
    return sorted(scores, key=lambda pid: (-scores[pid], pid))


def deterministic_rerank(
    products: list[dict[str, Any]],
    scores: dict[str, float],
    *,
    concerns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rerank DETERMINIST (P2, ZERO LLM): la scor RRF EGAL, ridică produsele in-stock / la reducere
    / cu concern-overlap; tie-break final stabil pe id.

    Boost = DOAR departajator pe egalitate de relevanță (NU domină scorul RRF): cheia de sort e
    `(-scor_rrf, -boost, id)`, deci relevanța rămâne primară (greutăți mici, stabile). Boost-ul =
    `in_stock(1) + on_sale(1) + nr. concerns cerute care apar în attributes->'concerns'`."""
    cset = {_norm_concern(c) for c in (concerns or [])}

    def _boost(p: dict[str, Any]) -> int:
        b = 0
        if p.get("availability") in _IN_STOCK:
            b += 1
        if p.get("on_sale"):
            b += 1
        if cset:
            b += len(cset & _product_concerns(p))
        return b

    return sorted(products, key=lambda p: (-scores.get(_pid(p), 0.0), -_boost(p), _pid(p)))


def _merge_by_sort(
    lexical: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    sort_mode: str,
) -> list[dict[str, Any]]:
    """Union dedup pe id + re-sort determinist pe cheia explicită (preț/rating). Prima apariție
    a unui id câștigă (lexical înaintea vectorului). Reproduce ordinea SQL: preț efectiv exact,
    rating shrunk. Tie-break final pe id."""
    by_id: dict[str, dict[str, Any]] = {}
    for p in (*lexical, *vector):
        by_id.setdefault(_pid(p), p)
    items = list(by_id.values())
    # Tie-break-urile reproduc EXACT `_order_clause` din catalog.py (paritate SQL): pe preț, la preț
    # egal departajează ratingul shrunk desc, apoi id; pe rating, prețul asc, apoi id.
    if sort_mode == "price_asc":
        items.sort(key=lambda p: (_eff_price(p), -_shrunk_rating(p), _pid(p)))
    elif sort_mode == "price_desc":
        items.sort(key=lambda p: (-_eff_price(p), -_shrunk_rating(p), _pid(p)))
    elif sort_mode == "rating_desc":
        items.sort(key=lambda p: (-_shrunk_rating(p), _eff_price(p), _pid(p)))
    else:  # mod necunoscut → ordine stabilă pe id (niciodată ordine nedeterministă)
        items.sort(key=_pid)
    return items


def fuse_candidates(
    lexical: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    sort_mode: str = "relevance",
    concerns: list[str] | None = None,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuzionează cele două pool-uri într-o listă ordonată de produse (dict-uri).

    `relevance` → RRF pe ranguri + rerank determinist (in-stock/sale/concern ca departajator pe
    egalitate). Sort explicit (preț/rating) → re-sort determinist pe cheie (RRF/rerank nu se aplică:
    ordinea de preț e deja construită determinist și nu trebuie amestecată). Trunchierea la 6 NU se
    face aici (rămâne în orchestrator, după dedup vs displayed_products)."""
    if sort_mode == "relevance":
        by_id: dict[str, dict[str, Any]] = {}
        for p in lexical:
            by_id[_pid(p)] = p
        for p in vector:
            by_id.setdefault(_pid(p), p)
        scores = rrf_scores(lexical, vector, k=k)
        return deterministic_rerank(list(by_id.values()), scores, concerns=concerns)
    return _merge_by_sort(lexical, vector, sort_mode=sort_mode)
