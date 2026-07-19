"""NX-180 — teste pt gate-urile DETERMINISTE ale evaluatorului (pure, fără apeluri live).

Acoperă exact clasele de defect pe care baseline-ul trebuie să le prindă determinist, ca judge-ul
LLM (subiectiv) să nu fie singura plasă. Judge-ul live NU se testează aici (e non-determinist).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sim"))

import eval_gates  # noqa: E402
import eval_judge  # noqa: E402


def _turn(content="", products=None, suggestions=None, offer=None):
    return {
        "content": content,
        "products": products or [],
        "suggestions": suggestions or [],
        "offer": offer,
    }


def test_empty_reply_flagged():
    assert "empty_reply" in eval_gates.check_turn(_turn(""), None, {})
    assert eval_gates.check_turn(_turn("ceva util"), None, {"not_empty": True}) == []


def test_card_count_bounds():
    prods = [{"product_id": str(i), "name": f"P{i}", "price": 10.0} for i in range(5)]
    fails = eval_gates.check_turn(_turn("x", prods), None, {"min_cards": 1, "max_cards": 4})
    assert any(f.startswith("too_many_cards") for f in fails)
    assert (
        eval_gates.check_turn(_turn("x", prods[:2]), None, {"min_cards": 1, "max_cards": 4}) == []
    )
    assert any(
        f.startswith("too_few_cards")
        for f in eval_gates.check_turn(_turn("x", []), None, {"min_cards": 1})
    )


def test_ungrounded_price_caught_but_client_budget_not():
    prods = [{"product_id": "p1", "name": "Cremă", "price": 89.99}]
    # preț inventat (2 zecimale) care NU e al cardului → prins
    fails = eval_gates.check_turn(_turn("costă doar 49.90 lei", prods), None, {"grounded": True})
    assert any(f.startswith("ungrounded_price") for f in fails)
    # prețul REAL al cardului → OK
    assert eval_gates.check_turn(_turn("este 89.99 lei", prods), None, {"grounded": True}) == []
    # bugetul rotund al clientului („sub 80") NU e token de preț → fără fals-pozitiv
    assert (
        eval_gates.check_turn(_turn("am ales ceva sub 80 lei", prods), None, {"grounded": True})
        == []
    )


def test_ungrounded_link_and_offer_url_allowed():
    prods = [{"product_id": "p1", "name": "Cremă", "price": 20.0, "url": "https://shop.ro/p/crema"}]
    # link INVENTAT în proză (nu e al vreunui card, nici offer) → prins (#234)
    fails = eval_gates.check_turn(
        _turn("cumpără de aici https://evil.example/x", prods), None, {"grounded": True}
    )
    assert "ungrounded_link" in fails
    # URL-ul REAL al cardului → OK (chiar cu punctuație după)
    assert (
        eval_gates.check_turn(
            _turn("uite: https://shop.ro/p/crema.", prods), None, {"grounded": True}
        )
        == []
    )
    # URL-ul offer-ului (checkout) → permis chiar dacă nu e URL de produs
    offer = {"kind": "open_url", "url": "https://shop.ro/checkout?ref=abc"}
    assert (
        eval_gates.check_turn(
            _turn("plătește aici https://shop.ro/checkout?ref=abc", prods, offer=offer),
            None,
            {"grounded": True},
        )
        == []
    )


def test_requires_offer_gate():
    prods = [{"product_id": "p1", "name": "Cremă", "price": 20.0, "url": "https://shop.ro/p/crema"}]
    # cerere de link fără offer ȘI fără URL în text → prins (#234: înainte trecea determinist)
    fails = eval_gates.check_turn(
        _turn("uite linkurile de mai sus", prods), None, {"requires_offer": True}
    )
    assert "missing_offer_link" in fails
    # offer cu URL (checkout) → OK
    offer = {"kind": "open_url", "url": "https://shop.ro/checkout?ref=x"}
    assert (
        eval_gates.check_turn(
            _turn("plătește aici", prods, offer=offer), None, {"requires_offer": True}
        )
        == []
    )
    # URL de PRODUS AFIȘAT scris în text → OK (grounded off aici, izolăm requires_offer)
    assert (
        eval_gates.check_turn(
            _turn("uite: https://shop.ro/p/crema", prods),
            None,
            {"requires_offer": True, "grounded": False},
        )
        == []
    )
    # #234: un URL care NU e al unui produs afișat NU satisface requires_offer (nu orice URL)
    fails2 = eval_gates.check_turn(
        _turn("vezi https://random.example/x", prods),
        None,
        {"requires_offer": True, "grounded": False},
    )
    assert "missing_offer_link" in fails2
    # URL al unui produs afișat ANTERIOR (prev) → OK (produsul „așteptat" al cererii)
    prev = _turn("aici", prods)
    assert (
        eval_gates.check_turn(
            _turn("linkul: https://shop.ro/p/crema", []),
            prev,
            {"requires_offer": True, "grounded": False},
        )
        == []
    )


def test_forbidden_and_required_substr_diacritic_insensitive():
    prods = [{"product_id": "p1", "name": "Ser cu Retinol 1%", "price": 50.0}]
    fails = eval_gates.check_turn(
        _turn("recomand serul", prods), None, {"name_forbidden_substr": ["retinol"]}
    )
    assert any(f.startswith("name_forbidden") for f in fails)
    # required lipsă (fără diacritice în content vs cerință cu diacritice) → tot prins prin norm()
    fails2 = eval_gates.check_turn(
        _turn("mergi la un specialist"), None, {"content_required_substr": ["farmacist"]}
    )
    assert any(f.startswith("content_missing") for f in fails2)


def test_no_new_cards_on_followup():
    prev = _turn("aici", [{"product_id": "a"}, {"product_id": "b"}])
    # follow-up care NU introduce id-uri noi → OK
    assert (
        eval_gates.check_turn(_turn("prima", [{"product_id": "a"}]), prev, {"no_new_cards": True})
        == []
    )
    # follow-up cu un card NOU (id nemaiafișat) → prins
    fails = eval_gates.check_turn(
        _turn("uite altele", [{"product_id": "c"}]), prev, {"no_new_cards": True}
    )
    assert any(f.startswith("new_cards_on_followup") for f in fails)


def test_chip_too_long():
    fails = eval_gates.check_turn(
        _turn("x", suggestions=["Spune-mi mai multe despre această cremă hidratantă și prețul ei"]),
        None,
        {},
    )
    assert any(f.startswith("chip_too_long") for f in fails)


def test_opening_repeated_cross_turn():
    a = _turn("Pentru tenul tău gras, am câteva variante. Iată prima.")
    b = _turn("Pentru tenul tău gras, am câteva variante. Alta e asta.")
    assert eval_gates.opening_repeated(a, b) is True
    c = _turn("Sigur, uite ceva diferit acum.")
    assert eval_gates.opening_repeated(c, a) is False
    assert eval_gates.opening_repeated(a, None) is False


def test_judge_prompt_hash_stable_and_metric_shape():
    h1 = eval_judge.judge_prompt_sha256()
    h2 = eval_judge.judge_prompt_sha256()
    assert h1 == h2 and len(h1) == 64  # determinist, sha256
    # build_user_message include transcriptul + ultimul răspuns
    msg = eval_judge.build_user_message([{"role": "user", "text": "salut"}], "Bună!")
    assert "CLIENT: salut" in msg and "Bună!" in msg
