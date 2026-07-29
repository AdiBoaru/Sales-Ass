"""NX-203 — validare de integritate pentru qrels înainte de orice benchmark/gate."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from src.domain.normalize import normalize
from src.evals.retrieval.schema import Provenance, QrelsSet
from src.evals.retrieval.splits import Split, assign_split, split_key
from src.safety.external_data import contains_pii

#: Sub pragul ăsta, o felie de holdout nu mai măsoară nimic: un singur query greșit mișcă metrica
#: cu zeci de puncte, iar „a trecut gate-ul" devine zgomot prezentat ca dovadă.
MIN_HOLDOUT_SLICE = 20


@dataclass(frozen=True)
class ValidationReport:
    """TREI stări, nu două: trecut · picat · **neverificat**.

    A treia nu e un detaliu de formulare. O verificare care n-a putut rula nu e nici o problemă de
    date, nici o confirmare — iar dacă o îmbraci în oricare dintre celelalte două, minți în direcţii
    diferite: ca eşec, produce alarme false şi ajunge ignorată; ca succes, declară verificat exact
    ce n-a fost verificat niciodată.

    `blocking` opreşte. `unavailable` spune ce N-A FOST verificat şi de ce."""

    blocking: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Curat = zero blocaje ŞI zero verificări nerulate."""
        return not self.blocking and not self.unavailable


def integrity_issues(
    qset: QrelsSet,
    *,
    min_queries: int = 1,
    require_human_verified: bool = False,
    require_real_per_category: bool = False,
    require_split_sizes: bool = False,
    catalog_product_ids: Iterable[str] | None = None,
) -> list[str]:
    """Toate problemele într-o singură listă — inclusiv verificările nerulate.

    Păstrată pentru apelanţii care iau o decizie de GATE (`nx207_compare_shadow`): acolo o
    verificare nerulată trebuie să blocheze la fel ca o problemă, fiindcă nu porneşti un switch pe
    un qrels despre care nu ştii dacă e coerent. Cine are nevoie de distincţie foloseşte
    `validate_integrity()`."""
    report = validate_integrity(
        qset,
        min_queries=min_queries,
        require_human_verified=require_human_verified,
        require_real_per_category=require_real_per_category,
        require_split_sizes=require_split_sizes,
        catalog_product_ids=catalog_product_ids,
    )
    return report.blocking + report.unavailable


def validate_integrity(
    qset: QrelsSet,
    *,
    min_queries: int = 1,
    require_human_verified: bool = False,
    require_real_per_category: bool = False,
    require_split_sizes: bool = False,
    catalog_product_ids: Iterable[str] | None = None,
) -> ValidationReport:
    """Validare cu cele trei stări separate; nu schimbă setul de date."""
    issues: list[str] = []
    unavailable: list[str] = []
    if len(qset.queries) < min_queries:
        issues.append(f"sunt {len(qset.queries)} query-uri, sub minimul cerut {min_queries}")

    real_by_category: dict[str | None, int] = defaultdict(int)
    # NB: id-urile duplicate NU se verifică aici — `QrelsSet._unique_ids` le respinge deja la
    # construcție, deci un set cu duplicate nu ajunge niciodată în funcția asta.
    # text normalizat → feliile în care apare. Felia e derivată DETERMINIST din `split_key` (adică
    # `split_group_id`, altfel `id`), deci un query nu poate fi în două felii; ce se poate, însă, e
    # ca ACELAȘI text (sau o parafrază identică după normalizare) să ajungă în două grupuri diferite
    # și să aterizeze în tuning ȘI în holdout. Atunci holdout-ul nu mai e independent, iar gate-ul
    # măsoară ce a văzut deja la tuning.
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
            # ACEEAŞI cheie ca `partition()`: `split_group_id` dacă există, altfel `id`. Cu
            # `query.id`, validatorul raporta pentru o împărţire pe care nimeni n-o execută —
            # o poartă care verifică altceva decât ce se întâmplă e mai rea decât una lipsă.
            splits_by_text[text].add(assign_split(split_key(query)).value)

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
    #
    # Formularea contează: NU e „qrels invalid", e „verificarea nu s-a putut face". A treia stare,
    # distinctă şi de trecut, şi de picat. Un „a trecut" tăcut aici ar fi cel mai rău rezultat
    # posibil: ar declara verificat exact ce n-a fost verificat niciodată.
    if known_products is not None and all_referenced and not (all_referenced & known_products):
        unavailable.append(
            f"coerenţă cu catalogul: zero identificatori comuni ({len(all_referenced)} referite în "
            f"qrels, {len(known_products)} în catalogul dat). Qrels-ul foloseşte UUID-uri din DB, "
            f"catalogul de seed foloseşte slug-uri — dă un export din DB ca verificarea să conteze."
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
    return ValidationReport(blocking=issues, unavailable=unavailable)


def _split_sizes(qset: QrelsSet) -> dict[str, int]:
    """Dimensiunile feliilor exact aşa cum le produce `partition()`.

    Folosea `query.id`, deci pentru un `split_group` cu id-uri divergente raporta două felii acolo
    unde partiţionarea reală face una singură. Gate-ul valida o împărţire imaginară."""
    sizes: dict[str, int] = defaultdict(int)
    for query in qset.queries:
        sizes[assign_split(split_key(query)).value] += 1
    return dict(sizes)
