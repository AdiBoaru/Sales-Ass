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
    """Reproduce exact algoritmul vechi și compară. Fără asta, „livrat sub flag" e o afirmație."""
    get_settings.cache_clear()
    monkeypatch.delenv("STRUCTURED_HISTORY_ENABLED", raising=False)
    try:
        history = [_client("caut o cremă"), _bot("Îți recomand X.", REFS), _client("mai ieftin")]
        expected = "\n".join(["Client: caut o cremă", "Asistent: Îți recomand X."])[-1200:]
        assert conversation_transcript(history) == expected
    finally:
        get_settings.cache_clear()


# --- (1) clientul nu se taie niciodată -----------------------------------------------------------


def test_client_message_survives_whole_while_bot_prose_gives_way(structured):
    """Bugetul se ia din proza botului, nu din întrebarea clientului. Ăsta e tot cardul într-un
    test: la 28 de caractere medie, întrebarea e sub 4% din buget și e cea care contează."""
    question = "vreau o rutina pentru ten uscat, dar sa nu contina alcool si sa fie sub 250 lei"
    history = [
        _client(question),
        _bot("Recomandarea mea. " * 400),  # ~7200 chars, peste plafonul de 3500
        _client("acum"),
    ]
    t = conversation_transcript(history)
    assert f"Client: {question}" in t  # integral, cuvânt cu cuvânt
    assert len(t) <= get_settings().history_max_chars


def test_never_cuts_mid_word(structured):
    """Defectul original: transcriptul putea începe cu „aza sistemul). Spune-mi te rog…"."""
    history = [
        _client("prima intrebare"),
        _bot("Propozitie completa numarul unu. " * 300),
        _client("acum"),
    ]
    t = conversation_transcript(history)
    for line in t.splitlines():
        if line.startswith("Asistent: ") and line.endswith("…"):
            assert line[:-1].rstrip().endswith((".", "!", "?")) or line[-2] != " "
    assert not t.startswith("aza")
    # nicio linie nu începe cu un fragment de cuvânt: fiecare are eticheta ei de rol
    for line in t.splitlines():
        assert line.startswith(("Client: ", "Asistent: ", "Asistent [", "(Produsele"))


def test_drops_whole_entries_rather_than_mutilating_client(structured, monkeypatch):
    """Pasul 2 al politicii: când scurtarea prozei nu ajunge, se elimină intrări ÎNTREGI de la
    cea mai veche. Un mesaj de client poate dispărea (fereastra e mărginită prin definiție), dar
    nu poate fi mutilat."""
    monkeypatch.setenv("HISTORY_MAX_CHARS", "300")
    get_settings.cache_clear()
    long_q = "intrebare veche " * 20  # 320 chars: nu încape nici singură
    history = [_client(long_q), _bot("scurt"), _client("noua"), _bot("si mai scurt"), _client("x")]
    t = conversation_transcript(history)
    assert long_q.strip() not in t  # eliminată, nu tăiată
    assert "…" not in t.split("Asistent:")[0]  # ce a rămas din client e intact
    assert "Client: noua" in t


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


def test_bot_prose_gives_way_before_its_facts(structured, monkeypatch):
    """Ancora supraviețuiește retoricii: proza se scurtează, blocul de produse rămâne întreg."""
    monkeypatch.setenv("HISTORY_MAX_CHARS", "600")
    get_settings.cache_clear()
    prose = "Fraza lunga si redundanta. " * 60
    history = [_client("q"), _bot(prose, REFS), _client("acum")]
    t = conversation_transcript(history)
    assert "a3f2c1d4" in t and "b71e0055" in t  # ambele ref-uri, netăiate
    kept = next(ln for ln in t.splitlines() if ln.startswith("Asistent: "))
    assert len(kept) < len(prose)  # proza a cedat
    assert kept.rstrip().endswith((".", "…"))  # dar la o graniță, nu la mijloc de cuvânt


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
    assert props["client_chars"] > 0 and props["assistant_chars"] > 0
    blob = json.dumps(props, ensure_ascii=False)
    assert "intrebarea mea" not in blob and "Ser hidratant" not in blob
