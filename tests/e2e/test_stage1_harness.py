"""NX-247 — self-testele harnessului. „Cine testează testul" nu e o glumă aici: un harness care
minte produce un gate verde pe un sistem rupt, adică exact opusul scopului.

Împărțirea e deliberată:

  • partea PURĂ (majoritatea) rulează în jobul rapid, pe fiecare PR: gărzile de pornire, vocabularul
    închis de rute de control, igiena probelor, garda de rețea, embedderul, acoperirea invarianților
    și a matricei de defecte, coerența pragurilor;
  • partea INTEGRATION (`integration and stage1_web`) atinge Postgres: migrări la zi, izolarea celor
    doi tenanți cu ID-uri vecine și faptul că retrievalul semantic găsește ce trebuie.

Câteva teste sunt de MUTAȚIE: strică deliberat o vedere sau un artefact și cer checkerului să pice.
Un checker care nu poate eșua nu e o verificare, e o decorațiune — și doar mutația arată diferența.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Settings
from src.observability import slo
from tests.e2e import stage1_app as ha
from tests.e2e import stage1_probes as probes
from tests.e2e import stage1_scenarios as sc
from tests.e2e.test_stage1_failure_matrix import CANONICAL_BACKEND_SCENARIOS

ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = ROOT / "tests" / "e2e"

BASE_ENV = {
    # Loopback, ca in `docker-compose.stage1-e2e.yml`: poarta de pornire refuza acum orice
    # baza care nu e pe loopback, iar BASE_ENV trebuie sa reprezinte ce ruleaza cu adevarat.
    "SUPABASE_DB_URL": "postgresql://stage1:stage1@127.0.0.1:55432/stage1",
    "REDIS_URL": "redis://cache.internal:6379/0",
    "OPENAI_API_KEY": "sk-test",
    "META_VERIFY_TOKEN": "verify-123",
    "WEB_ACTION_KEYS": "e2e1:" + "A" * 44,
    "WEB_TURN_FINGERPRINT_SECRET": "fingerprint-secret-for-tests-0123456789",
    "WEB_FEEDBACK_PROMPT_SECRET": "prompt-secret-for-tests-0123456789",
}

CONTROL_SECRET = "s" * 64


def _settings(monkeypatch: pytest.MonkeyPatch, profile: str | None, **overrides) -> Settings:
    """`Settings` construit din profilul de flag-uri, ignorând `.env` local (test determinist).
    Dacă profilul ar fi o combinație ILEGALĂ de flag-uri, poarta de boot din `Settings` ridică —
    adică testul dovedește și legalitatea configurației pe care harnessul o declară."""
    env = dict(BASE_ENV)
    if profile:
        env.update(ha.FLAG_PROFILES[profile])
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Settings(_env_file=None)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    import src.config

    monkeypatch.setattr(src.config, "get_settings", lambda: settings)


# ── Gărzile de pornire ──────────────────────────────────────────────────────────────────────


def test_refuses_outside_env_test(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE, ENV="dev"))
    with pytest.raises(ha.HarnessRefused, match="ENV=test"):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


def test_refuses_production_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ENV=prod` cade pe prima gardă; testul există ca `is_prod` să rămână verificat EXPLICIT,
    nu doar implicit prin egalitatea cu „test" (dacă mâine apare `ENV=production`, tot cade)."""
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE, ENV="prod"))
    with pytest.raises(ha.HarnessRefused):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.com", ""])  # noqa: S104
def test_refuses_non_loopback_bind(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE))
    with pytest.raises(ha.HarnessRefused, match="loopback"):
        ha.assert_harness_allowed(bind_host=host, control_secret=CONTROL_SECRET)


@pytest.mark.parametrize("secret", ["", "short", "s" * 31])
def test_refuses_weak_control_secret(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE))
    with pytest.raises(ha.HarnessRefused, match="secretul de control"):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=secret)


def test_refuses_when_the_real_web_router_would_not_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mai bine refuz decât un harness care „trece" fără suprafața pe care o exersează."""
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE, WEB_ENABLED="false"))
    with pytest.raises(ha.HarnessRefused, match="WEB_ENABLED"):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


def test_refuses_when_v2_routes_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        _settings(
            monkeypatch,
            ha.CERTIFIED_PROFILE,
            WEB_TURN_V2_ENABLED="false",
            WEB_ACTIONS_ENABLED="false",
            WEB_FEEDBACK_ENABLED="false",
        ),
    )
    with pytest.raises(ha.HarnessRefused, match="WEB_TURN_V2_ENABLED"):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


def test_allows_the_certified_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE))
    ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


@pytest.mark.parametrize("profile", sorted(ha.FLAG_PROFILES))
def test_flag_profiles_are_legal_combinations(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """Porțile de boot din `Settings` (NX-241/246) refuză combinații imposibile. Dacă un profil
    declarat de harness ar fi ilegal, gate-ul ar certifica o configurație care nu pornește."""
    settings = _settings(monkeypatch, profile)
    assert settings.web_turn_v2_enabled
    assert settings.env == "test"


def test_certified_profile_is_the_transport_one() -> None:
    """Profilul certificat NU e cel de creier unic: promovarea `search_entities` are verdict
    `NOT-READY` (NX-238), iar NX-247 nu are voie să emită un GO pe care alt gate nu l-a dat."""
    assert ha.CERTIFIED_PROFILE == "v2_transport"
    assert ha.FLAG_PROFILES["v2_single_brain"]["SINGLE_BRAIN_ENABLED"] == "true"
    assert "SINGLE_BRAIN_ENABLED" not in ha.FLAG_PROFILES[ha.CERTIFIED_PROFILE]


# ── Routerul de control ─────────────────────────────────────────────────────────────────────


def _control_client() -> tuple[TestClient, ha.HarnessState]:
    """Router de control pe o aplicație PROPRIE. Deliberat nu pe cea de producție: un test unit nu
    are voie să mute la nivel global obiectul `app` pe care rulează restul suitei."""
    st = ha.HarnessState(control_secret=CONTROL_SECRET)
    app = FastAPI()
    app.include_router(ha.control_router(st))
    # `client=` e obligatoriu: hostul implicit al TestClient e „testclient", care NU e loopback —
    # iar poarta harnessului îl respinge, corect. Un test care ar ocoli asta ar dovedi altceva.
    return TestClient(app, client=("127.0.0.1", 12345)), st


def test_control_route_vocabulary_is_closed() -> None:
    st = ha.HarnessState(control_secret=CONTROL_SECRET)
    paths = {r.path for r in ha.control_router(st).routes}
    assert paths == set(ha.CONTROL_ROUTES), (
        f"rute nedeclarate: {sorted(paths - set(ha.CONTROL_ROUTES))}; "
        f"declarate inexistente: {sorted(set(ha.CONTROL_ROUTES) - paths)}"
    )


def test_control_router_exposes_no_clock_or_id_route() -> None:
    """Cardul cere ca ceasul și ID-urile să fie controlabile DOAR prin seam de fixture. O rută de
    control care mută ceasul ar fi exact „scenario switch în producție" pe altă ușă."""
    blob = " ".join(sorted(ha.CONTROL_ROUTES))
    for banned in ("clock", "time", "now", "uuid", "seed", "id/set"):
        assert banned not in blob, f"rută de control interzisă: {banned!r}"


def test_control_routes_require_the_process_secret() -> None:
    client, _ = _control_client()
    assert client.get(f"{ha.CONTROL_PREFIX}/health").status_code == 404
    assert (
        client.get(f"{ha.CONTROL_PREFIX}/health", headers={ha.CONTROL_HEADER: "wrong"}).status_code
        == 404
    )
    ok = client.get(f"{ha.CONTROL_PREFIX}/health", headers={ha.CONTROL_HEADER: CONTROL_SECRET})
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_control_refusal_is_404_not_403() -> None:
    """404, nu 403: un 403 confirmă că ruta există. Harnessul nu trebuie să fie descoperibil nici
    dacă ajunge, printr-o greșeală de deploy, într-un loc accesibil."""
    client, _ = _control_client()
    assert client.post(f"{ha.CONTROL_PREFIX}/reset").status_code == 404


def test_control_routes_reject_non_loopback_clients() -> None:
    client, _ = _control_client()
    with TestClient(client.app, client=("203.0.113.7", 1234)) as remote:
        assert (
            remote.get(
                f"{ha.CONTROL_PREFIX}/health", headers={ha.CONTROL_HEADER: CONTROL_SECRET}
            ).status_code
            == 404
        )


def test_scenario_route_rejects_unknown_scripts_and_faults() -> None:
    client, st = _control_client()
    h = {ha.CONTROL_HEADER: CONTROL_SECRET}
    assert (
        client.post(f"{ha.CONTROL_PREFIX}/scenario", json={"script": "nope"}, headers=h).status_code
        == 400
    )
    assert (
        client.post(f"{ha.CONTROL_PREFIX}/fault", json={"fault": "nope"}, headers=h).status_code
        == 400
    )
    assert st.fault == "none"


def test_reset_isolates_scenarios_between_tests() -> None:
    client, st = _control_client()
    h = {ha.CONTROL_HEADER: CONTROL_SECRET}
    client.post(f"{ha.CONTROL_PREFIX}/scenario", json={"script": "no_results"}, headers=h)
    client.post(f"{ha.CONTROL_PREFIX}/fault", json={"fault": "model_timeout"}, headers=h)
    st.llm.counters.classify = 7
    client.post(f"{ha.CONTROL_PREFIX}/reset", headers=h)
    assert st.fault == "none"
    assert st.llm.script.script == "recommend"
    assert st.llm.counters.snapshot()["calls_total"] == 0


def test_probe_routes_never_take_business_id_from_the_caller() -> None:
    """`business_id` e server-owned (P7). Dacă ruta de probă l-ar accepta din query, ea ar fi un
    oracol cross-tenant — exact ce matricea R18 spune că nu are voie să existe. Verificarea e pe
    SEMNĂTURĂ (parametrii handlerului), nu pe convenție de denumire."""
    import inspect

    st = ha.HarnessState(control_secret=CONTROL_SECRET)
    for route in ha.control_router(st).routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        params = set(inspect.signature(endpoint).parameters)
        assert "business_id" not in params, f"{route.path} acceptă business_id de la apelant"


def test_one_shot_faults_fire_exactly_once() -> None:
    """Un defect tranzitoriu care s-ar repeta la infinit nu dovedește „retry bounded", ci
    renunțarea."""
    st = ha.HarnessState(control_secret=CONTROL_SECRET)
    st.arm_fault("db_transient_at_commit")
    assert st.take_fault("db_transient_at_commit") is True
    assert st.take_fault("db_transient_at_commit") is False
    st.arm_fault("redis_dead")
    assert st.take_fault("redis_dead") is True
    assert st.take_fault("redis_dead") is True  # persistent, deliberat


# ── Igiena probelor ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(probes.PROBE_SQL))
def test_probe_sql_is_read_only_and_tenant_scoped(name: str) -> None:
    sql = " ".join(probes.PROBE_SQL[name].lower().split())
    assert sql.startswith("select "), f"{name}: proba nu începe cu select"
    assert "business_id = $1" in sql, f"{name}: proba nu filtrează explicit business_id = $1 (P7)"
    for verb in probes.FORBIDDEN_SQL_VERBS:
        assert not re.search(rf"\b{verb}\b", sql), f"{name}: proba conține verbul {verb!r}"


def test_no_sql_lives_outside_the_probe_registry() -> None:
    """Tot SQL-ul de probă e în `PROBE_SQL`; altfel testul de igienă de mai sus ar acoperi doar
    jumătate din statements și n-ar spune nimic despre restul."""
    source = (E2E_DIR / "stage1_probes.py").read_text(encoding="utf-8")
    body = source.split("PROBE_SQL: dict[str, str] = {", 1)[1].split("\n}\n", 1)[1]
    assert "select " not in body.lower(), "SQL în afara registrului PROBE_SQL"


def test_every_probe_statement_is_reachable() -> None:
    source = (E2E_DIR / "stage1_probes.py").read_text(encoding="utf-8")
    for name in probes.PROBE_SQL:
        assert source.count(f'"{name}"') >= 2, f"probă declarată dar nefolosită: {name}"


def test_probes_return_no_free_text_columns() -> None:
    """Probele întorc numere, statusuri și nume de produse sintetice. `safe_body`, `visitor_id`,
    tokenuri sau `response_json` nu au ce căuta într-un artefact de CI."""
    blob = " ".join(probes.PROBE_SQL.values()).lower()
    for banned in ("safe_body", "visitor", "sender_external_id", "session_ref", "action_payload"):
        assert banned not in blob, f"probele expun {banned!r}"


# ── Suprafața de producție ──────────────────────────────────────────────────────────────────


def test_production_image_does_not_ship_the_harness() -> None:
    """DoD: harnessul nu intră în imaginea de producție. Verificat pe Dockerfile, nu pe cuvânt."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = [line for line in dockerfile.splitlines() if line.strip().upper().startswith("COPY")]
    joined = " ".join(copies)
    assert "tests" not in joined, (
        "Dockerfile copiază tests/ — routerul de control ar ajunge în prod"
    )
    assert "stage1_e2e_server" not in joined, "Dockerfile copiază launcherul harnessului"
    assert "scripts/" not in joined or "scripts/migrate.py" in joined, (
        "Dockerfile copiază tot scripts/ — inclusiv launcherul"
    )


def test_no_production_module_imports_the_harness() -> None:
    """Direcția dependenței e o singură sens: `tests/e2e` vede `src/`, niciodată invers. Un import
    invers ar face harnessul parte din produs."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "tests.e2e" in path.read_text(encoding="utf-8")
        or "tests/e2e" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"module de producție care referă harnessul: {offenders}"


def test_no_test_only_flag_exists_in_settings() -> None:
    """Cardul interzice un provider fake selectabil în producție printr-un env public. Injecția
    trăiește în `tests/`, ca rescriere de atribut — nu ca opțiune de configurație."""
    config_src = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
    for banned in ("FAKE_LLM", "USE_FAKE", "STAGE1_TEST", "TEST_MODE", "MOCK_LLM"):
        assert banned not in config_src, f"`Settings` expune un comutator de test: {banned}"


# ── Garda de rețea ──────────────────────────────────────────────────────────────────────────


def test_network_guard_denies_provider_hosts_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE))
    guard = ha.deny_outbound_network()
    try:
        with pytest.raises(ha.OutboundNetworkDenied):
            import socket

            socket.getaddrinfo("api.openai.com", 443)
    finally:
        guard.uninstall()
    assert guard.attempts == ["dns:api.openai.com:443"]


def test_network_guard_allows_loopback_and_configured_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DSN remote ANUME: aici se verifică allowlistul de rețea, care trebuie să deducă hostul de DB
    # din configurație. Cu un DSN de loopback assertul ar trece trivial și n-ar mai dovedi nimic.
    # Poarta de pornire (care cere loopback) nu e chemată în acest test.
    _patch_settings(
        monkeypatch,
        _settings(
            monkeypatch,
            ha.CERTIFIED_PROFILE,
            SUPABASE_DB_URL="postgresql://u:p@db.internal:5432/db",
        ),
    )
    guard = ha.deny_outbound_network()
    try:
        assert guard._allowed("127.0.0.1")
        assert guard._allowed("db.internal"), "hostul de DB din configurație trebuie permis"
        assert guard._allowed("cache.internal"), "hostul de Redis trebuie permis"
        assert not guard._allowed("api.anthropic.com")
    finally:
        guard.uninstall()
    assert guard.attempts == []


async def test_fake_model_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dovada POZITIVĂ cerută de card: modelul fals trece prin toate metodele sub garda de rețea,
    iar contorul de refuzuri rămâne zero."""
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE))
    guard = ha.deny_outbound_network()
    llm = sc.Stage1FakeLLM()
    try:
        await llm.moderate("salut")
        await llm.embed(["ser cu vitamina C"])
        await llm.classify_json("s", "u")
        await llm.complete("s", "u")
        await llm.run_tool_loop("s", "u", [], _no_tools)
    finally:
        guard.uninstall()
    assert guard.attempts == []
    snapshot = llm.counters.snapshot()
    assert snapshot["calls_total"] == 5
    assert snapshot["generations"] == 1


#: Formatul REAL al unui rezultat de tool către model (`src/tools/catalog_tools.py::_brief`).
#: Testele folosesc EXACT forma de sârmă: prima versiune folosea JSON inventat, iar fake-ul „trecea"
#: pe o formă pe care producția nu emite niciodată — pe DB real nu extrăgea nimic.
NEWLINE = chr(10)


def _tool_line(pid: str, name: str, price: float) -> str:
    return f"[{pid}] {name} | Lumea Blanda | {price:.2f} lei | 4.6★ | stoc: in_stock | sumar"


async def _no_tools(name: str, args: dict) -> str:
    return "Niciun produs găsit."


def test_fake_model_refuses_unknown_script() -> None:
    with pytest.raises(ValueError, match="script necunoscut"):
        sc.Stage1FakeLLM().arm("whatever")


async def test_fake_model_grounds_prose_in_actual_tool_output() -> None:
    """Fake-ul nu are voie să inventeze nume: dacă tool-ul întoarce alt produs, proza se schimbă.
    Un fake cu text fix ar trece pe lângă validator și pe lângă grounding guard."""
    llm = sc.Stage1FakeLLM()
    llm.arm("recommend")

    async def execute(name: str, args: dict) -> str:
        return _tool_line("p1", "Ser Inventat X", 42.0)

    text = await llm.run_tool_loop("s", "u", [], execute)
    assert "Ser Inventat X" in text
    assert llm.counters.tool_calls == ["search_products"]


async def test_fake_model_prose_carries_no_numbers_except_when_asked_for_a_price() -> None:
    """NX-240: faptele stau pe carduri, nu în proză. Excepția motivată (întrebare despre preț)
    folosește EXACT cifra din rezultatul tool-ului."""
    llm = sc.Stage1FakeLLM()

    async def execute(name: str, args: dict) -> str:
        return _tool_line("p1", "Ser Alfa", 89.0)

    llm.arm("recommend")
    assert not re.search(r"\d", await llm.run_tool_loop("s", "u", [], execute))
    llm.arm("price_of_context_product")
    assert "89 lei" in await llm.run_tool_loop("s", "u", [], execute)


# ── Embedderul determinist ──────────────────────────────────────────────────────────────────


def test_embedder_dimension_matches_pgvector() -> None:
    assert sc.EMBED_DIM == 1536
    assert len(sc.embed_text("ser")) == 1536


def test_embedder_is_deterministic() -> None:
    assert sc.embed_text("ser cu vitamina C") == sc.embed_text("ser cu vitamina C")
    assert sc.embed_text("Ser cu Vitamina C") == sc.embed_text("ser cu vitamina c")


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_embedder_ranks_related_text_closer_than_unrelated() -> None:
    """Fără această proprietate, retrievalul din harness ar fi hazard determinist: ar trece, dar
    n-ar exersa ranking-ul."""
    query = sc.embed_text("ser cu vitamina C pentru ten uscat")
    related = sc.embed_text(sc.ALPHA_PRODUCTS[0].document)
    unrelated = sc.embed_text(sc.ALPHA_PRODUCTS[4].document)
    assert _cos(query, related) > _cos(query, unrelated)


def test_embedder_handles_empty_text() -> None:
    assert sc.embed_text("") == [0.0] * sc.EMBED_DIM


def test_sibling_business_ids_differ_only_in_the_last_nibble() -> None:
    low, high = sc.sibling_business_ids()
    assert low != high
    assert low[:-1] == high[:-1], "tenanții de test trebuie să aibă ID-uri vecine"


def test_faults_vocabulary_is_closed() -> None:
    assert "none" in sc.FAULTS
    with pytest.raises(Exception):  # noqa: B017 — HTTPException sau ValueError, ambele acceptabile
        ha.HarnessState(control_secret=CONTROL_SECRET).arm_fault("cosmic_ray")


# ── Acoperirea invarianților ────────────────────────────────────────────────────────────────


def test_every_backend_invariant_has_an_executable_checker() -> None:
    declared = sc.backend_invariants()
    implemented = set(sc.INVARIANT_CHECKS)
    assert declared == implemented, (
        f"invarianți backend fără checker: {sorted(declared - implemented)}; "
        f"checkere nedeclarate: {sorted(implemented - declared)}"
    )


def test_every_frontend_invariant_names_a_spec() -> None:
    """Un invariant „owner=frontend" fără spec e o promisiune fără adresă: PR B nu are ce
    implementa, iar acoperirea raportează un invariant care nu se verifică nicăieri."""
    for name, spec in sc.manifest()["invariants"].items():
        if name.startswith("_") or spec["owner"] != "frontend":
            continue
        assert spec.get("frontend_spec"), f"{name}: invariant de frontend fără `frontend_spec`"


def test_every_invariant_has_a_reason() -> None:
    for name, spec in sc.manifest()["invariants"].items():
        if name.startswith("_"):
            continue
        assert spec.get("why") or spec.get("frontend_spec"), f"{name}: invariant fără justificare"


def test_every_scenario_references_declared_invariants() -> None:
    known = set(sc.invariant_owners())
    for scenario in sc.manifest()["scenarios"]:
        unknown = sorted(set(scenario["invariants"]) - known)
        assert not unknown, f"{scenario['id']}: invarianți nedeclarați {unknown}"
        assert scenario["invariants"], f"{scenario['id']}: scenariu fără niciun invariant"


REQUIRED_SCENARIOS = (
    "welcome",
    "text_answer",
    "recommendation",
    "comparison",
    "clarification",
    "no_results",
    "routine",
    "memory_correction",
    "product_context",
    "commerce_success",
    "commerce_stale",
    "terminal_failure",
    "deadline_degraded",
    "feedback",
    "unknown_optional_block",
    "unsupported_major",
)


def test_all_canonical_scenarios_from_the_card_are_present() -> None:
    have = [s["id"] for s in sc.manifest()["scenarios"]]
    assert len(have) == len(set(have)), "id-uri de scenariu duplicate"
    missing = sorted(set(REQUIRED_SCENARIOS) - set(have))
    assert not missing, f"scenarii canonice absente: {missing}"


def test_every_scenario_uses_a_declared_tenant() -> None:
    tenants = {t["id"] for t in sc.manifest()["tenants"]}
    for scenario in sc.manifest()["scenarios"]:
        assert scenario["tenant"] in tenants, f"{scenario['id']}: tenant necunoscut"


# ── Acoperirea matricei de defecte ──────────────────────────────────────────────────────────


def _matrix() -> list[dict]:
    return sc.manifest()["failure_matrix"]


def test_failure_matrix_covers_r1_to_r22() -> None:
    ids = [row["id"] for row in _matrix()]
    assert ids == [f"R{i}" for i in range(1, 23)], f"matricea nu e completă/ordonată: {ids}"


def _test_names_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def test_every_matrix_row_points_at_a_test_that_exists() -> None:
    """Anti-„gate fals": o referință de test care nu există e mai rea decât un test lipsă, fiindcă
    arată ca acoperire. Se verifică prin parsare AST, nu prin căutare de text."""
    for row in _matrix():
        ref = row.get("backend_test")
        if ref is None:
            continue
        rel, _, func = ref.partition("::")
        path = ROOT / rel
        assert path.exists(), f"{row['id']}: fișierul {rel} nu există"
        assert func in _test_names_in(path), f"{row['id']}: testul {func} nu există în {rel}"


def test_rows_without_a_backend_test_are_justified() -> None:
    for row in _matrix():
        if row.get("backend_test") is not None:
            continue
        reason = row.get("backend_test_absent_because") or ""
        assert len(reason) > 80, (
            f"{row['id']}: absența unui test backend cere o justificare explicită, nu o notă"
        )
        assert row.get("frontend_spec"), f"{row['id']}: fără test backend și fără spec frontend"


def test_every_matrix_row_names_both_expectations() -> None:
    for row in _matrix():
        assert row.get("expect_backend"), f"{row['id']}: fără așteptare de backend"
        assert row.get("expect_frontend"), f"{row['id']}: fără așteptare de frontend"
        assert row.get("severity") in ("P0", "P1"), f"{row['id']}: severitate necunoscută"


def test_p0_rows_r1_to_r20_are_not_downgraded() -> None:
    """Cardul spune explicit: P0 R1–R20 nu pot fi marcate „flaky/quarantine". Severitatea lor e
    parte din contract, nu o etichetă de convenienți."""
    by_id = {row["id"]: row for row in _matrix()}
    for i in range(1, 21):
        assert by_id[f"R{i}"]["severity"] == "P0", f"R{i} nu mai e P0"


# ── Praguri ─────────────────────────────────────────────────────────────────────────────────


def test_thresholds_do_not_invent_latency_targets() -> None:
    """NX-247 nu inventează praguri. Dacă `slo.RATIFIED` s-ar aprinde fără ca artefactul să se
    actualizeze, testul pică — pragurile n-au voie să divergă între cod și gate."""
    th = sc.thresholds()
    assert th["sources"]["latency"]["ratified"] is slo.RATIFIED
    assert th["sources"]["latency"]["manifest"] == slo.LATENCY_SOURCE_MANIFEST
    for spec in th["gate"]["reported_not_judged"].values():
        if isinstance(spec, dict):
            assert spec.get("ratified") is False


def test_thresholds_min_samples_matches_slo_policy() -> None:
    assert sc.thresholds()["sources"]["sample_size"]["value"] == slo.MIN_SAMPLES


def test_correctness_thresholds_are_all_zero() -> None:
    zeros = sc.thresholds()["gate"]["must_be_zero"]
    for name, value in zeros.items():
        if name.startswith("_"):
            continue
        assert value == 0, f"{name} nu e zero — un invariant cu toleranță nu e un invariant"


def test_flake_policy_forbids_retries_on_p0() -> None:
    policy = sc.thresholds()["flake_policy"]
    assert policy["p0_retries"] == 0
    assert policy["p0_quarantine_allowed"] is False
    assert policy["p0_skip_allowed"] is False


def test_browser_matrix_requires_all_engines_before_release() -> None:
    matrix = sc.thresholds()["browser_matrix"]
    assert matrix["per_pull_request"] == ["chromium"]
    assert set(matrix["required_before_nx249_release"]) == {"chromium", "firefox", "webkit"}
    assert 320 in matrix["viewports"], "viewportul minim din card lipsește"


# ── Mutație: checkerele pot să eșueze ───────────────────────────────────────────────────────

_TENANT_FOR_CHECKS = sc.SyntheticTenant(
    key="alpha",
    business_id="00000000-0000-0000-0000-00000000000a",
    slug="alpha",
    locale="ro",
    channel_id="c",
    channel_token="t",
    session_secret="s",
    products=sc.ALPHA_PRODUCTS,
)


def _view(**over) -> dict:
    base = {
        "schema_version": "web-view.v2",
        "conversation": {"id": "c", "revision": 1},
        "turn": {"id": "t", "client_turn_id": "u", "status": "completed"},
        "messages": [{"id": "m", "role": "assistant", "blocks": [{"type": "text", "text": "ok"}]}],
    }
    base.update(over)
    return base


def _inp(view: dict, **over) -> sc.InvariantInput:
    return sc.InvariantInput(view=view, tenant=_TENANT_FOR_CHECKS, **over)


def test_renderable_checker_fails_on_a_silent_terminal() -> None:
    with pytest.raises(AssertionError):
        sc.INVARIANT_CHECKS["terminal_view_renderable"](_inp(_view(messages=[])))


def test_renderable_checker_fails_on_divider_only() -> None:
    view = _view(messages=[{"id": "m", "role": "assistant", "blocks": [{"type": "divider"}]}])
    with pytest.raises(AssertionError):
        sc.INVARIANT_CHECKS["terminal_view_renderable"](_inp(view))


def test_display_strings_checker_catches_a_raw_number_anywhere() -> None:
    view = _view(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "type": "product_list",
                        "items": [{"view_id": "v", "title": "X", "price": {"current": 89.0}}],
                    }
                ],
            }
        ]
    )
    with pytest.raises(AssertionError, match="număr pe sârmă"):
        sc.INVARIANT_CHECKS["display_strings_only"](_inp(view))


def test_display_strings_checker_allows_conversation_revision() -> None:
    sc.INVARIANT_CHECKS["display_strings_only"](_inp(_view()))


def test_price_checker_catches_a_price_that_is_not_in_the_catalog() -> None:
    product = sc.ALPHA_PRODUCTS[0]
    view = _view(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "type": "product_list",
                        "items": [
                            {
                                "view_id": "v",
                                "title": product.name,
                                "price": {"current": "77,00 lei"},
                            }
                        ],
                    }
                ],
            }
        ]
    )
    with pytest.raises(AssertionError, match="snapshot"):
        sc.INVARIANT_CHECKS["prices_match_catalog_snapshot"](_inp(view))


def test_price_checker_accepts_the_seeded_price() -> None:
    product = sc.ALPHA_PRODUCTS[0]
    view = _view(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "type": "product_list",
                        "items": [
                            {
                                "view_id": "v",
                                "title": product.name,
                                "price": {"current": f"{product.effective_price},00 lei"},
                            }
                        ],
                    }
                ],
            }
        ]
    )
    sc.INVARIANT_CHECKS["prices_match_catalog_snapshot"](_inp(view))


def test_tenant_checker_catches_a_product_from_the_other_catalog() -> None:
    view = _view(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [
                    {
                        "type": "product_list",
                        "items": [{"view_id": "v", "title": sc.BETA_PRODUCTS[0].name}],
                    }
                ],
            }
        ]
    )
    with pytest.raises(AssertionError, match="alt catalog"):
        sc.INVARIANT_CHECKS["product_ids_from_own_tenant"](_inp(view))


def test_cot_checker_catches_leaked_reasoning() -> None:
    view = _view(
        messages=[
            {
                "id": "m",
                "role": "assistant",
                "blocks": [{"type": "text", "text": "Thought: caut in catalog"}],
            }
        ]
    )
    with pytest.raises(AssertionError, match="raționament"):
        sc.INVARIANT_CHECKS["no_chain_of_thought"](_inp(view))


def test_unknown_block_checker_catches_an_undeclared_type() -> None:
    view = _view(
        messages=[{"id": "m", "role": "assistant", "blocks": [{"type": "iframe", "src": "x"}]}]
    )
    with pytest.raises(AssertionError, match="bloc necunoscut"):
        sc.INVARIANT_CHECKS["only_known_block_types"](_inp(view))


def test_execution_checker_catches_a_second_generation() -> None:
    with pytest.raises(AssertionError, match="generări"):
        sc.INVARIANT_CHECKS["single_execution"](_inp(_view(), counters={"generations": 2}))


def test_receipt_checker_catches_a_duplicate() -> None:
    with pytest.raises(AssertionError, match="receipts"):
        sc.INVARIANT_CHECKS["one_receipt_per_action"](_inp(_view(), probes={"receipts": 2}))


def test_revoked_need_checker_catches_a_surviving_need() -> None:
    state = {"revoked": ["skin:ten_uscat"], "active_needs": ["skin:ten_uscat"]}
    with pytest.raises(AssertionError, match="revocată"):
        sc.INVARIANT_CHECKS["revoked_need_absent"](_inp(_view(), state=state))


def test_check_invariants_reports_what_it_actually_ran() -> None:
    """Cheia acoperirii oneste: apelantul află CE s-a rulat, ca invarianții de frontend să nu
    treacă drept verificați de backend."""
    ran = sc.check_invariants(
        ["terminal_view_renderable", "dom_order_equals_backend_order"], _inp(_view())
    )
    assert ran == ["terminal_view_renderable"]


# ── Integration: DB real ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.stage1_web
async def test_migrations_are_current_on_the_target_db() -> None:
    """Harnessul nu are voie să ruleze pe o schemă incompletă: ar raporta „recovery a picat" acolo
    unde de fapt lipsește un tabel."""
    from scripts.migrate import pending_migrations
    from src.db.connection import admin_conn, close_pool, get_pool

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            pending = await pending_migrations(conn)
    finally:
        await close_pool()
    assert not pending, f"migrări neaplicate: {[m.filename for m in pending]}"


@pytest.mark.integration
@pytest.mark.stage1_web
async def test_seeded_tenants_are_isolated_and_searchable() -> None:
    """Cele două tenanți cu ID-uri vecine: fiecare vede DOAR catalogul lui, iar retrievalul
    semantic (pgvector real) găsește produsele intenționate."""
    from src.config import get_settings
    from src.db.connection import admin_conn, close_pool, get_pool
    from src.db.queries.catalog import has_embeddings, search_products_semantic

    settings = get_settings()
    alpha, beta = sc.make_tenants()
    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            for tenant in (alpha, beta):
                await sc.seed_tenant(conn, tenant, embed_model=settings.model_embed)
        async with admin_conn(pool) as conn:
            assert await has_embeddings(conn, alpha.business_id)
            assert await probes.product_names(conn, alpha.business_id) == sorted(
                p.name for p in sc.ALPHA_PRODUCTS
            )
            assert await probes.product_names(conn, beta.business_id) == sorted(
                p.name for p in sc.BETA_PRODUCTS
            )
            hits = await search_products_semantic(
                conn,
                alpha.business_id,
                sc.embed_text("ser cu vitamina C pentru ten uscat"),
                limit=3,
            )
            assert len(hits) == 3, "retrievalul semantic nu a întors trei candidați"
            assert all("vitamina C" in h["name"] for h in hits), (
                f"ranking pe hazard, nu pe semnal: {[h['name'] for h in hits]}"
            )
            beta_names = {p.name for p in sc.BETA_PRODUCTS}
            assert not (beta_names & {h["name"] for h in hits}), "scurgere cross-tenant"
    finally:
        async with admin_conn(pool) as conn:
            for tenant in (alpha, beta):
                await sc.drop_tenant(conn, tenant.business_id)
        await close_pool()


# ── Acoperirea declarată vs cea care se execută ─────────────────────────────────────────────
# Testele sunt PURE (citesc manifestul + constanta suitei) și de aceea trăiesc aici, în jobul rapid:
# un scenariu marcat `covered` fără să ruleze nimic pentru el e o minciună care trebuie prinsă pe
# fiecare PR, nu doar în nightly.


def test_declared_backend_coverage_matches_what_runs() -> None:
    """Acoperirea declarată în manifest = acoperirea care se EXECUTĂ.

    Fără acest test, un scenariu putea fi marcat `covered` fără să ruleze nimic pentru el (sau
    invers), iar raportul de gate ar fi numărat scenarii pe care nu le-a măsurat — „gate fals".

    `welcome`, `deadline_degraded` și `feedback` nu trec prin testul parametrizat de mai jos
    (primul n-are tur, celelalte două au mecanică proprie), deci sunt enumerate explicit.
    """
    declared = {s["id"] for s in sc.manifest()["scenarios"] if s["backend_coverage"] == "covered"}
    runs = set(CANONICAL_BACKEND_SCENARIOS) | {"welcome", "deadline_degraded", "feedback"}
    assert declared == runs, (
        f"declarate `covered` dar nerulate: {sorted(declared - runs)}; "
        f"rulate dar nedeclarate: {sorted(runs - declared)}"
    )


def test_blocked_scenarios_say_why() -> None:
    """Un scenariu `blocked` fără motiv e o omisiune tăcută. Motivul trebuie să numească ce
    blochează (defect găsit, flag nepromovat, funcționalitate absentă)."""
    for s in sc.manifest()["scenarios"]:
        if s["backend_coverage"] != "blocked":
            continue
        why = s.get("backend_coverage_blocked_because") or ""
        assert len(why) > 60, f"{s['id']}: motivul blocării e prea vag"


def test_no_scenario_coverage_value_is_invented() -> None:
    for s in sc.manifest()["scenarios"]:
        assert s["backend_coverage"] in ("covered", "blocked", "frontend_only"), s["id"]


def test_parser_reads_the_real_tool_wire_format() -> None:
    """Parserul e legat de formatul produs de `_brief`. Testul îl fixează: dacă forma de sârmă se
    schimbă, fake-ul află AICI, nu printr-o proză de zero-rezultate pe DB real."""
    raw = NEWLINE.join(
        [
            _tool_line("id-1", "Ser Alfa", 89.0),
            _tool_line("id-2", "Ser Beta", 129.5),
            "linie care nu e produs",
        ]
    )
    parsed = sc._parse_tool_result(raw)
    assert [p["id"] for p in parsed["products"]] == ["id-1", "id-2"]
    assert [p["name"] for p in parsed["products"]] == ["Ser Alfa", "Ser Beta"]
    assert [p["price"] for p in parsed["products"]] == [89.0, 129.5]


def test_parser_reports_zero_products_for_the_real_empty_result() -> None:
    assert sc._parse_tool_result("Niciun produs găsit.")["products"] == []


async def test_dynamic_tool_args_use_ids_from_the_previous_step() -> None:
    """`compare_products` cere id-uri REALE. Fake-ul le ia din rezultatul căutării, nu le inventează
    — altfel comparația ar testa respingerea unor id-uri inexistente."""
    llm = sc.Stage1FakeLLM()
    llm.arm("compare")
    seen: list[tuple[str, dict]] = []

    async def execute(name: str, args: dict) -> str:
        seen.append((name, args))
        if name == "search_products":
            return NEWLINE.join(
                [_tool_line("id-1", "Ser Alfa", 89.0), _tool_line("id-2", "Ser Beta", 99.0)]
            )
        return "diferențe: preț"

    await llm.run_tool_loop("s", "u", [], execute)
    assert [n for n, _ in seen] == ["search_products", "compare_products"]
    assert seen[1][1] == {"product_ids": ["id-1", "id-2"]}


def test_every_backend_invariant_is_referenced_by_a_scenario() -> None:
    """Un invariant cu checker, dar fără scenariu care să-l ceară, NU rulează niciodată.

    Exact așa scăpaseră `single_ledger_row` și `single_execution`: declarați, implementați,
    mutație-testați — și inerți pe calea reală. Testul de acoperire dinainte verifica doar direcția
    inversă (scenariu → invariant declarat), ceea ce lăsa gaura deschisă.
    """
    referenced: set[str] = set()
    for scenario in sc.manifest()["scenarios"]:
        referenced |= set(scenario["invariants"])
    orphans = sorted(sc.backend_invariants() - referenced)
    assert not orphans, f"invarianți backend pe care nu-i cere niciun scenariu: {orphans}"


def test_unexecuted_backend_invariants_belong_only_to_blocked_scenarios() -> None:
    """Ce NU se execută trebuie să aibă o cauză declarată. Un invariant care nu rulează și nu e
    legat de un scenariu `blocked` e acoperire pierdută în tăcere — nu un compromis asumat."""
    man = sc.manifest()
    covered = {s["id"] for s in man["scenarios"] if s["backend_coverage"] == "covered"}
    executed: set[str] = set()
    for scenario in man["scenarios"]:
        if scenario["id"] in covered:
            executed |= set(scenario["invariants"])
    blocked_or_frontend = {s["id"] for s in man["scenarios"] if s["backend_coverage"] != "covered"}
    for name in sorted(sc.backend_invariants() - executed):
        owners = {s["id"] for s in man["scenarios"] if name in s["invariants"]}
        assert owners and owners <= blocked_or_frontend, (
            f"{name} nu se execută, dar apare în scenarii declarate `covered`: "
            f"{sorted(owners - blocked_or_frontend)}"
        )


def test_known_gaps_in_thresholds_match_the_manifest() -> None:
    """Golurile publicate în artefactul de praguri trebuie să fie NUMERELE REALE din manifest.

    Un artefact care declară „9 scenarii acoperite" în timp ce manifestul spune altceva e mai rău
    decât niciun număr: NX-249 ar citi acoperirea din el.
    """
    gaps = sc.thresholds()["gate"]["known_gaps"]
    scenarios = sc.manifest()["scenarios"]
    counts = collections.Counter(s["backend_coverage"] for s in scenarios)
    assert gaps["scenarios_total"] == len(scenarios)
    assert gaps["scenarios_backend_covered"] == counts["covered"]
    assert gaps["scenarios_blocked"] == counts["blocked"]
    assert gaps["scenarios_frontend_only"] == counts["frontend_only"]

    backend = sc.backend_invariants()
    covered = {s["id"] for s in scenarios if s["backend_coverage"] == "covered"}
    executed = set()
    for s in scenarios:
        if s["id"] in covered:
            executed |= set(s["invariants"])
    assert gaps["backend_invariants_total"] == len(backend)
    assert gaps["backend_invariants_executed_on_real_data"] == len(backend & executed)


def test_thresholds_do_not_claim_coverage_of_blocked_scenarios() -> None:
    """`must_be_complete` se raportează la ce e DECLARAT, iar declarația se verifică separat față
    de execuție. Aici refuzăm reapariția cheii absolute, care număra scenarii nerulate."""
    complete = sc.thresholds()["gate"]["must_be_complete"]
    assert "canonical_scenarios_covered_ratio" not in complete, (
        "cheia veche promitea acoperirea TUTUROR scenariilor canonice; backendul acoperă 9/16"
    )
    assert complete["declared_backend_coverage_matches_execution_ratio"] == 1.0


def test_blocked_subcategories_sum_to_the_blocked_total() -> None:
    """Sub-categoriile de blocaj trebuie să acopere EXACT scenariile blocate.

    Fără asta, un scenariu putea rămâne blocat fără să apară în nicio categorie — adică un gol
    nenumărat într-un artefact al cărui rost e tocmai să numere golurile. Categoriile sunt cauze
    distincte, iar la NX-249 fiecare se închide altfel: un defect se repară, un flag se promovează,
    un profil se certifică, o funcționalitate se implementează.
    """
    gaps = sc.thresholds()["gate"]["known_gaps"]
    buckets = {k: v for k, v in gaps.items() if k.startswith("blocked_by_")}
    assert buckets, "nicio sub-categorie de blocaj declarată"
    assert sum(buckets.values()) == gaps["scenarios_blocked"], (
        f"sub-categoriile însumează {sum(buckets.values())}, dar sunt "
        f"{gaps['scenarios_blocked']} scenarii blocate: {buckets}"
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@db.example.supabase.co:5432/postgres",
        "postgresql://u:p@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",
        "postgresql://u:p@10.0.0.5:5432/db",
    ],
)
def test_refuses_non_loopback_database(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    """Singurul lucru în care harnessul SCRIE era singurul pe care nu-l verifica.

    Regresie măsurată (2026-08-19): baza de PRODUCȚIE avea patru tenanți sintetici
    `NX-247 alpha/beta`, cu canale `webchat` ACTIVE, din două rulări diferite. Gărzile existente
    cer loopback pentru hostul de BIND și `ENV=test` — dar `deny_outbound_network()` permite
    explicit hostul de DB citit din configurație, iar `ENV` e independent de DSN. Rulezi suita cu
    `.env`-ul de producție (exact ce faci ca să meargă testele într-un worktree fresh, unde `.env`
    e gitignored) și seedarea intră în Supabase.
    """
    _patch_settings(monkeypatch, _settings(monkeypatch, ha.CERTIFIED_PROFILE, SUPABASE_DB_URL=dsn))
    with pytest.raises(ha.HarnessRefused, match="loopback"):
        ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)


def test_accepts_the_ephemeral_loopback_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reversul: DSN-ul din `docker-compose.stage1-e2e.yml` trebuie să treacă neschimbat — altfel
    garda ar fi o interdicție, nu o poartă."""
    _patch_settings(
        monkeypatch,
        _settings(
            monkeypatch,
            ha.CERTIFIED_PROFILE,
            SUPABASE_DB_URL="postgresql://stage1:stage1@127.0.0.1:55432/stage1",
        ),
    )
    ha.assert_harness_allowed(bind_host="127.0.0.1", control_secret=CONTROL_SECRET)
