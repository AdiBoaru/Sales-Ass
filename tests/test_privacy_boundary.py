"""NX-230 — frontiera de privacy. Pur: fara DB, fara LLM, fara retea.

TOT PII-ul de aici e SINTETIC: numere generate, adrese inventate, chei false. Cardul cere explicit
sa nu intre date reale intr-un fisier de test — un corpus de test traieste in git pentru totdeauna.

Ce apara suita, in ordinea in care conteaza:
  1. bucla raw: ce se scrie in DB nu mai poate reintroduce PII in promptul turului urmator;
  2. `RawText` nu scapa prin repr / f-string / log / JSON;
  3. detectoarele NU strica date legitime (EAN-13 vs CNP e capcana centrala);
  4. detectorul cazut nu duce la persistarea textului brut.
"""

from __future__ import annotations

import json
import logging

import pytest

from src.models import Author, Direction, Message
from src.privacy import (
    RawText,
    SensitiveTokenMap,
    apply_boundary,
    detect,
    make_safe,
    redact,
    safe_for_telemetry,
)
from src.privacy.policy import PERSIST, PROMPT, TELEMETRY, profile
from src.worker.context import conversation_transcript


def _valid_cnp(first12: str) -> str:
    """Completeaza un CNP sintetic cu cifra de control corecta."""
    weights = (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)
    total = sum(int(d) * w for d, w in zip(first12, weights, strict=True))
    control = total % 11
    return first12 + str(1 if control == 10 else control)


CNP = _valid_cnp("198021041562")
EAN13 = "5941234567890"  # cod de produs real ca forma: 13 cifre, exact ca un CNP
PHONE = "0712345678"
EMAIL = "client@exemplu.invalid"
IBAN = "RO49AAAA1B31007593840000"
CARD = "4111111111111111"  # numar de test Visa, trece Luhn


# ── Bucla pe care o repara cardul ───────────────────────────────────────────────────────────


def test_history_redacts_legacy_raw_rows():
    """MIEZUL CARDULUI.

    Randurile scrise INAINTE de frontiera sunt brute si nu dispar singure. Fara redactare la
    citire, un telefon scris luna trecuta s-ar intoarce in promptul de maine — exact bucla pe care
    masca din NX-121 o lasa deschisa, cu propriul ei comentariu ca dovada.
    """
    history = [
        Message(
            id="1",
            direction=Direction.INBOUND,
            author=Author.CONTACT,
            body=f"sunati-ma la {PHONE} sau {EMAIL}",
        ),
        Message(id="2", direction=Direction.OUTBOUND, author=Author.BOT, body="Sigur!"),
        Message(id="3", direction=Direction.INBOUND, author=Author.CONTACT, body="si acum?"),
    ]
    transcript = conversation_transcript(history)
    assert PHONE not in transcript
    assert EMAIL not in transcript
    assert "[telefon]" in transcript and "[email]" in transcript


def test_redaction_at_read_is_idempotent():
    """Randurile NOI sunt deja redactate — a doua trecere nu trebuie sa le strice."""
    once = make_safe(f"suna la {PHONE}").text
    twice = make_safe(once).text
    assert once == twice


# ── Garda de tip: RawText ───────────────────────────────────────────────────────────────────


def test_rawtext_does_not_leak_in_repr_or_str():
    raw = RawText(f"telefonul meu e {PHONE}")
    assert PHONE not in repr(raw)
    assert PHONE not in str(raw)
    assert PHONE not in f"{raw}"
    assert PHONE not in "{}".format(raw)  # noqa: UP032 — testam exact calea .format()


def test_rawtext_is_not_json_serialisable():
    """`json.dumps` pe un payload cu raw strecurat trebuie sa EXPLODEZE, nu sa scrie."""
    with pytest.raises(TypeError):
        json.dumps({"body": RawText(PHONE)})


def test_rawtext_does_not_leak_through_logging(caplog):
    """`log.info("%s", raw)` trece prin `__str__` — deci placeholder, nu continut."""
    raw = RawText(f"scrie-mi pe {EMAIL}")
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("mesaj primit: %s", raw)
    assert EMAIL not in caplog.text
    assert "redacted" in caplog.text


def test_rawtext_value_is_the_only_way_out():
    raw = RawText(PHONE)
    assert raw.value == PHONE  # explicit, greppable, vizibil in review


def test_rawtext_does_not_compare_equal_to_str():
    """O comparatie cu `str` ar fi un oracol prin care se ghiceste continutul."""
    assert RawText("abc") != "abc"
    assert RawText("abc") == RawText("abc")


def test_length_bucket_hides_exact_length():
    """Lungimea EXACTA a unui text scurt e ea insasi un semnal (11 caractere ≈ un telefon)."""
    assert RawText("x" * 11).length_bucket == "xs"
    assert RawText("x" * 11).length_bucket == RawText("x" * 20).length_bucket


def test_token_map_refuses_to_serialise():
    """Un token map persistat e un vault raw pe care nimeni nu l-a aprobat."""
    import pickle

    tm = SensitiveTokenMap()
    token = tm.issue("phone", PHONE)
    assert tm.resolve(token) == PHONE
    assert PHONE not in repr(tm)
    with pytest.raises(TypeError):
        pickle.dumps(tm)


# ── Detectoare: nu strica date legitime ─────────────────────────────────────────────────────


def test_ean13_is_not_mistaken_for_cnp():
    """CAPCANA CENTRALA: CNP-ul are 13 cifre, EAN-13 la fel.

    Fara verificarea structurii (data valida, judet valid, cifra de control), fiecare cod de produs
    din catalog ar fi mascat ca CNP — si abia apoi am fi aflat, dintr-un raspuns gresit.
    """
    out, counts = redact(EAN13)
    assert out == EAN13
    assert "cnp" not in counts


def test_valid_cnp_is_redacted():
    out, counts = redact(f"CNP-ul meu e {CNP}")
    assert CNP not in out
    assert counts["cnp"] == 1


def test_cnp_with_impossible_date_is_not_redacted():
    """Structura, nu doar forma: luna 99 nu exista."""
    fake = "1999910415620"
    assert redact(fake)[0] == fake


def test_card_needs_luhn_and_real_pan_length():
    assert "[card]" in redact(CARD)[0]
    assert redact("1234567812345678")[0] == "1234567812345678"  # 16 cifre, pica Luhn


def test_phone_needs_a_phone_prefix():
    """Fara prefix (+tara sau 0 local), orice cod de 10 cifre ar deveni telefon."""
    assert "[telefon]" in redact(f"suna la {PHONE}")[0]
    assert "[telefon]" in redact("+40 712 345 678")[0]
    assert redact("cod 1234567890")[0] == "cod 1234567890"


def test_address_needs_a_number_not_just_a_street_word():
    """«locuiesc pe strada mea» nu e o adresa. Un detector lacom strica text legitim."""
    assert "[adresa]" in redact("stau pe str. Florilor nr. 12, bl. A2")[0]
    assert redact("imi place strada asta")[0] == "imi place strada asta"


def test_secrets_are_redacted_by_prefix_and_by_label():
    assert "[secret]" in redact("cheia e sk-abcdefghijklmnopqrst")[0]
    assert "[secret]" in redact("api_key: Abcdef123456xyz")[0]
    assert "sk-abcdefghijklmnopqrst" not in redact("cheia e sk-abcdefghijklmnopqrst")[0]


def test_iban_and_email_redacted():
    out = redact(f"{IBAN} si {EMAIL}")[0]
    assert IBAN not in out and EMAIL not in out


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "a" * 5000,
        "​​​",  # zero-width
        "𝓉𝑒𝓍𝓉 unicode",
        '{"nested": {"json": "0712345678"}}',
        "🙂" * 200,
    ],
)
def test_detectors_never_raise_on_hostile_input(text):
    """Input ostil nu are voie sa produca o exceptie: frontiera ar deveni un DoS de o linie."""
    out, counts = redact(text)
    assert isinstance(out, str) and isinstance(counts, dict)


def test_nested_json_pii_is_still_found():
    out = redact('{"phone": "0712345678"}')[0]
    assert PHONE not in out


# ── Profiluri de politica ───────────────────────────────────────────────────────────────────


def test_order_ref_survives_persist_but_not_telemetry():
    """Decizie explicita: `check_order` are nevoie de numarul de comanda, o metrica nu.

    A masca numarul in corpul mesajului ar rupe fluxul de comenzi ca sa castige aproape nimic —
    o comanda fara cont are valoare mica pentru cine ar citi randul.
    """
    text = "unde e comanda 44231?"
    assert "44231" in make_safe(text, sink="persist").text
    assert "44231" not in safe_for_telemetry(text)


def test_unknown_profile_falls_back_to_strictest():
    """O greseala de tipografie in numele profilului nu are voie sa deschida o scurgere."""
    assert profile("typo-inexistent") == TELEMETRY
    assert PERSIST < TELEMETRY  # persist e strict inclus in telemetry
    assert PROMPT < TELEMETRY


def test_persist_profile_redacts_every_core_pii():
    text = f"{PHONE} {EMAIL} {IBAN} {CARD} {CNP} str. Florilor nr. 3 sk-abcdefghijklmnopqrst"
    safe = make_safe(text, sink="persist")
    for value in (PHONE, EMAIL, IBAN, CARD, CNP):
        assert value not in safe.text, value
    assert safe.had_pii


# ── Frontiera ───────────────────────────────────────────────────────────────────────────────


def test_apply_boundary_returns_both_forms():
    raw, safe = apply_boundary(f"sunt Ana, {PHONE}")
    assert raw.body.value == f"sunt Ana, {PHONE}"  # D6: agentul poate vedea brutul in memorie
    assert PHONE not in safe.text  # dar pe disc ajunge doar forma safe


def test_boundary_fails_safe_when_detector_breaks(monkeypatch):
    """Detector cazut → placeholder marcat, NU textul original «ca sa nu pierdem mesajul».

    Un mesaj pierdut e o paguba de produs; un PII scris pe disc e una de conformitate, si doar una
    dintre ele se poate repara retroactiv.
    """
    import src.privacy.boundary as b

    def _boom(*_a, **_kw):
        raise RuntimeError("detector stricat")

    monkeypatch.setattr(b, "redact", _boom)
    safe = b.make_safe(f"telefonul meu e {PHONE}")
    assert safe.degraded is True
    assert PHONE not in safe.text


def test_boundary_fails_safe_on_oversize_input():
    """Regex cu backtracking pe sute de KB e un DoS; taiem inainte, marcat degradat."""
    safe = make_safe("a" * 100_000)
    assert safe.degraded is True
    assert len(safe.text) < 100


def test_counts_are_bucketed_not_exact():
    """Numarul exact de potriviri e un semnal fin; evenimentele primesc bucketul."""
    safe = make_safe(f"{PHONE} {EMAIL}")
    assert safe.count_bucket() == "2-3"
    assert make_safe("nimic aici").count_bucket() == "0"


def test_detect_returns_counts_without_text():
    """Shadow mode: comparam CATE, niciodata CE."""
    counts = detect(f"{PHONE} {EMAIL}")
    assert counts == {"phone": 1, "email": 1}


def test_safe_inbound_has_no_field_carrying_original_text():
    """Un «sample pentru debug» e tot PII, iar fragmentele ajung in bug reports."""
    safe = make_safe(f"suna la {PHONE}")
    for value in vars(safe).values():
        assert PHONE not in str(value)
