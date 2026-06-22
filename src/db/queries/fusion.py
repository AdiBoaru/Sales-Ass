"""NX-113b — fuziune de candidați PURĂ (Reciprocal Rank Fusion + merge determinist).

ZERO DB, ZERO LLM (P2): primește două pool-uri deja ranguite (lexical + vector) și le combină
într-o singură listă ordonată. `relevance` → RRF (produsele în AMBELE liste urcă natural);
`price_asc`/`price_desc`/`rating_desc` → re-sort determinist pe cheia cerută (RRF ar strica
ordinea de preț construită determinist în SQL). Tot ce e aici e testabil fără infrastructură.
"""

from __future__ import annotations

from typing import Any

# Constanta RRF standard (Cormack 2009). k mare → rangurile mici contează aproape egal, k mic →
# accentuează top-ul. 60 = valoarea uzuală din literatură; suficient de generic peste verticale.
RRF_K = 60


def _pid(item: Any) -> str:
    """Id-ul unui candidat: dict de produs (`id`) sau direct un id (string)."""
    return str(item["id"]) if isinstance(item, dict) else str(item)


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


def rrf_fuse(
    lexical_ranked: list[Any],
    vector_ranked: list[Any],
    *,
    k: int = RRF_K,
) -> list[str]:
    """Reciprocal Rank Fusion: scor(id) = Σ 1/(k + rang) pe fiecare listă (rang de la 1).

    Un produs prezent în AMBELE liste acumulează scor din amândouă → urcă peste unul prezent
    doar într-una, chiar dacă acolo are rang mai bun. Determinist: tie-break stabil pe id
    (crescător). Întoarce id-uri ordonate descrescător după scor. Pură — testabilă fără DB.
    """
    scores: dict[str, float] = {}
    for ranked in (lexical_ranked, vector_ranked):
        for rank, item in enumerate(ranked, start=1):
            pid = _pid(item)
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda pid: (-scores[pid], pid))


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
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuzionează cele două pool-uri într-o listă ordonată de produse (dict-uri).

    `relevance` → RRF pe ranguri. Sort explicit (preț/rating) → re-sort determinist pe cheie
    (RRF nu se aplică: ordinea de preț e deja construită determinist și nu trebuie amestecată).
    Trunchierea la 6 NU se face aici (rămâne în orchestrator, după dedup vs displayed_products).
    """
    if sort_mode == "relevance":
        by_id: dict[str, dict[str, Any]] = {}
        for p in lexical:
            by_id[_pid(p)] = p
        for p in vector:
            by_id.setdefault(_pid(p), p)
        return [by_id[pid] for pid in rrf_fuse(lexical, vector, k=k)]
    return _merge_by_sort(lexical, vector, sort_mode=sort_mode)
