"""Kernel `kernel.v1.0`, pasul 4a (NX-332) — politica de răspuns. PURĂ.

Contractul (§„Answer policy", I12): un verdict ÎNTRE produse („X e mai bun decât Y la ecran") e
interzis când atributul care decide e necunoscut pe vreunul dintre ele. Politica se calculează DUPĂ
unelte (pe faptele pe care le-au întors executorii) și ÎNAINTE de compunere; aplicarea e a
compunerii (pasul 6), iar regula de superlativ din `grounding_guard` rămâne plasa.

Dimensiunea decisivă, în ordine (prima găsită decide):

1. `Reference.dimension` al unei referințe `extreme` a actului („cel mai ieftin"), acceptată doar
   dacă e prețul, ratingul sau o dimensiune a vocabularului (propunerea modelului, validată de cod,
   ca la resolver);
2. o ETICHETĂ DE RÂND a unei fațete, scrisă de client în cererea actului („care e mai bun la
   ecran?"). Etichetele sunt date de pachet (`TypedFacet.labels`, `FacetSpec.labels`, per locale),
   potrivite pe cuvinte întregi. `resolve_any` nu ajunge aici: rezolvă VALORI, deci «ecran» n-ar da
   niciodată dimensiunea `screen`.

`UNKNOWN ≠ MISMATCH`: un atribut absent e „necunoscut", nu „nu are". Politica întoarce doar ce
lipsește (`missing`), niciodată o afirmație că un produs NU are ceva.

Textul actului (`Act.query`) e text al clientului, deci se citește O SINGURĂ dată, în `_read_query`
(tiparul lui `provenance._read_quote`), declarat în `tests/kernel_contract_allowlist.json`
(`raw_text_readers`). Modulul nu cheamă modelul (I13) și nu conține literali de vertical (I14)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from src.catalog.query_terms import inflection_suffixes, stopwords, tokens
from src.catalog.vocabulary import CATEGORY_DIMENSION, CatalogVocabulary
from src.conversation.interpretation import (
    Act,
    AmbiguityDecision,
    AnswerPolicy,
    Reference,
    ResolvedRef,
)
from src.conversation.references import ProductFacts

PRICE = "price"
RATING = "rating"
#: Dimensiunile ordonabile care nu stau în `attributes`: coloane ale produsului, pe orice vertical.
_COLUMN_DIMENSIONS: frozenset[str] = frozenset({PRICE, RATING})
#: Actele care cer o judecată între produse, pe lângă orice verdict `act_both`.
_JUDGED_ACTS: frozenset[str] = frozenset({"compare", "detail"})
#: O judecată e ÎNTRE produse: sub doi candidați nu e nimic de politicat.
_MIN_CANDIDATES = 2
#: Tulpina minimă pentru potrivirea modulo flexiune (aceeași ca la resolver, NX-329).
_MIN_STEM = 3


@dataclass(frozen=True)
class QueryEvidence:
    """Ce spune cererea actului, după tabelele locale-i și etichetele pachetului: are cuvinte de
    conținut? numește o dimensiune (prin eticheta ei de rând)? Nicio altă urmă a textului."""

    has_words: bool
    dimension: str | None


def _row_labels(pack: object | None, locale: str | None) -> list[tuple[str, tuple[str, ...]]]:
    """(dimensiune, cuvintele etichetei) pentru fiecare etichetă de rând din pachet: a fațetei
    tipizate și a rândului de comparație, în locale-ul turului (toate locale-urile când lipsește).
    Raftul nu e un atribut de judecat, deci nu intră."""
    out: list[tuple[str, tuple[str, ...]]] = []
    specs = [
        *(getattr(pack, "facets", ()) or ()),
        *(getattr(pack, "facet_labels", ()) or getattr(pack, "comparison_facets", ()) or ()),
    ]
    for spec in specs:
        key = getattr(spec, "key", None)
        labels = getattr(spec, "labels", None)
        if not key or key == CATEGORY_DIMENSION or not isinstance(labels, Mapping):
            continue
        # Locale-ul regional cade pe limbă („ro-RO" → „ro"), ca șabloanele porții și `query_terms`;
        # altfel un tur `ro-RO` n-ar vedea nicio etichetă, iar I12 n-ar mai rula (recenzia NX-332).
        lang = (locale or "").strip().lower()
        chosen = (
            [labels.get(lang) or labels.get(lang.split("-")[0])] if lang else list(labels.values())
        )
        for label in chosen:
            words = tuple(tokens(label)) if isinstance(label, str) else ()
            if words and (key, words) not in out:
                out.append((key, words))
    return out


def dimension_label(pack: object | None, dimension: str, locale: str | None) -> str | None:
    """Eticheta de rând a unei dimensiuni, în limba turului (NX-336 C2: fraza care spune ce
    lipsește pe o comparație fără verdict). Aceleași surse și aceeași cădere pe limba de bază ca
    `_row_labels`; fără etichetă ⇒ `None` (niciodată cheia brută în fața clientului)."""
    lang = (locale or "").strip().lower()
    specs = [
        *(getattr(pack, "facets", ()) or ()),
        *(getattr(pack, "facet_labels", ()) or getattr(pack, "comparison_facets", ()) or ()),
    ]
    for spec in specs:
        labels = getattr(spec, "labels", None)
        if getattr(spec, "key", None) != dimension or not isinstance(labels, Mapping):
            continue
        label = labels.get(lang) or labels.get(lang.split("-")[0])
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def _same_word(asked: str, carried: str, suffixes: Collection[str]) -> bool:
    """Același cuvânt, eventual flexionat cu un sufix al locale-i («ecranul» = „ecran" + „ul")."""
    if asked == carried:
        return True
    short, long_ = (asked, carried) if len(asked) < len(carried) else (carried, asked)
    return len(short) >= _MIN_STEM and long_.startswith(short) and long_[len(short) :] in suffixes


def _read_query(
    query: str | None,
    labels: Sequence[tuple[str, tuple[str, ...]]],
    locale: str | None,
) -> QueryEvidence:
    """SINGURUL loc care citește `Act.query` (text al clientului). Excepție declarată de la poarta
    de text brut: etichetele sunt date de pachet, iar potrivirea e pe cuvinte întregi, după
    normalizarea și sufixele din `query_terms`. Prima etichetă care apare în cerere (cea mai
    lungă, la aceeași poziție) numește dimensiunea. Deciziile de după ramifică pe
    `QueryEvidence`, niciodată pe text."""
    words = tuple(tokens(query or ""))
    stop = stopwords(locale)
    has_words = any(w not in stop for w in words)
    suffixes = inflection_suffixes(locale)
    best: tuple[int, int, str] | None = None
    for dimension, label in labels:
        n = len(label)
        for start in range(len(words) - n + 1):
            window = words[start : start + n]
            if all(_same_word(a, b, suffixes) for a, b in zip(window, label, strict=True)):
                rank = (start, -n, dimension)
                if best is None or rank < best:
                    best = rank
                break
    return QueryEvidence(has_words=has_words, dimension=best[2] if best else None)


def read_query(act: Act, *, pack: object | None, locale: str | None) -> QueryEvidence:
    """Dovada din cererea unui act (a porții, pentru regula „fără cuvinte", și a politicii)."""
    return _read_query(act.query, _row_labels(pack, locale), locale)


def _extreme_dimension(
    act: Act, references: Sequence[Reference], vocab: CatalogVocabulary | None
) -> str | None:
    """Dimensiunea unei referințe `extreme` a actului, dacă e una pe care codul o poate citi."""
    by_id = {r.id: r for r in references}
    allowed = set(_COLUMN_DIMENSIONS)
    if vocab is not None and not vocab.is_empty():
        allowed |= set(vocab.facet_names)
    for target in act.targets:
        ref = by_id.get(target)
        if ref is None or ref.kind != "extreme":
            continue
        dimension = ref.dimension or PRICE  # contractul resolverului: fără dimensiune = prețul
        if dimension in allowed:
            return dimension
    return None


def _source_key(pack: object | None, dimension: str) -> str:
    for facet in getattr(pack, "facets", ()) or ():
        if getattr(facet, "key", None) == dimension:
            return str(getattr(facet, "source_key", dimension) or dimension)
    return dimension


def _known(facts: ProductFacts | None, dimension: str, source_key: str) -> bool:
    """Valoarea e CUNOSCUTĂ pe produs. Absentă ⇒ necunoscută, nu „nu are" (UNKNOWN ≠ MISMATCH)."""
    if facts is None:
        return False
    if dimension == PRICE:
        return facts.price is not None
    if dimension == RATING:
        return facts.rating is not None
    value = facts.attributes.get(source_key)
    return value is not None and value != "" and value != [] and value != ()


def _candidates(act: Act, resolved: Sequence[ResolvedRef], partners: Sequence[str]) -> list[str]:
    """Produsele judecate: țintele rezolvate ale actului, în ordine, plus partenerii pe care i-a
    adăugat EXECUTORUL (o comparație cu o țintă, calea `COMPARE_WITH_SIMILAR`). Partenerii vin
    numiți, nu deduși din fapte: faptele unui tur cu mai multe acte conțin și produsele altui act
    (recenzia NX-332: husele fără `screen` ale actului vecin interziceau un verdict corect)."""
    by_id = {r.ref_id: r for r in resolved}
    ids: list[str] = []
    for target in act.targets:
        ref = by_id.get(target)
        for pid in ref.product_ids if ref is not None else ():
            if pid not in ids:
                ids.append(pid)
    ids += [pid for pid in partners if pid not in ids]
    return ids


def answer_policy(
    act: Act,
    resolved: Sequence[ResolvedRef],
    facts: Mapping[str, ProductFacts],
    *,
    vocab: CatalogVocabulary | None,
    pack: object | None,
    references: Sequence[Reference] = (),
    ambiguity: AmbiguityDecision | None = None,
    locale: str | None = None,
    partners: Sequence[str] = (),
) -> AnswerPolicy | None:
    """PUR. None = actul nu cere o judecată între produse (nimic de politicat).

    Se aplică pe `compare`, pe `detail` cu ≥ 2 candidați și pe orice verdict `act_both`. Fără
    dimensiune decisivă ⇒ None (nu s-a cerut o judecată pe un atribut). Dimensiunea necunoscută pe
    VREUN candidat ⇒ `verdict_allowed=False`, `missing=[dimensiunea]` (I12).

    `references` = referințele interpretării (dimensiunea unei referințe `extreme`), `ambiguity` =
    verdictul porții, `locale` = limba etichetelor de rând (None ⇒ etichetele tuturor limbilor),
    `partners` = produsele adăugate de executor (partenerul unei comparații cu o țintă)."""
    both = ambiguity is not None and ambiguity.verdict == "act_both"
    if act.kind not in _JUDGED_ACTS and not both:
        return None
    candidates = _candidates(act, resolved, partners)
    if len(candidates) < _MIN_CANDIDATES:
        return None
    dimension = _extreme_dimension(act, references, vocab)
    if dimension is None:
        dimension = read_query(act, pack=pack, locale=locale).dimension
    if dimension is None and act.question:
        # NX-381 (kernel.v6.3): pe `compare`/`detail` cererea stă în `Act.question`, nu în `query`
        # («care e mai hidratantă?»). Același cititor declarat, aceleași etichete de rând.
        dimension = _read_query(act.question, _row_labels(pack, locale), locale).dimension
    if dimension is None:
        return None
    source = _source_key(pack, dimension)
    if all(_known(facts.get(pid), dimension, source) for pid in candidates):
        return AnswerPolicy(verdict_allowed=True, missing=[])
    return AnswerPolicy(verdict_allowed=False, missing=[dimension])


__all__ = ["QueryEvidence", "answer_policy", "dimension_label", "read_query"]
