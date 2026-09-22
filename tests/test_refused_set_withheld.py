"""NX-306 — un set REFUZAT de model, care era deja un compromis, nu ajunge pe ecran.

Regresia pinuită vine dintr-un tur REAL (`sole-ro`, conversația `70da107c`, turul `78b347fa`,
2026-09-21 08:35). Clientul întrebase «cat cost una?» despre mănușa de aplicare pe care botul
tocmai o pomenise. Căutarea a nimerit raftul greșit (`category="accesorii"`, adică
`Machiaj > Accesorii`), scara de relaxare a renunțat la filtrul de tip ca să iasă ceva, iar
rezultatul au fost seturi de pensule de 1.300 lei. Modelul a scris ONEST că nu sunt potrivite, și
codul le-a atașat oricum: patru carduri sub un text care le nega.

Nimic din aval nu putea prinde asta — produsele și prețurile erau REALE, deci stagiul 8 și
`grounding_guard` le-au lăsat să treacă. Sunt porți de ADEVĂR, nu de POTRIVIRE.
"""

from src.agent.finalize import render
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import PromptInputs
from src.models import (
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Relevance,
    RetrievalResult,
    TurnContext,
)
from src.worker import compose
from src.worker.runner import PipelineDeps

INP = PromptInputs.build("D", "ecommerce", "ro", ["Accesorii"], [])

#: Setul SERVIT pe turul real: seturi de pensule, cu numele scurtat ca pe card.
BRUSHES = [
    {
        "id": "brush-1",
        "name": "ROYAL AND LANGNICKEL Omnia Pro Gold Ferrule - set pensule",
        "brand": "ROYAL AND LANGNICKEL",
        "price": 1300.0,
        "url": "https://shop/brush-1",
        "product_url": "https://shop/brush-1",
        "availability": "in_stock",
    },
    {
        "id": "brush-2",
        "name": "ROYAL AND LANGNICKEL Omnia Pro - set pensule sintetice",
        "brand": "ROYAL AND LANGNICKEL",
        "price": 900.0,
        "url": "https://shop/brush-2",
        "product_url": "https://shop/brush-2",
        "availability": "in_stock",
    },
    # Al treilea produs servit pe turul real, cu nume nelegat de celelalte două. Există ca să
    # arate că potrivirea chiar SELECTEAZĂ, nu doar acceptă tot ce i se dă.
    {
        "id": "keyring",
        "name": "SKIN1004 1004Day Signature Mirror Keyring",
        "brand": "SKIN1004",
        "price": 50.0,
        "url": "https://shop/keyring",
        "product_url": "https://shop/keyring",
        "availability": "in_stock",
    },
]

#: Proza REALĂ a modelului pe turul măsurat. Nu numește niciun produs, fiindcă îi neagă pe toate.
REFUSAL = (
    "Nu am găsit în catalog o mănușă pentru aplicarea autobronzantului. Rezultatele disponibile "
    "sunt pensule de machiaj, nu sunt potrivite pentru aplicarea pe tot corpul."
)


class _LLM:
    """Model care REUȘEȘTE apelul structurat și nu numește niciun produs, adică
    `no-items-selected`, nu `structured-call-failed`. Distincția e chiar poarta testată."""

    def __init__(self, *, prose: str = REFUSAL, items: list | None = None):
        self._prose = prose
        self._items = items if items is not None else []

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        return self._prose

    async def complete_schema(self, system, user, schema, *, model=None):
        return {
            "intro": None,
            "items": self._items,
            "pick": None,
            "education": None,
            "suggestions": [],
        }


def _ctx(*, relaxed: bool) -> TurnContext:
    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="cat cost una?"),
        conversation_id="conv",
        state=ConversationState(),
    )
    ctx.language = "ro"
    ctx.retrieval = RetrievalResult(
        products=list(BRUSHES),
        relevance=Relevance(relaxed=relaxed),
    )
    return ctx


def _plan(**kw):
    base = dict(
        products=[dict(p) for p in BRUSHES],
        final=REFUSAL,
        is_order=False,
        query="cat cost una?",
        history="",
        inp=INP,
        mode="rich",
    )
    base.update(kw)
    return ResponsePlan(handled=False, **base)


# ── primitiva ───────────────────────────────────────────────────────────────────────────────────


def test_named_products_returns_nothing_when_prose_names_nothing():
    assert compose.named_products(REFUSAL, BRUSHES) == []


def test_named_products_selects_and_does_not_take_the_whole_set():
    prose = "Am doar ROYAL AND LANGNICKEL Omnia Pro, nu și altceva."
    named = compose.named_products(prose, BRUSHES)
    assert [p["id"] for p in named] == ["brush-1", "brush-2"]
    assert "keyring" not in {p["id"] for p in named}


def test_naming_one_member_of_a_name_family_pulls_its_siblings():
    """Comportament DECLARAT, nu ascuns: numele care împart prefixul se recunosc împreună.

    `_mention_index` încearcă prefixe descrescătoare de cuvinte, deci o frază care numește
    „ROYAL AND LANGNICKEL Omnia Pro Gold Ferrule" potrivește și „ROYAL AND LANGNICKEL Omnia Pro".
    Pe catalogul pilot nu e un caz de laborator: 216 capete de nume se repetă (familii de nuanțe și
    gramaje), iar `display_name` nu are noțiune de coliziune.

    Nu se repară AICI, deliberat. `named_products` refolosește EXACT potrivitorul după care se
    reordonează cardurile (`_order_by_first_mention`); un al doilea potrivitor ar face ca ordinea și
    apartenența să răspundă la întrebări diferite despre aceeași frază. Efectul e oricum
    conservator: mulțimea rămâne o submulțime a retrievalului, deci poate adăuga un frate, nu un
    produs străin. Dezambiguizarea numelor e o decizie proprie, pe măsurătoare (D15).
    """
    prose = "Singura variantă apropiată e ROYAL AND LANGNICKEL Omnia Pro Gold Ferrule."
    assert [p["id"] for p in compose.named_products(prose, BRUSHES)] == ["brush-1", "brush-2"]


def test_named_products_ignores_empty_prose():
    assert compose.named_products(None, BRUSHES) == []
    assert compose.named_products("   ", BRUSHES) == []


# ── poarta ──────────────────────────────────────────────────────────────────────────────────────


async def test_refused_relaxed_set_is_not_shown():
    """Turul real: refuz + set obținut prin relaxare ⇒ ZERO carduri sub textul care le neagă.

    Pe codul dinainte, `render` atașa `_card_products(products)` necondiționat, deci clientul
    primea cele două seturi de pensule. Testul pică pe codul vechi exact pe assert-ul de mai jos.
    """
    ctx = _ctx(relaxed=True)
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=_LLM()), _plan())

    assert ctx.reply is not None
    assert ctx.reply.products == [], ctx.reply.products
    assert ctx.reply.rich is None  # nici NX-302 nu are voie să reînvie setul, cu badge și motiv
    assert ctx.reply.text == REFUSAL


async def test_refused_relaxed_set_is_not_cacheable_and_offers_a_way_out():
    """Un „nu am găsit" scris în `semantic_cache` s-ar re-servi sărind agentul, iar un tur fără
    carduri e o fundătură — exact starea în care clientul are nevoie de o cale de continuare."""
    ctx = _ctx(relaxed=True)
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=_LLM()), _plan())

    assert ctx.reply is not None
    assert ctx.reply.cacheable is False
    withheld = [e for e in ctx.events if e.type == "refused_set_withheld"]
    assert withheld and withheld[0].properties == {
        "retrieved": 3,
        "named": 0,
        "turn_id": "t",
    }
    recommended = [e for e in ctx.events if e.type == "agent_recommended"]
    assert recommended and recommended[0].properties["n"] == 0


async def test_refused_but_exact_set_is_still_shown():
    """Contra-exemplul care ține poarta îngustă.

    `no-items-selected` singur NU ajunge. Pe follow-up-ul „ceva mai ieftin", setul e ales
    DETERMINIST de `cheaper_intent` și `relevance` rămâne nesetat (fail-open, vezi `Relevance`):
    acolo serverul știe mai bine decât modelul ce a cerut clientul. Un set găsit STRICT și refuzat
    rămâne pe ecran.
    """
    ctx = _ctx(relaxed=False)
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=_LLM()), _plan())

    assert ctx.reply is not None
    assert [p["product_id"] for p in (ctx.reply.products or [])] == [
        "brush-1",
        "brush-2",
        "keyring",
    ]
    assert not [e for e in ctx.events if e.type == "refused_set_withheld"]


async def test_prose_that_names_a_product_keeps_only_what_it_names():
    """Refuzul nu e absolut: dacă proza numește totuși un produs, ăla e chiar subiectul textului.

    Cheia e ce NU rămâne: brelocul de 50 de lei, servit de aceeași căutare relaxată, dispare.
    """
    prose = "Singura variantă apropiată e ROYAL AND LANGNICKEL Omnia Pro Gold Ferrule."
    ctx = _ctx(relaxed=True)
    await render(
        ctx,
        PipelineDeps(conn=object(), redis=None, llm=_LLM(prose=prose)),
        _plan(final=prose),
    )

    assert ctx.reply is not None
    shown = [p["product_id"] for p in (ctx.reply.products or [])] or [
        it.product_id for it in (ctx.reply.rich.items if ctx.reply.rich else [])
    ]
    assert "keyring" not in shown, shown
    assert set(shown) == {"brush-1", "brush-2"}, shown
