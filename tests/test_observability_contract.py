"""NX-246 — contractul de observabilitate: nume, etichete, cardinalitate, trace, export.

Testele astea nu verifică „se emit metrici", ci exact proprietățile pe care se sprijină deciziile
de release: că o etichetă otrăvită nu poate exploda backendul, că un trace supraviețuiește
restartului fără nimic persistat, că un exporter căzut nu atinge turul și că o config imposibilă
refuză boot-ul în loc să tacă.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.observability import bootstrap, hooks, metrics, tracing
from src.observability import config as obs_config
from src.observability.contract import METRICS, ContractViolation, assert_contract
from src.observability.export import CaptureSink, SpanExporter, SpanRecord


def _settings(**over):
    base = dict(
        observability_enabled=True,
        observability_traces_enabled=True,
        observability_metrics_enabled=True,
        observability_exporter="capture",
        observability_otlp_endpoint="",
        observability_otlp_headers="",
        observability_otlp_timeout_ms=2000,
        observability_sample_ratio=1.0,
        observability_queue_max=2048,
        observability_export_batch=256,
        observability_flush_timeout_ms=100,
        observability_trace_secret="test-secret",
        service_name="nativx-assistant",
        env="test",
        release_sha="deadbeef",
        release_track="candidate",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def obs():
    """Observabilitate PORNITĂ, cu sink în memorie și mod strict (abaterile ridică)."""
    cfg = bootstrap.setup(_settings())
    metrics.reset(strict=True)
    sink = bootstrap.capture_sink()
    yield SimpleNamespace(cfg=cfg, sink=sink)
    obs_config.configure(None)
    tracing.set_exporter(None)
    metrics.reset()


@pytest.fixture
def obs_lenient():
    """Ca `obs`, dar NEstrict: comportamentul de PRODUCȚIE (normalizare + drop numărat)."""
    bootstrap.setup(_settings())
    metrics.reset(strict=False)
    yield bootstrap.capture_sink()
    obs_config.configure(None)
    tracing.set_exporter(None)
    metrics.reset()


# ── Contractul ──────────────────────────────────────────────────────────────────────────────


def test_registrul_de_metrici_e_coerent():
    assert_contract()  # ridică dacă un counter are bucket-uri, o histogramă n-are etc.
    assert "web_turn_terminal_total" in METRICS


def test_metrica_nedeclarata_e_refuzata_in_strict(obs):
    with pytest.raises(ContractViolation):
        metrics.record_counter("web_inventat_total", outcome="ok")


def test_eticheta_nedeclarata_e_refuzata_in_strict(obs):
    with pytest.raises(ContractViolation):
        metrics.record_counter(
            "web_turn_terminal_total",
            status="completed",
            safe_error_code="none",
            release_track="champion",
            business_id="6098812a-50fc-44bd-a1ba-bc77e6399158",  # exact ce nu are voie
        )


def test_business_id_nu_e_eticheta_in_nicio_metrica():
    """P12 mecanic: nicio metrică nu poate purta identificatori de tenant/conversație/tur."""
    interzise = {"business_id", "conversation_id", "turn_id", "client_turn_id", "visitor_id", "ip"}
    for spec in METRICS.values():
        assert not (spec.label_keys & interzise), f"{spec.name} are etichetă interzisă"


# ── Cardinalitate ───────────────────────────────────────────────────────────────────────────


def test_bugetul_de_valori_distincte_izoleaza_explozia(obs_lenient):
    """`model_id` are buget 24: primele 24 rămân serii proprii, restul devin `other` + drop."""
    for i in range(40):
        hooks.on_model_call(f"model-{i}", model_role="agent", outcome="ok")
    snap = metrics.snapshot()["counters"]
    serii = [k for k in snap if k.startswith("web_model_calls_total")]
    assert len(serii) <= 25, f"cardinalitate scăpată: {len(serii)} serii"
    assert any("model_id=other" in k for k in serii)
    assert (
        snap.get(
            "web_observability_dropped_total{signal=label,reason=cardinality_budget}",
            0,
        )
        > 0
    ), "explozia s-a oprit, dar nimeni n-a numărat-o"


def test_valoare_de_eticheta_cu_text_liber_devine_other(obs_lenient):
    """Un `safe_error_code` construit din mesajul unei excepții cade aici — intenționat."""
    hooks.on_terminal(
        "failed",
        safe_error_code_="connection to 10.0.0.5 failed for user ionescu",
        release_track="champion",
    )
    snap = metrics.snapshot()["counters"]
    assert any("safe_error_code=other" in k for k in snap)
    assert not any("ionescu" in k for k in snap)


def test_latenta_negativa_nu_intra_in_percentile(obs_lenient):
    """Clock skew: sample invalid + contor, niciodată tăcut în histogramă."""
    metrics.record_histogram("web_turn_accept_duration_seconds", -0.5, outcome="accepted")
    assert metrics.histogram_for("web_turn_accept_duration_seconds", outcome="accepted") is None
    assert (
        metrics.snapshot()["counters"].get(
            "web_observability_dropped_total{signal=metric,reason=invalid_value}", 0
        )
        == 1
    )


def test_snapshot_e_determinist(obs):
    hooks.on_replay("completed")
    hooks.on_replay("completed")
    assert metrics.snapshot() == metrics.snapshot()


# ── Trace: derivare, continuitate, eșantionare ──────────────────────────────────────────────

TURN = "11111111-2222-3333-4444-555555555555"


def test_trace_id_e_derivat_determinist_din_turn_id():
    """Continuitatea peste restart NU cere nimic persistat: același turn ⇒ același trace."""
    a = tracing.trace_id_for(TURN, "secret")
    b = tracing.trace_id_for(TURN, "secret")
    assert a == b and len(a) == 32
    assert int(a, 16) != 0


def test_trace_id_nu_e_derivabil_din_turn_id_fara_secret():
    """Clientul cunoaște `turn_id`; fără secret ar putea calcula traceul (confidențialitate de
    corelare, nu izolare). Cu secret, nu poate."""
    assert tracing.trace_id_for(TURN, "secret-A") != tracing.trace_id_for(TURN, "secret-B")
    assert tracing.trace_id_for(TURN, "secret-A") != tracing.trace_id_for(TURN, "")


def test_reclaim_e_alt_span_in_acelasi_trace(obs):
    """Failure matrix: „worker restart după accept ⇒ trace continuat, attempt separat"."""
    with tracing.turn_trace(TURN, attempt=1):
        pass
    with tracing.turn_trace(TURN, attempt=2):
        pass
    tracing.get_exporter().flush()
    roots = obs.sink.by_name("web.turn.execute")
    assert len(roots) == 2
    assert roots[0].trace_id == roots[1].trace_id, "reclaim-ul a rupt traceul"
    assert roots[0].span_id != roots[1].span_id, "două încercări cu același span id"


def test_traceparent_din_browser_nu_devine_parinte(obs_lenient):
    """Un context public nesemnat nu poate atașa spans în traceul nostru — doar se numără."""
    tracing.reject_inbound_traceparent("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
    assert (
        metrics.snapshot()["counters"][
            "web_observability_dropped_total{signal=trace_context,reason=untrusted_inbound}"
        ]
        == 1
    )


def test_parse_traceparent_respinge_forme_invalide():
    assert tracing.parse_traceparent(None) is None
    assert tracing.parse_traceparent("garbage") is None
    assert tracing.parse_traceparent("00-" + "0" * 32 + "-" + "b" * 16 + "-01") is None
    ok = tracing.parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    assert ok == ("a" * 32, "b" * 16, True)


def test_esantionarea_pe_coada_pastreaza_traceul_cu_eroare():
    """Cu ratio 0 nu iese nimic — EXCEPTÂND turele care au eșuat. Altfel am arunca exact
    evenimentele pe care le investighează cineva."""
    bootstrap.setup(_settings(observability_sample_ratio=0.0))
    metrics.reset()
    sink = bootstrap.capture_sink()
    try:
        with tracing.turn_trace(TURN, attempt=1):
            pass
        tracing.get_exporter().flush()
        assert sink.spans == [], "tur sănătos exportat deși ratio=0"

        with pytest.raises(ValueError):
            with tracing.turn_trace(TURN, attempt=2):
                with tracing.span("web.validate", stage="validation"):
                    raise ValueError("boom")
        tracing.get_exporter().flush()
        assert [s.name for s in sink.spans] == ["web.validate", "web.turn.execute"]
        assert sink.by_name("web.validate")[0].status == "error"
    finally:
        obs_config.configure(None)
        tracing.set_exporter(None)
        metrics.reset()


def test_esantionarea_e_determinista_pe_trace_id():
    """Același trace dă același verdict în orice proces — altfel traces ciobite."""
    tid = tracing.trace_id_for(TURN, "s")
    assert tracing.should_sample(tid, 1.0) is True
    assert tracing.should_sample(tid, 0.0) is False
    assert tracing.should_sample(tid, 0.5) == tracing.should_sample(tid, 0.5)


def test_span_cu_nume_nedeclarat_e_refuzat_in_strict(obs):
    with tracing.turn_trace(TURN):
        with pytest.raises(ContractViolation):
            with tracing.span("web.inventat"):
                pass


def test_atribut_nedeclarat_nu_ajunge_pe_span(obs_lenient):
    with tracing.turn_trace(TURN):
        with tracing.span("web.validate", stage="validation") as sp:
            sp.set_attribute("prompt", "textul clientului")
    tracing.get_exporter().flush()
    sink = bootstrap.capture_sink()
    assert "prompt" not in sink.by_name("web.validate")[0].attributes


def test_turn_id_uuid_supravietuieste_pe_span(obs_lenient):
    """Regresie: `11111111-2222-3333-4444-555555555555` conține un run de 16 cifre care începe cu
    `4` și trece Luhn ⇒ detectorul de PII îl vede drept CARD. Aplicat pe `turn_id`, asta ar
    șterge ALEATORIU cheia de corelare pentru care există traceul. Identificatorii structurali
    generați de server se validează pe formă, nu se scanează."""
    with tracing.turn_trace(TURN, attempt=1):
        pass
    tracing.get_exporter().flush()
    root = bootstrap.capture_sink().by_name("web.turn.execute")[0]
    assert root.attributes["turn_id"] == TURN


def test_valoare_otravita_pe_eticheta_declarata_e_respinsa(obs_lenient):
    """Allowlist-ul pe CHEIE nu ajunge: o cheie API are formă perfectă de identificator."""
    hooks.on_model_call("sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", model_role="agent")
    snap = metrics.snapshot()["counters"]
    assert not any("sk-proj" in k for k in snap)
    assert any("model_id=other" in k for k in snap)


def test_span_fara_trace_activ_e_no_op(obs):
    """Job/script/cale neinstrumentată: nu inventăm traces orfane pe care nu le citește nimeni."""
    with tracing.span("web.validate", stage="validation") as sp:
        assert sp is None


def test_totul_e_no_op_cu_flagul_stins():
    """Default = byte-identic: nici span, nici contor, nici alocare."""
    bootstrap.setup(_settings(observability_enabled=False))
    metrics.reset(strict=True)
    try:
        with tracing.turn_trace(TURN) as sp:
            assert sp is None
        hooks.on_terminal("completed", safe_error_code_=None, release_track="champion")
        assert metrics.snapshot() == {"counters": {}, "histograms": {}}
    finally:
        obs_config.configure(None)
        tracing.set_exporter(None)
        metrics.reset()


# ── Export: coadă mărginită, drop numărat, exporter căzut ───────────────────────────────────


def _rec(i: int) -> SpanRecord:
    return SpanRecord("web.validate", "a" * 32, f"{i:016x}", None, 0, 1, "ok", {})


def test_coada_plina_arunca_si_numara(obs_lenient):
    dropped: list[tuple[str, str]] = []
    exp = SpanExporter(
        CaptureSink(), queue_max=3, batch=2, on_drop=lambda s, r: dropped.append((s, r))
    )
    for i in range(10):
        exp.enqueue(_rec(i))
    assert exp.depth == 3, "coada nu e mărginită"
    assert exp.stats.dropped_queue_full == 7
    assert dropped and dropped[0] == ("span", "queue_full")


def test_sink_care_crapa_nu_propaga_si_nu_pierde_bucla(obs_lenient):
    """Failure matrix: OTLP down ⇒ turul continuă. Exportul eșuat e numărat, nu ridicat."""

    class Broken:
        def emit_spans(self, spans):
            raise ConnectionError("colector jos")

        def emit_metrics(self, snapshot):
            return None

        def shutdown(self, timeout_s):
            return None

    exp = SpanExporter(Broken(), queue_max=10, batch=2)
    for i in range(4):
        exp.enqueue(_rec(i))
    exp.flush()  # nu ridică
    assert exp.stats.export_errors == 2
    assert exp.depth == 0, "batch-ul eșuat trebuie consumat, nu rebuclat la infinit"


def test_shutdown_e_marginit():
    async def _run():
        exp = SpanExporter(CaptureSink(), flush_timeout_ms=50)
        exp.start()
        exp.enqueue(_rec(1))
        await asyncio.wait_for(exp.shutdown(), timeout=5)

    asyncio.run(_run())


# ── Poarta de config ────────────────────────────────────────────────────────────────────────


def test_endpoint_fara_master_switch_refuza_bootul():
    with pytest.raises(obs_config.ObservabilityConfigError, match="OBSERVABILITY_ENABLED"):
        obs_config.from_settings(
            _settings(
                observability_enabled=False,
                observability_exporter="otlp",
                observability_otlp_endpoint="http://collector:4318/v1/traces",
            )
        )


def test_otlp_fara_endpoint_refuza_bootul():
    with pytest.raises(obs_config.ObservabilityConfigError, match="ENDPOINT"):
        obs_config.from_settings(_settings(observability_exporter="otlp"))


def test_endpoint_invalid_refuza_bootul():
    with pytest.raises(obs_config.ObservabilityConfigError, match="invalid"):
        obs_config.from_settings(
            _settings(observability_exporter="otlp", observability_otlp_endpoint="collector:4318")
        )


def test_release_track_necunoscut_refuza_bootul():
    with pytest.raises(obs_config.ObservabilityConfigError, match="RELEASE_TRACK"):
        obs_config.from_settings(_settings(release_track="prod-ish"))


def test_batch_peste_coada_refuza_bootul():
    with pytest.raises(obs_config.ObservabilityConfigError, match="batch"):
        obs_config.from_settings(
            _settings(observability_queue_max=10, observability_export_batch=100)
        )


def test_settings_reale_valideaza_poarta_la_boot(monkeypatch):
    """Poarta trăiește în `Settings` (ca NX-233/241): config imposibilă ⇒ procesul nu pornește."""
    from pydantic import ValidationError

    from src.config import Settings

    monkeypatch.setenv("OBSERVABILITY_OTLP_ENDPOINT", "http://collector:4318/v1/traces")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    with pytest.raises(ValidationError):
        Settings()
