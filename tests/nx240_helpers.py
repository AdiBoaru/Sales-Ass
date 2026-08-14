"""Constructori partajați pentru testele NX-240 (grounding + projector `web-view.v2`).

Nu e un modul de test: e vocabularul lor. Un rând „complet" înseamnă aici EXACT ce cere
contractul ca să afișeze tot (preț + monedă + `synced_at` + stoc + recenzii), ca fiecare test de
degradare să poată scoate un singur câmp și să arate consecința lui, izolat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.agent.answer_plan import (
    AnswerPlanV2,
    PlanFacts,
    PlanObligation,
    PlanRecommendation,
    SelectedProduct,
    StyleSignals,
)
from src.channels.web.render_v2 import TurnIdentity

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
VERIFIED_AT = datetime(2026, 8, 14, 11, 30, 0, tzinfo=UTC)  # 30 min vechime → fresh la SLA 1 zi
BUSINESS_ID = "6098812a-50fc-44bd-a1ba-bc77e6399158"
CLIENT_TURN_ID = "11111111-1111-4111-8111-111111111111"
PID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
PID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def row(product_id: str = PID_A, **overrides: Any) -> dict[str, Any]:
    """Un rând de catalog COMPLET (toate sursele prezente). Testele scot câmpuri cu
    `row(price=None)` — `None` explicit înseamnă „sursa lipsește", nu „valoare implicită"."""
    base: dict[str, Any] = {
        "id": product_id,
        "business_id": BUSINESS_ID,
        "name": "Ser hidratant LumaDerm",
        "brand": "LumaDerm",
        "price": 89.0,
        "list_price": 120.0,
        "currency": "RON",
        "url": "https://shop.example/p/ser-lumaderm",
        "image": "https://cdn.example/ser.jpg",
        "availability": "in_stock",
        "stock": 3,
        "rating": 4.75,
        "review_count": 120,
        "review_summary": "Clienții laudă hidratarea de lungă durată.",
        "synced_at": VERIFIED_AT,
        "variants": [],
    }
    base.update(overrides)
    return base


def plan(**overrides: Any) -> AnswerPlanV2:
    """Un `AnswerPlanV2` valid, minim, cu o recomandare motivată pe evidence."""
    base: dict[str, Any] = {
        "schema_version": 2,
        "business_id": BUSINESS_ID,
        "locale": "ro",
        "intent_summary": "recomandare ser ten uscat",
        "obligations": (PlanObligation(kind="recommend", key="recommend_0"),),
        "direct_answer": "Pentru ten uscat merge serul LumaDerm.",
        "selected_products": (
            SelectedProduct(
                product_id=PID_A, variant_id=None, evidence_ids=(f"product:{PID_A}:identity",)
            ),
        ),
        "claims": (),
        "facts": PlanFacts(prices=(), stocks=(), urls=()),
        "recommendations": (
            PlanRecommendation(
                product_id=PID_A,
                variant_id=None,
                reason="are acid hialuronic, exact pentru ten uscat",
                evidence_ids=(f"product:{PID_A}:identity",),
                need_ids=("concerns",),
            ),
        ),
        "comparison": None,
        "constraints_applied": (),
        "unknowns": (),
        "relaxations": (),
        "clarification": None,
        "no_results": None,
        "state_update_proposals": (),
        "action_intents": (),
        "disclosures": (),
        "handoff": False,
        "confirmed_actions": (),
        "style_signals": StyleSignals(tone="neutral", verbosity="short"),
    }
    base.update(overrides)
    return AnswerPlanV2(**base)


def identity(**overrides: Any) -> TurnIdentity:
    base: dict[str, Any] = {
        "turn_id": "t-nx240",
        "client_turn_id": CLIENT_TURN_ID,
        "conversation_id": "conv-nx240",
        "conversation_revision": 3,
        "status": "completed",
    }
    base.update(overrides)
    return TurnIdentity(**base)


class FakeArgs:
    """Argumentele unei acțiuni emise, cât îi trebuie projectorului (ref + ordinal)."""

    def __init__(self, product_ref: str | None = None, option_ref: int | None = None) -> None:
        self.product_ref = product_ref
        self.option_ref = option_ref


class FakePlan:
    def __init__(self, kind: str, args: FakeArgs | None = None) -> None:
        self.kind = kind
        self.args = args or FakeArgs()


class FakeIssued:
    """`IssuedAction` fals: projectorul nu semnează nimic, doar transportă tokenul opac."""

    def __init__(self, kind: str, action_id: str, token: str, **args: Any) -> None:
        self.plan = FakePlan(kind, FakeArgs(**args))
        self.action_id = action_id
        self.token = token
        self.expires_at = 0
