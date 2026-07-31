"""NX-203 — metrici de retrieval (pure-Python, determinist).

Recall@k, nDCG@k, Top-k hit rate, MRR + rata de încălcare a produselor interzise. Implementare
proprie ca harness-ul să ruleze oriunde, fără dependență la runtime; `ir-measures` (pinned în
requirements-dev) e folosit ca CROSS-CHECK la rularea completă. Formulele sunt cele standard —
testate în `tests/test_retrieval_harness.py` cu valori calculate de mână.

Un „rezultat de retrieval" = lista ordonată de `product_id` (cel mai relevant primul).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from src.evals.retrieval.schema import QrelsQuery


def _rel_map(q: QrelsQuery) -> dict[str, int]:
    return {j.product_id: int(j.relevance) for j in q.judgments}


def recall_at_k(q: QrelsQuery, ranked: Sequence[str], k: int) -> float:
    """Fracția produselor relevante (relevance>=1) prinse în primele k. 1.0 dacă nu există relevante
    (nimic de ratat) — decizie explicită ca să nu penalizăm query-uri fără gold."""
    rel = {pid for pid, r in _rel_map(q).items() if r >= 1}
    if not rel:
        return 1.0
    hit = sum(1 for pid in ranked[:k] if pid in rel)
    return hit / len(rel)


def top_k_hit(q: QrelsQuery, ranked: Sequence[str], k: int, min_rel: int = 2) -> float:
    """1.0 dacă cel puțin un produs cu relevance>=min_rel e în primele k, altfel 0.0."""
    good = {pid for pid, r in _rel_map(q).items() if r >= min_rel}
    if not good:
        return 1.0
    return 1.0 if any(pid in good for pid in ranked[:k]) else 0.0


def dcg_at_k(gains: Sequence[int], k: int) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(q: QrelsQuery, ranked: Sequence[str], k: int) -> float:
    """nDCG cu grade reale de relevanță. Ideal = gradele sortate descrescător."""
    rmap = _rel_map(q)
    gains = [rmap.get(pid, 0) for pid in ranked]
    ideal = sorted(rmap.values(), reverse=True)
    idcg = dcg_at_k(ideal, k)
    if idcg == 0:
        return 1.0  # nimic relevant → nimic de ordonat greșit
    return dcg_at_k(gains, k) / idcg


def mrr(q: QrelsQuery, ranked: Sequence[str], min_rel: int = 2) -> float:
    """Reciprocal rank al primului produs cu relevance>=min_rel. 0.0 dacă niciunul în listă."""
    good = {pid for pid, r in _rel_map(q).items() if r >= min_rel}
    for i, pid in enumerate(ranked):
        if pid in good:
            return 1.0 / (i + 1)
    return 0.0


def forbidden_violations(q: QrelsQuery, ranked: Sequence[str], k: int) -> int:
    """Câte EXCEPŢII EXPLICITE apar în primele k.

    Componentă, nu metrica finală: acoperă doar incompatibilităţile fără reprezentare în atribute.
    Raportul foloseşte `violations_at_k`, care o reuneşte cu cele derivate din constrângeri."""
    forb = set(q.forbidden_products)
    return sum(1 for pid in ranked[:k] if pid in forb)


def violations_at_k(
    q: QrelsQuery, ranked: Sequence[str], k: int, catalog: Mapping[str, dict] | None
) -> int | None:
    """Câte produse din top-k încalcă contractul query-ului. SINGURUL numărător folosit de raport.

    `None` dacă lipseşte catalogul: verificarea nu s-a putut face. A treia stare, nu zero — un zero
    tăcut ar raporta „nicio încălcare" acolo unde nimic n-a fost verificat.

    REUNIUNE, nu sumă, între două surse diferite:
      · EXCEPŢIILE EXPLICITE din qrels — incompatibilităţi reale pe care atributele nu le exprimă;
      · violările DERIVATE din `hard_constraints` contra catalogului — independente de ce a
        returnat sistemul testat.
    Un produs prins de ambele e o singură încălcare; altfel rata ar creşte cu cât un caz e mai bine
    acoperit de constrângeri, nu cu câte produse greşite au ieşit."""
    if catalog is None:
        return None
    from src.evals.retrieval.constraints import violates_any  # noqa: PLC0415

    top = ranked[:k]
    explicit_ids = set(q.forbidden_products)
    explicit = {pid for pid in top if pid in explicit_ids}
    derived = {
        pid for pid in top if (p := catalog.get(pid)) and violates_any(p, q.hard_constraints)
    }
    return len(explicit | derived)
