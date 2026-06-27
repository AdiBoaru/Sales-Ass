"""IZI — badge de card DERIVAT din semnale reale (nu inventat). `derive_badge` (pur) + integrarea
în `compose.assemble` (badge pre-seedat curat are prioritate; kill-switch oprește derivarea)."""

from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    InboundMessage,
    TurnContext,
)
from src.worker.badges import derive_badge
from src.worker.compose import assemble


def _prod(**kw):
    base = {"id": "p1", "name": "A", "price": 50.0, "availability": "in_stock", "top_pros": ["bun"]}
    base.update(kw)
    return base


# --- derive_badge: pur, determinist -----------------------------------------


def test_deal_badge_on_real_discount():
    assert derive_badge(_prod(price=60.0, list_price=80.0), "ro") == "Super Preț"  # 25% ≥ 20


def test_no_deal_below_threshold_falls_to_top():
    # 10% reducere < prag deal; dar rating+recenzii califică „Top Favorit"
    p = _prod(price=72.0, list_price=80.0, rating=4.8, review_count=120)
    assert derive_badge(p, "ro") == "Top Favorit"


def test_top_badge_needs_rating_and_reviews():
    assert derive_badge(_prod(rating=4.8, review_count=120), "ro") == "Top Favorit"
    assert derive_badge(_prod(rating=4.8, review_count=10), "ro") is None  # recenzii < 50
    assert derive_badge(_prod(rating=4.4, review_count=500), "ro") is None  # rating < 4.7


def test_no_badge_when_unremarkable():
    assert derive_badge(_prod(rating=4.5, review_count=30), "ro") is None


def test_deal_beats_top_priority():
    p = _prod(price=60.0, list_price=80.0, rating=4.9, review_count=300)  # califică ambele
    assert derive_badge(p, "ro") == "Super Preț"  # deal câștigă (semnal de conversie)


def test_locale_labels():
    assert derive_badge(_prod(price=60.0, list_price=80.0), "en") == "Great Deal"
    assert derive_badge(_prod(rating=4.8, review_count=120), "hu") == "Top kedvenc"


def test_custom_rules_override_thresholds():
    p = _prod(rating=4.8, review_count=120)
    assert derive_badge(p, "ro", {"top_rating": 4.9}) is None  # pragul ridicat → nu mai califică


def test_malformed_fields_no_crash():
    assert derive_badge({"id": "x", "rating": "n/a", "review_count": None}, "ro") is None


# --- integrare în compose.assemble ------------------------------------------


def _ctx():
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="crema"),
        conversation_id="conv",
    )
    ctx.language = "ro"
    return ctx


def _j():  # JSON minimal de la model: un produs cu fit_clause
    return {
        "items": [{"product_id": "p1", "pro_index": 0, "fit_clause": "potrivită"}],
        "pick": None,
        "education": None,
        "suggestions": [],
    }


def test_assemble_applies_derived_badge():
    retrieved = [_prod(rating=4.8, review_count=120, url="u", image="i")]
    rich = assemble(_ctx(), _j(), retrieved)
    assert rich.items[0].badge == "Top Favorit"  # derivat din rating + recenzii


def test_assemble_seeded_clean_badge_wins():
    # un badge pre-seedat CURAT (fără cifre/%) are prioritate peste derivare.
    retrieved = [_prod(rating=4.8, review_count=120, badge="Recomandat", url="u")]
    rich = assemble(_ctx(), _j(), retrieved)
    assert rich.items[0].badge == "Recomandat"


def test_assemble_killswitch_off_no_derivation(monkeypatch):
    monkeypatch.setattr(get_settings(), "card_badges_enabled", False)
    retrieved = [_prod(rating=4.8, review_count=120, url="u")]
    rich = assemble(_ctx(), _j(), retrieved)
    assert rich.items[0].badge is None  # derivare oprită → fără badge (comportament vechi)
