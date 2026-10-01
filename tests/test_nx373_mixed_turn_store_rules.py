"""NX-373 — pe un tur mixt (produse + o regulă a magazinului) partea de magazin e a codului.

Rularea pe producție din 2026-10-01 (`tasks/stage1/KERNEL-LIVE-2026-10-01.md`, clasa A2, k7 T1):
«arata-mi un cushion pentru ten gras si spune-mi cat costa livrarea». Runda 1 a chemat
`search_products` ȘI `faq_lookup`, proza rundei 2 avea regula de livrare corectă, iar compunerea
bogată (care primește doar produsele) a scris „Nu am informații despre costul livrării.", servit
clientului. Acum: regulile pe care proza le redă și pe care clientul le-a întrebat se adaugă în
cuvintele magazinului, propozițiile compunerii despre subiectul de magazin care nu numesc un produs
ies, iar compunerea primește nota că partea de magazin nu e a ei.

Textele sunt cele REALE ale turului (`model_io`): proza rundei 2 și intro-ul compunerii.
"""

from __future__ import annotations

import pytest

from src.agent.finalize import MIXED_STORE_NOTE, render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.agent.store_rules import drop_store_sentences
from src.config import get_settings
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    RetrievalResult,
    TurnContext,
)
from src.worker.runner import PipelineDeps

CLIENT = "arata-mi un cushion pentru ten gras si spune-mi cat costa livrarea"
#: Regulile SOLE (`db/seed/faqs_sole_ro.json`), cum le aduce `faq_lookup`.
DELIVERY = (
    "Taxa de livrare e între 19,9 și 24,90 lei cu TVA inclus, iar suma exactă o vezi la "
    "finalizarea comenzii. Peste 199 lei transportul e gratuit, iar dacă ai mai comandat la noi "
    "pragul scade la 149 lei."
)
RETURN = "Ai 30 de zile de la cumpărare să returnezi un produs."
QUESTIONS = {DELIVERY: "Cât costă livrarea?", RETURN: "Cât timp am pentru retur?"}
#: Proza rundei 2 (`model_io`, apelul 3 al turului `08b07b7c`).
PROSE = (
    "Pentru ten gras, aș lua în calcul aceste cushion-uri cu finish matifiant:\n\n"
    "• TIRTIR Mask Fit Red Cushion, 21C Cool Ivory, 135 lei, ★ 5.0. Are pudră matifiantă și "
    "ajută la controlul sebumului.\n"
    "• MUZIGAE MANSION Sleek Matt Cushion, N19, 190 lei, ★ 5.0. Are finish mat și SPF 50+ "
    "PA++++.\n\n"
    "Livrarea costă între 19,90 și 24,90 lei, iar suma exactă apare la finalizarea comenzii. E "
    "gratuită peste 199 lei, sau peste 149 lei dacă ai mai comandat la SOLE."
)
#: Intro-ul compunerii bogate pe același tur (apelul 4, `sales_recommendation`).
RICH_INTRO = (
    "Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere și acoperire, iar "
    "MUZIGAE MANSION Sleek Matt Cushion adaugă protecție solară. Nu am informații despre costul "
    "livrării."
)
DENIAL = "Nu am informații despre costul livrării."
PRODUCTS = [
    {
        "id": "tirtir",
        "name": "TIRTIR Mask Fit Red Cushion - fond de ten compact cu pudra matifianta",
        "brand": "TIRTIR",
        "price": 135.0,
        "url": "https://shop/tirtir",
        "product_url": "https://shop/tirtir",
        "availability": "in_stock",
    },
    {
        "id": "muzigae",
        "name": "MUZIGAE MANSION Sleek Matt Cushion - fond de ten compact cu SPF 50+",
        "brand": "MUZIGAE MANSION",
        "price": 190.0,
        "url": "https://shop/muzigae",
        "product_url": "https://shop/muzigae",
        "availability": "in_stock",
    },
]


class _LLM:
    """Compunerea bogată REUȘITĂ, cu intro-ul real al turului. Ține minte mesajul primit."""

    def __init__(self, *, intro: str = RICH_INTRO, fail: bool = False):
        self.intro = intro
        self.fail = fail
        self.user: str | None = None

    async def complete(self, system, user, *, model=None):
        return ""

    async def complete_schema(self, system, user, schema, *, model=None):
        self.user = user
        if self.fail:
            raise RuntimeError("timeout")
        return {
            "intro": self.intro,
            "items": [
                {"product_id": "P1", "pro_index": 0, "fit_clause": "Pentru ten gras."},
                {"product_id": "P2", "pro_index": 0, "fit_clause": "Cu protecție solară."},
            ],
            "pick": None,
            "education": None,
            "suggestions": [],
        }


def _ctx(body: str = CLIENT) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body=body),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.retrieval = RetrievalResult(products=list(PRODUCTS))
    return ctx


def _plan(*, final: str = PROSE, sources=(DELIVERY, RETURN), query: str = CLIENT) -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[dict(p) for p in PRODUCTS],
        final=final,
        is_order=False,
        query=query,
        history="",
        inp=PromptInputs.build("D", "ecommerce", "ro", ["Fata"], []),
        mode="rich",
        grounded_sources=list(sources),
        grounded_questions={k: v for k, v in QUESTIONS.items() if k in sources},
    )


async def _render(llm: _LLM, plan: ResponsePlan, ctx: TurnContext | None = None) -> TurnContext:
    ctx = ctx or _ctx()
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), plan)
    return ctx


def _events(ctx, name):
    return [e for e in ctx.events if getattr(e, "type", None) == name]


# --- pe turul real ---------------------------------------------------------------------------


async def test_the_real_mixed_turn_serves_the_delivery_rule_in_the_store_words():
    ctx = await _render(_LLM(), _plan())
    intro = ctx.reply.rich.intro
    assert DELIVERY in intro  # regula întreagă, în cuvintele magazinului
    assert DENIAL not in intro and DENIAL not in ctx.reply.text  # negarea compunerii a ieșit
    assert intro.startswith("Pentru ten gras, TIRTIR Mask Fit Red Cushion")  # partea de produs
    assert RETURN not in intro  # regula pe care clientul n-a întrebat-o nu se servește
    assert len(ctx.reply.rich.items) == 2
    [event] = _events(ctx, "store_rules_appended")
    assert event.properties == {**event.properties, "n": 1, "dropped": 1, "path": "rich"}


async def test_the_composition_is_told_the_store_part_is_not_its_own():
    llm = _LLM()
    await _render(llm, _plan())
    assert MIXED_STORE_NOTE in llm.user


async def test_a_product_turn_without_store_rules_is_untouched():
    llm = _LLM(intro="Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere.")
    ctx = await _render(llm, _plan(sources=()), _ctx("arata-mi un cushion pentru ten gras"))
    assert (
        ctx.reply.rich.intro
        == "Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere."
    )
    assert MIXED_STORE_NOTE not in llm.user
    assert not _events(ctx, "store_rules_appended")


async def test_rules_the_prose_did_not_use_are_not_served():
    """Turul a citit regulile, dar proza a vorbit doar despre produse: nimic de adăugat."""
    prose = PROSE.split("\n\nLivrarea")[0]
    llm = _LLM(intro="Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere.")
    ctx = await _render(llm, _plan(final=prose))
    assert DELIVERY not in ctx.reply.rich.intro
    assert MIXED_STORE_NOTE not in llm.user


async def test_a_rule_the_client_did_not_ask_about_is_not_served():
    """Proza redă regula de livrare, dar clientul n-a întrebat de livrare (testul NX-369)."""
    ctx = await _render(
        _LLM(),
        _plan(query="arata-mi un cushion pentru ten gras"),
        _ctx("arata-mi un cushion pentru ten gras"),
    )
    assert DELIVERY not in ctx.reply.rich.intro


async def test_the_rules_survive_when_the_cards_come_from_the_catalog():
    """Compunerea bogată pică (NX-302: cardurile din catalog); partea de magazin rămâne."""
    ctx = await _render(_LLM(fail=True), _plan())
    rich = ctx.reply.rich
    assert rich is not None and rich.items
    assert DELIVERY in (rich.intro or "")
    [event] = _events(ctx, "store_rules_appended")
    assert event.properties["path"] == "facts"


async def test_the_kill_switch_restores_the_old_composition(monkeypatch):
    monkeypatch.setattr(get_settings(), "mixed_turn_store_rules_enabled", False)
    llm = _LLM()
    ctx = await _render(llm, _plan())
    assert ctx.reply.rich.intro == RICH_INTRO
    assert MIXED_STORE_NOTE not in llm.user


# --- recenzia adversarială: căile de text rămase ------------------------------------------------


async def test_the_rule_is_not_served_twice_on_the_catalog_path():
    """C4: proza citează o propoziție a regulii literal (trece NX-346) și devine intro pe
    recuperarea din catalog; regula întreagă se adaugă, iar propoziția nu apare de două ori."""
    second = (
        "Peste 199 lei transportul e gratuit, iar dacă ai mai comandat la noi pragul scade la "
        "149 lei."
    )
    prose = f"Pentru ten gras, TIRTIR Mask Fit Red Cushion are finish mat. {second}"
    ctx = await _render(_LLM(fail=True), _plan(final=prose))
    intro = ctx.reply.rich.intro
    assert intro.count(second) == 1 and DELIVERY in intro


async def test_the_rule_survives_a_refused_set():
    """C5: modelul refuză setul nou (`no-items-selected`); răspunsul de magazin rămâne."""
    from src.models import Relevance

    class _Refuses(_LLM):
        async def complete_schema(self, system, user, schema, *, model=None):
            self.user = user
            return {"intro": "", "items": [], "pick": None, "education": None, "suggestions": []}

    ctx = _ctx()
    ctx.retrieval = RetrievalResult(products=list(PRODUCTS), relevance=Relevance(relaxed=False))
    await _render(_Refuses(), _plan(), ctx)
    assert DELIVERY in ctx.reply.text


async def test_the_rules_go_before_a_closing_question():
    """P1: întrebarea de îngustare (NX-315) rămâne ultima frază a `intro`."""
    intro = (
        "Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere. Îl vrei mat sau "
        "satinat?"
    )
    ctx = await _render(_LLM(intro=intro), _plan())
    text = ctx.reply.rich.intro
    assert text.endswith("Îl vrei mat sau satinat?")
    assert text.index(DELIVERY) < text.index("Îl vrei mat sau satinat?")


async def test_the_store_text_passes_the_voice_net():
    """P2: o regulă a comerciantului cu punct și virgulă ajunge la widget fără el (P13)."""
    rule = "Livrarea costă 19,90 lei; peste 199 lei e gratuită."
    prose = (
        "TIRTIR Mask Fit Red Cushion e mat. Livrarea costă 19,90 lei și peste 199 lei e gratuită."
    )
    plan = _plan(final=prose, sources=(rule,))
    plan.grounded_questions = {rule: "Cât costă livrarea?"}
    ctx = await _render(_LLM(), plan)
    assert ";" not in ctx.reply.rich.intro and "19,90 lei" in ctx.reply.rich.intro


# --- `drop_store_sentences`, pur ----------------------------------------------------------------


NAMES = [p["name"] for p in PRODUCTS]


def _drop(text, rules=(DELIVERY,), names=NAMES):
    return drop_store_sentences(text, list(rules), "ro", questions=QUESTIONS, names=names)


def test_a_store_sentence_without_a_product_goes():
    text, dropped = _drop(RICH_INTRO)
    assert dropped == 1 and DENIAL not in text and text.startswith("Pentru ten gras")


@pytest.mark.parametrize(
    "sentence",
    [
        "Toate trei costă sub 100 de lei.",  # C1: jumătate exact, propoziție de produs
        "Prima costă 135 lei, a doua 190 lei.",
        "Pentru ten sensibil, ambele creme au ingrediente blânde.",
        "TIRTIR Mask Fit Red Cushion costă 135 lei și se potrivește tenului gras.",
    ],
)
def test_product_sentences_stay(sentence):
    assert _drop(sentence) == (sentence, 0)


def test_a_denial_in_the_rule_own_words_goes():
    """C2: «taxa», «transport» sunt cuvintele regulii, nu ale întrebării clientului."""
    assert _drop("Nu am detalii despre taxa de transport.") == ("", 1)


def test_lists_and_line_breaks_survive_a_drop():
    """C3: propoziția iese pe loc; marcajele și rândurile rămase nu se ating (lecția NX-299)."""
    kept = "Am ales:\n\n• COSRX ceva bun.\n• Beauty altceva."
    assert _drop(kept + "\n\n" + DENIAL) == (kept, 1)


def test_without_rules_nothing_goes():
    assert _drop(RICH_INTRO, rules=()) == (RICH_INTRO, 0)


#: Un magazin de electronice: vocabularul magazinului e al regulii lui, nu al cosmeticelor.
WARRANTY = "Garanția e de 24 de luni pentru toate produsele electronice."
WARRANTY_Q = {WARRANTY: "Cât durează garanția?"}
HEADPHONES = ["Căștile Aurora Pro"]


def test_the_store_vocabulary_is_the_store_data_on_another_domain():
    intro = "Căștile Aurora sunt wireless și au bas puternic. Nu știu cât durează garanția."
    text, dropped = drop_store_sentences(
        intro, [WARRANTY], "ro", questions=WARRANTY_Q, names=HEADPHONES
    )
    assert dropped == 1 and text == "Căștile Aurora sunt wireless și au bas puternic."


def test_declared_gap_a_two_word_denial_with_a_generic_word_stays():
    """Declarat (cardul NX-373): «Nu am informații despre garanție» are un cuvânt al magazinului din
    două, deci nu are majoritate. Pragul invers ar scoate «Toate trei costă sub 100 de lei». Aici
    garda e nota către compunere, măsurată prin `store_rules_appended.dropped`."""
    intro = "Căștile Aurora sunt wireless. Nu am informații despre garanție."
    assert drop_store_sentences(
        intro, [WARRANTY], "ro", questions=WARRANTY_Q, names=HEADPHONES
    ) == (intro, 0)


def test_a_locale_without_word_tables_keeps_the_text():
    """Fără tabelul de cuvinte goale al locale-i (pilotul e `ro`), fiecare cuvânt numără, deci
    proporția scade și filtrul păstrează: eroarea e de partea textului păstrat."""
    rule = "Shipping costs 5 dollars, and it is free over 50 dollars."
    intro = "The Aurora headphones are wireless. I have no information about shipping costs."
    text, dropped = drop_store_sentences(
        intro, [rule], "en", questions={rule: "How much does shipping cost?"}, names=HEADPHONES
    )
    assert (text, dropped) == (intro, 0)


# --- recenzia adversarială 2 (F1-F4) ------------------------------------------------------------


def _store_text(text: str, plan: ResponsePlan | None = None) -> str:
    from src.agent.finalize import _store_text as store_text

    return store_text(_ctx(), plan or _plan(), text, (DELIVERY,), "prose")


async def test_f1_a_closing_question_with_a_dotted_brand_stays_after_the_product_text():
    """F1: «DR.JART+» are un punct, deci vechea despărțire lua tot textul drept întrebare și punea
    regula ÎNAINTEA textului de produs."""
    question = "Îl preferi pe acesta sau pe DR.JART+ Cicapair?"
    intro = f"Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere. {question}"
    ctx = await _render(_LLM(intro=intro), _plan())
    text = ctx.reply.rich.intro
    assert text.index("Pentru ten gras") < text.index(DELIVERY) < text.index(question)
    assert text.endswith(question)


async def test_f1_a_lone_question_keeps_the_rule_after_it():
    """F1: un intro care e doar o întrebare despre produse nu primește regula în față."""
    question = "Îl preferi pe TIRTIR sau pe DR.JART+ Cicapair?"
    ctx = await _render(_LLM(intro=question), _plan())
    text = ctx.reply.rich.intro
    assert text.startswith(question) and text.index(question) < text.index(DELIVERY)


def test_f1_a_bullet_list_then_a_question_keeps_the_list_first():
    """F1: forma lui `_deterministic_reply` (listă fără punctuație finală, apoi întrebarea)."""
    reply = (
        "Îți recomand:\n• TIRTIR Mask Fit Red Cushion, 135 lei\n"
        "• MUZIGAE MANSION Sleek Matt Cushion, 190 lei\nVrei detalii sau linkul la vreunul?"
    )
    text = _store_text(reply)
    assert text.index("190 lei") < text.index(DELIVERY) < text.index("Vrei detalii")
    assert text.startswith("Îți recomand:\n• TIRTIR")


def test_f1_an_unsure_split_puts_the_rule_after_the_whole_text():
    """F1: «DR. JART» are punct urmat de spațiu; despărțirea nu e sigură, deci regula merge la
    sfârșit, niciodată în mijlocul unei propoziții despre produs."""
    reply = "Am ales TIRTIR pentru tine.\nCe zici de DR. JART Cicapair?"
    text = _store_text(reply)
    assert text.index("Ce zici de DR. JART Cicapair?") < text.index(DELIVERY)


async def test_f2_without_prose_the_asked_rule_is_still_served():
    """F2: `faq_lookup` în runda 1, `search_products` în runda 2, runda de proză sărită: proza e
    goală, iar regula cerută se ia din sursele turului, după mesajul clientului."""
    llm = _LLM()
    ctx = await _render(llm, _plan(final=""))
    intro = ctx.reply.rich.intro
    assert DELIVERY in intro and DENIAL not in intro and RETURN not in intro
    assert MIXED_STORE_NOTE in llm.user


async def test_f2_without_prose_a_rule_nobody_asked_about_is_not_served():
    ctx = await _render(
        _LLM(intro="Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere."),
        _plan(final="", query="arata-mi un cushion pentru ten gras"),
        _ctx("arata-mi un cushion pentru ten gras"),
    )
    assert DELIVERY not in ctx.reply.rich.intro


def test_f2_a_turn_that_read_store_rules_keeps_its_prose_round(monkeypatch):
    """F2, cauza: decizia NX-312 era pe RUNDĂ. Runda 2 a chemat doar căutarea, deci runda de proză
    se sărea, deși runda 1 citise regulile magazinului. Acum decizia e pe tur."""
    from types import SimpleNamespace

    from src.agent import turn_profile
    from src.worker.stages.agent import _ProseRoundGate

    monkeypatch.setattr(turn_profile, "name_for_turn", lambda ctx: "recommend")
    run = SimpleNamespace(retrieved=[])
    gate = _ProseRoundGate(SimpleNamespace(), run, is_order=False)
    assert gate(["faq_lookup"]) is False
    run.retrieved = [dict(PRODUCTS[0])]
    assert gate(["search_products"]) is False and gate.reason == "other_tool"


def test_f2_search_only_rounds_still_skip(monkeypatch):
    """NX-312 rămâne: o căutare fără rezultat, apoi una cu rezultat, sare runda de proză."""
    from types import SimpleNamespace

    from src.agent import turn_profile
    from src.worker.stages.agent import _ProseRoundGate

    monkeypatch.setattr(turn_profile, "name_for_turn", lambda ctx: "recommend")
    run = SimpleNamespace(retrieved=[])
    gate = _ProseRoundGate(SimpleNamespace(), run, is_order=False)
    assert gate(["search_products"]) is False
    run.retrieved = [dict(PRODUCTS[0])]
    assert gate(["search_products"]) is True and gate.reason == "search_only"


def _sole_faqs() -> dict[str, str]:
    """Setul REAL de reguli SOLE (`faq_lookup` îl aduce întreg): răspuns → întrebare."""
    import json
    from pathlib import Path

    raw = json.loads(Path("db/seed/faqs_sole_ro.json").read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("faqs", raw)
    return {f["answer"].strip(): f["question"].strip() for f in items}


@pytest.mark.parametrize(
    ("client", "question"),
    [
        (CLIENT, "Cât costă livrarea?"),
        ("arata-mi un ser si zi-mi in cat timp primesc comanda", "În cât timp primesc comanda?"),
        ("vreau o crema, pot plati in rate?", "Pot plăti în rate?"),
        ("cat costa un cushion bun pentru ten gras?", None),
        ("arata-mi un cushion pentru ten gras", None),
    ],
)
def test_f2_asked_rule_on_the_whole_sole_set(client, question):
    """Fără proză, din cele 20 de reguli SOLE se alege UNA, cea întrebată, sau niciuna."""
    from src.agent.store_rules import asked_rule

    faqs = _sole_faqs()
    got = asked_rule(list(faqs), "ro", client=client, questions=faqs)
    assert [faqs[a] for a in got] == ([question] if question else [])


PRICES = [p["price"] for p in PRODUCTS]


@pytest.mark.parametrize(
    "sentence",
    [
        "Costă 135 lei.",
        "Prima costă 149 lei.",
        "Ambele costă sub 199 lei.",
        "Prețul e 135 lei, cu TVA inclus.",
    ],
)
def test_f3_product_price_sentences_stay(sentence):
    """F3: un cuvânt de preț («costă»), o sumă sau „TVA inclus” nu fac din ea o regulă."""
    got = drop_store_sentences(
        sentence, [DELIVERY], "ro", questions=QUESTIONS, names=NAMES, prices=PRICES
    )
    assert got == (sentence, 0)


def test_f3_an_invented_store_amount_still_goes():
    """O regulă inventată («Livrarea costă 15 lei») e a magazinului: regula reală o înlocuiește."""
    got = drop_store_sentences(
        "Livrarea costă 15 lei.", [DELIVERY], "ro", questions=QUESTIONS, names=NAMES, prices=PRICES
    )
    assert got == ("", 1)


async def test_f4_a_product_price_question_is_not_a_store_question():
    """F4: «cât costă un cushion» împarte cu «Cât costă livrarea?» doar „costă”. Subiectul regulii
    («livrare») lipsește, deci regula nu se servește."""
    client = "cat costa un cushion bun pentru ten gras?"
    ctx = await _render(
        _LLM(intro="Pentru ten gras, TIRTIR Mask Fit Red Cushion pune accent pe matifiere."),
        _plan(query=client),
        _ctx(client),
    )
    assert DELIVERY not in ctx.reply.rich.intro and DELIVERY not in ctx.reply.text


def test_f4_match_rules_on_the_rule_subject():
    from src.agent.store_rules import match_rules

    product = "cat costa un cushion bun pentru ten gras?"
    kw = dict(questions=QUESTIONS, topical=True)
    assert match_rules(PROSE, [DELIVERY, RETURN], "ro", client=product, **kw).rules == ()
    assert match_rules(PROSE, [DELIVERY, RETURN], "ro", client=CLIENT, **kw).rules == (DELIVERY,)
    # fără `topical`, testul NX-369 rămâne cel de dinainte (orice rădăcină comună)
    assert match_rules(PROSE, [DELIVERY], "ro", client=product, questions=QUESTIONS).rules == (
        DELIVERY,
    )
