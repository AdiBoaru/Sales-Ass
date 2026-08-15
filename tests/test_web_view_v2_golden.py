"""NX-240 — golden snapshots ale envelope-ului `web-view.v2`.

Un golden nu testează o funcție, testează o DECIZIE: exact acești bytes ajung în browser. Ce
apără e regresia tăcută — o virgulă mutată în formatter, un câmp care începe brusc să apară, un
bloc reordonat. Toate trec de teste unitare și niciuna nu trece neobservată aici.

Scenariile acoperă exact cazurile pe care cardul le cere să nu mintă: date complete, date
absente, comparație cu gaură, no-results onest, clarificare, coș, terminal eșuat, alt locale.

Regenerare deliberată (după o schimbare INTENȚIONATĂ de contract):
    NX240_UPDATE_GOLDEN=1 python -m pytest tests/test_web_view_v2_golden.py
Diff-ul rezultat e ce se citește în review — nu „testele au fost actualizate".
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agent.answer_plan import (
    ComparisonCell,
    PlanClarification,
    PlanComparison,
    PlanNoResults,
    SelectedProduct,
)
from src.agent.evidence_bundle import build_evidence_bundle
from src.agent.grounding_guard import ground_answer
from src.channels.web.render_v2 import project
from src.web.contracts_v2 import parse_view
from tests.nx240_helpers import BUSINESS_ID, NOW, PID_A, PID_B, FakeIssued, identity, plan, row

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "web_v2_golden"
SLA = 86_400


def _bundle(rows, **kwargs):
    return build_evidence_bundle(
        business_id=BUSINESS_ID, locale="ro", rows=rows, now=NOW, sla_s=SLA, **kwargs
    )


def _cart_snapshot():
    from src.commerce.cart_models import build_snapshot
    from src.commerce.facts_provider import build_facts

    facts = build_facts([dict(row())], [(PID_A, None)], now=NOW, sla_s=SLA)
    return build_snapshot(
        cart_id="cart-nx240",
        version=4,
        status="active",
        items=[{"product_id": PID_A, "variant_id": None, "quantity": 2}],
        facts=facts.facts,
    )


def _full():
    return (
        plan(),
        _bundle([row()]),
        {"commerce_enabled": True},
        (FakeIssued("cart_add_line", "act-cart", "TOKEN-CART", product_ref=PID_A),),
        "ro",
        identity(),
    )


def _unknown_sources():
    stripped = row(
        price=None,
        currency=None,
        list_price=None,
        rating=None,
        review_count=0,
        availability=None,
        stock=None,
        image=None,
        url=None,
        brand=None,
        synced_at=None,
    )
    return plan(), _bundle([stripped]), {"commerce_enabled": True}, (), "ro", identity()


def _comparison():
    p = plan(
        direct_answer="Diferența principală e volumul.",
        selected_products=(
            SelectedProduct(product_id=PID_A, variant_id=None, evidence_ids=("e",)),
            SelectedProduct(product_id=PID_B, variant_id=None, evidence_ids=("e",)),
        ),
        recommendations=(),
        comparison=PlanComparison(
            product_ids=(PID_A, PID_B),
            axes=("volum", "textură"),
            cells=(
                ComparisonCell(product_id=PID_A, axis="volum", value="50 ml", evidence_id="e"),
                ComparisonCell(product_id=PID_B, axis="volum", value="30 ml", evidence_id="e"),
                ComparisonCell(product_id=PID_A, axis="textură", value="gel", evidence_id="e"),
            ),
        ),
    )
    rows = [row(), row(PID_B, name="Ser intensiv NoctuRA", price=139.0, list_price=None, stock=12)]
    return p, _bundle(rows), {}, (), "ro", identity()


def _no_results():
    p = plan(
        direct_answer="",
        selected_products=(),
        recommendations=(),
        no_results=PlanNoResults(
            reason_class="no_match", criteria=("budget_max", "brand"), alternatives=()
        ),
    )
    return p, _bundle([]), {}, (), "ro", identity()


def _clarification():
    p = plan(
        direct_answer="Am două direcții, în funcție de când îl folosești.",
        clarification=PlanClarification(
            question="Îl vrei pentru dimineață sau pentru seară?",
            target_need="usage",
            reason="information_gain",
            options=("pentru dimineață", "pentru seară"),
        ),
    )
    actions = (
        FakeIssued("answer_clarification", "act-0", "TOKEN-0", option_ref=0),
        FakeIssued("answer_clarification", "act-1", "TOKEN-1", option_ref=1),
    )
    return p, _bundle([row()]), {}, actions, "ro", identity()


def _cart():
    return (
        plan(direct_answer="Gata, l-am pus în coș."),
        _bundle([row()], cart=_cart_snapshot()),
        {"commerce_enabled": True},
        (FakeIssued("checkout", "act-checkout", "TOKEN-CHECKOUT"),),
        "ro",
        identity(),
    )


def _english():
    return plan(locale="en"), _bundle([row()]), {}, (), "en", identity()


def _failed():
    return (
        plan(),
        _bundle([row()]),
        {},
        (),
        "ro",
        identity(status="failed", error_code="deadline_exceeded"),
    )


SCENARIOS = {
    "recommendation_full": _full,
    "recommendation_unknown_sources": _unknown_sources,
    "comparison_partial": _comparison,
    "no_results_honest": _no_results,
    "clarification_options": _clarification,
    "cart_summary": _cart,
    "locale_en": _english,
    "terminal_failed": _failed,
}


def _render(name: str) -> dict:
    p, bundle, kwargs, actions, locale, ident = SCENARIOS[name]()
    answer = ground_answer(p, bundle, locale=locale, **kwargs)
    assert answer.ok, (name, answer.failures)
    view = project(answer, identity=ident, locale=locale, issued_actions=actions, now=NOW)
    return view.model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_view_matches_golden(name):
    rendered = _render(name)
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("NX240_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert rendered == expected


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_still_parses_as_the_contract(name):
    """Un golden e o promisiune făcută frontendului: dacă nu mai trece `parse_view`, promisiunea
    e ruptă indiferent cât de „corect" arată diff-ul."""
    parse_view(json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")))


def test_every_block_type_the_projector_can_emit_appears_in_some_golden():
    """Acoperire, nu speranță: dacă projectorul capătă un bloc nou fără scenariu, testul cade și
    cere fixture-ul odată cu funcționalitatea."""
    emitted = set()
    for name in SCENARIOS:
        view = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
        for message in view.get("messages", []):
            emitted.update(block["type"] for block in message["blocks"])
    assert emitted == {
        "text",
        "notice",
        "comparison",
        "product_list",
        "cart_summary",
        "action_row",
    }


def _numeric_paths(node, path="$"):
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _numeric_paths(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _numeric_paths(v, f"{path}[{i}]")]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return [path]
    return []


def test_no_golden_carries_a_number_the_browser_could_compute_with():
    """Invariantul central al contractului v2, verificat pe payload-ul REAL: singurul număr
    permis e `conversation.revision` (o versiune, nu o valoare comercială). Orice altceva ar fi
    o invitație la aritmetică în browser — exact bug-ul din v1."""
    for name in SCENARIOS:
        payload = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert _numeric_paths(payload) == ["$.conversation.revision"], name


def test_replay_is_byte_identical_across_projections():
    """Aceleași fapte înghețate + aceleași acțiuni ⇒ aceiași bytes. Ăsta e replay-ul exact pe care
    îl cere cardul: un GET repetat nu poate produce alt răspuns."""
    for name in SCENARIOS:
        assert _render(name) == _render(name), name


def test_changing_the_catalog_after_the_fact_cannot_change_a_frozen_answer():
    """Faptele sunt în bundle, nu în catalog. Un preț schimbat după commit nu are pe unde să
    intre — nu pentru că memorăm un ecran, ci pentru că memorăm faptele."""
    before = _render("recommendation_full")
    frozen = _bundle([row()])
    _ = _bundle([row(price=1.0, name="ALT NUME")])  # catalogul s-a schimbat între timp
    answer = ground_answer(plan(), frozen, locale="ro", commerce_enabled=True)
    after = project(
        answer,
        identity=identity(),
        locale="ro",
        issued_actions=(FakeIssued("cart_add_line", "act-cart", "TOKEN-CART", product_ref=PID_A),),
        now=datetime(2027, 1, 1, tzinfo=UTC),  # și ceasul a mers mai departe
    ).model_dump(mode="json", exclude_none=True)
    assert after == before


def test_unknown_sources_scenario_shows_a_card_without_inventing_anything():
    view = json.loads((GOLDEN_DIR / "recommendation_unknown_sources.json").read_text("utf-8"))
    item = view["messages"][0]["blocks"][1]["items"][0]
    assert set(item) == {"view_id", "title", "reason", "badges", "actions"}
    assert item["actions"] == []


# ── Grounding verificat de evaluator, pe payload-ul real ────────────────────────────────────
def test_every_golden_passes_the_v2_grounding_evaluator():
    """Bucla se închide: projectorul produce, evaluatorul verifică — pe ACELEAȘI bytes pe care
    le-ar primi browserul, cu urma spre catalog dată de `view_index` (NX-240 e owner-ul mapării,
    fiindcă e singurul loc unde adevărul mai e la îndemână)."""
    from src.channels.web.render_v2 import view_index
    from src.evals.web_response import validate_web_view_v2

    for name in SCENARIOS:
        p, bundle, kwargs, actions, locale, ident = SCENARIOS[name]()
        answer = ground_answer(p, bundle, locale=locale, **kwargs)
        rendered = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
        sources = [
            {
                "id": product.product_id,
                "price": float(product.fact("price").value)
                if product.fact("price").usable
                else None,
                "list_price": float(product.fact("list_price").value)
                if product.fact("list_price").usable
                else None,
                "url": product.fact("url").value if product.fact("url").usable else None,
            }
            for product in bundle.products
        ]
        check = validate_web_view_v2(
            rendered,
            source_products=sources,
            view_index=view_index(answer, turn_id=ident.turn_id),
        )
        assert check.passed, (name, check.failures)


def test_the_evaluator_would_catch_an_overstated_discount():
    """Verificare NEGATIVĂ: dacă evaluatorul trece orice, nu verifică nimic. Umflăm procentul cu
    un punct și cerem să pice."""
    from src.evals.web_response import validate_web_view_v2

    rendered = json.loads((GOLDEN_DIR / "recommendation_full.json").read_text(encoding="utf-8"))
    item = rendered["messages"][0]["blocks"][1]["items"][0]
    assert item["price"]["discount"] == "-25%"
    item["price"]["discount"] = "-26%"
    check = validate_web_view_v2(
        rendered, source_products=[{"id": PID_A, "price": 89.0, "list_price": 120.0}]
    )
    assert not check.passed
    assert any("peste cel real" in f for f in check.failures)
