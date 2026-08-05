"""NX-217 felia 3 — motorul de acțiuni. Funcție PURĂ: zero DB, zero LLM.

Ce pinuim aici sunt exact regulile de business pe care un bug le-ar transforma în sfaturi
false: ordinea `restock` înaintea lui `add_to_catalog` (nu cere cumpărarea a ceva ce ai),
pragurile, separarea semnalelor de forță diferită (cereri vs abonări), trendul și faptul că
`faq_miss` NU devine acțiune (nu știm despre ce era întrebarea).
"""

from src.analytics.actions import build_actions, health_indicators


def _fact(signal, kind, key, n, *, evidence=("c1",)):
    return {
        "signal_kind": signal,
        "dimension_kind": kind,
        "dimension_key": key,
        "request_count": n,
        "evidence_conversation_ids": list(evidence),
    }


def _kinds(actions):
    return {(a["kind"], a["dimension_key"]) for a in actions}


# --- add_to_catalog vs restock: ordinea care ține raportul credibil ----------


def test_absent_brand_becomes_add_to_catalog():
    facts = [_fact("unmet_no_result", "brand", "Bioderma", 73)]
    actions = build_actions(facts, brand_presence={})
    assert _kinds(actions) == {("add_to_catalog", "Bioderma")}
    assert actions[0]["count"] == 73
    assert actions[0]["evidence_conversation_ids"] == ["c1"]


def test_existing_brand_without_stock_becomes_restock_not_add():
    """Brandul EXISTĂ în catalog dar nimic nu e cumpărabil → reaprovizionare, NU „adaugă-l".
    Sfatul „adaugă un brand pe care îl ai" e cel care omoară încrederea în raport."""
    facts = [_fact("unmet_no_result", "brand", "Avene", 12)]
    presence = {"avene": {"products": 4, "buyable": 0}}
    actions = build_actions(facts, brand_presence=presence)
    assert _kinds(actions) == {("restock", "Avene")}


def test_existing_brand_with_stock_produces_no_action():
    """Brand prezent ȘI cumpărabil: cererea neîmplinită vine din altceva (filtre, preț) —
    nicio acțiune de catalog, ca să nu inventăm muncă."""
    facts = [_fact("unmet_no_result", "brand", "Avene", 12)]
    actions = build_actions(facts, brand_presence={"avene": {"products": 4, "buyable": 3}})
    assert actions == []


def test_brand_match_is_case_insensitive():
    facts = [_fact("unmet_no_result", "brand", "BIODERMA", 5)]
    actions = build_actions(facts, brand_presence={"bioderma": {"products": 2, "buyable": 0}})
    assert _kinds(actions) == {("restock", "BIODERMA")}


def test_one_dimension_yields_at_most_one_action():
    """Aceeași dimensiune apare o singură dată, chiar dacă mai multe semnale o ating."""
    facts = [
        _fact("unmet_out_of_stock", "product", "p1", 40),
        _fact("unmet_no_result", "product", "p1", 6),
    ]
    state = {"p1": {"name": "Ser C", "availability": "out_of_stock", "subscribers": 19}}
    actions = build_actions(facts, product_state=state)
    assert len(actions) == 1 and actions[0]["kind"] == "restock"


# --- praguri, semnale secundare, trend ---------------------------------------


def test_below_threshold_is_ignored():
    facts = [_fact("unmet_no_result", "brand", "Rar", 2)]
    assert build_actions(facts, brand_presence={}, min_requests=3) == []
    assert len(build_actions(facts, brand_presence={}, min_requests=2)) == 1


def test_subscribers_are_a_separate_count_never_summed():
    """„41 cereri + 19 abonări", niciodată 60: sunt semnale de forță diferită."""
    facts = [_fact("unmet_out_of_stock", "product", "p9", 41)]
    state = {"p9": {"name": "Ser C 30ml", "availability": "out_of_stock", "subscribers": 19}}
    a = build_actions(facts, product_state=state)[0]
    assert a["count"] == 41
    assert a["supporting_counts"]["back_in_stock_subscribers"] == 19
    assert a["context"]["product_name"] == "Ser C 30ml"


def test_trend_carries_previous_window_count():
    facts = [_fact("unmet_no_result", "brand", "Bioderma", 73)]
    prev = [_fact("unmet_no_result", "brand", "Bioderma", 41)]
    a = build_actions(facts, prev_facts=prev, brand_presence={})[0]
    assert (a["count"], a["prev_count"]) == (73, 41)


def test_missing_previous_window_is_zero_not_null():
    a = build_actions([_fact("unmet_no_result", "brand", "Nou", 9)], brand_presence={})[0]
    assert a["prev_count"] == 0


def test_actions_sorted_by_strength():
    facts = [
        _fact("unmet_no_result", "brand", "Mic", 5),
        _fact("unmet_no_result", "brand", "Mare", 80),
    ]
    actions = build_actions(facts, brand_presence={})
    assert [a["dimension_key"] for a in actions] == ["Mare", "Mic"]


# --- restul regulilor --------------------------------------------------------


def test_missing_variant_becomes_add_variant():
    facts = [_fact("unmet_missing_variant", "variant_attr", "medium bej", 28)]
    actions = build_actions(facts)
    assert _kinds(actions) == {("add_variant", "medium bej")}


def test_price_gap_becomes_action_with_product_context():
    facts = [_fact("unmet_price_gap", "product", "p3", 64)]
    state = {"p3": {"name": "Cremă X", "availability": "in_stock", "subscribers": 0}}
    a = build_actions(facts, product_state=state)[0]
    assert a["kind"] == "price_gap" and a["context"]["product_name"] == "Cremă X"


def test_clarify_field_becomes_content_action():
    facts = [_fact("clarify_asked", "clarify_field", "category", 11)]
    assert _kinds(build_actions(facts)) == {("add_faq_content", "category")}


def test_faq_miss_is_a_health_indicator_not_an_action():
    """Știm CÂTE întrebări n-au primit răspuns, nu DESPRE CE erau (clustering = Faza 4).
    „Botul ratează 194 de întrebări" e onest; „scrie un FAQ despre X" ar fi inventat."""
    facts = [_fact("faq_miss", "none", "", 194)]
    assert build_actions(facts) == []
    assert health_indicators(facts) == {"faq_misses": 194, "clarifications": 0}


def test_no_estimated_value_or_confidence_anywhere():
    """Invarianta de onestitate D.4, verificată pe payload-ul efectiv."""
    facts = [
        _fact("unmet_no_result", "brand", "B", 10),
        _fact("unmet_out_of_stock", "product", "p1", 10),
    ]
    state = {"p1": {"name": "X", "availability": "out_of_stock", "subscribers": 2}}
    for a in build_actions(facts, brand_presence={}, product_state=state):
        assert "estimated_value" not in a and "confidence" not in a
        assert "estimated_value" not in a["context"] and "confidence" not in a["context"]


def test_empty_window_returns_no_actions():
    assert build_actions([]) == []
    assert health_indicators([]) == {"faq_misses": 0, "clarifications": 0}
