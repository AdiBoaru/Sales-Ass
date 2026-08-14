"""NX-239 — checks OBIECTIVE de naturalețe (`conversation_quality`). Pure, fără LLM/DB."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.conversation_quality import (
    direct_lead,
    evaluate_reply,
    generic_reason,
    no_results_class_valid,
    opening_fingerprint,
    question_count,
    repeated_opening,
)

# --- direct lead ----------------------------------------------------------------


def test_direct_lead_passes_on_answer_first():
    assert direct_lead("Livrarea costă 20 de lei prin curier.")


def test_direct_lead_fails_on_template_openers():
    for opener in (
        "Sigur! Livrarea costă 20 de lei.",
        "Bună! Cu ce te pot ajuta?",
        "Mulțumesc că ne-ai scris! Livrarea...",
        "Îmi pare rău, dar...",
    ):
        assert not direct_lead(opener), opener


def test_direct_lead_fails_on_empty():
    assert not direct_lead("")


# --- repetiție ------------------------------------------------------------------


def test_repeated_opening_detected():
    prev = ["Pentru ten uscat recomand serul LumaDerm cu acid hialuronic."]
    text = "Pentru ten uscat recomand crema Nivea cu ceramide."
    assert repeated_opening(text, prev)


def test_different_opening_not_flagged():
    prev = ["Pentru ten uscat recomand serul LumaDerm."]
    assert not repeated_opening("Livrarea durează 2-3 zile lucrătoare.", prev)


def test_short_fingerprint_never_accuses():
    assert not repeated_opening("Da.", ["Da."])


def test_fingerprint_normalizes_diacritics():
    assert opening_fingerprint("Pentru părul vopsit recomand") == opening_fingerprint(
        "pentru parul vopsit recomand"
    )


# --- clarificare + motive + no-results ------------------------------------------


def test_question_count():
    assert question_count("Ce buget ai? Și pentru cine e?") == 2
    assert question_count("Serul costă 89 lei.") == 0


def test_generic_reason_flagged():
    for reason in (
        "un produs excelent",
        "ideal pentru tine",
        "o alegere bună",
        "calitate superioară",
    ):
        assert generic_reason(reason), reason


def test_concrete_reason_not_flagged():
    assert not generic_reason("are acid hialuronic 2%, exact pentru tenul tău uscat")


def test_no_results_taxonomy_closed():
    assert no_results_class_valid("no_match")
    assert no_results_class_valid("insufficient_data")
    assert no_results_class_valid("dependency_unavailable")
    assert not no_results_class_valid("not_sure")


# --- evaluate_reply (agregatorul emis ca `conversation_quality`) ----------------


def _plan(**overrides):
    base = dict(recommendations=(), no_results=None, clarification=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _outcomes(checks):
    return {c.check: c.outcome for c in checks}


def test_evaluate_reply_all_pass():
    out = _outcomes(
        evaluate_reply(
            "Serul LumaDerm costă 89 lei și are acid hialuronic.",
            plan=_plan(),
            previous_bot_texts=(),
        )
    )
    assert out["direct_lead"] == "pass"
    assert out["repeated_opening"] == "pass"
    assert out["max_one_clarification"] == "pass"
    assert out["concrete_reasons"] == "pass"


def test_evaluate_reply_flags_generic_recommendation():
    plan = _plan(
        recommendations=(SimpleNamespace(reason="un produs excelent", evidence_ids=("e1",)),)
    )
    out = _outcomes(evaluate_reply("Recomand serul X.", plan=plan))
    assert out["concrete_reasons"] == "fail"


def test_evaluate_reply_flags_missing_evidence_reason():
    plan = _plan(recommendations=(SimpleNamespace(reason="are acid hialuronic", evidence_ids=()),))
    out = _outcomes(evaluate_reply("Recomand serul X.", plan=plan))
    assert out["concrete_reasons"] == "fail"


def test_evaluate_reply_flags_two_questions():
    out = _outcomes(evaluate_reply("Ce buget ai? Pentru cine e?", plan=_plan()))
    assert out["max_one_clarification"] == "fail"


def test_evaluate_reply_honest_no_results():
    plan = _plan(no_results=SimpleNamespace(reason_class="no_match"))
    out = _outcomes(evaluate_reply("Nu am găsit produse sub 50 lei.", plan=plan))
    assert out["honest_no_results"] == "pass"
