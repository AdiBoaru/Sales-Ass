"""NX-248 — health/startup/readiness: semantica, nu prezența rutelor.

Testele urmăresc exact distincțiile care fac contractul util:
  • `live` nu atinge nimic (un Postgres jos NU repornește flota);
  • `required` scoate din rotație, `optional` produce `degraded` și rămâne în rotație;
  • schema prea veche și prea nouă sunt verdicte DIFERITE (cer acțiuni diferite);
  • răspunsul public nu spune ce a picat, cel de operator spune — dar niciunul nu spune secrete.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.observability import metrics
from src.ops import health
from src.ops.build_info import BuildInfo


@pytest.fixture(autouse=True)
def _clean_state():
    health.reset_startup_latch()
    health.reset_ready_cache()
    metrics.reset()
    yield
    health.reset_startup_latch()
    health.reset_ready_cache()
    metrics.reset()


def _build(requires: int = 42, tolerates: int = 43) -> BuildInfo:
    return BuildInfo(
        service="nativx-assistant",
        role="api",
        env="test",
        release_sha="abc1234",
        release_track="candidate",
        image_digest_claimed="sha256:" + "0" * 64,
        built_at="2026-08-17T00:00:00+00:00",
        config_revision="deadbeefcafe",
        schema_requires=requires,
        schema_tolerates=tolerates,
    )


# ── Matricea de dependențe ───────────────────────────────────────────────────────────────────


def test_redis_e_required_pe_api_si_optional_pe_worker():
    """Clasificarea NU e o preferință: pe `api` rate-limitul de accept e fail-closed, deci fără
    Redis marginea chiar nu poate servi; pe `worker` autoritatea e Postgres, iar admission cade
    pe local — un worker fără Redis lucrează mai încet, nu greșit."""
    assert health.requirement_for("redis", health.ROLE_API) is health.Requirement.REQUIRED
    assert health.requirement_for("redis", health.ROLE_WORKER) is health.Requirement.OPTIONAL


def test_dependenta_nedeclarata_pentru_rol_nu_e_sondata():
    assert health.requirement_for("executor", health.ROLE_API) is health.Requirement.NOT_APPLICABLE
    assert (
        health.requirement_for("ledger", health.ROLE_SCHEDULER) is health.Requirement.NOT_APPLICABLE
    )


def test_toate_componentele_din_matrice_sunt_in_vocabularul_de_metrici():
    """Dacă cineva adaugă o componentă și uită contractul, eticheta ar fi normalizată tăcut la
    `other` — adică exact motivul pentru care te uiți la metrică ar dispărea."""
    assert set(health.DEPENDENCY_MATRIX) <= health.COMPONENTS


# ── Verdicte de schemă ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("applied", "reason"),
    [
        (41, health.REASON_SCHEMA_TOO_OLD),
        (42, health.REASON_OK),
        (43, health.REASON_OK),
        (44, health.REASON_SCHEMA_TOO_NEW),
    ],
)
def test_schema_prea_veche_si_prea_noua_sunt_verdicte_distincte(monkeypatch, applied, reason):
    """Prea veche → rulează migrarea. Prea nouă → promovează imaginea corectă / oprește
    rollbackul. Un singur cod de eroare pentru ambele ar trimite operatorul în direcția greșită
    exact în jumătate din cazuri."""

    class _Conn:
        async def fetchval(self, *_a, **_k):
            return applied

    monkeypatch.setattr(health, "_probe_schema_conn", None, raising=False)
    result = asyncio.run(_probe_schema_with(_Conn(), _build()))
    assert result.reason == reason
    assert (result.state is health.State.OK) == (reason == health.REASON_OK)


async def _probe_schema_with(conn, build):
    """Rulează logica de verdict a `probe_schema` peste o conexiune falsă."""
    import contextlib

    from src.db import connection as db_connection

    @contextlib.asynccontextmanager
    async def _fake_admin_conn(_pool):
        yield conn

    async def _fake_pool():
        return object()

    import src.ops.health as mod

    orig_admin, orig_pool = db_connection.admin_conn, db_connection.get_pool
    db_connection.admin_conn, db_connection.get_pool = _fake_admin_conn, _fake_pool
    try:
        return await mod.probe_schema(build)
    finally:
        db_connection.admin_conn, db_connection.get_pool = orig_admin, orig_pool


# ── required vs optional în verdictul final ──────────────────────────────────────────────────


def test_optional_esuat_devine_degraded_si_ramane_in_rotatie(monkeypatch):
    async def _ok(component):
        return health.ProbeResult(component, health.State.OK, health.REASON_OK)

    async def _boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(health, "cached_build_info", lambda _role: _build())
    monkeypatch.setattr(health, "probe_schema", lambda _b: _ok("schema"))
    monkeypatch.setattr(
        health,
        "PROBES",
        {
            "postgres_control": lambda: _ok("postgres_control"),
            "postgres_tenant": lambda: _ok("postgres_tenant"),
            "ledger": lambda: _ok("ledger"),
            "redis": _boom,
            "executor": lambda: _ok("executor"),
        },
    )
    report = asyncio.run(health.check_ready(health.ROLE_WORKER, cache_s=0.0))
    assert report.ok is True, "Redis optional picat NU are voie să scoată workerul din rotație"
    assert "redis" in report.degraded
    states = {p.component: p.state for p in report.probes}
    assert states["redis"] is health.State.DEGRADED


def test_required_esuat_scoate_din_rotatie(monkeypatch):
    async def _ok(component):
        return health.ProbeResult(component, health.State.OK, health.REASON_OK)

    async def _boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(health, "cached_build_info", lambda _role: _build())
    monkeypatch.setattr(health, "probe_schema", lambda _b: _ok("schema"))
    monkeypatch.setattr(
        health,
        "PROBES",
        {
            "postgres_control": lambda: _ok("postgres_control"),
            "postgres_tenant": lambda: _ok("postgres_tenant"),
            "ledger": lambda: _ok("ledger"),
            "redis": _boom,
        },
    )
    report = asyncio.run(health.check_ready(health.ROLE_API, cache_s=0.0))
    assert report.ok is False
    assert {p.component: p.reason for p in report.probes}["redis"] == health.REASON_UNREACHABLE


def test_sonda_care_atarna_devine_timeout_nu_asteptare(monkeypatch):
    """O sondă fără deadline propriu ar face `/health/ready` să atârne exact când proxy-ul are
    nevoie de un răspuns rapid ca să te scoată din rotație."""

    async def _ok(component):
        return health.ProbeResult(component, health.State.OK, health.REASON_OK)

    async def _hang():
        await asyncio.sleep(10)
        return health.ProbeResult("redis", health.State.OK, health.REASON_OK)

    monkeypatch.setattr(health, "cached_build_info", lambda _role: _build())
    monkeypatch.setattr(health, "probe_schema", lambda _b: _ok("schema"))
    monkeypatch.setattr(health, "PROBES", {"redis": _hang})
    report = asyncio.run(health.check_ready(health.ROLE_API, timeout_s=0.05, cache_s=0.0))
    assert report.ok is False
    assert {p.component: p.reason for p in report.probes}["redis"] == health.REASON_TIMEOUT


# ── Ce iese pe sârmă ─────────────────────────────────────────────────────────────────────────


def test_raspunsul_public_nu_spune_ce_a_picat():
    report = health.HealthReport(
        kind="ready",
        ok=False,
        build=_build(),
        probes=(
            health.ProbeResult("postgres_tenant", health.State.FAILED, health.REASON_AUTH_FAILED),
        ),
        checked_at="2026-08-17T00:00:00+00:00",
    )
    public = json.dumps(report.public())
    assert "postgres" not in public and "auth_failed" not in public
    assert "unavailable" in public
    # Operatorul are voie să vadă: e cale autorizată, nu endpoint deschis.
    assert "auth_failed" in json.dumps(report.operator())


def test_nici_public_nici_operator_nu_scurg_secrete():
    """Canary în TOATE câmpurile pe care le compune raportul (aceeași disciplină ca testul de
    privacy NX-246: căutăm canary-ul în toată suprafața, nu doar unde ne așteptăm)."""
    canary = "sk-CANARY-nx248-do-not-leak"
    build = BuildInfo(
        service=canary,  # chiar dacă cineva bagă secretul într-un câmp greșit...
        role="api",
        env="test",
        release_sha="abc1234",
        release_track="champion",
        image_digest_claimed="unknown",
        built_at="unknown",
        config_revision="0" * 12,
        schema_requires=42,
        schema_tolerates=43,
    )
    report = health.HealthReport("ready", True, build, (), "2026-08-17T00:00:00+00:00")
    # ...ce iese trebuie să fie exact ce am pus noi acolo, nu un DSN sau un token din config.
    assert json.dumps(report.public()).count(canary) == 1
    for probe in report.probes:
        assert "password" not in json.dumps(probe.detail).lower()


def test_metricile_de_readiness_numara_tranzitii_nu_stari(monkeypatch):
    async def _ok(component):
        return health.ProbeResult(component, health.State.OK, health.REASON_OK)

    async def _boom():
        raise ConnectionError("down")

    monkeypatch.setattr(health, "cached_build_info", lambda _role: _build())
    monkeypatch.setattr(health, "probe_schema", lambda _b: _ok("schema"))
    monkeypatch.setattr(health, "PROBES", {"redis": lambda: _ok("redis")})
    metrics.reset(strict=True)
    from src.observability import config as obs_config
    from src.observability.config import ObservabilityConfig

    obs_config.configure(ObservabilityConfig(enabled=True, metrics_enabled=True))
    try:
        asyncio.run(health.check_ready(health.ROLE_API, cache_s=0.0))  # ok (prima citire)
        monkeypatch.setattr(health, "PROBES", {"redis": _boom})
        asyncio.run(health.check_ready(health.ROLE_API, cache_s=0.0))  # → unavailable
        snap = metrics.snapshot()["counters"]
        transitions = {k: v for k, v in snap.items() if k.startswith("ops_readiness_transitions")}
        assert transitions == {"ops_readiness_transitions_total{role=api,state=unavailable}": 1.0}
        # `strict=True` ar fi ridicat deja dacă vreo etichetă/componentă lipsea din contract.
        assert any(k.startswith("ops_dependency_probe_total") for k in snap)
    finally:
        obs_config.configure(None)


# ── Rutele HTTP ──────────────────────────────────────────────────────────────────────────────


def test_live_nu_atinge_nicio_dependenta(monkeypatch):
    """Dacă `live` ar sonda Postgres, o pană de 30s la provider ar reporni toată flota."""

    async def _explode(*_a, **_k):
        raise AssertionError("live nu are voie să facă I/O")

    monkeypatch.setattr(health, "check_ready", _explode)
    monkeypatch.setattr(health, "check_startup", _explode)
    from src.webhook.app import app

    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_detail_fara_token_e_404_nu_401(monkeypatch):
    """401 confirmă că ruta există și invită la ghicit — singura informație pe care o are de dat
    un endpoint de diagnostic."""
    from src.config import get_settings
    from src.webhook.app import app

    get_settings.cache_clear()
    monkeypatch.delenv("OPS_HEALTH_TOKEN", raising=False)
    with TestClient(app) as client:
        assert client.get("/health/detail").status_code == 404
        assert client.get("/health/detail", headers={"x-ops-token": "ghicit"}).status_code == 404
    get_settings.cache_clear()
