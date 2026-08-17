"""NX-246 — proba de privacy: canary-ul NU iese din proces. Pe NICIUN semnal.

Testul e scris în forma pe care o cere „Codex Attack Plan" din card: pune telefon, email, bearer
token, cheie OpenAI, CNP, IBAN, prompt și product ID în tot ce poate ajunge la telemetrie
(excepții imbricate, URL-uri, headere, argumente de tool, atribute de span, etichete de metrică),
apoi caută fiecare canary în TOT ce a ieșit. Orice apariție e finding CONFIRMED.

Diferența față de un test care verifică atributele cunoscute: aici se caută în `all_text()` —
suprafața ÎNTREAGĂ a sink-ului. Un câmp nou adăugat mâine de cineva care n-a citit contractul
cade exact aici, nu într-un audit peste șase luni.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.observability import bootstrap, hooks, metrics, tracing
from src.observability import config as obs_config
from src.observability.sanitize import (
    exception_chain,
    safe_args,
    safe_error_code,
    safe_headers,
    safe_text,
    safe_url,
)

# Canary-uri REALE ca formă (nu date reale): dacă vreunul apare într-un semnal, avem o scurgere.
TELEFON = "0721 345 678"
TELEFON_INTL = "+40721345678"
EMAIL = "maria.ionescu@example.ro"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.QWxhZGRpbjpvcGVuc2VzYW1l"
OPENAI_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
# CNP cu cifră de control VALIDĂ. Contează: `privacy.detectors._valid_cnp` cere structură + checksum
# tocmai ca un EAN-13 din catalog să nu fie mascat ca CNP — deci un canary cu control greșit ar
# trece neatins și ar face testul să pice pe o problemă care nu există.
CNP = "2980512420017"
IBAN = "RO49AAAA1B31007593840000"
CARD = "4111 1111 1111 1111"
PROMPT = "caut o cremă pentru rozacee, sunt însărcinată în luna a cincea"
PRODUCT_ID = "9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"

CANARY = [TELEFON, TELEFON_INTL, EMAIL, BEARER, OPENAI_KEY, CNP, IBAN, CARD, PROMPT, PRODUCT_ID]


def _settings(**over):
    base = dict(
        observability_enabled=True,
        observability_traces_enabled=True,
        observability_metrics_enabled=True,
        observability_exporter="capture",
        observability_otlp_endpoint="",
        observability_otlp_headers="",
        observability_sample_ratio=1.0,
        observability_queue_max=2048,
        observability_export_batch=256,
        observability_flush_timeout_ms=100,
        observability_trace_secret="test-secret",
        service_name="nativx-assistant",
        env="test",
        release_sha="deadbeef",
        release_track="champion",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def sink():
    bootstrap.setup(_settings())
    metrics.reset(strict=False)  # producție: normalizează + numără, nu ridică
    yield bootstrap.capture_sink()
    obs_config.configure(None)
    tracing.set_exporter(None)
    metrics.reset()


def _assert_curat(text: str, *, exceptii: tuple[str, ...] = ()) -> None:
    for canary in CANARY:
        if canary in exceptii:
            continue
        assert canary not in text, f"SCURGERE: {canary!r} a ajuns în telemetrie"
        # și forma fără separatoare (un „0721345678" e la fel de PII ca „0721 345 678")
        compact = canary.replace(" ", "").replace("-", "")
        if len(compact) > 8:
            assert compact not in text.replace(" ", "").replace("-", ""), (
                f"SCURGERE (formă compactă): {canary!r}"
            )


# ── Sanitizerul, pe fiecare formă de intrare ────────────────────────────────────────────────


def test_text_liber_e_redactat_pe_toate_categoriile():
    """Redactarea, izolat de trunchiere: cu limită mare, TOT ce e PII trebuie deja înlocuit."""
    murdar = f"sună-mă la {TELEFON} sau {EMAIL}, CNP {CNP}, IBAN {IBAN}, card {CARD}"
    out = safe_text(murdar, limit=1000)
    for placeholder in ("[telefon]", "[email]", "[cnp]", "[iban]", "[card]"):
        assert placeholder in out, f"{placeholder} lipsește din {out!r}"
    _assert_curat(out)


def test_textul_liber_e_si_trunchiat():
    """A doua plasă: redactarea prinde FORMELE pe care le știm, nu o poveste în proză. Limita face
    ca, în cel mai rău caz, ce scapă să fie un fragment — nu un transcript."""
    out = safe_text(PROMPT * 20)
    assert len(out) <= 120


def test_mesajul_exceptiei_nu_devine_niciodata_cod_de_eroare():
    """`safe_error_code` derivă din TIP. Mesajul e text liber generat de o bibliotecă terță —
    exact locul unde apar query-uri, URL-uri cu token și chei."""
    exc = ConnectionError(f"failed to reach {EMAIL} with {BEARER}")
    cod = safe_error_code(exc)
    assert cod == "connection_error"
    _assert_curat(cod)


def test_lantul_de_exceptii_pastreaza_tipurile_nu_continutul():
    try:
        try:
            raise TimeoutError(f"query: select * from contacts where phone = '{TELEFON}'")
        except TimeoutError as inner:
            raise RuntimeError(f"tool a eșuat pentru {EMAIL}") from inner
    except RuntimeError as exc:
        lant = exception_chain(exc)
    assert lant == "runtime_error<timeout_error"
    _assert_curat(lant)


def test_lant_circular_nu_bucleaza():
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert exception_chain(a).count("<") < 5


def test_url_pierde_query_string_si_identificatorii():
    url = f"https://shop.example.ro/api/orders/{PRODUCT_ID}/items?token={OPENAI_KEY}&email={EMAIL}"
    out = safe_url(url)
    assert out == "https://shop.example.ro/api/orders/:id/items"
    _assert_curat(out)


def test_headerele_sensibile_sunt_doar_prezenta():
    out = safe_headers(
        {
            "Authorization": BEARER,
            "Cookie": f"session={OPENAI_KEY}",
            "X-Api-Key": OPENAI_KEY,
            "Content-Type": "application/json",
            "X-Custom": EMAIL,  # nici măcar allowlistat → dispare complet
        }
    )
    assert out == {
        "authorization": "present",
        "cookie": "present",
        "x-api-key": "present",
        "content-type": "application/json",
    }
    _assert_curat(repr(out))


def test_argumentele_de_tool_devin_forma_nu_valori():
    out = safe_args(
        {
            "category": "creme",
            "concerns": ["rozacee", "sarcina"],
            "query": PROMPT,
            "product_id": PRODUCT_ID,
            "budget_max": 120,
            "contact_phone": TELEFON,
        }
    )
    assert out == {
        "category": "str",
        "concerns": "list[2]",
        "query": "str",
        "product_id": "str",
        "budget_max": "int",
        "contact_phone": "str",
    }
    _assert_curat(repr(out))


def test_correlation_ref_nu_e_inversabil():
    from src.observability.sanitize import correlation_ref

    ref = correlation_ref(PRODUCT_ID, salt="s")
    assert len(ref) == 16 and PRODUCT_ID not in ref
    assert ref == correlation_ref(PRODUCT_ID, salt="s")  # stabil (grupare)
    assert ref != correlation_ref(PRODUCT_ID, salt="alt")  # legat de salt


# ── Proba pe TOATE semnalele deodată ────────────────────────────────────────────────────────


def test_canary_ul_nu_ajunge_in_niciun_semnal(sink):
    """Drive-ul cerut de Codex Attack Plan, punctul 1."""
    turn_id = "11111111-2222-3333-4444-555555555555"
    with pytest.raises(RuntimeError):
        with tracing.turn_trace(turn_id, attempt=1, attempt_bucket="1"):
            with tracing.span("web.agent.call", stage="model", model_id="gpt-5.4-mini") as sp:
                # Cineva încearcă să pună conținut pe span, prin toate ușile:
                sp.set_attribute("outcome", "ok")
                sp.set_attribute("prompt", PROMPT)  # atribut nedeclarat
                sp.set_attribute("model_id", OPENAI_KEY)  # atribut declarat, valoare otrăvită
                tracing.set_attribute("tool_name", f"search_products?q={EMAIL}")
                raise RuntimeError(f"apel eșuat pentru {TELEFON} / {BEARER}")

    # ...și prin metrici:
    hooks.on_model_call(OPENAI_KEY, model_role="agent", outcome="ok")
    hooks.on_terminal("failed", safe_error_code_=PROMPT, release_track="champion")
    hooks.on_turn_request("rejected", release_track="champion", duration_s=0.01)
    with pytest.raises(ValueError):
        with hooks.tool_call(f"search_products({PRODUCT_ID})"):
            raise ValueError(EMAIL)

    tracing.get_exporter().flush()
    sink.emit_metrics(metrics.snapshot())
    _assert_curat(sink.all_text())


def test_exceptia_marcata_pastreaza_spanul_rosu_fara_mesaj(sink):
    """Calea care PRINDE excepția și degradează onest (P6): turul continuă, traceul nu minte."""
    with tracing.turn_trace("22222222-2222-3333-4444-555555555555"):
        with tracing.span("web.tool.call", stage="tools"):
            try:
                raise PermissionError(f"acces refuzat pentru {EMAIL}")
            except PermissionError as e:
                tracing.mark_error(e)
    tracing.get_exporter().flush()
    span = sink.by_name("web.tool.call")[0]
    assert span.status == "error"
    assert span.attributes["exception_type"] == "permission_error"
    _assert_curat(sink.all_text())


def test_privacy_boundary_ramane_singura_sursa_de_adevar():
    """NX-230 e sursa unică pentru „ce e PII". Dacă cineva adaugă o a doua listă de regexuri în
    observability, testul ăsta nu o prinde — dar comentariul din `sanitize.py` explică de ce nu
    trebuie; ce verificăm aici e că DELEGAREA chiar are loc."""
    from src.privacy.boundary import safe_for_telemetry

    murdar = f"contact: {TELEFON}"
    assert safe_text(murdar) == safe_for_telemetry(murdar)
