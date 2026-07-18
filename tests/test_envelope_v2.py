"""NX-183 — ResponseEnvelope V2-light: evidence OPACE + motive compuse determinist + flag/degradare.

Proprietatea de siguranță: modelul referă DOAR id-uri opace din meniu → nu poate inventa dovezi;
un id invalid e ignorat. OFF → calea rich (verificat de regresia compose, nu aici).
"""

from src.agent import envelope
from src.config import get_settings


def _prod(pid, name, pros):
    return {"id": pid, "name": name, "price": 50.0, "top_pros": pros}


def test_evidence_menu_opaque_ids_per_product():
    products = [
        _prod("p1", "Cremă A", ["Textură lejeră", "Fără parfum"]),
        _prod("p2", "Ser B", ["Cu niacinamidă"]),
    ]
    menu = envelope.evidence_menu(products)
    assert set(menu) == {"p1", "p2"}
    # id-uri opace, mapate la fapte reale
    assert list(menu["p1"].values()) == ["Textură lejeră", "Fără parfum"]
    assert all(eid.startswith("e0_") for eid in menu["p1"])
    assert all(eid.startswith("e1_") for eid in menu["p2"])


def test_compose_reason_only_valid_evidence():
    menu = envelope.evidence_menu([_prod("p1", "A", ["Textură lejeră", "Fără parfum"])])
    eids = list(menu["p1"])
    # evidence valide → microcopy cu conector + fapte
    out = envelope.compose_reason("p1", eids, "best_if", menu, "ro")
    assert "Bun dacă vrei" in out and "Textură lejeră" in out and "Fără parfum" in out
    # id invalid (nu e în meniul produsului) → ignorat; niciun fapt valid → gol
    assert envelope.compose_reason("p1", ["e9_9"], "good_for", menu, "ro") == ""
    # id împrumutat de la alt produs → ignorat (membership pe produs)
    assert envelope.compose_reason("p2", eids, "good_for", menu, "ro") == ""
    # EN locale
    out_en = envelope.compose_reason("p1", [eids[0]], "good_for", menu, "en")
    assert "Good for" in out_en


def test_evidence_menu_drops_medical_claim(monkeypatch):
    # P0-safety (Codex): un fapt cu claim medical (din recenzie) NU intră în meniu → modelul nu-l
    # poate selecta, codul nu-l poate compune în text-only (care nu trece prin validate_prose).
    monkeypatch.setattr(get_settings(), "safety_medical_guardrail_enabled", True)
    p = _prod("p1", "A", ["Textură lejeră", "Tratează acneea în 7 zile"])
    facts = list(envelope.evidence_menu([p])["p1"].values())
    assert "Textură lejeră" in facts
    assert all("acnee" not in f.lower() for f in facts)  # claimul medical a fost eliminat


def test_response_envelope_v2_effective_per_business():
    s = get_settings()
    orig = getattr(s, "response_envelope_v2_enabled", False)

    class _Biz:
        def __init__(self, settings):
            self.settings = settings

    try:
        s.response_envelope_v2_enabled = False
        assert (
            envelope.response_envelope_v2_effective(_Biz({"response_envelope_v2_enabled": True}))
            is False
        )
        s.response_envelope_v2_enabled = True
        assert envelope.response_envelope_v2_effective(_Biz({})) is False  # lipsă → OFF
        assert (
            envelope.response_envelope_v2_effective(_Biz({"response_envelope_v2_enabled": False}))
            is False
        )
        assert envelope.response_envelope_v2_effective(_Biz(None)) is False  # fără dict → OFF
        assert (
            envelope.response_envelope_v2_effective(_Biz({"response_envelope_v2_enabled": True}))
            is True
        )
    finally:
        s.response_envelope_v2_enabled = orig


async def test_finalize_v2_degrades_to_false_on_llm_error():
    from types import SimpleNamespace

    from src.agent.finalize import _finalize_v2

    class _BoomLLM:
        async def complete_schema(self, *a, **k):
            raise RuntimeError("API down")

    plan = SimpleNamespace(
        products=[_prod("p1", "A", ["x"])],
        inp=None,
        query="q",
        history="",
        commerce_note="",
        response_shape="",
        checkout_url=None,
    )

    class _Ctx:
        language = "ro"
        business = SimpleNamespace(domain_pack=None)

        def emit(self, *a, **k):
            pass

    # build_v2_system(None) e ok (inp=None → _store_header tolerant? nu — dar apelul llm crapă întâi
    # DOAR dacă build_v2_system e ok). Ca să izolăm degradarea, forțăm eroarea la complete_schema.
    deps = SimpleNamespace(llm=_BoomLLM())
    # build_v2_system are nevoie de un PromptInputs valid; folosim unul minimal.
    from src.agent.prompt_builder import PromptInputs

    plan.inp = PromptInputs.build("Demo", "beauty", "ro", [], [])
    ok = await _finalize_v2(deps, plan, _Ctx())
    assert ok is False  # eroare LLM → fall-through la rich (nu setează reply)
