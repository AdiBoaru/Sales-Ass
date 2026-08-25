"""NX-257 — poarta de POTRIVIRE: un produs ale cărui date CONTRAZIC ce a cerut clientul iese.

Sistemul are porți de ADEVĂR peste tot (validatorul stagiului 8, `grounding_guard`, safety,
fereastra de promoție NX-191). Toate pot respinge o minciună. **Niciuna nu poate respinge un
adevăr nepotrivit** — de asta un tur poate recomanda onest un ser de ten uscat cuiva care tocmai
a spus că are ten gras: fiecare propoziție e verificabilă, iar întregul e greșit.

Modulul ăsta e poarta lipsă, și e DETERMINIST — cod, nu model. Trei reguli, în ordine:

  1. **Doar fațetele `partitioning` pot exclude** (`domain.facets`). Cumpărătorul are exact un tip
     de ten / o mărime / un tip de motor: un produs cu altă valoare îl CONTRAZICE. O fațetă
     `additive` (obiective, beneficii) nu exclude niciodată — „riduri" nu contrazice un ser
     hidratant, doar nu-l țintește. Declarația e a fațetei, nu a frazei: zero liste de cuvinte,
     zero limbă hardcodată, zero vertical special (P11/D3).

  2. **`UNKNOWN` trece MEREU** (D7). Excludem doar produse ale căror date CONTRAZIC explicit. Un
     produs neetichetat nu e un produs nepotrivit — a-l arunca ar transforma o gaură de catalog
     într-un „nu avem".

  3. **Sub pragul de acoperire nu se aplică nimic** (`min_coverage`, declarat deja în registru din
     NX-188 și necitit de nimeni până acum). Dacă printre candidați prea puțini au o valoare
     cunoscută pentru fațetă, nu putem distinge „contrazice" de „neetichetat", deci fațeta nu
     capătă drept de excludere în turul ăsta. Pragul se măsoară pe SETUL DE CANDIDAȚI, nu pe
     catalog: e evidența deciziei curente, e ieftin (in-memory) și nu cere un job de statistici.

Pur: dict-uri în, dict-uri afară. Fără I/O, fără ceas, fără LLM — două rulări dau același rezultat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.agent.match_gate import MatchSet, canonical_facet_key
from src.domain.facets import TypedFacet

#: Fațetele care POT exclude declară asta explicit. Vezi `_BINDING_KINDS` în domain.facets.
BINDING_PARTITIONING = "partitioning"


@dataclass(frozen=True)
class MaskOutcome:
    """Ce a făcut masca. `kept` sunt produsele care rămân, în ORDINEA primită (rankingul nu se
    rescrie aici). Restul câmpurilor sunt pentru telemetrie și pentru runbook: o poartă care taie
    fără să spună de ce e o poartă pe care n-o poți depana."""

    kept: tuple[dict[str, Any], ...]
    excluded_ids: tuple[str, ...]
    #: Fațetele care au avut efectiv drept de excludere în turul ăsta (partitioning + peste prag).
    enforced_facets: tuple[str, ...]
    #: Fațete partitioning sărite fiindcă acoperirea printre candidați e sub pragul lor declarat.
    skipped_low_coverage: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.excluded_ids)


def _known_value(product: dict[str, Any], facet: TypedFacet) -> bool:
    """Produsul are o valoare CUNOSCUTĂ pentru fațetă? Contează pentru acoperire, nu pentru verdict
    (verdictul l-a dat deja `match_gate`). Import local ca să nu duplicăm regulile de extragere."""
    from src.domain.facets import extract_value  # noqa: PLC0415 — evită ciclul la import

    attributes = product.get("attributes")
    value = extract_value(facet, product, attributes if isinstance(attributes, dict) else {})
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, str)) and len(value) == 0:
        return False
    return True


def _coverage(products: list[dict[str, Any]], facet: TypedFacet) -> float:
    """Fracția candidaților cu valoare cunoscută pe fațetă. Set gol → 0.0 (nu 1.0: fără dovezi nu
    se aplică nimic; a întoarce 1.0 ar da drept de excludere exact când nu știm nimic)."""
    if not products:
        return 0.0
    return sum(1 for p in products if _known_value(p, facet)) / len(products)


def enforceable_facets(
    products: list[dict[str, Any]],
    match_set: MatchSet,
    facets_by_key: dict[str, TypedFacet],
) -> tuple[set[str], tuple[str, ...]]:
    """Care fațete au drept de excludere în turul ăsta → `(chei_aplicabile, sărite_sub_prag)`.

    O fațetă intră DOAR dacă: e `partitioning`, a produs cel puțin un verdict hard în `match_set`
    (altfel n-are ce exclude) și acoperirea printre candidați îi atinge pragul declarat."""
    hard_keys = {
        canonical_facet_key(facets_by_key, r.facet)
        for v in match_set.verdicts
        for r in v.constraint_results
        if r.strength == "hard"
    }
    enforceable: set[str] = set()
    skipped: list[str] = []
    for key in sorted(hard_keys):
        facet = facets_by_key.get(key)
        if facet is None or facet.binding != BINDING_PARTITIONING:
            continue  # additive (sau necunoscută registrului): influențează scorul, nu apartenența
        if _coverage(products, facet) < facet.min_coverage:
            skipped.append(key)
            continue
        enforceable.add(key)
    return enforceable, tuple(skipped)


def apply_mask(
    products: list[dict[str, Any]],
    match_set: MatchSet | None,
    facets_by_key: dict[str, TypedFacet],
) -> MaskOutcome:
    """Scoate produsele care CONTRAZIC o constrângere hard pe o fațetă cu drept de excludere.

    `match_set=None` (gate-ul n-a rulat: fără fațete, fără constrângeri, flag stins) → no-op
    explicit, nu o excepție: absența verdictelor înseamnă „n-avem pe ce ne baza", iar poarta
    fail-open e singura variantă onestă atunci (P6).

    `rejected` din MatchSet NU se folosește direct: el înseamnă „≥1 hard MISMATCH pe ORICE fațetă",
    inclusiv una `additive` sau una sub prag. Recalculăm pe fațetele care chiar au drept, altfel
    o fațetă fără drept ar exclude prin ușa din dos."""
    if match_set is None or not products:
        return MaskOutcome(tuple(products), (), (), ())

    enforceable, skipped = enforceable_facets(products, match_set, facets_by_key)
    if not enforceable:
        return MaskOutcome(tuple(products), (), (), skipped)

    contradicted: set[str] = set()
    for verdict in match_set.verdicts:
        for r in verdict.constraint_results:
            if (
                r.strength == "hard"
                and r.status == "MISMATCH"  # UNKNOWN trece MEREU (D7)
                and canonical_facet_key(facets_by_key, r.facet) in enforceable
            ):
                contradicted.add(verdict.product_id)
                break

    kept = tuple(p for p in products if str(p.get("id")) not in contradicted)
    excluded = tuple(str(p.get("id")) for p in products if str(p.get("id")) in contradicted)
    return MaskOutcome(kept, excluded, tuple(sorted(enforceable)), skipped)
