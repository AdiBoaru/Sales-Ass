"""NX-180 — teste pt logica evaluatorului (`eval_run`) care NU cere apeluri live.

Acoperă exact fix-urile din review-ul Codex #234: judge-ul vede întrebarea curentă, redactarea
PII, hash-ul fixture-urilor independent de LF/CRLF, metrica joint natural∧answered și p95 pe raw.
Importul lui `eval_run` nu atinge DB/OpenAI (setează doar env-ul + patch-uri la runtime în main()).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sim"))

import eval_run  # noqa: E402


def test_redact_pii():
    assert eval_run._redact("sună-mă la 0722 123 456") == "sună-mă la [REDACTED]"
    assert eval_run._redact("mail: ana.pop@example.com te rog") == "mail: [REDACTED] te rog"
    assert eval_run._redact("crema costă 89.99 lei") == "crema costă 89.99 lei"  # preț ≠ PII
    assert eval_run._redact("") == ""


def test_fixtures_signature_line_ending_independent(tmp_path, monkeypatch):
    """Fix #234: hash pe JSON canonic → LF și CRLF produc ACEEAȘI semnătură (git convertește)."""
    payload = {"conversations": [{"id": "x", "turns": [{"user": "salut", "gates": {}}]}]}
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    d_lf = tmp_path / "lf"
    d_lf.mkdir()
    (d_lf / "c.json").write_bytes(body.replace("\r\n", "\n").encode("utf-8"))
    monkeypatch.setattr(eval_run, "CONV_DIR", d_lf)
    sig_lf = eval_run._fixtures_signature()

    d_crlf = tmp_path / "crlf"
    d_crlf.mkdir()
    (d_crlf / "c.json").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(eval_run, "CONV_DIR", d_crlf)
    sig_crlf = eval_run._fixtures_signature()

    assert sig_lf == sig_crlf and len(sig_lf) == 16


def test_p95_nearest_rank():
    assert eval_run._p95([]) == 0.0
    assert eval_run._p95([10.0]) == 10.0
    # p95 nearest-rank pe 20 valori = a 19-a sortată
    assert eval_run._p95([float(i) for i in range(1, 21)]) == 19.0


def _turn(natural, answered, latencies):
    """Construiește forma minimă de tur agregat pe care o consumă `_summarize`."""
    return {
        "user": "u",
        "judge": {
            "natural": {"median": natural, "spread": 0},
            "answered": {"median": answered, "spread": 0},
        },
        "gate_pass_runs": 1,
        "runs": 1,
        "gate_fails_union": [],
        "opening_repeat_runs": 0,
        "latency_ms_raw": latencies,
        "unstable": False,
    }


def test_summarize_joint_metric_and_latency_over_raw():
    cases = [
        {
            "id": "c1",
            "turns": [
                _turn(5, 5, [100.0, 200.0]),  # natural∧answered ≥4 ✓
                _turn(5, 2, [300.0]),  # natural bun, answered slab → NU joint
            ],
        },
        {"id": "c2", "turns": [_turn(2, 5, [400.0])]},  # answered bun, natural slab → NU joint
    ]
    s = eval_run._summarize(cases)
    # doar 1 din 3 tururi are AMBELE ≥4
    assert s["pct_turns_natural_AND_answered_ge4"] == round(100 / 3, 1)
    # p95 GLOBAL pe TOATE latențele brute (100,200,300,400), nu p95-de-p95-uri
    assert s["n_latency_samples"] == 4
    assert s["latency_ms_p95"] == 400.0


class _FakeTurn:
    def __init__(self, content):
        self.content = content
        self.products = []
        self.suggestions = []
        self.offer = None


class _FakeClient:
    async def say(self, msg):
        return _FakeTurn(f"răspunsul botului la: {msg}")


class _RecordingLLM:
    """Înregistrează mesajul USER pe care-l primește judge-ul (ca să dovedim că vede întrebarea)."""

    model_agent = "fake"
    model_triage = "fake-nano"

    def __init__(self):
        self.seen_user_msgs = []
        self.seen_models = []

    async def complete_schema(self, system, user, schema, model=None):
        self.seen_user_msgs.append(user)
        self.seen_models.append(model)
        return {
            m: 4 for m in ("answered", "natural", "non_repetitive", "concise", "honest", "overall")
        }


async def test_judge_sees_current_question():
    """Fix BLOCANT #234: judge-ul primește transcript-ul INCLUZÂND întrebarea curentă (înainte era
    chemat înainte de a adăuga user_msg → evalua 'answered' orb la întrebare)."""
    llm = _RecordingLLM()

    async def mk(_label):
        return _FakeClient()

    convo = {"id": "t", "turns": [{"user": "am tenul gras, ce ser?", "gates": {}}]}
    await eval_run._run_conversation(convo, mk, llm, 1)

    assert llm.seen_user_msgs, "judge-ul n-a fost chemat"
    assert any("am tenul gras, ce ser?" in u for u in llm.seen_user_msgs)


# --- NX-204: model-swap orb ---------------------------------------------------------------------


async def test_judge_model_is_pinned_independent_of_agent_arm():
    """Confound-ul central al NX-204: judge-ul moștenea `llm.model_agent`, deci un experiment de
    model-swap ar fi comparat „mini judecat de mini" vs „frontier judecat de frontier" — două
    rigle diferite. Judecătorul trebuie să rămână ACELAȘI pe ambele brațe."""
    llm = _RecordingLLM()
    llm.model_agent = "brat-frontier"  # brațul B tocmai a mutat modelul agentului

    async def mk(_label):
        return _FakeClient()

    convo = {"id": "t", "turns": [{"user": "ce ser?", "gates": {}}]}
    await eval_run._run_conversation(convo, mk, llm, 1, "judecator-pinuit")

    assert llm.seen_models == ["judecator-pinuit"], (
        f"judge-ul a folosit {llm.seen_models}, adică a urmat brațul agentului"
    )


def test_pricing_gate_rejects_model_without_explicit_rates():
    """Poarta de cost: `rates_for` cade tăcut pe tarifele `mini` pentru un model necunoscut →
    raportul ar minți exact pe cifra care decide swap-ul. `has_rates` face fallback-ul VIZIBIL."""
    from src.agent import pricing

    assert pricing.has_rates("gpt-5.4-mini") is True
    assert pricing.has_rates("model-inexistent-xyz") is False
    # Dovada că fallback-ul chiar e TĂCUT (de-asta e nevoie de poartă, nu de `rates_for`):
    # un model necunoscut primește tarifele implicite, fără niciun semnal.
    # Comparat cu `_DEFAULT`, NU cu `rates_for("gpt-5.4-mini")` — al doilea depinde de
    # `LLM_PRICING_JSON` din mediu (override-ul NX-204a îl schimbă) și ar face testul fragil.
    assert pricing.rates_for("model-inexistent-xyz") == pricing._DEFAULT


def test_permanent_errors_distinguished_from_transient_throttling():
    """Lecția rulării din 2026-07-31: creditele epuizate vin tot ca `RateLimitError` (429), deci
    retry-ul le trata ca tranzitorii → 1,5h de fallback-uri prezentate ca rezultat. Eroarea
    PERMANENTĂ trebuie recunoscută după corp/cod, nu după statusul HTTP."""

    class _Err(Exception):
        def __init__(self, body=None, status_code=None, msg=""):
            super().__init__(msg)
            self.body = body
            self.status_code = status_code

    quota = _Err(body={"error": {"code": "credit_balance_exhausted", "type": "insufficient_quota"}})
    assert eval_run._permanent_reason(quota) is not None
    assert "credite" in eval_run._permanent_reason(quota)

    # throttling REAL (se rezolvă în secunde) → NU se abandonează rularea
    throttle = _Err(body={"error": {"code": "rate_limit_exceeded", "type": "requests"}})
    assert eval_run._permanent_reason(throttle) is None

    assert eval_run._permanent_reason(_Err(status_code=401, msg="invalid_api_key")) is not None
    assert eval_run._permanent_reason(_Err(msg="connection reset")) is None


async def test_run_aborts_on_permanent_error_instead_of_collecting_fallbacks(monkeypatch):
    """Abandonul e imediat: un tur pe fallback nu e „mai slab", e necomparabil."""
    monkeypatch.setitem(eval_run._llm_failures, "fatal", None)
    monkeypatch.setitem(eval_run._llm_failures, "n", 0)
    llm = _RecordingLLM()

    async def mk(_label):
        return _FakeClient()

    # primul tur ridică steagul fatal (ca și cum adaptorul ar fi epuizat retry-urile)
    convo = {"id": "t", "turns": [{"user": "a", "gates": {}}, {"user": "b", "gates": {}}]}
    original_check = eval_run._check_fatal
    calls = {"n": 0}

    def _fake_check():
        calls["n"] += 1
        eval_run._llm_failures["fatal"] = "credite/cotă OpenAI epuizate"
        original_check()

    monkeypatch.setattr(eval_run, "_check_fatal", _fake_check)
    with pytest.raises(eval_run.RunAborted):
        await eval_run._run_conversation(convo, mk, llm, 1)
    assert calls["n"] == 1, "s-a oprit la PRIMUL tur, nu a mai colectat fallback-uri"


def test_cost_is_summed_per_call_model_not_from_aggregate_tokens():
    """Un tur amestecă nano (triaj) cu agentul; costul dedus din tokenii agregați ar fi greșit.
    `_summarize` însumează costul BRUT per tur×rulare, nu mediane."""
    turns = [
        {**_turn(5, 5, [100.0]), "cost_usd_raw": [0.001, 0.003]},
        {**_turn(5, 5, [200.0]), "cost_usd_raw": [0.002]},
    ]
    s = eval_run._summarize([{"id": "c1", "turns": turns}])
    assert s["n_cost_samples"] == 3
    assert s["cost_usd_total"] == 0.006
    assert s["cost_usd_per_turn_median"] == 0.002


# --- #252 review: garduri de admitere web (429) + cleanup structural --------------------------


def test_admission_reason_only_for_429():
    """Doar 429 (respingere de ADMITERE la marginea web) devine abandon controlat. 503 & co.
    rămân eroarea originală — un defect de mediu nu se ascunde sub un mesaj prietenos."""
    from fastapi import HTTPException

    cost = eval_run._admission_reason(HTTPException(status_code=429, detail="budget exceeded"))
    assert cost is not None and "plafonul de cost" in cost

    rl = eval_run._admission_reason(HTTPException(status_code=429, detail="rate limited"))
    assert rl is not None and "rate limit" in rl

    assert eval_run._admission_reason(HTTPException(status_code=503, detail="unavailable")) is None
    assert eval_run._admission_reason(HTTPException(status_code=413, detail="too big")) is None
    assert eval_run._admission_reason(RuntimeError("ceva")) is None


class _Cleanup:
    """Urmărește dacă purja + închiderea poolului chiar au rulat."""

    def __init__(self):
        self.purged = 0
        self.closed = 0


def _install_main_stubs(monkeypatch, tmp_path, client_factory, spy: _Cleanup):
    """Montează `main()` pe stub-uri: fără DB, fără OpenAI, fără fakeredis global. Lăsăm intacte
    exact bucățile testate — ordinea try/finally, maparea 429 și cleanup-ul."""
    import contextlib

    import web_audit

    import src.agent.llm as llm_mod
    import src.config as config_mod
    import src.db.connection as conn_mod
    import src.db.queries.channels as channels_mod

    monkeypatch.setattr(eval_run, "OUT_DIR", tmp_path / "reports")
    monkeypatch.setattr(eval_run, "_install_token_meter", lambda: None)
    monkeypatch.setattr(eval_run, "_install_failure_meter", lambda: None)
    monkeypatch.setattr(web_audit, "_install_fake_redis", lambda: None)
    monkeypatch.setattr(
        eval_run,
        "_load_conversations",
        lambda only=None: [
            {"id": "t", "turns": [{"user": "a", "gates": {}}, {"user": "b", "gates": {}}]}
        ],
    )

    class _Settings:
        cache_enabled = True
        daily_cost_cap_usd = 5.0

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(llm_mod, "get_llm", _RecordingLLM)

    async def _get_pool():
        return "POOL"

    class _Conn:
        """Conn minimal: preflight-ul de plafon citește `businesses.daily_cost_cap_usd`."""

        def __init__(self, cap=50.0):
            self.cap = cap

        async def fetchrow(self, *a, **k):
            return {"daily_cost_cap_usd": self.cap}

    @contextlib.asynccontextmanager
    async def _admin_conn(_pool):
        yield _Conn()

    async def _close_pool():
        spy.closed += 1

    monkeypatch.setattr(conn_mod, "get_pool", _get_pool)
    monkeypatch.setattr(conn_mod, "admin_conn", _admin_conn)
    monkeypatch.setattr(conn_mod, "close_pool", _close_pool)

    async def _resolve(_conn, _token):
        return {"business_id": "biz"}

    monkeypatch.setattr(channels_mod, "resolve_web_session", _resolve)

    async def _purge(_conn, _biz):
        spy.purged += 1
        return 1

    monkeypatch.setattr(web_audit, "_purge_audit", _purge)

    async def _catalog_sig(_conn, _biz):
        return "n=1;sha256=deadbeef"

    monkeypatch.setattr(eval_run, "_catalog_signature", _catalog_sig)

    async def _session(_token, _label):
        return ("vid", "sig")

    monkeypatch.setattr(web_audit, "_session", _session)
    monkeypatch.setattr(web_audit, "WebClient", lambda *a, **k: client_factory())
    monkeypatch.setattr(sys, "argv", ["eval_run.py", "--runs", "1", "--token", "tok"])


class _FailingClient:
    """Turul 1 trece, turul 2 e respins de marginea web cu `status`."""

    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        self.n = 0

    async def say(self, msg):
        from fastapi import HTTPException

        self.n += 1
        if self.n >= 2:
            raise HTTPException(status_code=self.status, detail=self.detail)
        return _FakeTurn(f"răspuns la {msg}")


async def test_web_429_aborts_controlled_and_still_cleans_up(monkeypatch, tmp_path):
    """Finding CONFIRMED #252: cost-guard-ul web NU degradează fără LLM — `/web/chat` ridică
    429 înainte de `handle_turn`. Excepția urca necontrolat, ocolind purja și `close_pool`."""
    monkeypatch.setitem(eval_run._llm_failures, "fatal", None)
    monkeypatch.setitem(eval_run._llm_failures, "n", 0)
    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FailingClient(429, "budget exceeded"), spy)

    rc = await eval_run.main()

    assert rc == 1, "abandon controlat, nu ieșire de succes"
    assert spy.purged == 1, "purja vizitatorilor de eval NU a rulat"
    assert spy.closed == 1, "close_pool NU a rulat"
    assert (
        not list((tmp_path / "reports").glob("*.json")) and not (tmp_path / "reports").exists()
    ), "s-a scris raport dintr-o rulare abandonată"


async def test_web_503_propagates_original_error_but_cleanup_runs(monkeypatch, tmp_path):
    """503 nu e gard de admitere: eroarea rămâne EROAREA ORIGINALĂ (diagnosticabilă), dar
    curățarea trebuie să ruleze la fel — cleanup-ul e o proprietate a ieșirii, nu o ramură."""
    from fastapi import HTTPException

    monkeypatch.setitem(eval_run._llm_failures, "fatal", None)
    monkeypatch.setitem(eval_run._llm_failures, "n", 0)
    spy = _Cleanup()
    _install_main_stubs(
        monkeypatch, tmp_path, lambda: _FailingClient(503, "business unavailable"), spy
    )

    with pytest.raises(HTTPException) as ei:
        await eval_run.main()

    assert ei.value.status_code == 503, "eroarea originală a fost înlocuită"
    assert spy.purged == 1 and spy.closed == 1, "cleanup-ul nu a rulat pe calea de excepție"
    assert not (tmp_path / "reports").exists(), "raport scris dintr-o rulare căzută"


async def test_init_failure_still_closes_pool(monkeypatch, tmp_path):
    """Review #252: o eroare de INIȚIALIZARE (rezolvarea canalului, `_catalog_signature`, orice
    query) apărea DUPĂ `get_pool()` dar ÎNAINTE de bucla de rulare — deci ocolea cleanup-ul și
    lăsa poolul deschis. Curățarea are acum un singur proprietar, la nivelul poolului."""
    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FakeClient(), spy)

    async def _boom(_conn, _biz):
        raise RuntimeError("catalog signature a crăpat")

    monkeypatch.setattr(eval_run, "_catalog_signature", _boom)

    with pytest.raises(RuntimeError, match="catalog signature"):
        await eval_run.main()

    assert spy.closed == 1, "close_pool NU a rulat pe eroare de inițializare"
    assert not (tmp_path / "reports").exists(), "raport scris dintr-o rulare care n-a pornit"


async def test_missing_channel_closes_pool(monkeypatch, tmp_path):
    """Aceeași gaură pe ramura „niciun canal webchat": ieșire timpurie cu poolul deja deschis."""
    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FakeClient(), spy)

    async def _no_session(_conn, _token):
        return None

    import src.db.queries.channels as channels_mod

    monkeypatch.setattr(channels_mod, "resolve_web_session", _no_session)
    monkeypatch.setattr(sys, "argv", ["eval_run.py", "--runs", "1"])  # fără token → caută în DB

    class _NoRowConn:
        async def fetchrow(self, *a, **k):
            return None

    import contextlib

    import src.db.connection as conn_mod

    @contextlib.asynccontextmanager
    async def _admin_conn(_pool):
        yield _NoRowConn()

    monkeypatch.setattr(conn_mod, "admin_conn", _admin_conn)

    rc = await eval_run.main()

    assert rc == 1
    assert spy.closed == 1, "close_pool NU a rulat pe ieșirea „niciun canal webchat”"


async def test_preflight_refuses_when_daily_cap_below_estimated_cost(monkeypatch, tmp_path):
    """Plafonul zilnic sub costul estimat = rulare tăiată la mijloc de 429, după ~30 min și
    credite consumate. Preflight-ul e READ-ONLY: refuză pornirea, NU ridică plafonul singur —
    ajustarea rămâne o decizie deliberată și temporară a omului."""
    import contextlib

    import src.db.connection as conn_mod

    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FakeClient(), spy)

    class _TightConn:
        async def fetchrow(self, *a, **k):
            return {"daily_cost_cap_usd": 0.001}  # sub costul oricărei rulări

    @contextlib.asynccontextmanager
    async def _admin_conn(_pool):
        yield _TightConn()

    monkeypatch.setattr(conn_mod, "admin_conn", _admin_conn)

    rc = await eval_run.main()

    assert rc == 1, "rularea a pornit deși plafonul o taie la mijloc"
    assert spy.closed == 1, "close_pool NU a rulat pe refuzul de preflight"
    assert not (tmp_path / "reports").exists()


async def test_preflight_can_be_overridden_explicitly(monkeypatch, tmp_path):
    """`--ignore-cost-cap` = ieșire de urgență explicită, nu implicit tăcut."""
    import contextlib

    import src.db.connection as conn_mod

    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FakeClient(), spy)

    class _TightConn:
        async def fetchrow(self, *a, **k):
            return {"daily_cost_cap_usd": 0.001}

    @contextlib.asynccontextmanager
    async def _admin_conn(_pool):
        yield _TightConn()

    monkeypatch.setattr(conn_mod, "admin_conn", _admin_conn)
    monkeypatch.setattr(
        sys, "argv", ["eval_run.py", "--runs", "1", "--token", "tok", "--ignore-cost-cap"]
    )

    rc = await eval_run.main()

    assert rc == 0, "override-ul explicit n-a lăsat rularea să pornească"
    assert spy.closed == 1


async def test_report_meta_carries_all_reproducibility_pins(monkeypatch, tmp_path):
    """Pinurile din `meta` sunt CONTRACTUL de reproductibilitate al baseline-ului: fără ele nu poți
    spune contra cărui sistem s-a măsurat. Un pin pierdut nu rupe nimic vizibil — raportul se scrie,
    suita trece, iar peste o lună compari două rulări necomparabile. (S-a și întâmplat: o rescriere
    mecanică a transformat `"cache_enabled": False` și `"kind"` în comentarii, fără ca ruff sau
    testele să sesizeze.) Testul fixează cheile obligatorii."""
    spy = _Cleanup()
    _install_main_stubs(monkeypatch, tmp_path, lambda: _FakeClient(), spy)

    rc = await eval_run.main()
    assert rc == 0

    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    meta = json.loads(reports[0].read_text(encoding="utf-8"))["meta"]

    for key in (
        "kind",
        "cache_enabled",
        "valid",
        "business_id",
        "runs_per_case",
        "model_triage",
        "model_agent",
        "judge_model",
        "judge_model_pinned",
        "judge_prompt_sha256",
        "catalog_signature",
        "fixtures_sha256",
        "paired_mode",
        "denominator",
    ):
        assert key in meta, f"pin de reproductibilitate lipsă din raport: {key}"

    assert meta["cache_enabled"] is False, "cache-ul TREBUIE raportat ca oprit pe durata rulării"
    assert meta["kind"] == "baseline"
    assert meta["judge_model_pinned"] is True
