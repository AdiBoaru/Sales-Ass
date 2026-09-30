"""NX-255 — istoricul structurat: clientul verbatim, botul cu proza întreagă + ce a arătat.

Findingul reparat, în două părți: (1) `[-1200:]` pe stringul unit tăia mesajele ieftine ale
clientului ca să păstreze proza scumpă a botului, și tăia la mijlocul cuvântului; (2) ce a ARĂTAT
botul nu se persista nicăieri, deci referințele ordinale n-aveau ancoră dincolo de ultimul tur.
"""

import json

import pytest

from src.config import get_settings
from src.models import Author, Direction, Message
from src.worker.context import conversation_transcript


@pytest.fixture
def structured(monkeypatch):
    """Flagul aprins, cu cache-ul de settings curățat pe AMBELE capete (altfel un test scurge
    configurația în următorul, iar paritatea cu flagul stins ar deveni nemăsurabilă)."""
    get_settings.cache_clear()
    monkeypatch.setenv("STRUCTURED_HISTORY_ENABLED", "true")
    yield
    get_settings.cache_clear()


def _client(body: str) -> Message:
    return Message(direction=Direction.INBOUND, author=Author.CONTACT, body=body)


def _bot(body: str, shown: list[dict] | None = None) -> Message:
    payload = {"turn_id": "t", "fragment_index": 0}
    if shown is not None:
        payload["shown"] = shown
    return Message(direction=Direction.OUTBOUND, author=Author.BOT, body=body, payload=payload)


REFS = [
    {"product_id": "a3f2c1d4", "name": "Ser hidratant X", "price": 89.0},
    {"product_id": "b71e0055", "name": "Cremă de noapte Y", "price": 129.0},
]


# --- paritate: flagul stins nu schimbă niciun byte ----------------------------------------------


def test_flag_off_is_byte_identical_to_old_behaviour(monkeypatch):
    """Reproduce exact algoritmul vechi și compară. Kill-switch-ul trebuie să fie chiar calea
    veche, nu o aproximare a ei."""
    get_settings.cache_clear()
    monkeypatch.setenv("STRUCTURED_HISTORY_ENABLED", "false")
    try:
        long_bot = "Îți recomand X. " * 120
        history = [_client("caut o cremă"), _bot(long_bot, REFS), _client("mai ieftin")]
        expected = "\n".join(["Client: caut o cremă", f"Asistent: {long_bot.strip()}"])[-1200:]
        assert conversation_transcript(history) == expected
    finally:
        get_settings.cache_clear()


def test_flag_is_on_by_default(monkeypatch):
    """ON implicit (defect măsurat, ca NX-311): fără variabilă de mediu, istoricul nu se taie."""
    get_settings.cache_clear()
    monkeypatch.delenv("STRUCTURED_HISTORY_ENABLED", raising=False)
    try:
        assert get_settings().structured_history_enabled is True
    finally:
        get_settings.cache_clear()


# --- (1) nicio tăiere de caractere ---------------------------------------------------------------


def _bot_reply_like_sole(n: int) -> str:
    """Un răspuns de bot de mărimea celor reale pe `sole-ro` (1.200-1.600 de caractere): proză +
    lista de produse cu prețuri + chips, exact forma din `messages.body`."""
    lines = [f"Intro pentru setul {n}, cu ce am pus pe masă și cum alegi."]
    for i in range(6):
        lines.append(f"{i + 1}. Produs {n}.{i} cu nume lung de catalog, 110,00 lei  ⭐4.9")
        lines.append("   Motivul cardului, o propoziție despre ingrediente și potrivire.")
    lines.append("Îți mai pot arăta: Caut ceva pentru calmare · Vreau ceva sub 100 lei")
    return "\n".join(lines) * 3


def test_conversation_1748f988_every_client_message_reaches_turn_3(structured):
    """Conversația reală `1748f988` (2026-09-29): la turul 3, tăierea veche `[-1200:]` lăsa doar
    coada listei de la turul 2, fără NICIUN mesaj al clientului, deci modelul nu știa că e vorba de
    o cremă pentru ten uscat și a servit măști de 10 lei."""
    history = [
        _client("vreau o crema de hidratare"),
        _bot(_bot_reply_like_sole(1), REFS),
        _client("am ten uscat"),
        _bot(_bot_reply_like_sole(2), REFS),
        _client("sub 100 de lei sa vad"),
    ]
    assert len(_bot_reply_like_sole(1)) > 1200  # fiecare răspuns trece singur de vechiul plafon
    t = conversation_transcript(history)
    assert "Client: vreau o crema de hidratare" in t
    assert "Client: am ten uscat" in t
    # proza botului rămâne ÎNTREAGĂ, pe ambele ture
    assert _bot_reply_like_sole(1).strip() in t
    assert _bot_reply_like_sole(2).strip() in t


def test_no_character_cap_on_client_or_bot(structured):
    """Nicio margine de caractere: un mesaj de client la plafonul de intrare (2.000, `web/app.py`)
    și un răspuns de bot foarte lung trec neatinse. Singura margine e fereastra de mesaje."""
    question = "vreau o rutina pentru ten uscat, fara alcool, sub 250 lei. " * 33  # ~2.000
    prose = "Recomandarea mea. " * 400  # ~7.200
    t = conversation_transcript([_client(question), _bot(prose), _client("acum")])
    assert f"Client: {question.strip()}" in t
    assert f"Asistent: {prose.strip()}" in t
    assert "…" not in t


def test_window_of_messages_is_the_only_bound(structured):
    """Fereastra e cea ÎNCĂRCATĂ (`HISTORY_LIMIT`), nu 6: rezumatul acoperă doar ce e înaintea
    mesajelor încărcate, deci cu 6 din 8 un tur întreg nu apărea nicăieri (recenzia NX-255).
    Mesajele mai vechi decât fereastra ies ÎNTREGI, nu tăiate."""
    from src.db.queries.messages import HISTORY_LIMIT

    n = HISTORY_LIMIT + 2  # ultimul e mesajul curent
    history = [_client(f"intrebarea {i}") if i % 2 == 0 else _bot(f"raspuns {i}") for i in range(n)]
    t = conversation_transcript(history)
    shown = [ln for ln in t.splitlines() if ln.startswith(("Client: ", "Asistent: "))]
    assert len(shown) == HISTORY_LIMIT
    assert "intrebarea 0" not in t  # cel mai vechi a ieșit, întreg
    assert "Asistent: raspuns 1" in t and "Client: intrebarea 2" in t  # 7-8 în urmă: acum apar
    for line in t.splitlines():
        assert line.startswith(("Client: ", "Asistent: ", "Asistent [", "(Produsele"))


# --- (2) ce a arătat botul ------------------------------------------------------------------------


def test_shown_products_reach_the_prompt_with_age(structured):
    history = [_client("ce ai pentru ten uscat"), _bot("Uite doua variante.", REFS), _client("q")]
    t = conversation_transcript(history)
    assert "Asistent [a aratat, în turul precedent]:" in t
    assert "a3f2c1d4" in t and "Ser hidratant X" in t
    assert "Asistent: Uite doua variante." in t  # proza rămâne INTEGRALĂ, nu rezumată


def test_shown_block_is_compact_valid_json(structured):
    history = [_client("q"), _bot("text", REFS), _client("acum")]
    line = next(
        ln
        for ln in conversation_transcript(history).splitlines()
        if ln.startswith("Asistent [a aratat")
    )
    items = json.loads(line.split(": ", 1)[1])
    assert items == [
        {"id": "a3f2c1d4", "n": "Ser hidratant X", "p": 89.0},
        {"id": "b71e0055", "n": "Cremă de noapte Y", "p": 129.0},
    ]


def test_age_grows_with_distance(structured):
    history = [
        _client("q1"),
        _bot("r1", REFS),
        _client("q2"),
        _bot("r2"),
        _client("q3"),
        _bot("r3"),
        _client("acum"),
    ]
    t = conversation_transcript(history)
    assert "acum 3 ture" in t


def test_legend_appears_once_and_only_with_shown(structured):
    from src.worker.context import _SHOWN_LEGEND

    with_shown = conversation_transcript(
        [_client("q"), _bot("r", REFS), _client("q2"), _bot("r2", REFS), _client("acum")]
    )
    assert with_shown.count(_SHOWN_LEGEND) == 1  # două blocuri, o singură legendă
    assert with_shown.startswith(_SHOWN_LEGEND)

    without = conversation_transcript([_client("q"), _bot("r"), _client("acum")])
    assert "a aratat" not in without  # nicio regulă despre date inexistente


def test_legend_obeys_voice_rules(structured):
    """Principiul 13: promptul se scrie ÎN vocea pe care o cere. Un exemplu cu liniuță de pauză
    sau cu „;" în prompt îl învață pe model exact ce îi interzicem în `VOICE_RULES`."""
    from src.worker.context import _SHOWN_LEGEND

    assert ";" not in _SHOWN_LEGEND
    for dash in ("—", "–"):
        assert dash not in _SHOWN_LEGEND
    assert " - " not in _SHOWN_LEGEND


def test_consumer_distinguishes_callers(structured):
    """Mai mulți consumatori cer transcriptul pe același tur. Fără etichetă, orice agregare
    dublează tăcut mărimea istoricului."""
    seen: list[str] = []
    for who in ("agent", "compare"):
        conversation_transcript(
            [_client("q"), _bot("r", REFS), _client("acum")],
            emit=lambda t, **p: seen.append(p["consumer"]),
            consumer=who,
        )
    assert seen == ["agent", "compare"]


# --- robustețe pe date vechi/stricate -------------------------------------------------------------


@pytest.mark.parametrize(
    "shown",
    [
        None,  # rând scris înainte de NX-255
        "nu e listă",
        [{"name": "fără id", "price": 1}],
        [{"product_id": "x", "name": "fără preț"}],
        ["nu e dict"],
    ],
)
def test_malformed_shown_degrades_to_prose_only(structured, shown):
    """Un payload vechi sau stricat degradează la „fără bloc", niciodată la excepție: istoricul e
    pe calea fierbinte a fiecărui tur."""
    msg = _bot("text", None) if shown is None else _bot("text", shown)  # type: ignore[arg-type]
    t = conversation_transcript([_client("q"), msg, _client("acum")])
    assert "Asistent: text" in t
    assert "[a aratat" not in t


def test_redaction_applies_to_shown_values_too(structured):
    """NX-230: poarta de redactare nu se ocolește pe drumul nou. Istoricul e ultimul loc dinaintea
    promptului, deci plasa acoperă TOT ce pleacă, nu doar câmpurile presupuse riscante."""
    poisoned = [{"product_id": "p1", "name": "Suna la 0722123456 acum", "price": 10.0}]
    t = conversation_transcript([_client("q"), _bot("text", poisoned), _client("acum")])
    assert "0722123456" not in t


# --- persistarea: fără ea, tot ce e mai sus n-are ce citi ----------------------------------------


def _ctx_with_products(products):
    from src.models import BusinessConfig, Contact, InboundMessage, Reply, TurnContext

    ctx = TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )
    ctx.reply = Reply(text="Uite doua variante.", products=products)
    return ctx


CARDS = [
    {"product_id": "a3f2c1d4", "name": "Ser hidratant X", "price": 89.0, "url": "/p/ser"},
    {"product_id": "b71e0055", "name": "Cremă de noapte Y", "price": 129.0, "stock": 4},
]


@pytest.mark.parametrize("deliver", [True, False])
def test_shown_refs_persisted_on_both_delivery_paths(structured, deliver):
    """Defectul original: pe web sincron `_build_fragment` ieșea înainte de a construi payload-ul
    bogat, iar pe canalele async produsele mergeau în OUTBOX, nu pe rândul de mesaj. În ambele
    cazuri, istoricul de mâine rămânea fără fapte."""
    from src.worker.processor import _build_fragment

    frag = _build_fragment(
        _ctx_with_products(CARDS),
        "Uite doua variante.",
        index=0,
        turn_id="t",
        to="v1",
        deliver=deliver,
        is_rich=False,
        has_products=True,
    )
    assert frag.message_payload["shown"] == [
        {"product_id": "a3f2c1d4", "name": "Ser hidratant X", "price": 89.0},
        {"product_id": "b71e0055", "name": "Cremă de noapte Y", "price": 129.0},
    ]


def test_shown_refs_are_refs_not_objects(structured):
    """P8: în payload intră id + nume + preț, nu cardul complet (url, stoc, variante)."""
    from src.worker.processor import _build_fragment

    frag = _build_fragment(
        _ctx_with_products(CARDS),
        "text",
        index=0,
        turn_id="t",
        to="v1",
        deliver=False,
        is_rich=False,
        has_products=True,
    )
    assert set(frag.message_payload["shown"][0]) == {"product_id", "name", "price"}


def test_shown_only_on_first_fragment_and_only_with_flag(structured, monkeypatch):
    from src.worker.processor import _build_fragment

    def build(index: int):
        return _build_fragment(
            _ctx_with_products(CARDS),
            "text",
            index=index,
            turn_id="t",
            to="v1",
            deliver=True,
            is_rich=False,
            has_products=True,
        )

    assert "shown" not in build(1).message_payload  # split-ul e același reply, nu al doilea set
    monkeypatch.setenv("STRUCTURED_HISTORY_ENABLED", "false")
    get_settings.cache_clear()
    assert "shown" not in build(0).message_payload  # flag stins ⇒ payload byte-identic cu azi


def test_emit_reports_roles_without_content(structured):
    """P12: observabilitatea raportează lungimi și contoare, niciodată conținut."""
    events: list[tuple[str, dict]] = []
    conversation_transcript(
        [_client("intrebarea mea"), _bot("raspuns", REFS), _client("acum")],
        emit=lambda t, **p: events.append((t, p)),
    )
    (name, props) = events[0]
    assert name == "history_budget"
    assert props["shown_turns"] == 1
    assert props["consumer"] == "unknown"  # apelant fără etichetă → explicit, nu tăcut
    assert props["client_chars"] > 0 and props["assistant_chars"] > 0
    blob = json.dumps(props, ensure_ascii=False)
    assert "intrebarea mea" not in blob and "Ser hidratant" not in blob
