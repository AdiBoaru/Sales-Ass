"""NX-320 felia 1 — runda de tool-calling pe `/v1/responses` + gărzile comune cu `_chat`.

Client FAKE (zero apeluri reale). Ce se pinuiește:
  * cererea Responses: raționamentul NU e forțat `none`, `store=False` mereu, conținutul criptat al
    raționamentului cerut doar cu raționament pornit, unelte în forma PLATĂ, fără `temperature`;
  * calea de chat a rundei e EXACT cererea buclei de azi (`none` forțat);
  * usage pe schema Responses: tokeni, cache și raționament citiți, deci costul nu iese 0;
  * `_guarded` e comun: bugetul NX-311 pentru un apel care raționează, rândul `per_call`, retry-ul;
  * mesajul de user extras din `agent_stage` e byte-identic cu forma de dinainte.
"""

from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent import llm, usage
from src.agent.llm import LLMClient
from src.config import get_settings
from src.worker.stages.agent import tool_loop_user_parts

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "caută",
            "strict": True,
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


class _Recorder:
    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        b = self._behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def _responses_resp(*, calls=(), text="", usage_obj=None, status="completed"):
    output = [
        SimpleNamespace(type="reasoning", id="rs_1"),
        *(
            SimpleNamespace(type="function_call", name=n, arguments=a, call_id=f"c{i}")
            for i, (n, a) in enumerate(calls)
        ),
    ]
    return SimpleNamespace(
        output=output, output_text=text, usage=usage_obj, status=status, incomplete_details=None
    )


def _chat_resp(tool_calls=None, content=""):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def _client(*, chat=(), responses=(), model="gpt-6-luna"):
    chat_rec, resp_rec = _Recorder(chat), _Recorder(responses)
    raw = SimpleNamespace(chat=SimpleNamespace(completions=chat_rec), responses=resp_rec)
    return LLMClient(raw, model_agent=model), chat_rec, resp_rec


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    async def _s(_x):
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", _s)


# --- cererea pe Responses ------------------------------------------------------------------------


async def test_responses_cere_rationamentul_si_nu_il_forteaza_none():
    c, chat, resp = _client(responses=[_responses_resp(calls=[("search_products", "{}")])])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    kw = resp.calls[0]
    assert kw["reasoning"] == {"effort": "low"}
    assert kw["store"] is False  # P12: conversațiile clienților nu rămân la furnizor
    assert kw["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in kw  # raționament pornit ⇒ doar valoarea implicită
    assert kw["instructions"] == "sys"
    assert kw["input"] == [{"role": "user", "content": "usr"}]
    assert chat.calls == []  # nimic nu pleacă pe chat


async def test_responses_trimite_uneltele_in_forma_plata():
    c, _chat, resp = _client(responses=[_responses_resp()])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    tool = resp.calls[0]["tools"][0]
    assert tool["type"] == "function" and tool["name"] == "search_products"
    assert "function" not in tool  # forma de chat ar fi respinsă de /v1/responses
    assert tool["strict"] is True and tool["parameters"] == _TOOLS[0]["function"]["parameters"]


async def test_responses_fara_rationament_e_controlul_cu_temperatura():
    """Brațul de CONTROL al replay-ului: același endpoint, fără raționament. Regula bitului e cea de
    pe chat: raționament oprit ⇒ temperatura configurată pleacă, conținutul criptat nu se cere."""
    c, _chat, resp = _client(responses=[_responses_resp()])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="none")
    kw = resp.calls[0]
    assert kw["reasoning"] == {"effort": "none"}
    assert "include" not in kw
    assert kw["temperature"] == get_settings().llm_temperature_agent


async def test_runda_de_chat_e_exact_cererea_buclei_de_azi():
    tc = SimpleNamespace(
        function=SimpleNamespace(name="search_products", arguments='{"query": "crema"}')
    )
    c, chat, resp = _client(chat=[_chat_resp([tc])])
    rnd = await c.tool_round("sys", "usr", _TOOLS, via="chat")
    assert chat.calls[0]["reasoning_effort"] == "none"  # forțat, ca în `run_tool_loop`
    assert chat.calls[0]["tools"] == _TOOLS  # forma de chat, neatinsă
    assert resp.calls == []
    assert rnd.calls == (("search_products", '{"query": "crema"}'),)


async def test_function_call_se_citeste_in_ordinea_emisa():
    c, _chat, _resp = _client(
        responses=[
            _responses_resp(calls=[("search_products", '{"a": 1}'), ("faq_lookup", '{"b": 2}')])
        ]
    )
    rnd = await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert rnd.calls == (("search_products", '{"a": 1}'), ("faq_lookup", '{"b": 2}'))


async def test_model_fara_responses_tools_e_refuzat_zgomotos():
    """`gpt-5.6-luna` nu e declarat cu `responses_tools`: calea nouă refuză înainte de sârmă, în loc
    să plece o cerere care ar putea lua 400."""
    c, _chat, resp = _client(responses=[_responses_resp()], model="gpt-5.6-luna")
    with pytest.raises(ValueError, match="responses_tools"):
        await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert resp.calls == []


def test_profilurile_gpt6_declara_responses_tools():
    assert llm.model_profile("gpt-6-luna").responses_tools is True
    assert llm.model_profile("gpt-6-sol").responses_tools is True
    assert llm.model_profile("gpt-5.6-luna").responses_tools is False


# --- gărzile comune -------------------------------------------------------------------------------


async def test_apelul_responses_primeste_ceasul_unui_apel_care_rationeaza():
    """NX-311: fereastra largă urmează bitul de raționament, pe orice endpoint."""
    c, _chat, resp = _client(responses=[_responses_resp()])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert resp.calls[0]["timeout"] == get_settings().llm_timeout_reasoning_s


async def test_retry_ul_se_aplica_si_pe_responses():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    rate = openai.RateLimitError(
        "rl", response=httpx.Response(429, headers={"retry-after": "0"}, request=req), body=None
    )
    c, _chat, resp = _client(responses=[rate, _responses_resp()])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert len(resp.calls) == 2


async def test_400_pe_responses_e_terminal():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    bad = openai.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    c, _chat, resp = _client(responses=[bad, _responses_resp()])
    with pytest.raises(openai.BadRequestError):
        await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert len(resp.calls) == 1


async def test_raspuns_incomplet_se_numara(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(llm.turn_latency, "degrade", seen.append)
    c, _chat, _resp = _client(responses=[_responses_resp(status="incomplete")])
    await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    assert "llm_responses_incomplete" in seen


# --- usage pe schema Responses ---------------------------------------------------------------


def _responses_usage():
    return SimpleNamespace(
        input_tokens=1000,
        output_tokens=400,
        input_tokens_details=SimpleNamespace(cached_tokens=800),
        output_tokens_details=SimpleNamespace(reasoning_tokens=300),
    )


async def test_usage_responses_are_cost_si_rand_per_call():
    c, _chat, _resp = _client(responses=[_responses_resp(usage_obj=_responses_usage())])
    acc, token = usage.push()
    try:
        await c.tool_round("sys", "usr", _TOOLS, via="responses", effort="low")
    finally:
        usage.pop(token)
    assert acc.tokens_in == 1000 and acc.tokens_out == 400 and acc.cached_tokens == 800
    assert acc.reasoning_tokens == 300
    assert acc.cost_usd > 0  # fără citirea schemei Responses, costul ar fi ieșit 0
    row = acc.call_rows[-1]
    assert row["shape"] == "responses_tools" and row["reasoning"] is True
    assert row["tokens_in"] == 1000 and row["cached"] == 800 and row["reasoning_tokens"] == 300


def test_usage_de_chat_ramane_citit_ca_inainte():
    u = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    acc, token = usage.push()
    try:
        usage.record_chat(SimpleNamespace(usage=u), "gpt-6-luna")
    finally:
        usage.pop(token)
    assert (acc.tokens_in, acc.tokens_out, acc.cached_tokens, acc.reasoning_tokens) == (10, 5, 4, 2)


def test_forma_responses_nu_se_amesteca_cu_tools():
    assert usage.request_shape({"tools": [1], "messages": []}) == "tools"
    assert usage.request_shape({"tools": [1], "input": []}) == "responses_tools"
    assert usage.request_shape({"input": []}) == "text"


# --- extragerea din agent_stage --------------------------------------------------------------


def test_mesajul_buclei_e_byte_identic_cu_forma_de_dinainte():
    """Forma compusă inline în `agent_stage` până la NX-320, copiată aici ca referință fixă."""
    parts = tool_loop_user_parts(
        language="ro",
        history="Client: salut",
        hints="Categorie probabilă: ten\nBuget: 80\n",
        context="[produse arătate] x",
        query="ceva mai ieftin",
    )
    expected = (
        "Limba clientului: ro\nCategorie probabilă: ten\nBuget: 80\n[produse arătate] x\n\n"
        "Conversație până acum:\nClient: salut\n\n"
        "Mesaj client: ceva mai ieftin"
    )
    assert parts.legacy() == expected


def test_mesajul_buclei_fara_istoric_si_context():
    parts = tool_loop_user_parts(language="ro", history="", hints="", context="", query="salut")
    assert parts.legacy() == "Limba clientului: ro\nMesaj client: salut"
