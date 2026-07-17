"""NX-173 (P0) — contractul de COMPUNERE: fraza de siguranță e GARANTATĂ de cod, o singură dată,
localizată; modelul compune doar partea comercială.

Review Codex pe #229: declinarea + trimiterea la medic nu ajungeau în reply-ul final (nota mergea
doar în `llm_view`-ul primului tool), iar golden-ul trecea cu un răspuns fără nicio declinare.
Testele de aici sunt exact contra-proba.
"""

import pytest

from src.models import Reply, RichItem, RichReply
from src.safety import messages
from src.safety.compose import enforce, safety_sentence_for
from src.safety.contraindications import Block
from src.safety.policy import Decision


class _Ctx:
    def __init__(self, reply=None, decision=None, language="ro"):
        self.reply, self.safety_decision, self.language = reply, decision, language
        self.events = []

    def emit(self, type_, **props):
        self.events.append((type_, props))


def _blocked_decision(**over):
    d = {
        "kept": [],
        "blocked": [
            Block(
                product_id="p1",
                context_id="pregnancy",
                rule_id="pregnancy-retinoids",
                matched="retinol",
            )
        ],
        "contexts": ("pregnancy",),
        "rule_ids": ("pregnancy-retinoids",),
        "must_refer": True,
        "message_key": "safety.blocked",
    }
    d.update(over)
    return Decision(**d)


# --- fraza garantată ---------------------------------------------------------------------------


def test_sentence_acknowledges_context_says_what_was_left_out_and_refers():
    """Cele trei părți cerute, într-o singură propoziție naturală."""
    s = safety_sentence_for(_blocked_decision(), "ro")
    assert "ești însărcinată" in s  # 1. recunoaște contextul
    assert "retinoizi" in s  # 2. ce s-a lăsat deoparte
    assert "medicul sau farmacistul" in s  # 3. trimite la medic
    assert "Nu pot confirma" in s  # declinare onestă, nu afirmație medicală


def test_sentence_without_blocked_does_not_claim_filtering():
    """Context activ, nimic exclus → NU pretinde că a filtrat ceva (ar fi o minciună mică)."""
    s = safety_sentence_for(
        _blocked_decision(blocked=[], rule_ids=(), message_key="safety.ack"), "ro"
    )
    assert "Țin cont că ești însărcinată" in s
    assert "medicul sau farmacistul" in s
    assert "lăsat deoparte" not in s


def test_sentence_has_no_internal_jargon():
    """Fără „EXCLUS determinist" / „REGULI DURE" / majuscule de prompt (review Codex)."""
    s = safety_sentence_for(_blocked_decision(), "ro")
    for bad in ("EXCLUS", "REGULI DURE", "determinist", "CONTEXT DE SIGURANȚĂ", "llm", "rule_id"):
        assert bad not in s


def test_sentence_is_localized():
    assert "doctor or pharmacist" in safety_sentence_for(_blocked_decision(), "en")
    assert "orvosoddal" in safety_sentence_for(_blocked_decision(), "hu")
    # locale necunoscut → cade pe RO (nu gol, nu crapă)
    assert "medicul" in safety_sentence_for(_blocked_decision(), "de")


def test_no_sentence_without_context():
    assert safety_sentence_for(None, "ro") == ""
    assert safety_sentence_for(Decision(kept=[]), "ro") == ""


def test_unavailable_registry_sentence_is_honest():
    s = safety_sentence_for(_blocked_decision(unavailable=True), "ro")
    assert "nu pot verifica" in s.lower()
    assert "medicul sau farmacistul" in s


# --- enforce pe reply --------------------------------------------------------------------------


def test_enforce_prepends_sentence_to_reply():
    ctx = _Ctx(
        Reply(text="Îți recomand Ser Bakuchiol la 84.00 lei. Vrei linkul?"), _blocked_decision()
    )
    enforce(ctx)
    assert "medicul sau farmacistul" in ctx.reply.text
    assert "Ser Bakuchiol" in ctx.reply.text  # partea comercială a modelului rămâne
    assert ctx.reply.text.index("Țin cont") < ctx.reply.text.index("Ser Bakuchiol")


def test_enforce_is_idempotent():
    """Retry / re-render nu dublează fraza."""
    ctx = _Ctx(Reply(text="ceva"), _blocked_decision())
    enforce(ctx)
    first = ctx.reply.text
    enforce(ctx)
    assert ctx.reply.text == first
    assert first.count("medicul sau farmacistul") == 1


def test_enforce_does_not_duplicate_when_model_already_said_it():
    ctx = _Ctx(
        Reply(text="Verifică cu medicul sau farmacistul. Îți recomand X."), _blocked_decision()
    )
    enforce(ctx)
    assert ctx.reply.text.count("medicul sau farmacistul") == 1


def test_enforce_noop_without_decision():
    ctx = _Ctx(Reply(text="Îți recomand X"), None)
    enforce(ctx)
    assert ctx.reply.text == "Îți recomand X"


def test_enforce_noop_without_context():
    ctx = _Ctx(Reply(text="Îți recomand X"), Decision(kept=[]))
    enforce(ctx)
    assert ctx.reply.text == "Îți recomand X"


def test_enforce_marks_reply_non_cacheable():
    """Reply-ul cu frază de siguranță e relativ la ACEST client → nu se cache-uiește."""
    ctx = _Ctx(Reply(text="X", cacheable=True), _blocked_decision())
    enforce(ctx)
    assert ctx.reply.cacheable is False


def _rich(intro="Dintre opțiuni, astea se potrivesc:", items=None):
    return RichReply(
        intro=intro, items=items or [], pick=None, education=None, chips=[], disclaimer=""
    )


def test_enforce_puts_sentence_in_rich_intro_too():
    """Canalele bogate arată `rich.intro`; fără asta fraza apărea doar pe canalul sărac."""
    ctx = _Ctx(Reply(text="text aplatizat", rich=_rich()), _blocked_decision())
    enforce(ctx)
    assert "medicul sau farmacistul" in ctx.reply.rich.intro
    assert "medicul sau farmacistul" in ctx.reply.text


def test_enforce_scrubs_blocked_card_that_slipped_through():
    """Plasa finală: un id blocat ajuns în carduri (cale nouă care a uitat gate-ul) → scos."""
    ctx = _Ctx(
        Reply(
            text="X",
            products=[
                {"product_id": "p1", "name": "Retinol"},
                {"product_id": "ok", "name": "Safe"},
            ],
        ),
        _blocked_decision(),
    )
    enforce(ctx)
    assert [p["product_id"] for p in ctx.reply.products] == ["ok"]
    assert any(e[0] == "safety_card_scrubbed" for e in ctx.events)


def test_enforce_scrubs_blocked_rich_item():
    rich = _rich(
        items=[
            RichItem(product_id="p1", name="Retinol Ser", price=119.0),
            RichItem(product_id="ok", name="Bakuchiol Ser", price=84.0),
        ]
    )
    ctx = _Ctx(Reply(text="X", rich=rich), _blocked_decision())
    enforce(ctx)
    assert [it.product_id for it in ctx.reply.rich.items] == ["ok"]


def test_enforce_emits_event():
    ctx = _Ctx(Reply(text="X"), _blocked_decision())
    enforce(ctx)
    ev = next(e for e in ctx.events if e[0] == "safety_sentence_enforced")
    assert ev[1]["contexts"] == ["pregnancy"]


# --- catalogul de mesaje -----------------------------------------------------------------------


@pytest.mark.parametrize("locale", ["ro", "en", "hu"])
def test_refer_sentence_exists_for_every_supported_locale(locale):
    assert messages.refer_sentence(locale)
    assert messages.unavailable_sentence(locale)
    assert messages.context_label("pregnancy", locale)
    assert messages.omission_label("pregnancy-retinoids", locale)


def test_unknown_rule_falls_back_to_generic_omission():
    """Regulă nouă în registru fără copy dedicat → frază generică, NU cheia brută."""
    lbl = messages.omission_label("some-new-rule", "ro")
    assert lbl and "some-new-rule" not in lbl


# --- „o singură dată" pe formele REALE produse de model (regresie live) -------------------------


@pytest.mark.parametrize(
    "model_text",
    [
        # observat live: modelul scrie la GENITIV → amprenta pe nominativ nu potrivea și codul mai
        # adăuga una peste → avertisment de două ori (exact repetiția interzisă de contract).
        "Fiind însărcinată, n-aș recomanda retinol fără acordul medicului sau farmacistului.",
        "În sarcină, decizia o ia medicul sau farmacistul.",
        "Întreabă un farmacist înainte.",
        "Discută cu medicul ori cu farmacista ta.",
    ],
)
def test_never_duplicates_warning_whatever_form_the_model_used(model_text):
    ctx = _Ctx(Reply(text=model_text), _blocked_decision())
    enforce(ctx)
    low = ctx.reply.text.lower()
    assert low.count("farmacist") == 1, f"avertisment repetat: {ctx.reply.text!r}"


def test_still_adds_when_model_only_says_doctor_without_pharmacist():
    """Modelul a zis doar «medic» → nu e trimiterea noastră completă; codul o pune (o dată)."""
    ctx = _Ctx(Reply(text="Nu pot recomanda asta."), _blocked_decision())
    enforce(ctx)
    assert ctx.reply.text.lower().count("farmacist") == 1


@pytest.mark.parametrize("locale,stem", [("en", "pharmacist"), ("hu", "gyógyszerész")])
def test_no_duplicate_in_other_locales(locale, stem):
    ctx = _Ctx(Reply(text=f"Please ask your {stem}."), _blocked_decision(), language=locale)
    enforce(ctx)
    assert ctx.reply.text.lower().count(stem.lower()) == 1
