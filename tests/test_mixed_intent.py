"""NX-184 — mixed-intent pre-FAQ (detector tri-state) + completare deterministă a obligației.

Detectorul (Codex): PURE_FAQ → FAQ poate early-exit; POSSIBLE_MIXED/UNKNOWN → NU early-exit
(pipeline complet). Completarea garantează că răspunsul de politică ajunge la client.
"""

from types import SimpleNamespace

from src.config import get_settings
from src.models import Reply
from src.worker.stages.agent import _complete_faq_obligation
from src.worker.stages.faq import mixed_intent_decision


def test_mixed_intent_decision_tri_state():
    dp = None  # fără DomainPack → doar cuvinte de produs generice
    # produs + politică + două clauze (conjuncție) → POSSIBLE_MIXED
    assert mixed_intent_decision("vreau o cremă și cât durează livrarea?", dp) == "POSSIBLE_MIXED"
    # două propoziții FĂRĂ conjuncție (două „?") → POSSIBLE_MIXED
    assert mixed_intent_decision("Crema asta e bună? Livrați mâine?", dp) == "POSSIBLE_MIXED"
    # brand/produs numit fără termen de categorie („aveți" = semnal) + două clauze → POSSIBLE_MIXED
    assert (
        mixed_intent_decision("aveți Hidra Boost? cât costă transportul?", dp) == "POSSIBLE_MIXED"
    )
    # FAQ pur care menționează accidental un termen de catalog, O SINGURĂ clauză → PURE_FAQ
    assert mixed_intent_decision("cum returnez o cremă?", dp) == "PURE_FAQ"
    # politică pură, o clauză → PURE_FAQ
    assert mixed_intent_decision("cât durează livrarea?", dp) == "PURE_FAQ"
    # două clauze + politică, dar niciun termen de produs recunoscut → UNKNOWN (conservator)
    assert mixed_intent_decision("asta merge și cât durează livrarea?", dp) == "UNKNOWN"
    # fără semnal de politică → PURE_FAQ (nimic de mixat; oricum n-ar fi lovit FAQ pe politică)
    assert mixed_intent_decision("ce cremă recomanzi pentru ten gras?", dp) == "PURE_FAQ"


def _ctx(grounded, text):
    return SimpleNamespace(faq_grounded=grounded, reply=Reply(text=text), emit=lambda *a, **k: None)


def test_complete_faq_obligation_appends_when_missing(monkeypatch):
    monkeypatch.setattr(get_settings(), "response_shape_hints_enabled", True)
    ctx = _ctx("Livrarea durează 2-3 zile lucrătoare.", "Îți recomand crema X.")
    _complete_faq_obligation(ctx)
    assert "crema X" in ctx.reply.text and "Livrarea durează 2-3 zile" in ctx.reply.text


def test_complete_faq_obligation_skips_when_present(monkeypatch):
    monkeypatch.setattr(get_settings(), "response_shape_hints_enabled", True)
    ctx = _ctx(
        "Livrarea durează 2-3 zile lucrătoare.",
        "Livrarea durează 2-3 zile lucrătoare. Plus crema X.",
    )
    before = ctx.reply.text
    _complete_faq_obligation(ctx)
    assert ctx.reply.text == before  # deja acoperit → nu dublăm


def test_complete_faq_obligation_gated_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "response_shape_hints_enabled", False)
    ctx = _ctx("Livrarea durează 2-3 zile.", "X")
    _complete_faq_obligation(ctx)
    assert ctx.reply.text == "X"  # OFF → no-op


def test_complete_faq_obligation_no_grounded_noop(monkeypatch):
    monkeypatch.setattr(get_settings(), "response_shape_hints_enabled", True)
    ctx = _ctx(None, "X")
    _complete_faq_obligation(ctx)
    assert ctx.reply.text == "X"  # fără faq_grounded → no-op
