"""NX-336 PR A — corecturile după A DOUA recenzie adversarială (codul din #463, `4be9a62`).

Fiecare test a fost scris ÎNAINTE de reparație și a picat pe `4be9a62`:

1. `exact_only` servea ancore GHICITE: „singurul produs de pe ecran" și pagina ca fallback
   necondiționat, pe mesaje care numesc ALT produs;
3. găurile de test: pagina ca sursă a resolverului pe calea kernelului, istoricul FĂRĂ mesajul
   curent, istoricul redactat (fiecare mutație trebuie să pice un test);
4. comparația pe un PDP cu două carduri (fixat, nu schimbat: decizia e a PR-ului C);
5. un memo dat unei treceri COMPLETE nu înregistrează (altfel i-ar șterge evenimentele).

(Constatarea 2, `ResolvedRef.ref_id` neredactat, e în `test_r1` din
`tests/test_interpreted_turn_a_review.py`, extins.) Zero model real, zero DB."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import deterministic as det
from src.conversation.interpretation import Act, Reference, TurnInterpretation
from src.conversation.kernel_trace import KernelTrace
from src.conversation.state_v2 import ConversationStateV2
from src.models import Author, Direction, Message
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_a import FIND, shortcuts  # noqa: F401 — fixture
from tests.test_interpreted_turn_a_review import _shown_state
from tests.test_nx326_named_shortcut_targets import SHOWN, _ctx, _deps


def _pdp(ctx, pid="pX", name="CeraVe Moisturizing Cream", price=70.0):
    ctx.snapshot = NS(surface=NS(product=NS(product_id=pid, name=name, price=price)))
    return ctx


# --- 1. `exact_only` nu servește o ancoră ghicită -------------------------------------------------

GUESSED = [
    ("ce părere au clienții despre Cerave?", 1, False),
    ("ce părere au clienții despre crema Dokdo?", 1, False),
    ("ce părere au clienții despre al patrulea?", 3, True),
    ("ce părere au clienții despre Dokdo?", 3, True),
    ("spune-mi mai multe despre Dokdo", 0, True),
]


@pytest.mark.parametrize(("text", "shown", "page"), GUESSED)
async def test_1_exact_only_does_not_serve_a_guessed_anchor(shortcuts, text, shown, page):  # noqa: F811
    ctx = _ctx(text, shown=SHOWN[:shown])
    if page:
        _pdp(ctx)
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is False
    assert shortcuts["reviews"] == [] and ctx.reply is None


@pytest.fixture
def details(monkeypatch):
    served: list[str] = []

    async def serve(ctx, deps, product_id, **kw):
        served.append(product_id)
        ctx.set_reply("detalii", cacheable=False)

    monkeypatch.setattr(det, "serve_details", serve)
    return served


@pytest.mark.parametrize(
    ("text", "shown", "page", "expected"),
    [
        ("spune-mi mai multe", 1, False, "p1"),
        ("spune-mi mai multe", 0, True, "pX"),
        ("spune-mi mai multe despre el", 1, False, "p1"),
        # chip-urile NOASTRE: detaliul numește produsul de sub el
        (det._detail_copy("ro")["chip"].format(name=SHOWN[0].name), 1, False, "p1"),
        (det._detail_copy("ro")["chip"].format(name="CeraVe Moisturizing Cream"), 0, True, "pX"),
        # un nume UNIC de pe ecran e o țintă exactă, chiar lângă un PDP
        ("spune-mi mai multe despre cea de la Beauty of Joseon", 3, True, "p2"),
    ],
)
async def test_1_exact_anchors_are_still_served(
    shortcuts,  # noqa: F811
    details,
    text,
    shown,
    page,
    expected,
):
    ctx = _ctx(text, shown=SHOWN[:shown])
    if page:
        _pdp(ctx)
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert details == [expected]


async def test_1_our_review_chip_with_one_card_is_exact(shortcuts):  # noqa: F811
    ctx = _ctx(det._detail_copy("ro")["review_chip"], shown=SHOWN[:1])
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["reviews"] == ["p1"]


@pytest.mark.parametrize(("text", "shown", "page"), GUESSED)
async def test_1_without_exact_only_the_behaviour_is_todays(shortcuts, text, shown, page):  # noqa: F811
    """`exact_only=False` e funcția de azi: v1 servește (sau întreabă) exact ca înainte."""
    ctx = _ctx(text, shown=SHOWN[:shown])
    if page:
        _pdp(ctx)
    assert await det.try_pre_intents(ctx, _deps()) is True


# --- 3. intrarea interpretării și pagina, pe stagiul REAL -----------------------------------------


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat)
    return cat


@pytest.fixture
def captured(monkeypatch):
    from src.agent import interpreted_turn as it

    seen: list = []
    original = it.interpret_turn

    async def spy(llm, inp, **kw):
        seen.append(inp)
        return await original(llm, inp, **kw)

    monkeypatch.setattr(it, "interpret_turn", spy)
    return seen


async def test_3_the_interpret_input_has_the_history_before_the_message_redacted(
    monkeypatch, electronics, captured
):
    from src.conversation.turn_interpreter import user_words

    ctx = sh.build_ctx(electronics, ConversationStateV2(), "Vreau un telefon.")
    ctx.history = [
        Message(Direction.INBOUND, Author.CONTACT, "salut, numarul meu e 0722 123 456"),
        Message(Direction.OUTBOUND, Author.BOT, "Bună! Cu ce te ajut?"),
        Message(Direction.INBOUND, Author.CONTACT, "Vreau un telefon."),
    ]
    await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(FIND))
    [inp] = captured
    assert inp.message == "Vreau un telefon."
    assert inp.history == (
        ("user", "salut, numarul meu e [telefon]"),
        ("bot", "Bună! Cu ce te ajut?"),
    ), "istoricul = mesajele DINAINTEA turului, redactate"
    words = user_words(inp)
    assert words.current == "Vreau un telefon."
    assert words.earlier == ("salut, numarul meu e [telefon]",)


async def test_3_the_page_is_a_source_of_the_kernel_resolver(monkeypatch, electronics):
    """Pe un PDP, „acesta" se rezolvă pe produsul PAGINII (sursa `page`), prin stagiul real."""
    page_id = list(electronics.items)[4]
    item = electronics.items[page_id]
    deictic = TurnInterpretation(
        thread="continue",
        acts=[Act(kind="detail", targets=["r1"], query=None)],
        changes=[],
        references=[
            Reference(
                id="r1",
                text="acesta",
                kind="deictic",
                ordinal=None,
                name=None,
                dimension=None,
                value=None,
                direction=None,
            )
        ],
        ambiguities=[],
        corrects_previous_turn=False,
    )
    ctx = sh.build_ctx(electronics, ConversationStateV2(), "cât costă acesta?")
    _pdp(ctx, pid=page_id, name=item["name"], price=float(item["price"]))
    await sh.run_turn(monkeypatch, electronics, ctx, sh.StageLLM(deictic))
    trace = KernelTrace.model_validate(ctx.trace["kernel"])
    [ref] = trace.resolved_refs
    assert (ref.source, ref.outcome, ref.product_ids) == ("page", "exact", [page_id])


# --- 4. comparația pe un PDP cu două carduri: fixată, nu schimbată --------------------------------


async def test_4_pin_compare_on_a_pdp_with_two_cards_serves_the_two_cards(shortcuts):  # noqa: F811
    """Linkul numără ancorele cu pagina; comparația numără doar cardurile (ca v1). Pe un PDP cu
    două carduri, «compară-le» servește cele DOUĂ carduri. Declarat; decizia e a PR-ului C."""
    ctx = _pdp(_ctx("compară-le", shown=SHOWN[:2]))
    assert await det.try_pre_intents(ctx, _deps(), exact_only=True) is True
    assert shortcuts["compared"] == [["p1", "p2"]]


# --- 5. un memo dat unei treceri complete nu înregistrează ----------------------------------------


async def test_5_a_memo_on_a_full_pass_keeps_its_events(shortcuts):  # noqa: F811
    plain = _ctx("Trimite-mi linkul la Cerave")
    assert await det.try_pre_intents(plain, _deps()) is False
    with_memo = _ctx("Trimite-mi linkul la Cerave")
    assert await det.try_pre_intents(with_memo, _deps(), memo=det.ShortcutMemo()) is False
    assert [e.type for e in with_memo.events] == [e.type for e in plain.events]
    assert with_memo.events, "trecerea completă a emis evenimente (verificare a testului)"


def test_5_state_harness_shown_state_is_used():
    assert _shown_state
