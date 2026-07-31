"""NX-203 — teste pentru scheletul de benchmark: corectitudinea metricilor (valori calculate de
mână), integritatea qrels, spliturile single-use și rularea harness-ului pe exemplul minuscul.

NU testează retrieval-ul real (aia = popularea NX-203, după etichetele NX-202) — testează că
SCHELETUL e corect: metrici, contract, anti-contaminare.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.evals.retrieval import metrics
from src.evals.retrieval.harness import (
    UNAVAILABLE,
    VERIFIED,
    CatalogSnapshot,
    RunConfig,
    compare_reports,
    run_benchmark,
)
from src.evals.retrieval.schema import (
    HardConstraint,
    Provenance,
    QrelJudgment,
    QrelsQuery,
    QrelsSet,
    Relevance,
)
from src.evals.retrieval.splits import Split, holdout_slice_for_gate, partition

EXAMPLE = Path(__file__).parent / "golden" / "retrieval_qrels_example.json"


def _q(**kw) -> QrelsQuery:
    base = dict(id="q", query="x", provenance=Provenance.synthetic, catalog_version="v0")
    base.update(kw)
    return QrelsQuery(**base)


def _catalog(version: str = "example-v0", **extra: dict) -> CatalogSnapshot:
    """Catalogul fixture pentru exemplul minuscul: aceleaşi id-uri ca în qrels."""
    products = {
        "px-1": {"category_slug": "creme-fata", "price": 70, "attributes": {}},
        "px-2": {"category_slug": "creme-fata", "price": 80, "attributes": {}},
        "px-3": {"category_slug": "creme-fata", "price": 60, "attributes": {}},
        "px-4": {"category_slug": "seruri", "price": 120, "attributes": {}},
        "px-5": {"category_slug": "seruri", "price": 140, "attributes": {}},
        "px-6": {
            "category_slug": "creme-fata",
            "price": 55,
            "attributes": {"fragrance_free": True},
        },
        # încalcă `price lte 90` FĂRĂ să fie în `forbidden_products` — derivat din constrângere
        "px-scump": {"category_slug": "creme-fata", "price": 240, "attributes": {}},
        # şi excepţie explicită, ŞI violare derivată (altă categorie) — se numără o dată
        "px-off-category": {"category_slug": "parfumuri", "price": 50, "attributes": {}},
        "px-cu-parfum": {
            "category_slug": "creme-fata",
            "price": 50,
            "attributes": {"fragrance_free": False},
        },
    }
    products.update(extra)
    return CatalogSnapshot(version=version, products=products)


# --- metrici (valori calculate de mână) --------------------------------------


def test_recall_at_k():
    q = _q(
        judgments=[
            QrelJudgment(product_id="a", relevance=Relevance.ideal),
            QrelJudgment(product_id="b", relevance=Relevance.relevant),
            QrelJudgment(product_id="c", relevance=Relevance.marginal),
        ]
    )
    # 3 relevante (a,b,c); ranked prinde a și c în primele 20 → 2/3
    assert metrics.recall_at_k(q, ["a", "z", "c", "y"], 20) == pytest.approx(2 / 3)
    # niciun relevant → 1.0 (nimic de ratat)
    assert metrics.recall_at_k(_q(), ["a"], 20) == 1.0


def test_ndcg_at_k_perfect_and_imperfect():
    q = _q(
        judgments=[
            QrelJudgment(product_id="a", relevance=Relevance.ideal),  # gain 3
            QrelJudgment(product_id="b", relevance=Relevance.relevant),  # gain 2
        ]
    )
    # ordine perfectă a,b → nDCG=1
    assert metrics.ndcg_at_k(q, ["a", "b"], 6) == pytest.approx(1.0)
    # ordine inversată b,a: DCG = 2/log2(2) + 3/log2(3); IDCG = 3/log2(2) + 2/log2(3)
    dcg = 2 / math.log2(2) + 3 / math.log2(3)
    idcg = 3 / math.log2(2) + 2 / math.log2(3)
    assert metrics.ndcg_at_k(q, ["b", "a"], 6) == pytest.approx(dcg / idcg)


def test_top_k_hit_and_mrr():
    q = _q(judgments=[QrelJudgment(product_id="a", relevance=Relevance.relevant)])
    assert metrics.top_k_hit(q, ["z", "a"], 6) == 1.0
    assert metrics.top_k_hit(q, ["z", "y"], 6) == 0.0
    assert metrics.mrr(q, ["z", "a"]) == pytest.approx(0.5)
    assert metrics.mrr(q, ["z", "y"]) == 0.0


def test_forbidden_violations():
    q = _q(
        judgments=[QrelJudgment(product_id="a", relevance=Relevance.ideal)],
        forbidden_products=["bad"],
        forbidden_rationale={"bad": "fixture: produs interzis de test"},
    )
    assert metrics.forbidden_violations(q, ["a", "bad", "c"], 6) == 1
    assert metrics.forbidden_violations(q, ["a", "c"], 6) == 0


# --- integritatea qrels ------------------------------------------------------


def test_qrels_rejects_relevant_and_forbidden_overlap():
    with pytest.raises(ValidationError):
        _q(
            judgments=[QrelJudgment(product_id="a", relevance=Relevance.ideal)],
            forbidden_products=["a"],
        )


def test_qrels_rejects_duplicate_forbidden():
    with pytest.raises(ValidationError):
        _q(forbidden_products=["a", "a"])


def test_qrelsset_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        QrelsSet(business_id="b", queries=[_q(id="dup"), _q(id="dup")])


# --- splituri single-use -----------------------------------------------------


def test_split_is_deterministic():
    from src.evals.retrieval.splits import assign_split

    assert assign_split("ex-crema-gras-buget") == assign_split("ex-crema-gras-buget")


def test_gate_to_holdout_mapping_is_single_use():
    assert holdout_slice_for_gate("NX-207") == Split.holdout_h1
    assert holdout_slice_for_gate("NX-209") == Split.holdout_h2
    assert holdout_slice_for_gate("NX-210") == Split.holdout_h3
    # cele trei gate-uri folosesc felii DISTINCTE (anti-contaminare)
    slices = {holdout_slice_for_gate(g) for g in ("NX-207", "NX-209", "NX-210")}
    assert len(slices) == 3
    with pytest.raises(ValueError):
        holdout_slice_for_gate("NX-999")


def test_partition_covers_all_queries_without_overlap():
    qset = QrelsSet(
        business_id="b",
        queries=[_q(id=f"q{i}", category="creme" if i % 2 else "seruri") for i in range(20)],
    )
    parts = partition(qset)
    total = sum(len(v) for v in parts.values())
    assert total == 20
    seen = [q.id for v in parts.values() for q in v]
    assert len(seen) == len(set(seen))  # zero suprapunere


# --- harness pe exemplul minuscul --------------------------------------------


def test_harness_runs_on_example_qrels():
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    assert len(qset.queries) == 3

    # retrieval fake perfect: întoarce produsele relevante în ordine + evită interzisele
    truth = {q.id: [j.product_id for j in q.judgments] for q in qset.queries}
    by_query = {q.query: q.id for q in qset.queries}

    def perfect(query: str):
        return truth[by_query[query]]

    report = run_benchmark(
        qset, perfect, RunConfig(label="skeleton-selftest", split="example"), _catalog()
    )
    assert report.n_queries == 3
    assert report.recall_at_20 == pytest.approx(1.0)
    assert report.ndcg_at_6 == pytest.approx(1.0)
    assert report.top_6_hit_rate == pytest.approx(1.0)
    # 0 aici înseamnă „verificat şi curat" — de aceea starea se afirmă separat de cifră.
    assert report.constraint_validation == VERIFIED
    assert report.forbidden_violation_rate == 0.0


def test_harness_detects_forbidden_and_misses():
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    by_query = {q.query: q for q in qset.queries}

    def bad(query: str):
        q = by_query[query]
        # întoarce produsele INTERZISE + rateaza relevantele. Substitutul e un produs REAL din
        # catalog, nu un id inventat: un id absent face raportul neverificat, nu „cu încălcări".
        return list(q.forbidden_products) or ["px-scump"]

    report = run_benchmark(qset, bad, RunConfig(label="skeleton-badcase"), _catalog())
    # query-urile cu forbidden trebuie semnalate
    assert report.forbidden_violation_rate > 0.0
    assert report.top_6_hit_rate < 1.0


def test_example_hard_constraints_parse():
    """Exemplul are hard_constraints valide (schema acceptă structura truth-first)."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    hc = qset.queries[0].hard_constraints
    assert hc and isinstance(hc[0], HardConstraint)
    assert hc[0].facet == "category"


def test_comparison_uses_same_query_count_and_exposes_deltas():
    qset = QrelsSet(business_id="b", queries=[_q(id="one")])
    cat = _catalog()
    baseline = run_benchmark(qset, lambda _query: ["px-1"], RunConfig(label="legacy"), cat)
    candidate = run_benchmark(qset, lambda _query: [], RunConfig(label="shadow"), cat)

    comparison = compare_reports(baseline, candidate)

    assert comparison.delta_recall_at_20 == 0.0
    assert comparison.delta_ndcg_at_6 == 0.0
    assert comparison.baseline.config.label == "legacy"
    assert comparison.candidate.config.label == "shadow"


def test_comparison_rejects_different_qrels_sizes():
    one = run_benchmark(
        QrelsSet(business_id="b", queries=[_q(id="one")]), lambda _q: [], RunConfig(label="one")
    )
    two = run_benchmark(
        QrelsSet(business_id="b", queries=[_q(id="one"), _q(id="two")]),
        lambda _q: [],
        RunConfig(label="two"),
    )

    with pytest.raises(ValueError, match="număr diferit"):
        compare_reports(one, two)


# --- catalogul ca input obligatoriu pentru constrângeri ----------------------


def test_without_catalog_constraints_are_unverified_not_clean():
    """Fără catalog: `None` + stare explicită. NICIODATĂ 0.

    Zero ar fi indistinct de „rulare curată", deci un benchmark fără catalog ar raporta exact
    rezultatul pe care şi-l doreşte cineva care nu l-a măsurat."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    by_query = {q.query: q for q in qset.queries}

    def returns_forbidden(query: str):
        return list(by_query[query].forbidden_products) or ["px-scump"]

    report = run_benchmark(qset, returns_forbidden, RunConfig(label="fara-catalog"))

    assert report.forbidden_violation_rate is None
    assert report.constraint_validation == UNAVAILABLE
    assert report.catalog_fingerprint is None
    assert all(r.forbidden_in_6 is None for r in report.per_query)
    # metricile de relevanţă rămân valide — lipsa catalogului nu invalidează recall/nDCG
    assert report.recall_at_20 == 0.0


def test_compatible_catalog_catches_derived_violation():
    """`px-scump` NU e în `forbidden_products` — încălcarea vine din `price lte 90`."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    target = qset.queries[0]  # ex-crema-gras-buget
    assert "px-scump" not in target.forbidden_products

    def leaks_expensive(query: str):
        return ["px-scump"] if query == target.query else ["px-4"]

    report = run_benchmark(qset, leaks_expensive, RunConfig(label="cu-catalog"), _catalog())

    assert report.constraint_validation == VERIFIED
    assert report.forbidden_violation_rate == pytest.approx(1 / 3)
    hit = next(r for r in report.per_query if r.id == target.id)
    assert hit.forbidden_in_6 == 1


def test_explicit_exception_and_derived_violation_count_once():
    """`px-off-category` e ŞI excepţie explicită, ŞI violare derivată (altă categorie).

    Reuniune, nu sumă: altfel acelaşi produs ar fi numărat de două ori şi rata ar depinde de cât
    de bine e acoperit un caz de constrângeri, nu de câte produse greşite au ieşit."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    target = qset.queries[0]
    assert "px-off-category" in target.forbidden_products

    report = run_benchmark(
        qset,
        lambda query: ["px-off-category"] if query == target.query else [],
        RunConfig(label="union"),
        _catalog(),
    )

    hit = next(r for r in report.per_query if r.id == target.id)
    assert hit.forbidden_in_6 == 1


def test_catalog_with_disjoint_ids_is_blocked():
    """Spaţii de identificatori diferite (UUID din DB vs. slug de seed) → oprire, nu zero."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    foreign = CatalogSnapshot(
        version="alt-export",
        products={"uuid-aaaa": {"category_slug": "creme-fata", "price": 10, "attributes": {}}},
    )

    with pytest.raises(ValueError, match="nu acoperă"):
        run_benchmark(qset, lambda _query: [], RunConfig(label="incompatibil"), foreign)


def test_partial_snapshot_is_blocked_not_accepted_as_overlapping():
    """Suprapunerea parţială NU e suficientă.

    Regresie: verificarea cerea doar intersecţie nenulă, deci un snapshot care conţine produsul
    judecat, dar nu şi restul catalogului, trecea drept compatibil."""
    qset = QrelsSet(**json.loads(EXAMPLE.read_text(encoding="utf-8")))
    partial = CatalogSnapshot(
        version="partial",
        products={"px-1": {"category_slug": "creme-fata", "price": 70, "attributes": {}}},
    )
    # intersecţia e nenulă — vechea condiţie ar fi acceptat snapshotul
    assert set(partial.products) & {j.product_id for j in qset.queries[0].judgments}

    with pytest.raises(ValueError, match="nu acoperă"):
        run_benchmark(qset, lambda _query: [], RunConfig(label="partial"), partial)


def test_product_outside_snapshot_makes_report_unverified_not_clean():
    """Un produs din top-6 absent din snapshot ⇒ NEVERIFICAT, nu ignorat.

    Regresie: produsul lipsă era sărit tăcut, deci un retrieval care întoarce exact produse din
    afara catalogului raporta zero încălcări şi `verified` — cel mai prost rezultat posibil
    prezentat drept cel mai bun."""
    qset = QrelsSet(
        business_id="b",
        queries=[
            _q(
                id="q1",
                query="x",
                judgments=[QrelJudgment(product_id="px-1", relevance=Relevance.ideal)],
                hard_constraints=[HardConstraint(facet="price", op="lte", value=90)],
            )
        ],
    )
    # snapshotul acoperă tot ce cere qrels-ul — blocajul de la intrare NU se aplică
    catalog = CatalogSnapshot(
        version="acoperitor",
        products={"px-1": {"category_slug": "creme-fata", "price": 70, "attributes": {}}},
    )

    report = run_benchmark(qset, lambda _query: ["fantoma"], RunConfig(label="absent"), catalog)

    assert report.constraint_validation == UNAVAILABLE
    assert report.forbidden_violation_rate is None
    assert report.per_query[0].forbidden_in_6 is None
    # cauza e numită, altfel „neverificat" ar fi un verdict fără remediu
    assert report.unverifiable_products == ["fantoma"]


def test_unverified_by_missing_product_names_cause_in_comparison():
    qset = QrelsSet(
        business_id="b",
        queries=[
            _q(id="q1", judgments=[QrelJudgment(product_id="px-1", relevance=Relevance.ideal)])
        ],
    )
    catalog = CatalogSnapshot(
        version="acoperitor",
        products={"px-1": {"category_slug": "creme-fata", "price": 70, "attributes": {}}},
    )
    ok = run_benchmark(qset, lambda _query: ["px-1"], RunConfig(label="ok"), catalog)
    broken = run_benchmark(qset, lambda _query: ["fantoma"], RunConfig(label="rupt"), catalog)

    with pytest.raises(ValueError, match="produse absente din catalog: \\['fantoma'\\]"):
        compare_reports(ok, broken)


def test_fingerprint_separates_snapshots_that_differ_only_in_content():
    """Acelaşi `version`, acelaşi set de id-uri, alt PREŢ → altă amprentă.

    Regresie: hash-ul acoperea doar setul de id-uri, iar `version` vine din `updated_at` trunchiat
    la secundă — două modificări de preţ în aceeaşi secundă produceau amprente identice, deci două
    rapoarte incomparabile treceau drept comparabile."""

    def snap(price: float) -> CatalogSnapshot:
        return CatalogSnapshot(
            version="live:1@2026-07-31T12:00:00",
            products={"p-1": {"category_slug": "creme-fata", "price": price, "attributes": {}}},
        )

    ieftin, scump = snap(70), snap(240)
    assert ieftin.version == scump.version
    assert set(ieftin.products) == set(scump.products)
    assert ieftin.fingerprint != scump.fingerprint


def test_fingerprint_is_stable_across_key_order_and_ignores_cosmetics():
    """Amprenta e dovadă de identitate pentru CONSTRÂNGERI, nu checksum de rând.

    Stabilă la ordinea cheilor (altfel două citiri ale aceluiaşi catalog ar părea diferite) şi
    insensibilă la `name`, pe care nicio constrângere nu-l citeşte — o corectură de text n-are voie
    să invalideze o comparaţie."""
    a = CatalogSnapshot(
        version="v",
        products={"p": {"attributes": {"y": 1, "x": 2}, "price": 10, "category_slug": "seruri"}},
    )
    b = CatalogSnapshot(
        version="v",
        products={"p": {"category_slug": "seruri", "price": 10, "attributes": {"x": 2, "y": 1}}},
    )
    cosmetic = CatalogSnapshot(
        version="v",
        products={
            "p": {
                "category_slug": "seruri",
                "price": 10,
                "attributes": {"x": 2, "y": 1},
                "name": "alt nume",
            }
        },
    )

    assert a.fingerprint == b.fingerprint == cosmetic.fingerprint


def test_comparison_refuses_unverified_report():
    qset = QrelsSet(business_id="b", queries=[_q(id="one")])
    verified = run_benchmark(qset, lambda _query: [], RunConfig(label="baseline"), _catalog())
    unverified = run_benchmark(qset, lambda _query: [], RunConfig(label="candidat"))

    with pytest.raises(ValueError, match="candidat.*n-a rulat"):
        compare_reports(verified, unverified)
    with pytest.raises(ValueError, match="baseline.*n-a rulat"):
        compare_reports(unverified, verified)


def test_comparison_refuses_different_catalogs():
    """Acelaşi qrels, catalog diferit: deltele ar fi schimbări de DATE, nu de calitate."""
    qset = QrelsSet(business_id="b", queries=[_q(id="one")])
    baseline = run_benchmark(qset, lambda _query: [], RunConfig(label="baseline"), _catalog())
    candidate = run_benchmark(
        qset,
        lambda _query: [],
        RunConfig(label="candidat"),
        _catalog(version="example-v1"),
    )

    with pytest.raises(ValueError, match="cataloage diferite"):
        compare_reports(baseline, candidate)


def test_fingerprint_changes_with_product_set_not_only_version():
    """Aceeaşi `version`, alt set de produse → altă amprentă.

    Un re-seed care păstrează eticheta de versiune e exact cazul în care o comparaţie ar trece
    tăcut peste un catalog schimbat."""
    same = _catalog()
    grown = _catalog(**{"px-nou": {"category_slug": "seruri", "price": 30, "attributes": {}}})

    assert same.version == grown.version
    assert same.fingerprint != grown.fingerprint
