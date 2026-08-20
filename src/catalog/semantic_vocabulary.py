"""Rezoluție SEMANTICĂ: sensul unei chei se citește din produsele care o poartă.

Problema pe care o rezolvă: cheile catalogului sunt tehnice și adesea englezești (`dry`, `matte`),
clientul scrie românește („ten uscat", „să nu lucească"). Puntea a fost până acum o hartă de
sinonime scrisă de mână — și exact ea a driftat tăcut cinci săptămâni. Derivarea prin co-ocurență
(`scripts/derive_concern_overlay.py`) recuperează o parte, dar NU tipul de ten: în catalog, frazele
despre ten corelează la fel de bine cu `oily` și cu `matte`, iar numărătoarea nu poate distinge
sinonimul de corelat.

Aici nu mai traducem cuvinte, ci **comparăm sensuri**. Fiecare cheie primește un CENTROID = media
embeddingurilor produselor care o poartă. Cheia `dry` nu e cuvântul englezesc „dry"; e direcția din
spațiul vectorial în care stau produsele descrise drept «Recomandat pentru ten uscat și
deshidratat». O interogare românească aterizează lângă ea fiindcă vorbește despre același lucru,
nu fiindcă ar exista o intrare într-un dicționar.

Trei proprietăți care contează:

* **Zero apeluri noi pentru vocabular.** Centroizii se calculează din `product_embeddings`, care
  există deja; agregarea o face Postgres (`avg(vector)`, pgvector ≥ 0.5). Nu se re-embeduie nimic.
* **Generic prin construcție.** Nicio limbă și niciun vertical nu apar în cod: un catalog de HVAC
  produce centroidul lui `R32` din propriile lui produse, în propria lui limbă.
* **Același contract tri-state.** Ieșirea e tot `Resolution`, cu aceeași invariantă (`KNOWN` cere
  dovadă) — deci se adaugă ca TREAPTĂ după cele deterministe, fără să schimbe nimic în jur.

Prag ȘI margine, amândouă: similaritatea singură ar accepta „cel mai apropiat din ce am", chiar
când nimic nu e aproape. Dacă prima și a doua sunt la egalitate, verdictul e `AMBIGUOUS` — adică o
întrebare, nu o ghicire. Sub prag: `UNKNOWN`, iar termenul nu devine filtru.

Pragurile implicite sunt CONSERVATOARE și trebuie calibrate pe interogări reale
(`scripts/verify_semantic_resolution.py`) înainte de a fi strânse: un prag prea larg reintroduce
exact clasa de defect pe care modulul o închide, doar cu un mecanism mai deștept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    Resolution,
    ResolutionStatus,
    VocabEntry,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

__all__ = [
    "MIN_MARGIN",
    "MIN_SIMILARITY",
    "KeyCentroids",
    "build_centroids",
    "resolve_semantic",
]

# Cosinus minim ca o potrivire să conteze. Sub el, „cel mai apropiat" nu înseamnă „aproape".
MIN_SIMILARITY = 0.30
# Cât trebuie să bată prima clasată pe a doua (diferență absolută de cosinus). Sub asta, două chei
# explică la fel de bine termenul ⇒ întrebare, nu alegere făcută în locul clientului.
MIN_MARGIN = 0.03
# Sub atâtea produse, un centroid e zgomot, nu sens.
MIN_MEMBERS = 3


@dataclass(frozen=True, slots=True)
class Centroid:
    dimension: str
    key: str
    members: int
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class KeyCentroids:
    """Centroizii cheilor unui tenant. `business_id` pe obiect: un centroid al altui client n-are
    voie să ajungă în rezoluția acestuia (P7)."""

    business_id: str
    items: tuple[Centroid, ...] = ()

    def for_dimensions(self, dimensions: tuple[str, ...] | None) -> tuple[Centroid, ...]:
        if dimensions is None:
            return self.items
        allowed = set(dimensions)
        return tuple(c for c in self.items if c.dimension in allowed)

    def is_empty(self) -> bool:
        return not self.items


# Agregarea se face ÎN Postgres: 300 de vectori × 1536 de dimensiuni nu au de ce să traverseze
# rețeaua ca să fie mediați în Python. Se expandează orice cheie text/listă-de-text, exact ca la
# descoperirea de vocabular — nicio cheie nu e numită aici.
_CENTROID_SQL = """
select kv.key   as dimension,
       e.elem   as value,
       count(*) as n,
       avg(pe.embedding)::text as centroid
  from products p
  join product_embeddings pe on pe.product_id = p.id
 cross join lateral jsonb_each(coalesce(p.attributes, '{}'::jsonb)) kv
 cross join lateral (
        select jsonb_array_elements_text(kv.value) as elem
         where jsonb_typeof(kv.value) = 'array'
        union all
        select kv.value #>> '{}' as elem
         where jsonb_typeof(kv.value) = 'string'
       ) e
 where p.business_id = $1
   and p.status = 'active'
   and e.elem is not null
 group by 1, 2
having count(*) >= $2
"""


def _parse(raw: str) -> tuple[float, ...]:
    return tuple(float(x) for x in raw.strip("[]").split(","))


def _normalized(v: tuple[float, ...]) -> tuple[float, ...]:
    norm = sum(x * x for x in v) ** 0.5
    return tuple(x / norm for x in v) if norm else v


async def build_centroids(
    conn: asyncpg.Connection, business_id: str, vocab: CatalogVocabulary
) -> KeyCentroids:
    """Centroidul fiecărei chei ADRESABILE, din embeddingurile deja stocate.

    Se păstrează doar cheile care există în vocabularul servabil: un centroid pentru o valoare pe
    care căutarea n-o poate filtra ar fi un sens fără uz — și, mai rău, ar putea câștiga rezoluția
    în locul uneia utile.
    """
    live = {
        (dim, e.key)
        for dim in vocab.dimensions
        if dim != CATEGORY_DIMENSION
        for e in vocab.entries(dim)
    }
    rows = await conn.fetch(_CENTROID_SQL, business_id, MIN_MEMBERS)
    items = tuple(
        Centroid(
            dimension=str(r["dimension"]),
            key=str(r["value"]),
            members=int(r["n"]),
            vector=_normalized(_parse(r["centroid"])),
        )
        for r in rows
        if (str(r["dimension"]), str(r["value"])) in live
    )
    return KeyCentroids(business_id=business_id, items=items)


def resolve_semantic(
    centroids: KeyCentroids,
    term: str,
    term_vector: tuple[float, ...] | list[float],
    vocab: CatalogVocabulary,
    *,
    dimensions: tuple[str, ...] | None = None,
    min_similarity: float = MIN_SIMILARITY,
    min_margin: float = MIN_MARGIN,
) -> Resolution:
    """Termen liber (deja embedat) → cheie reală, prin apropiere de centroizi.

    `term_vector` vine din ACELAȘI apel de embed ca interogarea turului (batch), deci treapta
    semantică nu adaugă niciun drum în plus la furnizor.
    """
    pool = centroids.for_dimensions(dimensions)
    if not pool or not term_vector:
        return Resolution(
            status=ResolutionStatus.UNKNOWN,
            term=term,
            dimension="",
            reason="no_centroids",
        )

    q = _normalized(tuple(float(x) for x in term_vector))
    scored = sorted(
        ((sum(a * b for a, b in zip(q, c.vector, strict=False)), c) for c in pool),
        key=lambda t: -t[0],
    )
    best_sim, best = scored[0]
    if best_sim < min_similarity:
        return Resolution(
            status=ResolutionStatus.UNKNOWN,
            term=term,
            dimension=best.dimension,
            matched_by="semantic",
            reason="below_similarity_threshold",
        )

    rivals = [c for sim, c in scored[1:] if best_sim - sim < min_margin]
    count = _count_for(vocab, best.dimension, best.key)
    if rivals:
        candidates = [VocabEntry(key=best.key, label=best.key, count=count)]
        candidates += [
            VocabEntry(key=c.key, label=c.key, count=_count_for(vocab, c.dimension, c.key))
            for c in rivals
            if c.dimension == best.dimension
        ]
        # Rivali din ALTE dimensiuni nu se pot uni într-o constrângere (sunt câmpuri diferite):
        # atunci termenul e ambiguu la nivel de sens, iar răspunsul onest e o întrebare.
        if len(candidates) > 1:
            return Resolution(
                status=ResolutionStatus.AMBIGUOUS,
                term=term,
                dimension=best.dimension,
                candidates=tuple(candidates),
                matched_by="semantic",
            )
        return Resolution(
            status=ResolutionStatus.UNKNOWN,
            term=term,
            dimension=best.dimension,
            matched_by="semantic",
            reason="cross_dimension_tie",
        )

    if count <= 0:
        # Centroidul există, dar cheia nu mai e servabilă ⇒ nu are dovadă ⇒ nu devine filtru.
        return Resolution(
            status=ResolutionStatus.UNKNOWN,
            term=term,
            dimension=best.dimension,
            matched_by="semantic",
            reason="key_not_servable",
        )
    return Resolution(
        status=ResolutionStatus.KNOWN,
        term=term,
        dimension=best.dimension,
        key=best.key,
        count=count,
        matched_by="semantic",
    )


def _count_for(vocab: CatalogVocabulary, dimension: str, key: str) -> int:
    for e in vocab.entries(dimension):
        if e.key == key:
            return e.count
    return 0
