"""NX-203 — validare de integritate pentru qrels înainte de orice benchmark/gate."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from src.domain.normalize import normalize
from src.evals.retrieval.schema import Provenance, QrelsSet
from src.evals.retrieval.splits import Split, assign_split
from src.safety.external_data import contains_pii

#: Sub pragul ăsta, o felie de holdout nu mai măsoară nimic: un singur query greșit mișcă metrica
#: cu zeci de puncte, iar „a trecut gate-ul" devine zgomot prezentat ca dovadă.
MIN_HOLDOUT_SLICE = 20


def integrity_issues(
    qset: QrelsSet,
    *,
    min_queries: int = 1,
    require_human_verified: bool = False,
    require_real_per_category: bool = False,
    require_split_sizes: bool = False,
    catalog_product_ids: Iterable[str] | None = None,
) -> list[str]:
    """Întoarce problemele ce blochează o rulare; nu schimbă setul de date."""
    issues: list[str] = []
    if len(qset.queries) < min_queries:
        issues.append(f"sunt {len(qset.queries)} query-uri, sub minimul cerut {min_queries}")

    real_by_category: dict[str | None, int] = defaultdict(int)
    # NB: id-urile duplicate NU se verifică aici — `QrelsSet._unique_ids` le respinge deja la
    # construcție, deci un set cu duplicate nu ajunge niciodată în funcția asta.
    # text normalizat → feliile în care apare. Split-urile sunt derivate DETERMINIST din id, deci
    # un query nu poate fi în două felii; ce se poate, însă, e ca ACELAȘI text (sau o parafrază
    # identică după normalizare) să primească două id-uri și să aterizeze în tuning ȘI în holdout.
    # Atunci holdout-ul nu mai e independent, iar gate-ul măsoară ce a văzut deja la tuning.
    splits_by_text: dict[str, set[str]] = defaultdict(set)
    known_products = {str(p) for p in catalog_product_ids} if catalog_product_ids else None
    # Acumulate ca să putem distinge „produse şterse din catalog" de „compari două spaţii de
    # identificatori diferite" — vezi verificarea de după buclă.
    all_referenced: set[str] = set()
    missing_by_query: list[str] = []

    for query in qset.queries:
        if not query.query.strip():
            issues.append(f"{query.id}: query gol")
        if contains_pii(query.query):
            issues.append(f"{query.id}: query-ul conține posibil PII")

        judged = [item.product_id for item in query.judgments]
        if len(judged) != len(set(judged)):
            issues.append(f"{query.id}: judgments conține product_id duplicat")
        if not any(int(item.relevance) > 0 for item in query.judgments):
            issues.append(f"{query.id}: lipsește cel puțin un produs relevant")
        if require_human_verified and not query.human_verified:
            issues.append(f"{query.id}: nu este marcat human_verified")
        if query.provenance is Provenance.real_sanitized:
            real_by_category[query.category] += 1

        if text := normalize(query.query).strip():
            splits_by_text[text].add(assign_split(query.id).value)

        # Un qrels care judecă produse inexistente produce metrici care arată bine și nu înseamnă
        # nimic: recall-ul se calculează contra unui adevăr care nu mai e în catalog.
        if known_products is not None:
            referenced = {str(item.product_id) for item in query.judgments}
            referenced |= {str(p) for p in query.forbidden_products}
            all_referenced |= referenced
            if missing := sorted(referenced - known_products):
                missing_by_query.append(
                    f"{query.id}: referă produse absente din catalog: {missing[:5]}"
                )

    # ZERO suprapunere între ce referă qrels-ul şi ce ştie catalogul înseamnă, aproape sigur, că
    # cele două vorbesc despre identificatori DIFERIŢI (qrels pe UUID-uri din DB vs. catalog de
    # seed pe slug-uri), nu că fiecare produs judecat a dispărut. Fără gardă, verificarea scuipă un
    # zid de findings false — iar o poartă care minte des ajunge să fie ignorată cu totul.
    if known_products is not None and all_referenced and not (all_referenced & known_products):
        issues.append(
            f"catalogul dat nu are NICIUN identificator comun cu qrels-ul "
            f"({len(all_referenced)} referite, {len(known_products)} în catalog) — cel mai "
            f"probabil compari spaţii diferite (UUID din DB vs. slug de seed), nu produse şterse"
        )
    else:
        issues.extend(missing_by_query)

    for text, splits in sorted(splits_by_text.items()):
        if len(splits) > 1:
            issues.append(
                f"text de query prezent în mai multe felii ({', '.join(sorted(splits))}) — "
                f"holdout contaminat: {text[:60]!r}"
            )

    if require_real_per_category:
        for category in sorted({query.category for query in qset.queries}, key=str):
            if real_by_category[category] == 0:
                label = category or "fără categorie"
                issues.append(f"{label}: lipsește un query real_sanitized")

    if require_split_sizes:
        sizes: Mapping[str, int] = _split_sizes(qset)
        for split in (Split.holdout_h1, Split.holdout_h2, Split.holdout_h3):
            if (n := sizes.get(split.value, 0)) < MIN_HOLDOUT_SLICE:
                issues.append(
                    f"{split.value}: {n} query-uri, sub pragul de {MIN_HOLDOUT_SLICE} — "
                    f"felia e prea mică pentru a măsura ceva"
                )
    return issues


def _split_sizes(qset: QrelsSet) -> dict[str, int]:
    sizes: dict[str, int] = defaultdict(int)
    for query in qset.queries:
        sizes[assign_split(query.id).value] += 1
    return dict(sizes)
