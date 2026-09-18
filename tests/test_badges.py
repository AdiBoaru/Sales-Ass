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
    assert derive_badge(_prod(rating=4.8, review_count=120), "en") == "Top Favorite"


def test_custom_rules_override_thresholds():
    p = _prod(rating=4.8, review_count=120)
    assert derive_badge(p, "ro", {"top_rating": 4.9}) is None  # pragul ridicat → nu mai califică


def test_malformed_fields_no_crash():
    assert derive_badge({"id": "x", "rating": "n/a", "review_count": None}, "ro") is None


# --- Full-eMAG: kind semantic + ton -----------------------------------------


def test_derive_badge_kind_and_tone():
    from src.worker.badges import BADGE_TONE, badge_label, derive_badge_kind

    assert derive_badge_kind(_prod(price=60.0, list_price=80.0)) == "deal"  # 25% reducere
    assert derive_badge_kind(_prod(rating=4.8, review_count=120)) == "top"
    assert derive_badge_kind(_prod(rating=4.5, review_count=30)) is None
    # NX-299: voucherul are același ton ca reducerea (amândouă vorbesc despre preț), `top` rămâne
    # informativ. Tonul e NEUTRU de locale, deci comparăm dicționarul întreg.
    assert BADGE_TONE == {"deal": "danger", "coupon": "danger", "top": "info"}
    assert badge_label("deal", "en") == "Great Deal" and badge_label(None, "ro") is None


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


def test_assemble_sets_badge_tone_and_details():
    # Full-eMAG: badge deal → ton „danger"; `details` din ai_summary (medical-guarded).
    retrieved = [_prod(price=60.0, list_price=80.0, ai_summary="Fluid matifiant cu zinc.", url="u")]
    rich = assemble(_ctx(), _j(), retrieved)
    it = rich.items[0]
    assert it.badge == "Super Preț" and it.badge_tone == "danger"
    assert it.details == "Fluid matifiant cu zinc."


# --- NX-299 felia 4: voucherul ------------------------------------------------


def _coupon(price=100.0, coupon_price=50.0, code="WELCOME15"):
    """Voucher de -50%: peste pragul implicit. Nu 85 (adică -15%) deliberat — aia e chiar valoarea
    promoției de MAGAZIN pe care pragul e menit s-o taie (vezi testul de mai jos)."""
    return {"id": "x", "price": price, "coupon_price": coupon_price, "coupon_code": code}


def test_coupon_badge_carries_the_real_percentage():
    """Cifra se calculează din două coloane reale (`coupon_price` vs `price`), exact ca procentul
    din spatele lui «Super Preț». De-aia are voie să treacă pe lângă `_safe_badge`, care respinge
    etichetele cu cifre venite din catalog: acelea sunt text al furnizorului."""
    from src.worker.badges import badge_label, coupon_discount_pct, derive_badge_kind

    assert derive_badge_kind(_coupon()) == "coupon"
    assert coupon_discount_pct(_coupon()) == 50
    assert badge_label("coupon", "ro", _coupon()) == "Voucher -50%"
    assert badge_label("coupon", "en", _coupon()) == "Coupon -50%"


def test_coupon_percentage_rounds_down():
    """Promisiunea afișată trebuie să rămână adevărată dacă prețul se mișcă puțin. 100 → 49,5 e
    50,5%, dar afișăm 50."""
    from src.worker.badges import coupon_discount_pct

    assert coupon_discount_pct(_coupon(coupon_price=49.5)) == 50


def test_a_shop_wide_welcome_voucher_is_not_a_product_signal():
    """Corecția cea mai importantă a feliei, găsită pe măsurătoare DUPĂ prima livrare.

    Pe catalogul pilot există UN singur cod (`WELCOME15`) pe 2.123 din 2.758 de produse, iar 90,6%
    dintre ele au exact -15%. Cu pragul inițial de 5%, badge-ul apărea pe 76,9% din catalog cu
    aceeași valoare — deci nu informa pe nimeni — și fura semnalul de reputație de la **994 din
    1.363** de produse eligibile de «Top Favorit». Un voucher de bun venit e o promoție de MAGAZIN,
    nu o proprietate a produsului."""
    from src.worker.badges import coupon_discount_pct, derive_badge_kind

    shop_wide = _coupon(coupon_price=85.0)  # -15%, promoția generală
    assert coupon_discount_pct(shop_wide) is None
    assert derive_badge_kind(shop_wide) is None
    # …iar reputația supraviețuiește pe produsele bune, exact ce se pierdea înainte.
    assert derive_badge_kind({**shop_wide, "rating": 4.9, "review_count": 500}) == "top"


def test_coupon_without_a_real_discount_is_not_a_badge():
    """`coupon_without_discount` era deja o anomalie cunoscută la import. Un cupon care nu scade
    prețul e o etichetă care minte, nu un semnal."""
    from src.worker.badges import coupon_discount_pct, derive_badge_kind

    assert coupon_discount_pct(_coupon(coupon_price=100.0)) is None
    assert coupon_discount_pct(_coupon(coupon_price=120.0)) is None
    assert coupon_discount_pct(_coupon(code=None)) is None
    assert coupon_discount_pct(_coupon(coupon_price=None)) is None
    assert coupon_discount_pct(_coupon(coupon_price=98.0)) is None  # sub prag
    assert derive_badge_kind(_coupon(coupon_price=98.0)) is None


def test_price_you_already_have_beats_one_you_must_claim():
    """Ordinea nu e de gust: o reducere deja în preț bate una obținută la finalizare, iar amândouă
    bat o etichetă de reputație."""
    from src.worker.badges import derive_badge_kind

    both = {**_coupon(), "list_price": 200.0}  # 50% deal + 50% cupon
    assert derive_badge_kind(both) == "deal"
    with_top = {**_coupon(), "rating": 4.9, "review_count": 500}
    assert derive_badge_kind(with_top) == "coupon"


def test_coupon_has_its_own_kill_switch():
    """Cuponul e o promisiune despre PREȚUL FINAL, deci trebuie să poată fi stins fără să stingi
    și «Top Favorit». Modulul rămâne PUR: poarta e un parametru, nu o citire de setări."""
    from src.worker.badges import derive_badge_kind

    assert derive_badge_kind(_coupon(), coupon_enabled=False) is None
    still_top = {**_coupon(), "rating": 4.9, "review_count": 500}
    assert derive_badge_kind(still_top, coupon_enabled=False) == "top"


def test_badge_label_is_silent_when_the_number_cannot_be_computed():
    """Un șablon cu `{pct}` fără date de cupon nu are voie să iasă cu un gol în el."""
    from src.worker.badges import badge_label

    assert badge_label("coupon", "ro", {"id": "x", "price": 100.0}) is None
    assert badge_label("coupon", "ro", None) is None
