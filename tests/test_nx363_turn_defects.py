"""NX-363 — detectorii de defecte pe tur: fiecare pe cazul real din care vine, plus numitorul.

Cazurile sunt ture reale din 2026-09-30 (`sole-ro`), reduse la câmpurile pe care le citește
detectorul. Un detector întoarce `None` când nu se aplică: acolo nu intră nici în numitor.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from scripts import turn_defects as td


def _turn(
    *,
    tid="t1",
    conv="c1",
    at="2026-09-30T10:00:00",
    reply=None,
    diagnostics=None,
    events=None,
    availability=None,
    product_types=None,
):
    return td.Turn(
        turn_id=tid,
        conversation_id=conv,
        created_at=at,
        reply=reply or {},
        diagnostics=diagnostics or {},
        events=events or {},
        availability=availability or {},
        product_types=product_types or {},
    )


def _rich(*items):
    return {"rich": {"items": [dict(i) for i in items]}}


# ── P0 ─────────────────────────────────────────────────────────────────────────────────────────


def test_refused_set_shown_on_our_own_list_but_not_on_model_prose():
    ours = _turn(events={"refused_set_withheld": [{"named": 4, "model_prose": False}]})
    model = _turn(events={"refused_set_withheld": [{"named": 1, "model_prose": True}]})
    withheld = _turn(events={"refused_set_withheld": [{"named": 0, "model_prose": False}]})
    assert td.refused_set_shown(ours) is True
    assert td.refused_set_shown(model) is False
    assert td.refused_set_shown(withheld) is False
    assert td.refused_set_shown(_turn()) is None


def test_refused_set_shown_on_old_traces_uses_the_skipped_prose_round():
    """`0a9c3590` e de dinaintea câmpului `model_prose`: proza sărită ține loc."""
    old = _turn(events={"refused_set_withheld": [{"named": 4}], "prose_round": [{"skipped": True}]})
    assert td.refused_set_shown(old) is True


def test_oos_on_card_and_first():
    """`5c117067`: ROUND LAB Birch Juice și BEAUTY OF JOSEON Dynasty, epuizate, pe locurile 1-2."""
    t = _turn(
        reply=_rich({"product_id": "a", "name": "A"}, {"product_id": "b", "name": "B"}),
        availability={"a": "out_of_stock", "b": "in_stock"},
    )
    assert td.oos_on_card(t) is True
    assert td.oos_first(t) is True
    later = _turn(
        reply=_rich({"product_id": "b", "name": "B"}, {"product_id": "a", "name": "A"}),
        availability={"a": "out_of_stock", "b": "in_stock"},
    )
    assert td.oos_on_card(later) is True
    assert td.oos_first(later) is False
    assert td.oos_on_card(_turn()) is None  # fără carduri, nu se aplică


def test_cards_are_read_from_plain_products_when_there_is_no_rich():
    t = _turn(reply={"products": [{"product_id": "a"}]}, availability={"a": "out_of_stock"})
    assert td.oos_on_card(t) is True


# ── P1 ─────────────────────────────────────────────────────────────────────────────────────────


def test_uttered_need_unknown_is_the_par_gras_turn():
    """`8c578b1f`: «păr gras» → `concerns` `not_in_vocabulary`, filtrul n-a rulat."""
    t = _turn(
        events={
            "vocabulary_resolved": [
                {"dimension": "category", "status": "known"},
                {"dimension": "concerns", "status": "unknown", "reason": "not_in_vocabulary"},
            ]
        }
    )
    assert td.uttered_need_unknown(t) is True
    known = _turn(events={"vocabulary_resolved": [{"status": "known"}]})
    assert td.uttered_need_unknown(known) is False
    assert td.uttered_need_unknown(_turn()) is None


def test_subject_lost_counts_only_cards_with_a_known_type():
    """`853e03bd`: șase creme de față, patru fără `product_type` derivat. `subject_match.share`
    zicea 0,33; pe cardurile cu tip cunoscut e 2/2, deci NU e subiect pierdut."""
    creams = _turn(
        reply=_rich(*({"product_id": f"p{i}"} for i in range(6))),
        product_types={"p0": "crema de fata", "p4": "crema de fata"},
        events={"subject_match": [{"subject_type_known": True, "type_matched": 2, "share": 0.33}]},
    )
    assert td.subject_lost(creams) is False
    masks = _turn(  # `09e677d6`: o cremă și patru măști la «mai arată-mi»
        reply=_rich(*({"product_id": f"p{i}"} for i in range(5))),
        product_types={"p0": "crema de fata", "p3": "masca de fata", "p4": "masca de fata"},
        events={"subject_match": [{"subject_type_known": True, "type_matched": 1, "share": 0.2}]},
    )
    assert td.subject_lost(masks) is True
    untyped = _turn(
        reply=_rich({"product_id": "p0"}, {"product_id": "p1"}),
        events={"subject_match": [{"subject_type_known": True, "type_matched": 0}]},
    )
    assert td.subject_lost(untyped) is None  # sub două carduri cu tip, nu judecăm


def test_ordinal_out_of_range_after_a_detail_turn():
    """`816c193d`: «compara prima cu a treia» după un detaliu, ecranul avea un singur produs."""
    t = _turn(
        diagnostics={
            "kernel": {
                "resolved_refs": [
                    {"kind": "ordinal", "reason": "ordinal_in_set"},
                    {"kind": "ordinal", "reason": "ordinal_out_of_range"},
                ]
            }
        }
    )
    assert td.ordinal_out_of_range(t) is True
    fine = _turn(events={"reference_v2": [{"kind": "ordinal", "reason": "ordinal_in_set"}]})
    assert td.ordinal_out_of_range(fine) is False
    assert td.ordinal_out_of_range(_turn()) is None


def test_vague_qualifier_and_exclusion_on_the_kernel_trace():
    """`8c578b1f` («nu ft scump») și `cae37e6a` («nu vreau cu acid hialuronic»)."""
    vague = _turn(
        diagnostics={"kernel": {"plans": [{"search_args": {"rank_terms": ["ft scump"]}}]}}
    )
    plain = _turn(diagnostics={"kernel": {"plans": [{"search_args": {"rank_terms": []}}]}})
    detail = _turn(diagnostics={"kernel": {"plans": [{"search_args": None}]}})
    assert td.vague_to_rank_terms(vague) is True
    assert td.vague_to_rank_terms(plain) is False
    assert td.vague_to_rank_terms(detail) is None
    assert td.exclusion_unserved(_turn(diagnostics={"kernel": {"gaps": ["exclusion"]}})) is True
    assert td.exclusion_unserved(_turn(diagnostics={"kernel": {"gaps": []}})) is False
    assert td.exclusion_unserved(_turn()) is None


def test_side_search_overwrote_session():
    """`816c193d`: `compare_products` + `search_products(limit=1)` ⇒ sesiune nouă de un produs."""
    t = _turn(
        events={
            "tool_call": [{"name": "compare_products"}, {"name": "search_products"}],
            "search_session": [{"action": "new"}],
        }
    )
    assert td.side_search_overwrote_session(t) is True
    compare_only = _turn(events={"tool_call": [{"name": "compare_products"}]})
    assert td.side_search_overwrote_session(compare_only) is False
    search_only = _turn(events={"tool_call": [{"name": "search_products"}]})
    assert td.side_search_overwrote_session(search_only) is None


def test_mostly_rejected_set():
    t = _turn(
        reply=_rich({"product_id": "a"}, {"product_id": "b"}),
        events={"product_search": [{"count": 6}]},
    )
    assert td.mostly_rejected_set(t) is True
    assert td.mostly_rejected_set(_turn(events={"product_search": [{"count": 3}]})) is None


# ── P2 ─────────────────────────────────────────────────────────────────────────────────────────


def test_card_reason_dropped_maps_handles_to_cards():
    """`5c117067`: motivul BEAUTY OF JOSEON («…tărâțe de orez…») tăiat de scrub."""
    t = _turn(
        reply=_rich(
            {"product_id": "uuid-3", "reason": None}, {"product_id": "uuid-4", "reason": "ok"}
        ),
        diagnostics={
            "rich_handles": {"P3": "uuid-3", "P4": "uuid-4"},
            "rich_raw": {
                "items": [
                    {"product_id": "P3", "fit_clause": "cu apă din tărâțe de orez"},
                    {"product_id": "P4", "fit_clause": "cu uree"},
                ]
            },
        },
    )
    assert td.card_reason_dropped(t) is True
    assert td.card_without_reason(t) is True
    kept = _turn(
        reply=_rich({"product_id": "uuid-4", "reason": "ok"}),
        diagnostics={
            "rich_handles": {"P4": "uuid-4"},
            "rich_raw": {"items": [{"product_id": "P4", "fit_clause": "cu uree"}]},
        },
    )
    assert td.card_reason_dropped(kept) is False


def test_same_name_other_product_across_turns_of_one_conversation():
    """BEAUTY OF JOSEON Dynasty: `499c4f61` pe turul 2, `0d2afd4a` pe turul 3."""
    t2 = _turn(
        tid="t2", at="2026-09-30T10:00:00", reply=_rich({"product_id": "x", "name": "BoJ Dynasty"})
    )
    t3 = _turn(
        tid="t3", at="2026-09-30T10:01:00", reply=_rich({"product_id": "y", "name": "BoJ Dynasty"})
    )
    t4 = _turn(
        tid="t4", at="2026-09-30T10:02:00", reply=_rich({"product_id": "y", "name": "BoJ Dynasty"})
    )
    other = _turn(tid="o", conv="c2", reply=_rich({"product_id": "z", "name": "BoJ Dynasty"}))
    turns = {t.turn_id: t for t in td.with_conversation_context([t4, other, t3, t2])}
    assert td.same_name_other_product(turns["t2"]) is False
    assert td.same_name_other_product(turns["t3"]) is True
    assert td.same_name_other_product(turns["t4"]) is True  # tot sub un nume cu două produse
    assert td.same_name_other_product(turns["o"]) is False  # altă conversație


def test_model_calls_exclude_the_dark_interpretation():
    """Interpretarea dark (NX-353) e un cost al măsurătorii, nu al căii de răspuns."""
    calls = [{"purpose": "interpret"}, {}, {}, {}]
    normal = _turn(events={"llm_usage": [{"phase": "turn", "llm_calls": 4, "per_call": calls}]})
    faq = _turn(events={"llm_usage": [{"phase": "turn", "llm_calls": 5, "per_call": [*calls, {}]}]})
    assert td.many_model_calls(normal) is False
    assert td.many_model_calls(faq) is True
    post = _turn(events={"llm_usage": [{"phase": "post_turn", "llm_calls": 9}]})
    assert td.many_model_calls(post) is None


def test_closing_missing_only_when_required():
    need = _turn(events={"answer_shape": [{"required": ["closing"], "missing": ["closing"]}]})
    no_need = _turn(events={"answer_shape": [{"required": ["fit_line"], "missing": []}]})
    assert td.closing_missing(need) is True
    assert td.closing_missing(no_need) is None


# ── agregarea ──────────────────────────────────────────────────────────────────────────────────


def test_summarize_uses_the_applicable_denominator_and_orders_by_severity():
    turns = [
        _turn(tid="a", reply=_rich({"product_id": "p"}), availability={"p": "out_of_stock"}),
        _turn(tid="b", reply=_rich({"product_id": "q"}), availability={"q": "in_stock"}),
        _turn(tid="c"),  # fără carduri: nu intră în numitorul detectorilor de card
    ]
    report = td.summarize(turns)
    rows = {r["detector"]: r for r in report["detectors"]}
    assert report["turns"] == 3
    assert rows["oos_on_card"]["hits"] == 1
    assert rows["oos_on_card"]["applicable"] == 2
    assert rows["oos_on_card"]["rate"] is None  # sub MIN_APPLICABLE nu raportăm rata
    assert rows["oos_on_card"]["examples"] == ["a"]
    assert rows["ordinal_out_of_range"]["applicable"] == 0
    assert rows["ordinal_out_of_range"]["rate"] is None
    severities = [r["severity"] for r in report["detectors"]]
    assert severities == sorted(severities)
    assert "a" in td.render_console(report)


def test_rate_is_reported_from_the_minimum_sample():
    n = td.MIN_APPLICABLE
    turns = [
        _turn(
            tid=f"t{i}",
            reply=_rich({"product_id": f"p{i}"}),
            availability={f"p{i}": "out_of_stock" if i < 5 else "in_stock"},
        )
        for i in range(n)
    ]
    row = next(r for r in td.summarize(turns)["detectors"] if r["detector"] == "oos_on_card")
    assert row["rate"] == round(5 / n, 3)


def test_detector_keys_are_unique():
    keys = [d.key for d in td.DETECTORS]
    assert len(keys) == len(set(keys))


# ── citirea (conexiune falsă) ──────────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, business_id="b"):
        self.business_id = business_id
        self.calls: list[str] = []

    async def fetchval(self, sql, *args):
        return self.business_id

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        assert args[0] == self.business_id  # P7: fiecare query e pe tenant
        if "from conversation_traces" in sql:
            return [
                {
                    "turn_id": "t1",
                    "conversation_id": "c1",
                    "created_at": datetime(2026, 9, 30, tzinfo=UTC),
                    "reply": '{"rich": {"items": [{"product_id": '
                    '"11111111-1111-1111-1111-111111111111", "name": "A"}]}}',
                    "diagnostics": "{}",
                }
            ]
        if "from analytics_events" in sql:
            return [{"turn_id": "t1", "event_type": "product_search", "properties": '{"count": 6}'}]
        if "from products" in sql:
            return [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "availability": "out_of_stock",
                    "product_type": "crema de fata",
                }
            ]
        raise AssertionError(sql)


async def test_load_builds_turns_from_traces_events_and_catalog():
    conn = _Conn()

    @asynccontextmanager
    async def opener(*_):
        yield conn

    turns = await td.load(
        "sole-ro",
        datetime(2026, 9, 29, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
        admin=opener,
        tenant=opener,
    )
    assert len(turns) == 1
    t = turns[0]
    assert t.first("product_search") == {"count": 6}
    assert td.oos_first(t) is True
    assert t.product_types == {"11111111-1111-1111-1111-111111111111": "crema de fata"}
