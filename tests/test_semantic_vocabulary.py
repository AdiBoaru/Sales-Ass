"""Rezoluția semantică — contractul, pe vectori sintetici (fără DB, fără API, fără credite)."""

from __future__ import annotations

from src.catalog.semantic_vocabulary import (
    Centroid,
    KeyCentroids,
    resolve_semantic,
)
from src.catalog.vocabulary import CatalogVocabulary, ResolutionStatus, VocabEntry

VOCAB = CatalogVocabulary(
    business_id="b",
    dimensions={
        "concerns": (
            VocabEntry(key="dry", label="dry", count=83),
            VocabEntry(key="oily", label="oily", count=23),
        ),
        "finish": (VocabEntry(key="matte", label="matte", count=17),),
    },
)


def _c(dim: str, key: str, vec: tuple[float, ...], members: int = 10) -> Centroid:
    return Centroid(dimension=dim, key=key, members=members, vector=vec)


def _cents(*items: Centroid) -> KeyCentroids:
    return KeyCentroids(business_id="b", items=items)


def test_nearest_centroid_resolves_with_evidence() -> None:
    """Un termen aterizat lângă centroidul lui `dry` devine `KNOWN`, cu dovada din vocabular —
    fără nicio hartă de sinonime între „ten uscat" și „dry"."""
    cents = _cents(_c("concerns", "dry", (1.0, 0.0)), _c("concerns", "oily", (0.0, 1.0)))
    r = resolve_semantic(cents, "ten uscat", (0.98, 0.02), VOCAB)
    assert r.status is ResolutionStatus.KNOWN
    assert r.key == "dry" and r.count == 83
    assert r.matched_by == "semantic"


def test_far_from_everything_is_unknown_not_nearest() -> None:
    """„Cel mai apropiat" nu înseamnă „aproape": sub prag, termenul NU devine filtru.

    Regresia esențială — fără pragul ăsta, orice cuvânt din lume ar primi o cheie, iar filtrul ar
    goli rezultatul exact ca înainte, doar cu un mecanism mai sofisticat.
    """
    cents = _cents(_c("concerns", "dry", (1.0, 0.0)), _c("concerns", "oily", (0.0, 1.0)))
    r = resolve_semantic(cents, "garantie extinsa", (0.7, 0.71), VOCAB, min_similarity=0.9)
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "below_similarity_threshold"
    assert r.constraint_keys == ()


def test_tie_inside_one_dimension_is_ambiguous_not_a_coin_flip() -> None:
    """Două chei ale ACELEIAȘI dimensiuni la egalitate ⇒ întrebare, cu uniunea ca constrângere."""
    cents = _cents(_c("concerns", "dry", (1.0, 0.0)), _c("concerns", "oily", (0.999, 0.04)))
    r = resolve_semantic(cents, "ceva", (1.0, 0.0), VOCAB)
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert set(r.constraint_keys) == {"dry", "oily"}


def test_tie_across_dimensions_refuses_to_guess() -> None:
    """`concerns=oily` și `finish=matte` explică la fel de bine termenul, dar sunt CÂMPURI diferite:
    nu se pot uni într-un filtru, deci nu inventăm unul. Exact cazul pe care derivarea prin
    co-ocurență nu-l putea decide („pe ten gras" → oily sau matte?)."""
    cents = _cents(_c("concerns", "oily", (1.0, 0.0)), _c("finish", "matte", (0.999, 0.03)))
    r = resolve_semantic(cents, "pe ten gras", (1.0, 0.0), VOCAB)
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "cross_dimension_tie"
    assert r.constraint_keys == ()


def test_key_without_evidence_never_resolves() -> None:
    """Centroid existent, cheie nemaiservabilă ⇒ `UNKNOWN`. Invarianta „KNOWN cere dovadă" ține și
    pe calea semantică, nu doar pe cea deterministă."""
    cents = _cents(_c("concerns", "disparuta", (1.0, 0.0)))
    r = resolve_semantic(cents, "orice", (1.0, 0.0), VOCAB)
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "key_not_servable"


def test_dimension_filter_restricts_the_pool() -> None:
    """Apelantul poate restrânge dimensiunile (o nevoie e o fațetă, nu o categorie)."""
    cents = _cents(_c("concerns", "dry", (1.0, 0.0)), _c("finish", "matte", (0.0, 1.0)))
    r = resolve_semantic(cents, "x", (0.0, 1.0), VOCAB, dimensions=("concerns",))
    assert r.dimension == "concerns"


def test_no_centroids_is_unknown() -> None:
    """Tenant fără embeddings ⇒ treapta semantică nu există; nu inventează și nu crapă."""
    r = resolve_semantic(KeyCentroids(business_id="b"), "x", (1.0, 0.0), VOCAB)
    assert r.status is ResolutionStatus.UNKNOWN
    assert r.reason == "no_centroids"
