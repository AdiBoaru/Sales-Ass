"""Creierul unic livrează RĂSPUNS BOGAT, nu carduri goale.

Regresia pe care o pinuiește fișierul: aprinderea `SINGLE_BRAIN_ENABLED` muta răspunsul de pe
ramura `rich` a lui `channels/web/render.py` pe ramura `products`, fiindcă `brain.py` chema
`set_reply` iar `set_rich_reply` avea doi apelanți, amândoi pe calea v1. Cardurile rămâneau REALE
(produse și prețuri din catalog), deci nicio poartă din aval nu putea protesta: validatorul
(stagiul 8) și `grounding_guard` judecă ADEVĂRUL, nu FORMA. Se pierdeau doar `reason`, `rating`,
`review_count`, `badge`, `list_price`, `currency`, `details` și chips-urile.

Al doilea defect, în aceeași ramură: `_card` citește `product_id`, retrievalul scrie `id`, deci
cardurile plecau la widget cu identitate `null`. Testele de stare nu-l vedeau, fiindcă
`processor._displayed_product_refs` are fallback pe `id`; sârma nu are.

Fiecare test de aici trece prin `render_web`, adică prin CONTRACTUL pe care îl citește widgetul,
nu prin forma internă a lui `RichReply`: defectul era invizibil tocmai la nivelul de deasupra.

ZERO OpenAI / ZERO DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.brain_rich import card_refs, plan_item_refs, rich_from_plan
from src.channels.web.render import render_web
from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from tests.nx240_helpers import BUSINESS_ID, PID_A, plan, row

PID_B = "11111111-2222-3333-4444-555555555555"

# Proza creierului: a trecut deja `validate_revised_draft`, criticul și `grounding_guard`. Poartă
# un preț DELIBERAT — e exact ce ar arunca `scrub_intro`, care nu cunoaște decât cifrele
# clientului și pe cele de specificație.
BRAIN_TEXT = "Pentru ten uscat îți recomand serul LumaDerm, 89,00 lei."


def _ctx() -> TurnContext:
    ctx = TurnContext(
        turn_id="t-rich",
        business=BusinessConfig(id=BUSINESS_ID, slug="demo", name="Demo"),
        contact=Contact(id="c", business_id=BUSINESS_ID),
        message=InboundMessage(provider_msg_id="m", body="ceva pentru ten uscat"),
        conversation_id="conv",
        state=ConversationState(),
        language="ro",
    )
    return ctx


def _run(coro):
    """`_set_brain_reply` e async; testele de aici sunt sincrone, fără event loop viu."""
    import asyncio

    return asyncio.run(coro)


def _rendered(ctx: TurnContext, **kwargs):
    """Planul → `RichReply` → reply → contractul widgetului. Drumul complet, ca în producție."""
    kwargs.setdefault("text", BRAIN_TEXT)
    rich = rich_from_plan(ctx, kwargs.pop("plan_obj", plan()), kwargs.pop("retrieved"), **kwargs)
    assert rich is not None
    from src.worker import compose

    ctx.set_rich_reply(rich, text=BRAIN_TEXT, products=compose.card_products(rich.items))
    return render_web(ctx.reply, "ro")


def test_cardul_bogat_poarta_exact_campurile_pe_care_ramura_saraca_le_pierdea():
    ctx = _ctx()
    out = _rendered(ctx, retrieved=[row()])

    card = out["products"][0]
    # Identitatea: defectul tăcut. Fără ea widgetul n-are pe ce lega coșul sau acțiunile NX-236.
    assert card["product_id"] == PID_A
    # Motivul per card = `recommendations[].reason` din plan, validat ca orice claim prin `to_v1()`.
    assert "acid hialuronic" in card["reason"]
    assert card["rating"] == pytest.approx(4.75)
    assert card["review_count"] == 120
    # Preț tăiat: `list_price` iese DOAR la reducere reală (120 > 89).
    assert card["list_price"] == pytest.approx(120.0)
    # `currency` vine din `DomainPack`, nu din rând — tenantul de test n-are pachet, deci cheia
    # lipsește (degradare grațioasă: `_card` nu inventează `null`-uri). E ACELAȘI `compose.assemble`
    # ca pe v1, deci moneda curge identic acolo unde pachetul există.
    assert "currency" not in card


def test_proza_creierului_ramane_intro_ul_si_nu_se_mai_trece_o_data_prin_scrub():
    """Textul a trecut porți STRICT mai tari decât `scrub_intro`. Re-scrubuit, un răspuns corect
    care numește un preț ar fi aruncat ÎNTREG: șase carduri pe ecran și zero text."""
    ctx = _ctx()
    out = _rendered(ctx, retrieved=[row()])

    assert "89,00 lei" in out["content"]
    assert "LumaDerm" in out["content"]


def test_fara_produse_turul_ramane_proza():
    """Clarificare pură sau refuz onest: `None`, deci apelantul cade pe `set_reply`. Niște carduri
    sub un refuz ar fi un răspuns care se contrazice singur."""
    ctx = _ctx()
    empty = plan(selected_products=(), recommendations=(), obligations=())

    assert rich_from_plan(ctx, empty, [row()], text=BRAIN_TEXT) is None


def test_produsele_selectate_fara_motiv_raman_carduri():
    """`_plan_products`, codul înlocuit, citea `selected_products`. Un tur poate selecta fără să
    motiveze („ce ai pe raftul X" e o listă, nu o pledoarie) — acelea nu au voie să dispară."""
    listing = plan(recommendations=())

    refs = plan_item_refs(listing)

    assert [r["product_id"] for r in refs] == [PID_A]
    assert "fit_clause" not in refs[0]


def test_recomandarea_si_selectia_aceluiasi_produs_nu_consuma_doua_sloturi():
    both = plan()  # helperul selectează ȘI recomandă acelaşi PID_A

    assert [r["product_id"] for r in plan_item_refs(both)] == [PID_A]


def test_un_id_care_nu_e_in_retrieval_nu_devine_card():
    """Poarta de apartenență a lui `compose.assemble` rămâne a ei; aici doar verificăm că un set
    golit de ea nu produce un contract bogat fără nimic bogat în el."""
    ctx = _ctx()
    strain = plan(
        selected_products=(),
        recommendations=(),
        obligations=(),
    )

    assert rich_from_plan(ctx, strain, [row(PID_B)], text=BRAIN_TEXT) is None


def test_chipsurile_ajung_la_widget_ca_sugestii():
    ctx = _ctx()
    out = _rendered(
        ctx,
        retrieved=[row()],
        text=BRAIN_TEXT,
        suggestions=("Am tenul uscat", "Pentru riduri"),
    )

    assert out["suggestions"] == ["Am tenul uscat", "Pentru riduri"]


def test_card_refs_arunca_randul_fara_identitate_in_loc_sa_l_trimita_mut():
    """Un card fără `product_id` nu e un card cu un câmp lipsă, e unul pe care nimic nu se leagă —
    și arată apăsabil. Mai bine absent."""
    rows = [row(), {"name": "Fără id", "price": 10.0}, row(PID_B, price=None)]

    cards = card_refs(rows)

    assert [c["product_id"] for c in cards] == [PID_A]
    assert cards[0]["price"] == pytest.approx(89.0)


def test_ordinea_cardurilor_urmeaza_proza_nu_doar_rankingul():
    """Defectul măsurat pe trafic în 2026-08-26: textul numea produsele într-o ordine, cardurile
    veneau în alta, iar clientul citea „al doilea" și găsea altceva. `assemble` are deja plasa
    (`_order_by_first_mention`), dar ea se uită în `j["intro"]` — pasat gol, nu reordona nimic."""
    ctx = _ctx()
    # Rankingul dă întâi A; proza numește întâi B.
    retrieved = [row(), row(PID_B, name="Cremă barieră NovaSkin")]
    two = plan(
        selected_products=(),
        obligations=(),
        recommendations=(
            plan().recommendations[0],
            plan().recommendations[0].model_copy(update={"product_id": PID_B}),
        ),
    )
    # Numele se potrivesc pe PREFIXE de cuvinte (`_mention_index`), ca în proza reală a modelului.
    text = "Cremă barieră NovaSkin ține bariera, iar Ser hidratant LumaDerm hidratează."

    rich = rich_from_plan(ctx, two, retrieved, text=text)

    assert [it.product_id for it in rich.items] == [PID_B, PID_A]
    # Contra-proba: fără proză, ordinea rămâne a rankingului — deci testul chiar măsoară mențiunea.
    blind = rich_from_plan(ctx, two, retrieved, text="Am două opțiuni bune.")
    assert [it.product_id for it in blind.items] == [PID_A, PID_B]


def test_comparatia_iese_ca_TABEL_nu_ca_recomandare(monkeypatch):
    """`run.compared` e populat de `compare_products` pe ACELAȘI ToolRun ca pe v1. Brain-ul nu-l
    citea, deci „compară-le" ieșea ca proză cu carduri — adică RE-recomandare, exact bug-ul pe care
    ordinea ramurilor din `finalize.render` îl prevenea."""
    from src.agent.brain import _set_brain_reply

    ctx = _ctx()
    compared = [row(), row(PID_B, name="Cremă barieră NovaSkin", price=59.0)]
    run = SimpleNamespace(retrieved=compared, compared=compared, checkout_url=None)

    _run(_set_brain_reply(ctx, SimpleNamespace(), plan(), run, BRAIN_TEXT))

    assert ctx.reply is not None
    assert ctx.reply.comparison is not None
    out = render_web(ctx.reply, "ro")
    assert "comparison" in out
    assert len(out["comparison"]["columns"]) == 2
    # Leadul rămâne al creierului: tabelul e al datelor, proza e a singurului writer semantic.
    assert out["content"].startswith(BRAIN_TEXT.split(",")[0])


def test_linkul_de_checkout_creat_in_tur_ajunge_ca_CTA():
    """NX-137 pe calea creierului unic: validatorul verifică doar că linkurile SCRISE sunt reale,
    niciodată că cel CREAT a fost scris. Fără CTA, un link creat în DB moare tăcut."""
    from src.agent.brain import _set_brain_reply

    ctx = _ctx()
    run = SimpleNamespace(
        retrieved=[row()], compared=[], checkout_url="https://shop.example/cart/abc"
    )

    _run(_set_brain_reply(ctx, SimpleNamespace(), plan(), run, BRAIN_TEXT))

    assert ctx.reply is not None and ctx.reply.offer is not None
    assert ctx.reply.offer.url == "https://shop.example/cart/abc"
    assert ctx.reply.offer.kind == "open_url"


def test_chipsurile_ajung_si_pe_ramura_fara_carduri(monkeypatch):
    """Pe v1, un no-result de sales primea căi de continuare. Un răspuns fără rezultate nu e o
    fundătură nici aici — doar că frazele vin din catalog, nu din copy generic."""
    import src.agent.brain as brain_mod
    from src.agent.brain import _set_brain_reply

    async def _menu_chips(ctx, deps):
        return ("Am tenul uscat", "Pentru riduri")

    monkeypatch.setattr(brain_mod, "_clarify_chips", _menu_chips)
    ctx = _ctx()
    empty = plan(selected_products=(), recommendations=(), obligations=())
    run = SimpleNamespace(retrieved=[], compared=[], checkout_url=None)

    _run(_set_brain_reply(ctx, SimpleNamespace(), empty, run, "N-am găsit nimic potrivit."))

    assert ctx.reply is not None and ctx.reply.rich is None
    assert ctx.reply.suggestions == ["Am tenul uscat", "Pentru riduri"]


def test_killswitch_stins_pastreaza_proza_dar_nu_cardurile_mute(monkeypatch):
    """Flagul e pentru FORMA bogată, nu pentru dreptul de a trimite carduri fără identitate."""
    from src.agent.brain import _set_brain_reply

    monkeypatch.setattr(get_settings(), "brain_rich_reply_enabled", False)
    ctx = _ctx()
    run = SimpleNamespace(retrieved=[row()], compared=[], checkout_url=None)

    _run(_set_brain_reply(ctx, SimpleNamespace(), plan(), run, BRAIN_TEXT))

    assert ctx.reply is not None
    assert ctx.reply.rich is None  # ramura săracă, ca înainte
    assert [p["product_id"] for p in ctx.reply.products] == [PID_A]
