"""NX-126 — adaptor OpenAI: retry bounded pe tranzitoriu, terminal pe 4xx, timeout pasat la
client, sampling params pe agent/triaj. Client FAKE (zero apeluri reale)."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent import llm
from src.agent.llm import LLMClient
from src.config import get_settings


def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit(retry_after="0"):
    resp = httpx.Response(429, headers={"retry-after": retry_after}, request=_req())
    return openai.RateLimitError("rate limited", response=resp, body=None)


def _bad_request():
    resp = httpx.Response(400, request=_req())
    return openai.BadRequestError("bad request", response=resp, body=None)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Resp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [SimpleNamespace(message=_Msg(content), finish_reason=finish_reason)]


class _Completions:
    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        self.calls.append(kwargs)
        b = self._behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def _client(behaviors):
    comp = _Completions(behaviors)
    return SimpleNamespace(chat=SimpleNamespace(completions=comp), _comp=comp)


def _llm_client(behaviors, *, model_triage="gpt-5.4-nano", model_agent="gpt-5.4-mini"):
    """Numele de model sunt REALE, nu „nano"/„mini": de ele atârnă acum ce parametri pleacă pe
    sârmă (`llm.supported_params`). Un fake fără prefix cunoscut ar fi tratat ca model necunoscut,
    deci testele ar valida calea fail-safe crezând că o validează pe cea normală."""
    cl = _client(behaviors)
    return LLMClient(cl, model_triage=model_triage, model_agent=model_agent), cl._comp


#: Un tool oarecare: contează DOAR că cererea poartă `tools`, nu ce e în ele.
_TOOLS = [{"type": "function", "function": {"name": "search_products", "parameters": {}}}]


async def _never_executed(name, args):  # pragma: no cover - modelul nu cere tool-uri în fake
    raise AssertionError("fake-ul nu emite tool_calls, deci executorul nu are ce rula")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Sleep no-op (retry rapid) + usage no-op (fără dependență de forma resp)."""

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(llm.usage, "record_chat", lambda *a, **k: None)


async def test_retry_on_429_then_success():
    c, comp = _llm_client([_rate_limit(), _Resp('{"route": "simple"}')])
    out = await c.classify_json("sys", "usr")
    assert out == {"route": "simple"}
    assert len(comp.calls) == 2  # 1 eșec tranzitoriu + 1 succes


async def test_retry_on_timeout_then_success():
    c, comp = _llm_client([openai.APITimeoutError(request=_req()), _Resp('{"ok": true}')])
    out = await c.classify_json("sys", "usr")
    assert out == {"ok": True}
    assert len(comp.calls) == 2


async def test_terminal_400_no_retry():
    c, comp = _llm_client([_bad_request(), _Resp("{}")])
    with pytest.raises(openai.BadRequestError):
        await c.classify_json("sys", "usr")
    assert len(comp.calls) == 1  # 4xx terminal → zero retry


async def test_retry_exhausted_raises():
    # llm_retry_max default 2 → 1 inițial + 2 retries = 3 încercări, toate 429 → ridică.
    c, comp = _llm_client([_rate_limit(), _rate_limit(), _rate_limit()])
    with pytest.raises(openai.RateLimitError):
        await c.classify_json("sys", "usr")
    assert len(comp.calls) == 3


async def test_agent_call_includes_sampling_params():
    """Testul ăsta cerea, până pe 24 aug 2026, `temperature` ȘI `reasoning_effort="high"` pe
    ACELAȘI apel — combinație pe care furnizorul o refuză cu `400` (măsurat pe `gpt-5.4-mini`:
    „'temperature' does not support 0.7 with this model"). Trecea fiindcă `_Completions` e un fake
    care nu validează nimic, deci suita confirma cu încredere un payload imposibil. Acum cere ce
    pleacă REAL: cu effort configurat, raționamentul e pornit, deci temperatura rămâne acasă."""
    c, comp = _llm_client([_Resp("raspuns")])
    await c.complete("sys", "usr")
    assert comp.last_kwargs["reasoning_effort"] == "high"
    assert "temperature" not in comp.last_kwargs
    # `max_tokens` e deprecat (→ 400 pe modelele curente); dacă vreodată se trimite un plafon, el
    # se numește `max_completion_tokens`. Implicit nu se trimite niciunul — vezi testul dedicat.
    assert "max_tokens" not in comp.last_kwargs


async def test_triage_has_temperature_but_no_ceiling():
    c, comp = _llm_client([_Resp("{}")])
    await c.classify_json("sys", "usr")
    assert comp.last_kwargs["temperature"] == get_settings().llm_temperature_triage
    assert "max_tokens" not in comp.last_kwargs  # JSON triaj nu primește plafon (răspuns scurt)
    assert "max_completion_tokens" not in comp.last_kwargs


async def test_sampling_disabled_kill_switch(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            llm_sampling_enabled=False,
            llm_reasoning_effort_agent="",
            llm_retry_max=2,
            llm_max_tokens_agent=800,
        ),
    )
    c, comp = _llm_client([_Resp("x")])
    await c.complete("sys", "usr")
    # reasoning (sampling off): fără temperature/max_tokens (le resping), dar PLAFON de output
    # rămâne prin max_completion_tokens (NX-125) — un completion patologic tot e tăiat.
    assert "temperature" not in comp.last_kwargs and "max_tokens" not in comp.last_kwargs
    assert comp.last_kwargs["max_completion_tokens"] == 800


# ── Poarta de rationament ─────────────────────────────────────────────────────────────────────
# Regresie pentru incidentul din 24 aug: `bbb77b3` a adus DOUA schimbari care, fiecare singura, ar
# fi omorat calea de vanzare — default-ul `model_agent` `gpt-5.4-mini` → `gpt-5.6-luna`, si
# `llm_reasoning_effort_agent="high"`. Masurat pe API-ul real, ambele 400-uri vin din acelasi bit:
# cu rationamentul PORNIT, furnizorul refuza si `temperature` != 1, si function tools. Fiind 4xx,
# `_with_retry` le trateaza terminal, `agent_stage` le inghite, si tot creierul de vanzare a
# raspuns cu fallback-ul de runner, cu triajul (nano) intact deasupra.


def test_profilul_declara_daca_familia_rationeaza_implicit():
    # Masurat pe API-ul real (2026-08-24), nu presupus.
    assert llm.model_profile("gpt-5.6-luna").reasons_by_default is True
    assert llm.model_profile("gpt-5.6-terra").reasons_by_default is True
    assert llm.model_profile("gpt-5.4-mini").reasons_by_default is False
    assert llm.model_profile("gpt-5.4-nano").reasons_by_default is False
    assert llm.supported_params("gpt-5.4-mini") == {"temperature", "reasoning_effort"}


def test_model_necunoscut_nu_primeste_niciun_optional():
    assert llm.model_profile("model-inventat-maine") is None
    assert llm.supported_params("model-inventat-maine") == frozenset()


async def test_bucla_cu_tooluri_forteaza_oprirea_rationamentului():
    """Apelul REAL al agentului: cu tool-uri pe cerere, `reasoning_effort` trebuie sa fie `none`.
    ABSENTA parametrului NU ajunge — pe un model care rationeaza implicit, tot 400 iese."""
    c, comp = _llm_client([_Resp("gata")], model_agent="gpt-5.6-luna")
    await c.run_tool_loop("sys", "usr", _TOOLS, _never_executed)
    assert comp.calls[0]["reasoning_effort"] == "none"
    # Cu rationamentul oprit, `temperature` REDEVINE valida — masurat, nu presupus.
    assert comp.calls[0]["temperature"] == get_settings().llm_temperature_agent


async def test_agent_fara_tooluri_pastreaza_effortul_configurat_si_pierde_temperature():
    c, comp = _llm_client([_Resp("raspuns")], model_agent="gpt-5.6-luna")
    await c.complete("sys", "usr")
    assert comp.last_kwargs["reasoning_effort"] == "high"
    assert "temperature" not in comp.last_kwargs  # rationament pornit ⇒ doar valoarea implicita


async def test_modelul_clasic_fara_effort_pastreaza_temperature():
    """`gpt-5.4-*` mergea nu fiindca ar fi „alta familie", ci fiindca implicit nu rationeaza."""
    c, comp = _llm_client([_Resp("{}")], model_triage="gpt-5.4-nano")
    await c.classify_json("sys", "usr")
    assert comp.last_kwargs["temperature"] == get_settings().llm_temperature_triage
    assert "reasoning_effort" not in comp.last_kwargs  # triajul nu primeste effort


async def test_triajul_pe_model_de_rationament_nu_trimite_temperature():
    """Poarta e pe CERERE, nu pe rol: daca `MODEL_TRIAGE` ajunge vreodata pe 5.6, aceeasi cadere."""
    c, comp = _llm_client([_Resp("{}")], model_triage="gpt-5.6-luna")
    await c.classify_json("sys", "usr")
    assert "temperature" not in comp.last_kwargs


async def test_divergenta_fata_de_config_se_numara():
    """`LLM_REASONING_EFFORT_AGENT=high` e INERT pe drumul cu tool-uri. Daca asta nu se numara,
    configul si sarma diverg tacut."""
    from src.observability import turn_latency

    acc, token = turn_latency.push()
    try:
        c, _comp = _llm_client([_Resp("gata")], model_agent="gpt-5.6-luna")
        await c.run_tool_loop("sys", "usr", _TOOLS, _never_executed)
    finally:
        turn_latency.pop(token)
    assert acc.degradations.get("llm_reasoning_disabled_for_tools") == 1


async def test_temperatura_lasata_acasa_se_numara():
    from src.observability import turn_latency

    acc, token = turn_latency.push()
    try:
        c, _comp = _llm_client([_Resp("raspuns")], model_agent="gpt-5.6-luna")
        await c.complete("sys", "usr")
    finally:
        turn_latency.pop(token)
    assert acc.degradations.get("llm_param_unsupported_temperature") == 1


def test_implicit_nu_exista_plafon_de_output():
    """Default-ul e 0 = FĂRĂ plafon. Pinuit, fiindcă a fost 800 și a rupt producția.

    NX-125 pusese 800 ca „un completion patologic să nu scape de ceiling". Premisa s-a rupt când
    am trecut pe modele care raționează: tokenii de gândire ies din ACELAȘI buget ca textul, deci
    plafonul nu mai tăia bucle — tăia răspunsuri normale la mijloc, și o făcea TĂCUT (200 cu
    conținut gol ⇒ `{}` ⇒ degradare raportată cu altă cauză).

    Măsurat pe trafic real (2026-08-26, `gpt-5.6-luna` + effort `high`): un tur simplu cerea 512
    tokeni de gândire și trecea; unul conversațional cerea 1312 și pica. 2 din 4 apeluri rich au
    întors gol. Un cap de tokeni oricum nu previne o buclă — bucla e în RUNDE de model — deci
    plătea preț de calitate pentru o protecție inexistentă. Ceilingul real e cost guard-ul
    ZILNIC."""
    assert get_settings().llm_max_tokens_agent == 0


async def test_plafon_explicit_pleaca_pe_sarma_daca_e_cerut(monkeypatch):
    """Mecanismul rămâne: o valoare > 0 se trimite ca `max_completion_tokens`. Cine repune un
    plafon o poate face — dar deliberat, nu moștenind un default care nu mai are motiv."""
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            llm_sampling_enabled=True,
            llm_reasoning_effort_agent="high",
            llm_temperature_agent=0.7,
            llm_retry_max=2,
            llm_max_tokens_agent=1500,
        ),
    )
    c, comp = _llm_client([_Resp("raspuns")], model_agent="gpt-5.6-luna")
    await c.complete("sys", "usr")
    assert comp.last_kwargs["max_completion_tokens"] == 1500
    # Plafonul și poarta de raționament sunt decizii independente.
    assert comp.last_kwargs["reasoning_effort"] == "high"


async def test_fara_plafon_niciun_parametru_de_lungime(monkeypatch):
    """Cu 0, NICIUN parametru de lungime nu pleacă — nici cel nou, nici cel deprecat."""
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            llm_sampling_enabled=True,
            llm_reasoning_effort_agent="high",
            llm_temperature_agent=0.7,
            llm_retry_max=2,
            llm_max_tokens_agent=0,
        ),
    )
    c, comp = _llm_client([_Resp("raspuns")], model_agent="gpt-5.6-luna")
    await c.complete("sys", "usr")
    assert "max_completion_tokens" not in comp.last_kwargs
    assert "max_tokens" not in comp.last_kwargs


async def test_model_necunoscut_se_numara_si_nu_trimite_optionale():
    from src.observability import turn_latency

    acc, token = turn_latency.push()
    try:
        c, comp = _llm_client([_Resp("raspuns")], model_agent="model-inventat-maine")
        await c.complete("sys", "usr")
    finally:
        turn_latency.pop(token)
    assert "temperature" not in comp.last_kwargs
    assert "reasoning_effort" not in comp.last_kwargs
    assert acc.degradations.get("llm_model_profile_unknown") == 1


async def test_completion_taiat_de_plafon_e_numarat():
    """Tokenii de raționament intră în ACELAȘI `max_completion_tokens` ca textul: un cap prea mic
    întoarce 200 cu conținut GOL. Fără contorul ăsta, cauza nu apare nicăieri."""
    from src.observability import turn_latency

    acc, token = turn_latency.push()
    try:
        c, _comp = _llm_client([_Resp("", finish_reason="length")], model_agent="gpt-5.6-luna")
        out = await c.complete("sys", "usr")
    finally:
        turn_latency.pop(token)
    assert out == ""
    assert acc.degradations.get("llm_output_truncated_empty") == 1


def test_get_llm_builds_client_with_timeout_and_no_sdk_retry(monkeypatch):
    captured: dict = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(llm, "_llm", None)
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="k",
            model_triage="nano",
            model_agent="mini",
            model_embed="emb",
            model_moderation="mod",
            model_vision="vis",
            llm_timeout_s=30.0,
        ),
    )
    assert llm.get_llm() is not None
    assert captured["timeout"] == 30.0
    assert captured["max_retries"] == 0  # SDK retry off (folosim _with_retry)
