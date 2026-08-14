"""NX-240 — data-readiness: matricea câmpurilor, bugetul de query-uri și semantica lui
`verified_at`.

Cardul cere o matrice `field → owner → source → freshness SLA → formatter → unknown behavior →
CTA`. Ea trăiește ca DATE în `FIELD_POLICY`, nu într-un document — un document poate diverge tăcut
de cod, iar exact divergența asta e ce a produs contractul v1 (frontendul deriva stocul dintr-un
`0/null` pentru că nimeni nu scrisese undeva că `0` înseamnă „necunoscut").

Testele verifică trei lucruri pe care un review nu le poate garanta:
  1. matricea e COMPLETĂ și coerentă cu implementarea (nu doar plauzibilă);
  2. bugetul de query-uri e ZERO în builder și în projector — deci nu poate apărea N+1;
  3. `verified_at` chiar separă „rândul s-a atins" de „faptul s-a verificat".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.agent.evidence_bundle import (
    FACT_FIELDS,
    FIELD_POLICY,
    STRUCTURALLY_UNSOURCED,
    build_evidence_bundle,
)
from src.agent.grounding_guard import ground_answer
from src.channels.web.render_v2 import project
from tests.nx240_helpers import BUSINESS_ID, NOW, identity, plan, row

SLA = 86_400


# ── Matricea ────────────────────────────────────────────────────────────────────────────────
def test_every_projectable_field_has_a_policy():
    """Un câmp fără politică ar putea ajunge în ViewModel fără ca nimeni să fi decis ce se
    întâmplă când lipsește — adică exact felul în care apar placeholder-e inventate."""
    assert set(FIELD_POLICY) == set(FACT_FIELDS)
    for name, policy in FIELD_POLICY.items():
        assert policy.field == name
        assert policy.owner and policy.unknown_behavior


def test_fields_without_a_source_are_exactly_the_declared_unsourced_ones():
    """Lista nu se ține în două locuri: `source=None` ⇔ `STRUCTURALLY_UNSOURCED`. Dacă apare un
    adaptor de livrare, un singur loc se schimbă și testul cere celălalt."""
    without_source = {name for name, p in FIELD_POLICY.items() if p.source is None}
    assert without_source == set(STRUCTURALLY_UNSOURCED)


def test_unsourced_fields_are_always_unknown_in_a_real_bundle():
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )
    for name in STRUCTURALLY_UNSOURCED:
        fact = bundle.products[0].fact(name)
        assert fact.status == "unknown" and fact.reason == "no_source", name


def test_cta_blocking_fields_match_the_sellability_rule():
    """Politica spune ce blochează un CTA; `sellable` o implementează. Testul le confruntă, ca
    matricea să nu devină documentație care descrie un cod care s-a schimbat."""
    blocking = {name for name, p in FIELD_POLICY.items() if p.blocks_commerce_cta}
    assert blocking == {"identity", "title", "currency", "price", "availability"}
    for name in ("price", "availability"):
        bundle = build_evidence_bundle(
            business_id=BUSINESS_ID,
            locale="ro",
            rows=[row(**{name: None})],
            now=NOW,
            sla_s=SLA,
        )
        assert bundle.products[0].sellable is not None, name


def test_sla_applies_only_to_facts_that_can_go_bad():
    """Un nume de produs nu „expiră"; un preț și un stoc da. A aplica SLA peste tot ar ascunde
    carduri întregi pentru o prospețime care nu spune nimic despre ele."""
    with_sla = {name for name, p in FIELD_POLICY.items() if p.sla_applies}
    assert with_sla == {
        "price",
        "list_price",
        "availability",
        "stock",
        "delivery_promise",
        "promotion",
        "voucher",
    }
    stale = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=NOW - timedelta(days=5))],
        now=NOW,
        sla_s=SLA,
    ).products[0]
    assert stale.fact("price").status == "stale"
    assert stale.fact("title").status == "known"  # numele nu se strică cu vremea


def test_every_formatter_named_by_the_matrix_exists():
    import src.web.localization as localization

    for policy in FIELD_POLICY.values():
        if policy.formatter != "-":
            assert hasattr(localization, policy.formatter), policy.formatter


# ── verified_at ≠ updated_at ────────────────────────────────────────────────────────────────
def test_updated_at_is_not_accepted_as_verification():
    """Semantica cerută de card, punctul 3 din sub-planul de data-readiness. `updated_at` spune
    când s-a scris rândul; poate fi o corectură de descriere. Nu e o verificare de preț — iar
    consecința e că faptul NU poate deveni „stale", nu că devine invizibil."""
    touched_only = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=None, updated_at=NOW)],
        now=NOW,
        sla_s=SLA,
    ).products[0]
    assert touched_only.fact("price").usable is True  # se poate AFIȘA (e prețul din catalog)
    assert touched_only.fact("price").verified is False  # dar nu avem dovada verificării
    assert touched_only.fact("price").status == "known"  # și nici dreptul de a-l numi expirat


def test_expired_verified_facts_block_what_unverified_ones_do_not():
    """Simetria care contează: o verificare EXPIRATĂ e o informație („știm că e vechi") și scoate
    prețul + butonul; absența verificării nu e o informație, deci nu scoate nimic."""
    stale = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=NOW - timedelta(days=5))],
        now=NOW,
        sla_s=SLA,
    ).products[0]
    unverified = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row(synced_at=None)], now=NOW, sla_s=SLA
    ).products[0]
    assert stale.sellable is not None
    assert unverified.sellable is None


def test_a_tenant_without_a_sync_pipeline_still_gets_an_honest_answer():
    answer = ground_answer(
        plan(),
        build_evidence_bundle(
            business_id=BUSINESS_ID, locale="ro", rows=[row(synced_at=None)], now=NOW, sla_s=SLA
        ),
        locale="ro",
        commerce_enabled=True,
    )
    assert answer.ok
    # Butonul apare, dar mutația rămâne păzită de CartService — garanția nu e pe buton.
    assert answer.products[0].commerce_allowed is True


# ── Buget de query-uri / N+1 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("count", [1, 6, 10])
def test_bundle_construction_performs_no_database_work(count, monkeypatch):
    """Anti-N+1 prin construcție: builderul primește rânduri deja hidratate. Dacă vreodată ar
    începe să citească „doar câmpul ăsta", testul cade la primul produs, nu la al zecelea."""

    def boom(*args, **kwargs):  # pragma: no cover — chemarea lui E eșecul
        raise AssertionError("builderul de evidence a atins baza de date")

    monkeypatch.setattr("src.db.connection.get_pool", boom, raising=False)
    monkeypatch.setattr("src.db.connection.tenant_conn", boom, raising=False)
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(f"p{i}") for i in range(count)],
        now=NOW,
        sla_s=SLA,
        query_count=1,
    )
    assert bundle.query_count == 1  # UN retrieval, indiferent de câte produse a întors
    assert len(bundle.products) == min(count, 6)


@pytest.mark.parametrize("count", [1, 6, 10])
def test_projection_cost_does_not_grow_with_a_query_per_product(count, monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("projectorul a atins baza de date")

    monkeypatch.setattr("src.db.connection.get_pool", boom, raising=False)
    from src.agent.answer_plan import SelectedProduct

    rows = [row(f"p{i}") for i in range(count)]
    selected = tuple(
        SelectedProduct(product_id=f"p{i}", variant_id=None, evidence_ids=("e",))
        for i in range(min(count, 6))
    )
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=rows, now=NOW, sla_s=SLA
    )
    answer = ground_answer(
        plan(selected_products=selected, recommendations=()), bundle, locale="ro"
    )
    view = project(answer, identity=identity(), locale="ro", now=NOW)
    assert len(view.messages[0].blocks[-1].items) == min(count, 6)


def test_comparison_does_not_hydrate_anything_extra(monkeypatch):
    from src.agent.answer_plan import ComparisonCell, PlanComparison, SelectedProduct

    def boom(*args, **kwargs):  # pragma: no cover
        raise AssertionError("comparația a atins baza de date")

    monkeypatch.setattr("src.db.connection.get_pool", boom, raising=False)
    rows = [row("p0"), row("p1", name="Al doilea")]
    p = plan(
        selected_products=tuple(
            SelectedProduct(product_id=pid, variant_id=None, evidence_ids=("e",))
            for pid in ("p0", "p1")
        ),
        recommendations=(),
        comparison=PlanComparison(
            product_ids=("p0", "p1"),
            axes=("volum",),
            cells=(ComparisonCell(product_id="p0", axis="volum", value="50 ml", evidence_id="e"),),
        ),
    )
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=rows, now=NOW, sla_s=SLA
    )
    answer = ground_answer(p, bundle, locale="ro")
    assert answer.comparison is not None
    project(answer, identity=identity(), locale="ro", now=NOW)


# ── Cursa de prospețime ─────────────────────────────────────────────────────────────────────
def test_facts_frozen_at_accept_are_the_facts_projected_at_read():
    """Cursa accept → execute → project: între cele trei momente catalogul se poate schimba.
    Faptele sunt înghețate o dată, deci proiecția nu are cum să amestece un preț nou cu un text
    vechi — cazul în care un ViewModel devine coerent-dar-fals."""
    frozen = build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row()], now=NOW, sla_s=SLA
    )
    answer = ground_answer(plan(), frozen, locale="ro")
    first = project(answer, identity=identity(), locale="ro", now=NOW).model_dump(mode="json")
    # Catalogul se schimbă; bundle-ul înghețat nu-l vede.
    build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=[row(price=1.0)], now=NOW, sla_s=SLA
    )
    second = project(answer, identity=identity(), locale="ro", now=NOW).model_dump(mode="json")
    assert first == second
