"""NX-240 — `GroundingGuard`: ce se respinge, ce se omite, și de ce diferența contează.

Un plan valid (a trecut validatorul NX-211/239) poate fi în continuare NELIVRABIL: referințele lui
sunt corecte, dar VALORILE din proză pot să nu fie. Aici se testează exact granița:

  • **respingere** = răspunsul nu se livrează așa (proză cu cifre nefondate, livrare/promo fără
    sursă, tenant greșit) → fallback determinist;
  • **omisiune** = răspunsul se livrează mai sărac (rating fără recenzii, CTA fără stoc verificat).

Un guard care le-ar confunda ar face una din două greșeli: ori ar bloca răspunsuri bune pentru un
câmp lipsă, ori ar livra un preț inventat pentru că „restul e în regulă".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.agent.answer_plan import (
    ComparisonCell,
    PlanClaim,
    PlanClarification,
    PlanComparison,
    PlanNoResults,
    PlanRecommendation,
    SelectedProduct,
)
from src.agent.evidence_bundle import build_evidence_bundle
from src.agent.grounding_guard import (
    answer_from_jsonb,
    answer_to_jsonb,
    ground_answer,
    omission_counts,
)
from tests.nx240_helpers import BUSINESS_ID, NOW, PID_A, PID_B, plan, row

SLA = 86_400


def bundle(rows=None, **kwargs):
    return build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=rows if rows is not None else [row()],
        now=NOW,
        sla_s=SLA,
        **kwargs,
    )


def ground(p=None, b=None, **kwargs):
    return ground_answer(p or plan(), b or bundle(), locale="ro", **kwargs)


def _omissions(answer) -> set[tuple[str, str]]:
    return {(o.field, o.reason) for o in answer.omissions}


# ── Drumul fericit ──────────────────────────────────────────────────────────────────────────
def test_grounded_plan_passes_with_its_reason_intact():
    answer = ground()
    assert answer.ok and answer.failures == ()
    assert [p.evidence.product_id for p in answer.products] == [PID_A]
    assert answer.products[0].reason.startswith("are acid hialuronic")
    assert answer.as_of == NOW


# ── Respingeri: proza afirmă ce nu există ───────────────────────────────────────────────────
def test_price_in_prose_that_contradicts_evidence_is_rejected():
    answer = ground(plan(direct_answer="Serul LumaDerm costă 49 lei."))
    assert not answer.ok and "ungrounded_prose" in answer.failures


def test_real_price_in_prose_is_accepted():
    answer = ground(plan(direct_answer="Serul LumaDerm costă 89 lei."))
    assert answer.ok, answer.failures


def test_percentage_must_recompute_from_two_prices():
    """`89` din `120` e 25,83% → afișăm „-25%". Un „-30%" e o cifră fără sursă, oricât ar suna
    mai bine; „-26%" e tot nefondat, fiindcă rotunjim în JOS (vezi `format_discount`)."""
    assert ground(plan(direct_answer="Acum are -25% reducere.")).ok
    bad = ground(plan(direct_answer="Acum are -30% reducere."))
    assert not bad.ok and "ungrounded_percentage" in bad.failures


def test_hundred_percent_is_a_composition_phrase_not_a_discount():
    assert ground(plan(direct_answer="Este 100% vegan ca formulare.")).ok


def test_discount_claim_without_a_list_price_is_rejected():
    answer = ground(plan(direct_answer="Are -20% acum."), bundle([row(list_price=None)]))
    assert not answer.ok and "ungrounded_percentage" in answer.failures


def test_delivery_promise_is_rejected_because_no_adapter_exists():
    answer = ground(plan(direct_answer="Livrare în 24 de ore, ajunge mâine."))
    assert not answer.ok and "unsourced_delivery_claim" in answer.failures


def test_voucher_claim_is_rejected_because_no_promotion_engine_exists():
    answer = ground(plan(direct_answer="Poți folosi un cod de reducere la finalizare."))
    assert not answer.ok and "unsourced_promo_claim" in answer.failures


def test_invented_link_in_prose_is_rejected():
    answer = ground(plan(direct_answer="Vezi aici https://alt-magazin.example/x"))
    assert not answer.ok and "ungrounded_prose" in answer.failures


def test_medical_claim_is_rejected_on_this_path_too():
    answer = ground(plan(direct_answer="Tratează acneea și e sigur în sarcină."))
    assert not answer.ok and "ungrounded_prose" in answer.failures


def test_superlative_is_rejected_because_no_fact_can_support_it():
    answer = ground(plan(direct_answer="Este cel mai bun ser din categorie."))
    assert not answer.ok and "unverifiable_superlative" in answer.failures


def test_warranty_claim_is_rejected_because_it_is_a_legal_fact_not_a_catalog_column():
    answer = ground(plan(direct_answer="Are garanție 24 de luni."))
    assert not answer.ok and "unsourced_warranty_claim" in answer.failures


def test_stock_claim_needs_an_availability_fact():
    assert ground(plan(direct_answer="Este în stoc acum.")).ok
    unknown_stock = ground(
        plan(direct_answer="Este în stoc acum."), bundle([row(availability=None)])
    )
    assert "unsupported_stock_claim" in unknown_stock.failures


def test_mentioning_reviews_and_discounts_is_allowed_when_the_facts_exist():
    """NX-117 le interzicea în bloc (nu le putea verifica). Aici sunt verificabile — iar un bot
    care nu poate spune „are 120 de recenzii" despre un produs cu 120 de recenzii e mai prost,
    nu mai sigur."""
    assert ground(plan(direct_answer="Are 120 de recenzii și o reducere de -25%.")).ok


def test_ungrounded_number_inside_a_claim_is_caught_too():
    """Nu doar `direct_answer`: TOT ce se livrează se validează. O cifră falsă într-un claim e
    la fel de falsă."""
    answer = ground(
        plan(
            claims=(
                PlanClaim(
                    claim_type="fact",
                    text="Are 500 ml în flacon.",
                    evidence_ids=(f"product:{PID_A}:identity",),
                    need_ids=(),
                ),
            )
        )
    )
    assert not answer.ok and "ungrounded_prose" in answer.failures


def test_clarification_text_is_validated_only_when_it_will_be_asked():
    """Poarta NX-235 decide DACĂ întrebăm; guardul validează doar ce se livrează. O întrebare
    suprimată nu poate respinge un răspuns bun."""
    p = plan(
        clarification=PlanClarification(
            question="Cauți varianta de 250 lei?",
            target_need="budget_max",
            reason="gain",
            options=("da", "nu"),
        )
    )
    assert not ground(p, ask_clarification=True).ok
    assert ground(p, ask_clarification=False).ok


# ── Tenant / locale ─────────────────────────────────────────────────────────────────────────
def test_tenant_mismatch_stops_everything_immediately():
    answer = ground(plan(business_id="alt-tenant"))
    assert answer.failures == ("tenant_mismatch",)
    assert answer.products == ()  # nimic nu se proiectează din datele altcuiva


def test_locale_mismatch_is_refused():
    assert "locale_mismatch" in ground(plan(locale="en")).failures


# ── Omisiuni: răspunsul se livrează, mai sărac ──────────────────────────────────────────────
def test_hard_mismatch_removes_the_product_entirely():
    answer = ground(
        b=bundle(constraints_by_product={PID_A: [{"facet": "spf", "verdict": "MISMATCH"}]})
    )
    assert answer.products == ()
    assert ("product", "blocked") in _omissions(answer)
    assert "no_renderable_product" in answer.failures


def test_unknown_verdict_keeps_the_product_but_does_not_declare_a_match():
    answer = ground(
        b=bundle(constraints_by_product={PID_A: [{"facet": "spf", "verdict": "UNKNOWN"}]})
    )
    assert answer.ok and answer.products[0].evidence.unknown_facets == ("spf",)


def test_product_missing_from_evidence_is_dropped():
    answer = ground(
        plan(
            selected_products=(
                SelectedProduct(product_id=PID_B, variant_id=None, evidence_ids=("e",)),
            ),
            recommendations=(),
        )
    )
    assert ("product", "not_in_evidence") in _omissions(answer)
    assert "no_renderable_product" in answer.failures


def test_generic_superlative_reason_is_dropped_but_the_product_stays():
    answer = ground(
        plan(
            recommendations=(
                PlanRecommendation(
                    product_id=PID_A,
                    variant_id=None,
                    reason="este cel mai bun produs din categorie",
                    evidence_ids=(f"product:{PID_A}:identity",),
                    need_ids=(),
                ),
            )
        )
    )
    assert answer.ok
    assert answer.products[0].reason is None
    assert ("reason", "generic_reason") in _omissions(answer)


# ── CTA comercial: promisiunea cere verificare ──────────────────────────────────────────────
def test_cta_requires_verified_stock_and_price():
    answer = ground(commerce_enabled=True)
    assert answer.products[0].commerce_allowed is True


def test_cta_survives_a_catalog_without_a_sync_pipeline():
    """Poarta e `usable`, nu `verified`: fără sync nimic nu e verificat, dar nici expirat. Un
    tenant curatat manual primește butoane, iar garanția rămâne la mutație (NX-237)."""
    answer = ground(b=bundle([row(synced_at=None)]), commerce_enabled=True)
    assert answer.products[0].commerce_allowed is True


def test_cta_disappears_when_facts_are_stale():
    stale = bundle([row(synced_at=NOW - timedelta(days=5))])
    answer = ground(b=stale, commerce_enabled=True)
    assert answer.products[0].commerce_allowed is False


def test_cta_disappears_when_out_of_stock():
    answer = ground(b=bundle([row(availability="out_of_stock")]), commerce_enabled=True)
    assert answer.products[0].commerce_allowed is False
    assert ("commerce_cta", "out_of_stock") in _omissions(answer)


def test_cta_disappears_when_price_is_unknown():
    answer = ground(b=bundle([row(price=None)]), commerce_enabled=True)
    assert answer.products[0].commerce_allowed is False
    assert ("commerce_cta", "price_unknown") in _omissions(answer)


def test_cta_disappears_when_the_cart_service_is_off():
    answer = ground(commerce_enabled=False)
    assert answer.products[0].commerce_allowed is False
    assert ("commerce_cta", "commerce_disabled") in _omissions(answer)


# ── Comparație ──────────────────────────────────────────────────────────────────────────────
def _comparison_plan(**overrides):
    return plan(
        selected_products=(
            SelectedProduct(product_id=PID_A, variant_id=None, evidence_ids=("e",)),
            SelectedProduct(product_id=PID_B, variant_id=None, evidence_ids=("e",)),
        ),
        recommendations=(),
        comparison=PlanComparison(
            product_ids=(PID_A, PID_B),
            axes=("volum", "textura"),
            cells=(
                ComparisonCell(product_id=PID_A, axis="volum", value="50 ml", evidence_id="e"),
                ComparisonCell(product_id=PID_B, axis="volum", value="30 ml", evidence_id="e"),
                ComparisonCell(product_id=PID_A, axis="textura", value="gel", evidence_id="e"),
            ),
            **overrides,
        ),
    )


def test_missing_comparison_cell_stays_explicitly_unknown():
    answer = ground(_comparison_plan(), bundle([row(PID_A), row(PID_B)]))
    assert answer.ok
    assert (PID_B, "textura") not in answer.comparison.cells
    assert ("comparison_cell", "unknown") in _omissions(answer)


def test_comparison_drops_when_fewer_than_two_columns_survive():
    """Un tabel cu o coloană nu compară nimic — și ar sugera un câștigător care n-a concurat."""
    answer = ground(
        _comparison_plan(),
        bundle(
            [row(PID_A), row(PID_B)],
            constraints_by_product={PID_B: [{"facet": "spf", "verdict": "MISMATCH"}]},
        ),
    )
    assert answer.comparison is None
    assert ("comparison", "not_in_evidence") in _omissions(answer)


# ── Fără conținut ───────────────────────────────────────────────────────────────────────────
def test_a_plan_with_nothing_to_say_is_refused():
    answer = ground(plan(direct_answer="", recommendations=(), selected_products=()))
    assert not answer.ok and "empty_answer" in answer.failures


def test_no_results_alone_is_content_enough():
    answer = ground(
        plan(
            direct_answer="",
            selected_products=(),
            recommendations=(),
            no_results=PlanNoResults(reason_class="no_match", criteria=(), alternatives=()),
        )
    )
    assert answer.ok and answer.no_results is not None


# ── Serializare ─────────────────────────────────────────────────────────────────────────────
def test_only_surviving_products_are_frozen():
    """Ce n-a trecut poarta nu se persistă — altfel o proiecție mai indulgentă l-ar putea
    „recupera" mai târziu."""
    answer = ground(
        plan(
            selected_products=(
                SelectedProduct(product_id=PID_A, variant_id=None, evidence_ids=("e",)),
                SelectedProduct(product_id=PID_B, variant_id=None, evidence_ids=("e",)),
            ),
            recommendations=(),
        ),
        bundle(
            [row(PID_A), row(PID_B)],
            constraints_by_product={PID_B: [{"facet": "spf", "verdict": "MISMATCH"}]},
        ),
    )
    payload = answer_to_jsonb(answer)
    assert [p["e"]["pid"] for p in payload["products"]] == [PID_A]


def test_roundtrip_preserves_everything_the_projector_reads():
    answer = ground(_comparison_plan(), bundle([row(PID_A), row(PID_B)]), commerce_enabled=True)
    back = answer_from_jsonb(answer_to_jsonb(answer))
    assert back is not None
    assert back.direct_answer == answer.direct_answer
    assert [p.evidence.product_id for p in back.products] == [
        p.evidence.product_id for p in answer.products
    ]
    assert back.comparison.cells == answer.comparison.cells
    assert back.as_of == answer.as_of
    assert [p.commerce_allowed for p in back.products] == [
        p.commerce_allowed for p in answer.products
    ]


@pytest.mark.parametrize("payload", [None, {}, {"schema_version": 99}, "nu e dict", []])
def test_unknown_payload_shapes_degrade_to_none(payload):
    assert answer_from_jsonb(payload) is None


def test_omission_counts_carry_no_product_ids():
    answer = ground(commerce_enabled=False)
    counts = omission_counts(answer)
    assert counts == {"commerce_cta:commerce_disabled": 1}
    assert all(PID_A not in key for key in counts)
