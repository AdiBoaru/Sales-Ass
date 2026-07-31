"""NX-203 — harness de benchmark (SCHELET). Rulează o funcție de retrieval peste qrels și produce
metrici comparabile + config-ul rulării. Fără dataset masiv: primește orice `QrelsSet` (exemplul
minuscul sau, ulterior, setul complet de 200-500 populat din etichetele NX-202).

Retrieval-ul e injectat ca `RetrieveFn` (query → listă ordonată de product_id) — harness-ul NU știe
de `search_products`/`search_entities`, ca aceeași măsurare să compare configurații diferite
(lexical vs +semantic vs +reranker; embeddings A vs B) fără să se schimbe codul de măsurare.

CATALOGUL e al doilea input pentru orice afirmaţie despre constrângeri: `hard_constraints` se
evaluează contra produselor, nu contra qrels-ului. Fără catalog nu există „zero încălcări", există
„neverificat". Contractul stă în tipuri (`float | None` + `constraint_validation`), ca un 0 să nu
poată apărea niciodată dintr-o verificare care n-a rulat.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from statistics import mean

from pydantic import BaseModel

from src.evals.retrieval import metrics
from src.evals.retrieval.schema import QrelsQuery, QrelsSet

# query text → listă ordonată de product_id (cel mai relevant primul).
RetrieveFn = Callable[[str], Sequence[str]]

#: Stările verificării de constrângeri. A treia stare e explicită, nu dedusă dintr-un `None`.
VERIFIED = "verified"
UNAVAILABLE = "constraint_validation_unavailable"


#: Câmpurile pe care `constraints.evaluate` chiar le citeşte. Amprenta se calculează DOAR peste
#: ele: `name` ar schimba identitatea snapshotului la o corectură cosmetică de text, fără ca vreo
#: constrângere să fie evaluată altfel.
_FINGERPRINTED_FIELDS = ("price", "category_slug", "attributes")


class CatalogSnapshot(BaseModel):
    """Catalogul contra căruia se evaluează `hard_constraints`, cu identitate proprie.

    Amprenta există fiindcă un raport rulat pe alt catalog produce delte care ARATĂ ca schimbări de
    calitate, dar sunt schimbări de DATE. Ca să fie dovadă, nu etichetă, se calculează peste
    CONŢINUTUL canonic (id + preţ + categorie + attributes, chei sortate), nu peste setul de
    id-uri: două seed-uri care schimbă doar preţuri au acelaşi set de id-uri, iar `version` bazată
    pe `updated_at` trunchiat la secundă coincide dacă modificările cad în aceeaşi secundă.
    `version` rămâne partea lizibilă — utilă la citit, insuficientă ca identitate.

    `products` mapează product_id → dict-ul de produs (`price`, `category_slug`, `attributes`),
    exact forma consumată de `constraints.evaluate`."""

    version: str
    products: Mapping[str, dict]

    @property
    def fingerprint(self) -> str:
        canonical = {
            pid: {f: product.get(f) for f in _FINGERPRINTED_FIELDS}
            for pid, product in self.products.items()
        }
        # `default=str` acoperă Decimal/datetime venite din DB; `sort_keys` face amprenta
        # independentă de ordinea în care au venit rândurile.
        payload = json.dumps(
            canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        return f"{self.version}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    def coverage_gaps(self, qset: QrelsSet) -> list[str]:
        """Id-urile la care qrels-ul se referă şi care LIPSESC din snapshot.

        Suprapunerea parţială nu e suficientă: un snapshot care conţine doar produsul judecat, dar
        nu şi ce întoarce retrieval-ul, trecea drept compatibil şi raporta zero încălcări. Cazul
        limită (spaţii de id complet diferite — UUID din DB vs. slug de seed) e doar forma extremă
        a aceleiaşi probleme, deci se verifică o singură dată, strict."""
        referenced = {j.product_id for q in qset.queries for j in q.judgments}
        referenced |= {p for q in qset.queries for p in q.forbidden_products}
        return sorted(referenced - set(self.products))


class RunConfig(BaseModel):
    """Config-ul complet al rulării — înregistrat în output ca rezultatele să fie reproductibile
    și comparabile (Codex: model embeddings, document_version, ponderi, reranker, data)."""

    label: str  # ex. "baseline-lexical+semantic+fusion"
    embedding_model: str | None = None
    document_version: str | None = None
    reranker: str | None = None
    weights: dict[str, float] | None = None
    split: str | None = None  # pe ce felie s-a rulat (tuning/holdout_hN)


class QueryResult(BaseModel):
    id: str
    recall_at_20: float
    ndcg_at_6: float
    top_6_hit: float
    mrr: float
    #: `None` = NEVERIFICAT (fără catalog sau cu produse absente din el). Distinct de 0 = verificat
    #: şi curat.
    forbidden_in_6: int | None = None
    #: Produsele din top-6 care nu există în snapshot. Nesolicitat de contract, dar fără ele
    #: „neverificat" ar fi un verdict fără cauză, deci nereparabil.
    unverifiable_ids: list[str] = []
    family_id: str | None = None  # din qrels, NU derivat din text la runtime


class BenchmarkReport(BaseModel):
    """Scorurile headline sunt agregate pe FAMILIE, nu pe query.

    Fără asta, un query prezent în două variante de formă (diacritice, typo) cântărește dublu: nu
    pentru că ar conta mai mult, ci pentru că a fost cules de două ori. Media pe familie şi apoi
    macro peste familii scoate din scor exact acest artefact de colectare.

    `n_queries` şi `n_families` sunt AMBELE expuse, ca să se vadă când ponderarea headline nu mai
    coincide cu numărul brut de interogări."""

    config: RunConfig
    n_queries: int
    n_families: int
    recall_at_20: float
    ndcg_at_6: float
    top_6_hit_rate: float
    mrr: float
    #: Fracția FAMILIILOR cu ≥1 încălcare în top-6 — sau `None` când verificarea n-a putut rula.
    #: OR în interiorul familiei: dacă o singură variantă scoate un produs interzis, familia e
    #: violată — o încălcare de constrângere nu se mediază, se numără. Altfel, adăugarea unei
    #: variante „curate" ar dilua o violare reală.
    #: NICIODATĂ 0 în lipsa catalogului: un zero acolo ar declara „curat" ceva nemăsurat.
    forbidden_violation_rate: float | None = None
    #: `verified` | `constraint_validation_unavailable` — explicit, ca un consumator automat să nu
    #: fie nevoit să deducă starea dintr-un `None`.
    constraint_validation: str = UNAVAILABLE
    catalog_fingerprint: str | None = None
    #: Toate produsele returnate care lipsesc din snapshot. Nevid ⇒ raportul e neverificat.
    #: Verificarea e totul-sau-nimic la nivel de raport: o rată calculată doar pe query-urile
    #: evaluabile ar fi corectă pe o submulţime tăcută, iar cititorul n-ar avea de unde şti pe care.
    unverifiable_products: list[str] = []
    per_query: list[QueryResult]


class BenchmarkComparison(BaseModel):
    """Comparație machine-readable între baseline și candidat pe exact același qrels split."""

    baseline: BenchmarkReport
    candidate: BenchmarkReport
    delta_recall_at_20: float
    delta_ndcg_at_6: float
    delta_top_6_hit_rate: float
    delta_forbidden_violation_rate: float


def compare_reports(baseline: BenchmarkReport, candidate: BenchmarkReport) -> BenchmarkComparison:
    """Diferențe candidat-minus-baseline; condiția de switch se decide în afara harness-ului."""
    if baseline.n_queries != candidate.n_queries:
        raise ValueError("nu pot compara rapoarte cu număr diferit de query-uri")
    if baseline.n_families != candidate.n_families:
        # Scorurile headline sunt macro pe familie; cu alt număr de familii, deltele compară
        # ponderări diferite şi arată ca o schimbare de calitate.
        raise ValueError("nu pot compara rapoarte cu număr diferit de familii")
    for report, label in ((baseline, "baseline"), (candidate, "candidat")):
        if report.constraint_validation != VERIFIED:
            # Un raport neverificat are rata `None`. Comparaţia ar trebui să scadă un `None` — sau,
            # mai rău, l-ar trata ca 0 şi ar raporta „fără regresie de siguranţă" pe o rulare în
            # care nicio constrângere n-a fost măsurată.
            cauza = (
                f"; produse absente din catalog: {report.unverifiable_products[:5]}"
                if report.unverifiable_products
                else ""
            )
            raise ValueError(
                f"{label}: verificarea constrângerilor n-a rulat "
                f"({report.constraint_validation}) — comparaţia ar prezenta ca egalitate ceva "
                f"nemăsurat{cauza}"
            )
    if baseline.catalog_fingerprint != candidate.catalog_fingerprint:
        raise ValueError(
            f"cataloage diferite ({baseline.catalog_fingerprint} vs "
            f"{candidate.catalog_fingerprint}) — deltele ar fi schimbări de DATE, nu de calitate"
        )
    return BenchmarkComparison(
        baseline=baseline,
        candidate=candidate,
        delta_recall_at_20=candidate.recall_at_20 - baseline.recall_at_20,
        delta_ndcg_at_6=candidate.ndcg_at_6 - baseline.ndcg_at_6,
        delta_top_6_hit_rate=candidate.top_6_hit_rate - baseline.top_6_hit_rate,
        delta_forbidden_violation_rate=(
            candidate.forbidden_violation_rate - baseline.forbidden_violation_rate
        ),
    )


def evaluate_query(
    q: QrelsQuery, ranked: Sequence[str], catalog: CatalogSnapshot | None = None
) -> QueryResult:
    """Fără catalog, `forbidden_in_6` rămâne `None`.

    Excepţiile explicite singure nu spun nimic despre respectarea contractului: sunt tocmai cazurile
    care NU au reprezentare în atribute. Un număr calculat doar din ele ar arăta ca o verificare de
    constrângeri, fără să fie."""
    products = catalog.products if catalog else None
    return QueryResult(
        id=q.id,
        family_id=q.family_id,
        recall_at_20=metrics.recall_at_k(q, ranked, 20),
        ndcg_at_6=metrics.ndcg_at_k(q, ranked, 6),
        top_6_hit=metrics.top_k_hit(q, ranked, 6),
        mrr=metrics.mrr(q, ranked),
        # Un singur numărător (reuniune excepţii ∪ derivate), în `metrics` — două implementări ar
        # ajunge să numere lucruri diferite fără ca vreun test să le pună faţă în faţă.
        forbidden_in_6=metrics.violations_at_k(q, ranked, 6, products),
        unverifiable_ids=metrics.missing_from_catalog(ranked, 6, products),
    )


def _families(results: Sequence[QueryResult]) -> dict[str, list[QueryResult]]:
    """Grupează rezultatele pe `family_id`.

    Un set LEGACY complet negrupat rămâne valid: fiecare query devine familie singleton, deci
    agregarea pe familie coincide exact cu media pe query. Seturile PARȚIAL grupate nu ajung aici —
    `QrelsSet` le respinge, fiindcă ar scuti tăcut tocmai intrările fără id."""
    out: dict[str, list[QueryResult]] = {}
    for r in results:
        out.setdefault(r.family_id or f"__singleton__:{r.id}", []).append(r)
    return out


def run_benchmark(
    qset: QrelsSet,
    retrieve: RetrieveFn,
    config: RunConfig,
    catalog: CatalogSnapshot | None = None,
) -> BenchmarkReport:
    """Rulează retrieval-ul pe fiecare query și agregă PE FAMILIE. Determinist dacă `retrieve` e.

    `catalog` e input-ul fără de care constrângerile nu pot fi evaluate. Lipsa lui NU e o eroare —
    metricile de relevanţă (recall/nDCG/MRR) rămân valide — dar raportul iese marcat
    `constraint_validation_unavailable`, cu rata `None`, şi nu mai poate intra într-o comparaţie.

    DOUĂ verificări de acoperire, la momente diferite:
      · ÎNAINTE de rulare — snapshotul trebuie să conţină toate id-urile la care se referă qrels-ul
        (altfel eroare: un snapshot parţial nu poate produce un verdict);
      · DUPĂ rulare — dacă retrieval-ul a întors produse absente din snapshot, raportul devine
        neverificat, cu id-urile respective în `unverifiable_products`."""
    if catalog is not None and (gaps := catalog.coverage_gaps(qset)):
        raise ValueError(
            f"snapshotul nu acoperă {len(gaps)} identificatori din qrels {gaps[:5]} — snapshot "
            "parţial sau spaţii de id diferite (UUID din DB vs. slug de seed). Produsele lipsă ar "
            "fi raportate ca fără încălcări, iar raportul ar ieşi marcat ca verificat."
        )
    per_query = [evaluate_query(q, list(retrieve(q.query)), catalog) for q in qset.queries]
    fams = _families(per_query)

    def macro(attr: str) -> float:
        """Medie ÎN familie, apoi macro peste familii — nu media globală pe query-uri."""
        if not fams:
            return 0.0
        return mean(mean(getattr(r, attr) for r in group) for group in fams.values())

    unverifiable = sorted({pid for r in per_query for pid in r.unverifiable_ids})
    if catalog is None or unverifiable:
        # Totul-sau-nimic la nivel de raport. O rată calculată doar pe query-urile evaluabile ar fi
        # corectă pe o submulţime pe care nimeni n-o vede — şi ar scădea exact când retrieval-ul
        # scoate produse din afara catalogului, adică tocmai când e mai suspect.
        rate: float | None = None
        state = UNAVAILABLE
    else:
        violated = sum(
            1 for group in fams.values() if any((r.forbidden_in_6 or 0) > 0 for r in group)
        )
        rate = violated / (len(fams) or 1)
        state = VERIFIED
    return BenchmarkReport(
        config=config,
        n_queries=len(per_query),
        n_families=len(fams),
        recall_at_20=macro("recall_at_20"),
        ndcg_at_6=macro("ndcg_at_6"),
        top_6_hit_rate=macro("top_6_hit"),
        mrr=macro("mrr"),
        forbidden_violation_rate=rate,
        constraint_validation=state,
        catalog_fingerprint=catalog.fingerprint if catalog else None,
        unverifiable_products=unverifiable,
        per_query=per_query,
    )
