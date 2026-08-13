"""NX-235 — precedența UNICĂ a referinței, inclusiv ce REFUZĂ să rezolve.

NX-234 a adus ancora paginii; aici se închide algoritmul: `action` > `named` > `ordinal` >
`page(deictic)` > `selected` > `single` > ambiguu. Jumătate din teste sunt negative, fiindcă
partea valoroasă a unui resolver e ce nu face: o ancoră expirată nu cade pe primul card, iar un
ordinal imposibil nu selectează „ceva apropiat".

Non-regresia NX-234 stă în `tests/test_page_reference_resolution.py` — modul legacy trece prin
aceeași implementare, cu `legacy_page_fallback=True`.
"""

from dataclasses import dataclass

import pytest

from src.agent import reference_resolver as rr

PAGE_ID = "page-0001"


@dataclass
class Ref:
    product_id: str
    name: str


def _refs(*pairs) -> tuple[Ref, ...]:
    return tuple(Ref(pid, name) for pid, name in pairs)


def _req(query: str, **kw) -> rr.ReferenceRequest:
    return rr.ReferenceRequest(query=query, **kw)


TWO = _refs(("p1", "Cremă hidratantă Petală"), ("p2", "Ser cu vitamina C"))


# ── 1. Ancora semnată (NX-236) ───────────────────────────────────────────────


def test_a_valid_action_anchor_wins_over_everything_else():
    r = rr.resolve_reference(
        _req(
            "de fapt crema hidratantă",
            refs=TWO,
            page=rr.PageAnchor(PAGE_ID),
            anchor=rr.ActionAnchor("p2"),
        )
    )
    assert r.product_id == "p2" and r.source == "action" and r.reason == "action_anchor"


def test_a_tampered_anchor_is_rejected_not_silently_replaced():
    r = rr.resolve_reference(
        _req(
            "și asta?",
            refs=TWO,
            page=rr.PageAnchor(PAGE_ID),
            anchor=rr.ActionAnchor("p2", valid=False),
        )
    )
    assert r.product_id is None and r.outcome == "stale" and r.reason == "anchor_invalid"


def test_a_stale_anchor_does_not_fall_back_to_the_page():
    """Rândul „action anchor tampered/stale" din failure matrix."""
    r = rr.resolve_reference(
        _req(
            "acesta",
            refs=TWO,
            page=rr.PageAnchor(PAGE_ID),
            anchor=rr.ActionAnchor("p2", revision=4),
            displayed_revision=9,
        )
    )
    assert r.product_id is None and r.stale and r.reason == "anchor_stale"


def test_an_unbound_anchor_is_accepted():
    r = rr.resolve_reference(
        _req("x", refs=TWO, anchor=rr.ActionAnchor("p1"), displayed_revision=9)
    )
    assert r.product_id == "p1"


def test_an_anchor_on_the_current_revision_resolves():
    r = rr.resolve_reference(
        _req("x", refs=TWO, anchor=rr.ActionAnchor("p1", revision=9), displayed_revision=9)
    )
    assert r.product_id == "p1" and r.name == "Cremă hidratantă Petală"


# ── 2-3. Numit > ordinal ─────────────────────────────────────────────────────


def test_an_explicit_name_beats_an_ordinal_in_the_same_sentence():
    """Expresie MIXTĂ: „a doua, cea cu vitamina C" — numele e afirmația precisă."""
    r = rr.resolve_reference(_req("a doua, cea cu vitamina C", refs=TWO))
    assert r.product_id == "p2" and r.source == "named"


def test_an_ordinal_resolves_against_the_displayed_list():
    r = rr.resolve_reference(_req("spune-mi de a doua", refs=_refs(("p1", "Alfa"), ("p2", "Beta"))))
    assert r.product_id == "p2" and r.source == "ordinal" and r.index == 1


def test_an_impossible_ordinal_selects_nothing():
    """Lista s-a scurtat/reordonat: „a treia" nu are voie să devină „a doua"."""
    r = rr.resolve_reference(_req("dă-mi a treia", refs=TWO, page=rr.PageAnchor(PAGE_ID)))
    assert r.product_id is None and r.outcome == "ambiguous" and r.reason == "ordinal_out_of_range"


def test_a_common_token_never_decides_between_two_products():
    r = rr.resolve_reference(
        _req("vreau crema", refs=_refs(("p1", "Cremă de zi"), ("p2", "Cremă de noapte")))
    )
    assert r.product_id is None and r.outcome == "ambiguous"


# ── 4. Pagina: deictic sau singurul candidat ─────────────────────────────────


@pytest.mark.parametrize(
    "query", ["ce părere ai despre acesta?", "și asta cum e?", "is this good?", "ez jó?"]
)
def test_a_deictic_expression_anchors_the_page(query):
    r = rr.resolve_reference(_req(query, refs=TWO, page=rr.PageAnchor(PAGE_ID, "Ser")))
    assert r.product_id == PAGE_ID and r.reason == "page_deictic"


def test_a_non_deictic_question_does_not_hijack_a_displayed_candidate():
    """Ancora spune UNDE se află clientul; nu e o afirmație despre ce vrea."""
    r = rr.resolve_reference(
        _req("care are rating mai bun?", refs=TWO, page=rr.PageAnchor(PAGE_ID))
    )
    assert r.product_id is None and r.outcome == "ambiguous"


def test_with_nothing_displayed_the_page_is_the_only_thing_it_can_mean():
    r = rr.resolve_reference(_req("spune-mi mai multe", page=rr.PageAnchor(PAGE_ID, "Ser")))
    assert r.product_id == PAGE_ID and r.reason == "page_only_candidate"


def test_a_named_product_beats_the_page_anchor():
    """Rândul „page product diferit de produs numit" din failure matrix."""
    r = rr.resolve_reference(
        _req("de fapt ce zici de crema hidratantă?", refs=TWO, page=rr.PageAnchor(PAGE_ID, "Ser"))
    )
    assert r.product_id == "p1" and r.source == "named"


def test_deixis_does_not_fire_on_a_substring():
    assert rr.is_deictic("acestea") is True
    assert rr.is_deictic("acestora de acolo") is False
    assert rr.is_deictic("ce contine produsul") is False


# ── 5-7. Selectat > unic > ambiguu ───────────────────────────────────────────


def test_a_previously_confirmed_product_is_used_when_nothing_else_points():
    r = rr.resolve_reference(_req("cât costă?", refs=TWO, selected_product="p2"))
    assert r.product_id == "p2" and r.source == "selected"


def test_a_selected_product_does_not_beat_an_explicit_name():
    r = rr.resolve_reference(_req("crema hidratantă", refs=TWO, selected_product="p2"))
    assert r.product_id == "p1" and r.source == "named"


def test_a_single_recent_product_resolves_when_unambiguous():
    r = rr.resolve_reference(_req("cât costă?", refs=_refs(("p1", "Unicul"))))
    assert r.product_id == "p1" and r.source == "single"


def test_a_selected_product_beats_a_single_displayed_one():
    r = rr.resolve_reference(
        _req("cât costă?", refs=_refs(("p1", "Unicul")), selected_product="p9")
    )
    assert r.product_id == "p9" and r.source == "selected"


def test_no_anchor_at_all_is_ambiguous_never_the_first_card():
    r = rr.resolve_reference(_req("și acela?", refs=TWO))
    assert r.product_id is None and r.outcome == "ambiguous" and r.reason == "no_anchor"


def test_an_empty_world_is_none_not_ambiguous():
    r = rr.resolve_reference(_req("ce zici?"))
    assert r.product_id is None and r.outcome == "none" and r.reason == "empty"


# ── Legacy: NX-234 rămâne byte-identic ca semantică ──────────────────────────


def test_the_legacy_entrypoint_keeps_the_unconditional_page_fallback():
    r = rr.resolve_product_reference(
        "care are rating mai bun?", list(TWO), page=rr.PageAnchor(PAGE_ID)
    )
    assert r.product_id == PAGE_ID and r.reason == "page_legacy"


def test_the_legacy_entrypoint_without_a_page_is_the_old_function():
    refs = list(_refs(("p1", "Unicul produs")))
    assert rr.resolve_product_reference("și asta?", refs, page=None) == rr.resolve_from_displayed(
        "și asta?", refs
    )


def test_the_legacy_entrypoint_tolerates_an_impossible_ordinal():
    """Modul legacy nu introduce refuzuri noi — asta e diferența de contract, explicită."""
    r = rr.resolve_product_reference("dă-mi a treia", list(TWO), page=rr.PageAnchor(PAGE_ID))
    assert r.product_id == PAGE_ID


# ── Proprietăți ──────────────────────────────────────────────────────────────


def test_resolution_is_deterministic():
    request = _req("a doua", refs=TWO, page=rr.PageAnchor(PAGE_ID))
    assert rr.resolve_reference(request) == rr.resolve_reference(request)


def test_every_resolution_carries_a_reason():
    for request in (
        _req("acesta", page=rr.PageAnchor(PAGE_ID)),
        _req("a doua", refs=TWO),
        _req("nimic", refs=TWO),
        _req("nimic"),
    ):
        assert rr.resolve_reference(request).reason != ""
