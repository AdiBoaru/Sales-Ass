"""NX-71 — poarta de gating proactiv. Cod pur: ZERO DB, ZERO LLM/embeddings, ZERO rețea.

NX-289: poarta avea trei etaje (consent → fereastra 24h Meta → template aprobat). Ultimele
două erau reguli ale PLATFORMEI WhatsApp, nu ale produsului, și au plecat cu canalul. A rămas
consentul, care e regula noastră — testele lui sunt neatinse, fiindcă nu s-a schimbat nimic în
semantica lui. Ce s-a schimbat e ce urmează după un `da`: un singur verdict, `mode='free'`.

Testele pe `render_template` / `get_approved_template` / `in_24h_window` au dispărut odată cu
funcțiile. Testul care rămâne și contează: după consent nu mai există ramură care poate BLOCA
tăcut un mesaj proactiv — înainte, `no_window_no_template` însemna „opt-in dat, mesaj neplecat".
"""

from src.models import Contact
from src.proactive.templates import ProactiveDecision, _has_optin, decide_proactive


def _contact(consent: dict) -> Contact:
    return Contact(id="contact-1", business_id="biz-1", consent=consent)


def _decide(*, consent, kind="awb_update"):
    return decide_proactive(
        contact=_contact(consent),
        kind=kind,
        free_text="Comanda ta a fost expediată.",
    )


# --------------------------------------------------------------------------- #
# _has_optin — convenția consent jsonb (override per-kind bate default-ul)
# --------------------------------------------------------------------------- #


def test_optin_default_proactive_for_transactional():
    assert _has_optin({"proactive": True}, "awb_update") is True
    assert _has_optin({}, "awb_update") is False
    assert _has_optin({"proactive": False}, "back_in_stock") is False


def test_optin_default_marketing_for_marketing_kinds():
    assert _has_optin({"marketing": True}, "abandoned_cart") is True
    assert _has_optin({"proactive": True}, "abandoned_cart") is False  # marketing ≠ proactive


def test_optin_per_kind_override():
    # opt-in fin: per-kind True bate absența default-ului de marketing
    assert _has_optin({"abandoned_cart": True}, "abandoned_cart") is True
    # opt-out fin: per-kind False bate default-ul de marketing True
    assert _has_optin({"marketing": True, "abandoned_cart": False}, "abandoned_cart") is False


# --------------------------------------------------------------------------- #
# decide_proactive — consent, și doar consent
# --------------------------------------------------------------------------- #


def test_no_optin_blocks():
    assert _decide(consent={}) == ProactiveDecision(
        allowed=False, mode="blocked", reason="no_optin"
    )


def test_optin_false_blocks():
    dec = _decide(consent={"proactive": False})
    assert dec.allowed is False and dec.reason == "no_optin"
    assert dec.rendered_text is None  # nimic randat pe o decizie de blocare


def test_optin_returns_free_text():
    dec = _decide(consent={"proactive": True})
    assert dec == ProactiveDecision(
        allowed=True,
        mode="free",
        reason="ok_free",
        rendered_text="Comanda ta a fost expediată.",
    )


def test_marketing_kind_needs_marketing_consent():
    assert _decide(consent={"proactive": True}, kind="abandoned_cart").allowed is False
    assert _decide(consent={"marketing": True}, kind="abandoned_cart").allowed is True


def test_consent_is_the_only_way_to_block():
    """Cu opt-in dat, poarta nu mai are cum să întoarcă `allowed=False`.

    Înainte putea: `no_window_no_template` însemna „clientul a cerut notificări, dar tenantul
    n-avea template aprobat la Meta pentru limba lui" — un mesaj pierdut din motive de platformă.
    Fixăm proprietatea, ca o viitoare ramură de blocare să fie o decizie, nu o regresie."""
    for kind in ("awb_update", "back_in_stock", "abandoned_cart", "follow_up"):
        dec = _decide(consent={"proactive": True, "marketing": True}, kind=kind)
        assert dec.allowed is True and dec.mode == "free", kind
