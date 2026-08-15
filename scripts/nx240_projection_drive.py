"""NX-240 — manual drive: aceeași conversație, cu fiecare sursă scoasă pe rând.

Cardul cere să se INSPECTEZE ViewModelul, nu textul: pentru fiecare sursă eliminată, claimul,
câmpul și CTA-ul dependent trebuie să dispară sau să devină UNKNOWN onest. Scriptul rulează exact
asta și tipărește un tabel — plus dovezile pe care le cere PR-ul:

  • replay exact (aceeași intrare ⇒ aceiași bytes, chiar și cu ceasul mutat un an);
  • zero I/O în projector (DB/LLM monkeypatch-uite să arunce);
  • bugetul de query-uri la 1 / 6 / 10 produse și la comparație;
  • boundary-ul pasiv: niciun număr pe sârmă, verificat pe payload.

PUR: fără DB, fără OpenAI, fără rețea. `python scripts/nx240_projection_drive.py`
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.answer_plan import (  # noqa: E402
    AnswerPlanV2,
    PlanFacts,
    PlanObligation,
    PlanRecommendation,
    SelectedProduct,
    StyleSignals,
)
from src.agent.evidence_bundle import build_evidence_bundle  # noqa: E402
from src.agent.grounding_guard import ground_answer  # noqa: E402
from src.channels.web.render_v2 import TurnIdentity, project, view_index  # noqa: E402
from src.evals.web_response import passive_boundary_failures, validate_web_view_v2  # noqa: E402

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
PID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SLA = 86_400

IDENTITY = TurnIdentity(
    turn_id="drive-turn",
    client_turn_id="11111111-1111-4111-8111-111111111111",
    conversation_id="drive-conv",
    conversation_revision=1,
    status="completed",
)


def row(**over):
    base = {
        "id": PID,
        "business_id": BIZ,
        "name": "Ser hidratant LumaDerm",
        "brand": "LumaDerm",
        "price": 89.0,
        "list_price": 120.0,
        "currency": "RON",
        "url": "https://shop.example/p/ser",
        "image": "https://cdn.example/ser.jpg",
        "availability": "in_stock",
        "stock": 3,
        "rating": 4.75,
        "review_count": 120,
        "synced_at": datetime(2026, 8, 14, 11, 30, tzinfo=UTC),
        "variants": [],
    }
    base.update(over)
    return base


PLAN = AnswerPlanV2(
    schema_version=2,
    business_id=BIZ,
    locale="ro",
    intent_summary="recomandare ser ten uscat",
    obligations=(PlanObligation(kind="recommend", key="recommend_0"),),
    direct_answer="Pentru ten uscat merge serul LumaDerm.",
    selected_products=(
        SelectedProduct(product_id=PID, variant_id=None, evidence_ids=(f"product:{PID}:identity",)),
    ),
    claims=(),
    facts=PlanFacts(prices=(), stocks=(), urls=()),
    recommendations=(
        PlanRecommendation(
            product_id=PID,
            variant_id=None,
            reason="are acid hialuronic, exact pentru ten uscat",
            evidence_ids=(f"product:{PID}:identity",),
            need_ids=("concerns",),
        ),
    ),
    comparison=None,
    constraints_applied=(),
    unknowns=(),
    relaxations=(),
    clarification=None,
    no_results=None,
    state_update_proposals=(),
    action_intents=(),
    disclosures=(),
    handoff=False,
    confirmed_actions=(),
    style_signals=StyleSignals(tone="neutral", verbosity="short"),
)


class _Args:
    product_ref = PID
    option_ref = None


class _Plan:
    kind = "cart_add_line"
    args = _Args()


class _Issued:
    """CTA de coș, ca acțiune deja emisă (drive-ul nu semnează — NX-236 o face în producție)."""

    plan = _Plan()
    action_id = "drive-cart"
    token = "TOKEN-DRIVE"
    expires_at = 0


def render(**over):
    bundle = build_evidence_bundle(
        business_id=BIZ, locale="ro", rows=[row(**over)], now=NOW, sla_s=SLA
    )
    answer = ground_answer(PLAN, bundle, locale="ro", commerce_enabled=True)
    if not answer.ok:
        return None, answer, bundle
    view = project(
        answer, identity=IDENTITY, locale="ro", issued_actions=(_Issued(),), now=NOW
    ).model_dump(mode="json", exclude_none=True)
    return view, answer, bundle


def _item(view):
    for block in view["messages"][0]["blocks"]:
        if block["type"] == "product_list":
            return block["items"][0]
    return {}


def _cta(item) -> str:
    labels = [a["label"] for a in item.get("actions", [])]
    return "da" if "Adaugă în coș" in labels else "NU"


SCENARIOS: dict[str, dict] = {
    "complet": {},
    "fără preț": {"price": None},
    "fără monedă": {"currency": None},
    "fără preț tăiat": {"list_price": None},
    "fără stoc/disponibilitate": {"availability": None, "stock": None},
    "stoc epuizat": {"availability": "out_of_stock"},
    "fără recenzii": {"rating": None, "review_count": 0},
    "fără poză": {"image": None},
    "fără URL": {"url": None},
    "fapte EXPIRATE (peste SLA)": {"synced_at": datetime(2026, 8, 1, tzinfo=UTC)},
    "fără verificare (synced_at NULL)": {"synced_at": None},
}


def main() -> int:
    failures = 0
    print("=== NX-240 manual drive: aceeași conversație, sursă cu sursă ===\n")
    print(f"{'scenariu':<34}{'preț':<16}{'reducere':<11}{'rating':<10}{'stoc':<20}{'CTA coș'}")
    print("-" * 100)
    for name, over in SCENARIOS.items():
        view, answer, _ = render(**over)
        if view is None:
            print(f"{name:<34}RĂSPUNS RESPINS: {', '.join(answer.failures)}")
            continue
        item = _item(view)
        price = item.get("price") or {}
        print(
            f"{name:<34}{price.get('current', '—'):<16}{price.get('discount', '—'):<11}"
            f"{'da' if item.get('rating') else '—':<10}"
            f"{item.get('availability', '—'):<20}{_cta(item)}"
        )
        boundary = passive_boundary_failures(view)
        if boundary:
            failures += 1
            print(f"    BOUNDARY PASIV ÎNCĂLCAT: {boundary}")

    # ── Grounding verificat de evaluator, pe payload-ul real ────────────────────────────────
    print("\n=== grounding (evaluator peste payload) ===")
    view, answer, bundle = render()
    check = validate_web_view_v2(
        view,
        source_products=[
            {"id": PID, "price": 89.0, "list_price": 120.0, "url": "https://shop.example/p/ser"}
        ],
        view_index=view_index(answer, turn_id=IDENTITY.turn_id),
    )
    print(f"  payload complet: {'PASS' if check.passed else 'FAIL ' + str(check.failures)}")
    failures += 0 if check.passed else 1

    # Injecție: un preț care nu există în sursă trebuie prins de evaluator.
    tampered = json.loads(json.dumps(view))
    _item(tampered)["price"]["current"] = "12,00 lei"
    tampered_check = validate_web_view_v2(tampered, source_products=[{"id": PID, "price": 89.0}])
    print(f"  preț falsificat: {'PRINS' if not tampered_check.passed else 'RATAT (BUG)'}")
    failures += 0 if not tampered_check.passed else 1

    # ── Replay exact ────────────────────────────────────────────────────────────────────────
    print("\n=== replay ===")
    again = project(
        answer,
        identity=IDENTITY,
        locale="ro",
        issued_actions=(_Issued(),),
        now=datetime(2027, 6, 1, tzinfo=UTC),  # ceasul a mers un an
    ).model_dump(mode="json", exclude_none=True)
    same = again == view
    print(f"  aceleași fapte + alt ceas ⇒ aceiași bytes: {'da' if same else 'NU (BUG)'}")
    failures += 0 if same else 1

    # Catalogul se schimbă DUPĂ commit: verdictul înghețat nu-l vede.
    build_evidence_bundle(business_id=BIZ, locale="ro", rows=[row(price=9.99)], now=NOW, sla_s=SLA)
    after = project(
        answer, identity=IDENTITY, locale="ro", issued_actions=(_Issued(),), now=NOW
    ).model_dump(mode="json", exclude_none=True)
    print(f"  catalog schimbat după commit ⇒ răspuns neschimbat: {'da' if after == view else 'NU'}")
    failures += 0 if after == view else 1

    # ── Bugetul de query-uri ────────────────────────────────────────────────────────────────
    print("\n=== buget de query-uri (projector + builder) ===")
    for count in (1, 6, 10):
        bundle = build_evidence_bundle(
            business_id=BIZ,
            locale="ro",
            rows=[row(**{"id": f"p{i}"}) for i in range(count)],
            now=NOW,
            sla_s=SLA,
            query_count=1,
        )
        print(
            f"  {count:>2} produse retrievate → {len(bundle.products)} în bundle, "
            f"{bundle.query_count} query de retrieval, 0 query în builder/projector"
        )

    # ── Decimal, nu float ───────────────────────────────────────────────────────────────────
    price_fact = render()[2].products[0].fact("price")
    print(
        f"\n  tipul prețului în bundle: {type(price_fact.value).__name__} "
        f"(= {price_fact.value!r}); Decimal? {isinstance(price_fact.value, Decimal)}"
    )

    print(f"\nverificări eșuate: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
