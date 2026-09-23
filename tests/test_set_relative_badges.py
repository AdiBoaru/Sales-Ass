"""NX-318 — un badge are valoare doar dacă DEOSEBEȘTE produsele din set.

Turul 4 al conversației reale `f4e1431e` (`sole-ro`, 2026-09-23) a arătat „Top Favorit” pe 6 din 6
carduri. Aici: suprimarea badge-urilor de clasament comune majorității (≥3 carduri, >50%), păstrarea
celor de preț, plus cele două defecte vecine (ratingul shrunk pentru `top`, pragul voucherului din
pachet). Flag `SET_RELATIVE_BADGES_ENABLED`; OFF = comportamentul de dinainte.
"""

from __future__ import annotations

from src.config import get_settings
from src.domain.pack import DomainPack
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.badges import (
    badge_label,
    common_ranking_kinds,
    coupon_discount_pct,
    derive_badge_kind,
)
from src.worker.compose import assemble

# Cardurile reale ale turului 4 (rating, recenzii) — toate calificau „Top Favorit” pe ratingul brut.
TURN4 = [
    ("1c1f", "MIZON Pore Fresh Clear Nose Pack", 4.86, 177),
    ("09ff", "IT'S SKIN The Fresh Blueberries", 4.90, 146),
    ("0d2d", "IT'S SKIN The Fresh Coconut", 4.94, 99),
    ("490b", "VILLAGE 11 FACTORY Active Clean Sheet Mask Lemon", 4.89, 100),
    ("2b2a", "SOME BY MI Real Snail Skin Barrier Care Mask", 4.96, 72),
    ("3880", "SOME BY MI Real Honey Luminous Care Mask", 4.98, 64),
]


def _prod(pid: str, name: str = "Produs", **kw) -> dict:
    base = {
        "id": pid,
        "name": name,
        "price": 10.0,
        "availability": "in_stock",
        "top_pros": ["bun"],
    }
    base.update(kw)
    return base


def _ctx(pack: DomainPack | None = None) -> TurnContext:
    business = BusinessConfig(id="b", slug="d", name="D")
    if pack is not None:
        business.domain_pack = pack
    ctx = TurnContext(
        turn_id="t",
        business=business,
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="ceva mai ieftin"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    return ctx


def _j(ids: list[str]) -> dict:
    return {
        "intro": None,
        "items": [{"product_id": pid, "fit_clause": "potrivită", "pro_index": 0} for pid in ids],
        "pick": None,
        "education": None,
        "suggestions": [],
    }


def _assemble(products: list[dict], ctx: TurnContext | None = None):
    ctx = ctx or _ctx()
    rich = assemble(ctx, _j([p["id"] for p in products]), products)
    return rich, ctx


def _suppressed(ctx: TurnContext) -> list[dict]:
    return [
        {k: v for k, v in e.properties.items() if k != "turn_id"}
        for e in ctx.events
        if e.type == "badges_suppressed"
    ]


def _raw_top(n: int) -> list[dict]:
    """`n` produse care califică „top” și pe ratingul brut, și pe cel shrunk (4,9 × 300 ⇒ 4,82)."""
    return [_prod(f"p{i}", rating=4.9, review_count=300) for i in range(n)]


# --- regula pură ------------------------------------------------------------------------------


def test_common_kind_on_all_cards():
    assert common_ranking_kinds(["top"] * 6) == {"top": 6}


def test_distinguishing_badge_is_kept():
    assert common_ranking_kinds(["top", "top", None, None, None, None]) == {}


def test_exactly_half_is_not_a_majority():
    assert common_ranking_kinds(["top", "top", "top", None, None, None]) == {}


def test_price_badges_are_never_suppressed():
    assert common_ranking_kinds(["deal"] * 4) == {}
    assert common_ranking_kinds(["coupon"] * 4) == {}


def test_under_three_cards_there_is_no_majority():
    assert common_ranking_kinds(["top"]) == {}
    assert common_ranking_kinds(["top", "top"]) == {}
    assert common_ranking_kinds(["top", "top", None]) == {"top": 2}


# --- integrarea în assemble --------------------------------------------------------------------


def test_top_on_every_card_is_removed_from_all():
    rich, ctx = _assemble(_raw_top(6))
    assert [it.badge for it in rich.items] == [None] * 6
    assert [it.badge_tone for it in rich.items] == [None] * 6
    assert _suppressed(ctx) == [{"kind": "top", "on_cards": 6, "total_cards": 6}]


def test_two_top_among_six_stay():
    products = _raw_top(2) + [_prod(f"q{i}", rating=4.2, review_count=10) for i in range(4)]
    rich, ctx = _assemble(products)
    assert [it.badge for it in rich.items[:2]] == ["Top Favorit", "Top Favorit"]
    assert _suppressed(ctx) == []


def test_four_deal_cards_keep_their_badge():
    products = [_prod(f"d{i}", price=60.0, list_price=80.0) for i in range(4)]
    rich, ctx = _assemble(products)
    assert [it.badge for it in rich.items] == ["Super Preț"] * 4
    assert _suppressed(ctx) == []


def test_single_card_keeps_its_badge():
    rich, ctx = _assemble(_raw_top(1))
    assert rich.items[0].badge == "Top Favorit"
    assert _suppressed(ctx) == []


def test_seeded_badge_is_not_touched():
    products = _raw_top(3) + [_prod("s", badge="Recomandat", rating=4.9, review_count=300)]
    rich, _ = _assemble(products)
    by_id = {it.product_id: it.badge for it in rich.items}
    assert by_id["s"] == "Recomandat"
    assert [by_id[f"p{i}"] for i in range(3)] == [None] * 3  # 3 din 4 ⇒ majoritate


def test_turn4_is_no_longer_top_on_six_of_six():
    products = [_prod(pid, name, rating=r, review_count=n) for pid, name, r, n in TURN4]
    rich, _ = _assemble(products)
    badges = [it.badge for it in rich.items]
    assert badges.count("Top Favorit") < 6


def test_flag_off_is_the_old_behavior(monkeypatch):
    monkeypatch.setattr(get_settings(), "set_relative_badges_enabled", False)
    products = [_prod(pid, name, rating=r, review_count=n) for pid, name, r, n in TURN4]
    rich, ctx = _assemble(products)
    assert [it.badge for it in rich.items] == ["Top Favorit"] * 6  # exact ce a văzut clientul
    assert _suppressed(ctx) == []


# --- defectele vecine --------------------------------------------------------------------------


def test_top_uses_shrunk_rating():
    # 5,0 × 55 trece pe brut (≥4,7, ≥50), dar shrunk dă (275 + 120) / 85 ≈ 4,65.
    p = _prod("x", rating=5.0, review_count=55)
    assert derive_badge_kind(p) == "top"
    assert derive_badge_kind(p, set_relative=True) is None
    strong = _prod("y", rating=4.9, review_count=300)
    assert derive_badge_kind(strong, set_relative=True) == "top"


def test_coupon_threshold_comes_from_the_pack_rules():
    p = _prod("c", price=100.0, coupon_price=85.0, coupon_code="WELCOME15")  # -15%
    rules = {"coupon_discount_pct": 10.0}
    assert coupon_discount_pct(p) is None  # default 25
    assert coupon_discount_pct(p, rules) == 15
    assert derive_badge_kind(p, rules) is None  # OFF: pragul pachetului era ignorat
    assert derive_badge_kind(p, rules, set_relative=True) == "coupon"
    assert badge_label("coupon", "ro", p, rules) == "Voucher -15%"


def test_coupon_pack_threshold_reaches_the_card():
    pack = DomainPack(vertical="ecommerce", badge_rules={"coupon_discount_pct": 10.0})
    p = _prod("c", price=100.0, coupon_price=85.0, coupon_code="WELCOME15")
    rich, _ = _assemble([p], _ctx(pack))
    assert rich.items[0].badge == "Voucher -15%"
