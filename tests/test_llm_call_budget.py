"""NX-311 — cât are voie să dureze un apel e o proprietate a APELULUI, nu o constantă globală.

Defectul măsurat în producție (`sole-ro`, 101 ture): `llm_timeout_s=30` se punea O DATĂ, la
construcția clientului, peste toate apelurile. Pentru o rundă de tool-calling (p50 2,9s) e un
plafon anti-hang onest; pentru apelul de compunere, care raționează (p90 25,9s, cu 24,6% din
apeluri peste 30s), taie prin coadă. Iar `APITimeoutError` fiind tratat ca eroare TRANZITORIE,
tăietura noastră declanșa retry: aceeași generare, același prompt, același cronometru → 30×3.

Testele de aici apără cele patru afirmații pe care se sprijină fixul:
  1. ceasul urmează BITUL de raționament, nu numele apelantului;
  2. bugetul aparține APELULUI, nu încercării (altfel 75s × 3 = 225s);
  3. cauza unui retry e distinctă („ne-am tăiat singuri" ≠ „furnizorul a picat");
  4. kill-switch-ul stins dă traseul de dinainte, fără niciun `timeout` pe sârmă.
"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent import llm
from src.agent.llm import LLMClient
from src.observability import turn_latency

#: Modelul REAL din producție. Prefixul decide profilul (`reasons_by_default=True`), deci un nume
#: inventat ar valida calea de model necunoscut crezând că o validează pe cea normală.
_PROD_MODEL = "gpt-5.6-luna"

#: Un tool oarecare: contează DOAR că cererea poartă `tools` (atunci raționamentul e forțat `none`).
_TOOLS = [{"type": "function", "function": {"name": "search_products", "parameters": {}}}]


def _req():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _timeout_error():
    """Exact excepția din producție: ceasul NOSTRU a tăiat un apel care mergea."""
    return openai.APITimeoutError(request=_req())


def _rate_limit():
    return openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=_req()), body=None
    )


class _Resp:
    def __init__(self, content="ok"):
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None), finish_reason="stop"
            )
        ]


class _Completions:
    """Fake care AVANSEAZĂ ceasul: durata unui apel e datul testului, nu o așteptare reală."""

    def __init__(self, behaviors, clock, *, takes_s=0.0):
        self._behaviors = list(behaviors)
        self._clock = clock
        self._takes_s = takes_s
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self._clock["t"] += self._takes_s
        b = self._behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def _client(behaviors, *, takes_s=0.0):
    clock = {"t": 0.0}
    comp = _Completions(behaviors, clock, takes_s=takes_s)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return LLMClient(fake, model_agent=_PROD_MODEL), comp, clock


def _settings(**over):
    base = dict(
        llm_sampling_enabled=True,
        llm_reasoning_effort_agent="high",
        llm_retry_max=2,
        llm_max_tokens_agent=0,
        llm_timeout_s=30.0,
        llm_timeout_reasoning_s=75.0,
        llm_call_total_cap_s=90.0,
        llm_call_cap_ms=8_000,
        llm_call_cap_reasoning_ms=60_000,
        llm_call_budget_by_role_enabled=True,
        llm_retry_min_budget_ms=600,
        # Cerute pe ramura FĂRĂ raționament: cu tool-uri, effortul e forțat `none`, deci
        # `temperature` redevine legală și `_sampling` o caută.
        llm_temperature_agent=0.7,
        llm_temperature_background=0.2,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def _nosleep(_s):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(llm.usage, "record_chat", lambda *a, **k: None)


@pytest.fixture
def _degradations():
    """Acumulatorul REAL de `turn_latency`: codurile se verifică pe drumul pe care chiar
    circulă."""
    acc, token = turn_latency.push()
    yield acc
    turn_latency.pop(token)


# ── 1. Ceasul urmează bitul de raționament ────────────────────────────────────────────────────


async def test_apelul_care_rationeaza_primeste_fereastra_lui(monkeypatch):
    """REGRESIA cardului: PICĂ pe codul vechi, unde `timeout` nu pleca deloc pe cerere, iar apelul
    rămânea cu cele 30s din constructor — exact pragul care a produs turele de 100-120s."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, comp, _ = _client([_Resp()])
    await c.complete("sys", "usr")  # fără tool-uri ⇒ `reasoning_effort=high` pleacă pe sârmă
    assert comp.calls[-1]["reasoning_effort"] == "high"
    assert comp.calls[-1]["timeout"] == 75.0


async def test_bucla_cu_tooluri_ramane_la_ceasul_scurt(monkeypatch):
    """Perechea obligatorie: o cerere cu tool-uri are raționamentul FORȚAT `none` (altfel 400), deci
    e o rundă de 2-3s. Dacă ar primi și ea fereastra largă, un apel chiar agățat ar sta 75s degeaba
    — fixul ar fi mutat problema, nu ar fi reparat-o."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, comp, _ = _client([_Resp()])
    await c._chat(agent=True, model=_PROD_MODEL, messages=[], tools=_TOOLS)
    assert comp.calls[-1]["reasoning_effort"] == "none"
    assert comp.calls[-1]["timeout"] == 30.0


def test_cap_ms_urmeaza_acelasi_proprietar():
    """Geamănul LATENT: `llm_call_cap_ms=8_000` s-ar fi aplicat și apelului de compunere în ziua în
    care cineva aprinde `TURN_DEADLINE_ENABLED` — adică aceeași clasă de defect, descoperită într-un
    incident. Ambele plafoane vin din ACELAȘI loc."""
    s = _settings()
    assert llm.call_budget(s, reasoning_on=True).cap_ms == 60_000
    assert llm.call_budget(s, reasoning_on=False).cap_ms == 8_000


async def test_model_necunoscut_nu_capata_tacut_fereastra_larga(monkeypatch):
    """Prefix nedeclarat ⇒ nu ȘTIM dacă raționează. Alegerea e ceasul de azi: un model necunoscut nu
    trebuie să obțină 75s pe tăcute, iar cazul e deja numărat (`llm_model_profile_unknown`)."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    cl, comp, _ = _client([_Resp()])
    cl.model_agent = "model-inventat-4"
    await cl.complete("sys", "usr")
    assert comp.calls[-1]["timeout"] == 30.0


# ── 2. Bugetul aparține APELULUI, nu încercării ───────────────────────────────────────────────


async def test_plafonul_total_opreste_retryurile_inainte_sa_se_inmulteasca(monkeypatch):
    """Fără plafon total, 75s × (1 + 2 retries) = 225s: aș fi reparat p50 stricând coada.

    Ceasul e fals, dar aritmetica e cea reală: fiecare încercare „durează" 40s, plafonul total e
    60s, deci a doua încercare consumă tot ce mai era și a treia nu mai pornește."""
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_call_total_cap_s=60.0))
    c, comp, clock = _client([_timeout_error(), _timeout_error(), _Resp()], takes_s=40.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    with pytest.raises(openai.APITimeoutError):
        await c.complete("sys", "usr")
    assert len(comp.calls) == 2  # a treia n-a mai pornit: plafonul apelului era consumat


async def test_incercarea_urmatoare_primeste_doar_ce_a_ramas(monkeypatch):
    """Nu doar „mai încerc / nu mai încerc": a doua încercare capătă
    `min(capul ei, total − consumat)` — altfel o singură încercare ar putea depăși
    singură plafonul total."""
    monkeypatch.setattr(llm, "get_settings", _settings)  # total 90s
    c, comp, clock = _client([_timeout_error(), _Resp()], takes_s=70.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    await c.complete("sys", "usr")
    assert comp.calls[0]["timeout"] == 75.0  # prima: capul ei
    assert comp.calls[1]["timeout"] == pytest.approx(20.0)  # a doua: 90 − 70 consumate


def test_timeoutul_nu_poate_ajunge_zero():
    """`timeout=0` înseamnă „fără timeout" pentru httpx, adică fix opusul intenției. Un
    buget deja consumat trebuie să dea un minim POZITIV, altfel defecțiunea s-ar vedea
    abia ca un apel agățat."""
    assert llm._min_positive(75.0, -5.0) > 0
    assert llm._min_positive(None, None) is None
    assert llm._min_positive(None, 12.0) == 12.0


# ── 3. Cauza unui retry e distinctă ───────────────────────────────────────────────────────────


async def test_timeoutul_nostru_si_esecul_furnizorului_nu_mai_impart_un_contor(
    monkeypatch, _degradations
):
    """`llm_retry` singur a ținut defectul ascuns o lună: numără la fel două situații care
    cer reparații OPUSE. Un timeout al nostru se repară lărgind fereastra; un 429 se repară
    așteptând."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, _, _ = _client([_timeout_error(), _Resp()])
    await c.complete("sys", "usr")
    assert _degradations.degradations.get("llm_retry_timeout") == 1
    assert "llm_retry_status" not in _degradations.degradations
    assert _degradations.degradations.get("llm_retry") == 1  # contorul vechi rămâne comparabil


async def test_cauza_429_e_status_nu_timeout(monkeypatch, _degradations):
    """În SDK `APITimeoutError` e SUBCLASĂ de `APIConnectionError`; testul apără și ordinea din
    `_retry_cause`, nu doar existența codurilor."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, _, _ = _client([_rate_limit(), _Resp()])
    await c.complete("sys", "usr")
    assert _degradations.degradations.get("llm_retry_status") == 1
    assert "llm_retry_timeout" not in _degradations.degradations


def test_conexiunea_cazuta_nu_e_etichetata_timeout():
    assert llm._retry_cause(_timeout_error()) == "llm_retry_timeout"
    assert llm._retry_cause(openai.APIConnectionError(request=_req())) == "llm_retry_connection"
    assert llm._retry_cause(_rate_limit()) == "llm_retry_status"


async def test_apelul_lent_se_numara_chiar_si_cand_reuseste(monkeypatch, _degradations):
    """Măsurătoarea care ridică CENZURA. Sub vechiul perete un apel de 29s arăta ca unul de 3s, iar
    unul de 40s nu exista — maximul observat era exact 30,1s. Fără contorul ăsta am fi mutat
    peretele fără să aflăm vreodată ce era în spatele lui, iar recalibrarea ar fi tot pe ghicite."""
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, _, clock = _client([_Resp()], takes_s=42.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    await c.complete("sys", "usr")
    assert _degradations.degradations.get("llm_call_over_30s") == 1


async def test_apelul_rapid_nu_e_numarat_lent(monkeypatch, _degradations):
    monkeypatch.setattr(llm, "get_settings", _settings)
    c, _, clock = _client([_Resp()], takes_s=2.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    await c.complete("sys", "usr")
    assert "llm_call_over_30s" not in _degradations.degradations


# ── 4. Kill-switch ────────────────────────────────────────────────────────────────────────────


async def test_kill_switch_stins_da_traseul_de_azi(monkeypatch, _degradations):
    """OFF = byte-identic: niciun `timeout` pe sârmă, `cap_ms` cel vechi, nicio măsurătoare nouă."""
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_call_budget_by_role_enabled=False)
    )
    c, comp, clock = _client([_Resp()], takes_s=42.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    await c.complete("sys", "usr")
    assert "timeout" not in comp.calls[-1]
    assert "llm_call_over_30s" not in _degradations.degradations
    assert llm.call_budget(_settings(llm_call_budget_by_role_enabled=False), reasoning_on=True) == (
        None,
        None,
        8_000,
        None,
    )


async def test_kill_switch_stins_nu_mai_plafoneaza_bucla(monkeypatch):
    """Stins, cele trei încercări de azi rulează până la capăt, oricât ar dura — comportamentul de
    dinainte de card, inclusiv cu costul lui."""
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_call_budget_by_role_enabled=False)
    )
    c, comp, clock = _client([_timeout_error(), _timeout_error(), _Resp()], takes_s=40.0)
    monkeypatch.setattr(llm, "perf_counter", lambda: clock["t"])
    await c.complete("sys", "usr")
    assert len(comp.calls) == 3
