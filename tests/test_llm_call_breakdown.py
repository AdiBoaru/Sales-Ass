"""NX-312 felia 1 — fiecare apel de model, măsurat separat (`llm_usage.per_call`).

Înainte, `turn_latency.phases.model` dădea `{n: 3, ms: 32437}` pe turul `d73822a3`, iar
împărțirea 3s + 3s + 26s se putea doar deduce prin scădere. Testele de aici fixează forma
rândului, derivarea `shape` din CEREREA care pleacă pe sârmă, rândul apelului EȘUAT, plafonul
listei și drumul până în evenimentul `llm_usage`. ZERO OpenAI, ZERO DB.
"""

from dataclasses import dataclass

import pytest

from src.agent import usage
from src.agent.llm import LLMClient
from src.config import get_settings
from src.models import BusinessConfig, Contact, InboundMessage, TurnContext
from src.worker.aftercare import _usage_event_props
from src.worker.runner import PipelineDeps, run_pipeline

# --- fake-uri OpenAI -----------------------------------------------------------------------


@dataclass
class _Details:
    cached_tokens: int


@dataclass
class _CompletionDetails:
    reasoning_tokens: int


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_details: _Details | None = None
    completion_tokens_details: _CompletionDetails | None = None


class _Func:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.type = "function"
        self.function = _Func(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Resp:
    def __init__(self, msg, usage_=None):
        self.choices = [_Choice(msg)]
        self.usage = usage_


class _Completions:
    """Scriptat: fiecare element e un `_Resp` de întors sau o excepție de ridicat."""

    def __init__(self, script):
        self.script = list(script)
        self.kwargs = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeOpenAI:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


@pytest.fixture
def luna_high(monkeypatch):
    """Profilul de PRODUCȚIE: luna + effort high. Bucla are raționamentul forțat OFF (tools),
    compunerea îl are ON — exact cele două populații pe care rândurile trebuie să le separe."""
    monkeypatch.setattr(get_settings(), "llm_reasoning_effort_agent", "high")
    return "gpt-5.6-luna"


def _u(prompt, completion, cached=0, reasoning=0):
    return _Usage(prompt, completion, _Details(cached), _CompletionDetails(reasoning))


# --- request_shape: PUR ----------------------------------------------------------------------


def test_shape_deriva_din_cerere():
    assert usage.request_shape({"tools": [{"t": 1}]}) == "tools"
    assert usage.request_shape({"response_format": {"type": "json_schema"}}) == "schema"
    assert usage.request_shape({"response_format": {"type": "json_object"}}) == "schema"
    assert usage.request_shape({"messages": []}) == "text"


def test_shape_tools_bate_response_format():
    # Bucla structurată a creierului unic trimite AMBELE; ce îi dictează durata e bitul de
    # raționament, iar pe ăla îl dictează uneltele.
    kw = {"tools": [{"t": 1}], "response_format": {"type": "json_schema"}}
    assert usage.request_shape(kw) == "tools"


def test_shape_tools_gol_nu_e_tools():
    # `tools=[]` nu pleacă pe sârmă ca unelte: un apel fără unelte poate raționa.
    assert usage.request_shape({"tools": [], "response_format": {"x": 1}}) == "schema"


# --- turul real, reprodus: buclă (2 runde) + compunere ---------------------------------------


async def test_turul_de_recomandare_are_trei_randuri_separate(luna_high):
    """Forma turului `d73822a3`: runda 1 cere căutarea, runda 2 scrie proză, compunerea
    raționează. Un rând per apel, în ordine, cu tokenii LUI — nu totalul."""
    script = [
        _Resp(
            _Msg(tool_calls=[_ToolCall("c1", "search_products", '{"query":"crema"}')]),
            _u(5000, 55),
        ),
        _Resp(_Msg(content="Proză."), _u(6100, 330, cached=4718)),
        _Resp(_Msg(content='{"intro":"x"}'), _u(4700, 2193, reasoning=1536)),
    ]
    llm = LLMClient(_FakeOpenAI(script), model_agent=luna_high)

    async def execute(name, args):
        return "rezultat"

    acc, token = usage.push()
    try:
        await llm.run_tool_loop("sys", "user", [{"t": 1}], execute)
        await llm.complete_schema("rich", "user", {"name": "s", "schema": {}})
    finally:
        usage.pop(token)

    rows = acc.call_rows
    assert [(r["shape"], r["reasoning"], r["ok"]) for r in rows] == [
        ("tools", False, True),
        ("tools", False, True),
        ("schema", True, True),
    ]
    assert [r["tokens_in"] for r in rows] == [5000, 6100, 4700]
    assert rows[1]["cached"] == 4718  # apelul 2 CITEȘTE cache-ul scris de apelul 1
    assert rows[2]["reasoning_tokens"] == 1536 and rows[2]["tokens_out"] == 2193
    assert all(r["ms"] >= 0 for r in rows)
    # Totalurile rămân ce erau: lista e o defalcare, nu o a doua contabilitate.
    assert acc.calls == 3 and acc.tokens_in == 15800


async def test_apelul_esuat_e_un_rand_si_exceptia_ramane_neatinsa(luna_high):
    """Un apel care moare (aici o eroare terminală) a costat timp turului: intră în listă cu
    `ok=False`, iar apelantul primește EXACT excepția de azi (P6: măsurătoarea nu schimbă
    nimic)."""
    llm = LLMClient(_FakeOpenAI([ValueError("boom")]), model_agent=luna_high)
    acc, token = usage.push()
    try:
        with pytest.raises(ValueError, match="boom"):
            await llm.complete_schema("rich", "user", {"name": "s", "schema": {}})
    finally:
        usage.pop(token)
    assert acc.calls == 0  # nimic facturat
    (row,) = acc.call_rows
    assert row["ok"] is False and row["shape"] == "schema" and row["reasoning"] is True
    assert row["tokens_in"] == 0 and row["reasoning_tokens"] is None


async def test_rationament_neraportat_e_none_nu_zero(luna_high):
    """Instrument de măsură: „nu știm" nu se colapsează în „zero gândire" (`_reasoning_from`)."""
    resp = _Resp(_Msg(content="{}"), _Usage(100, 50))  # fără completion_tokens_details
    llm = LLMClient(_FakeOpenAI([resp]), model_agent=luna_high)
    acc, token = usage.push()
    try:
        await llm.complete_schema("rich", "user", {"name": "s", "schema": {}})
    finally:
        usage.pop(token)
    assert acc.call_rows[0]["reasoning_tokens"] is None


# --- plafon + fără acumulator ------------------------------------------------------------------


def test_lista_e_plafonata_si_depasirea_se_numara():
    acc, token = usage.push()
    try:
        for _ in range(usage.MAX_CALL_ROWS + 3):
            usage.record_call(None, shape="text", reasoning=False, ms=1.0, ok=True)
    finally:
        usage.pop(token)
    assert len(acc.call_rows) == usage.MAX_CALL_ROWS
    assert acc.call_rows_dropped == 3


def test_fara_acumulator_e_no_op():
    usage.record_call(None, shape="tools", reasoning=False, ms=1.0, ok=True)  # nu ridică


def test_forma_necunoscuta_cade_pe_text():
    # Vocabular ÎNCHIS: o valoare nouă nu deschide cardinalitate în analytics.
    acc, token = usage.push()
    try:
        usage.record_call(None, shape="altceva", reasoning=False, ms=1.0, ok=True)
    finally:
        usage.pop(token)
    assert acc.call_rows[0]["shape"] == "text"


def test_randul_nu_poarta_text_sau_identificatori():
    """P12: numai numere și cuvinte din vocabularul închis."""
    acc, token = usage.push()
    try:
        usage.record_call(
            _Resp(_Msg(content="Secret"), _u(10, 5)), shape="text", reasoning=False, ms=2.0, ok=True
        )
    finally:
        usage.pop(token)
    row = acc.call_rows[0]
    assert set(row) == {
        "shape",
        "reasoning",
        "ok",
        "ms",
        "tokens_in",
        "cached",
        "tokens_out",
        "reasoning_tokens",
    }
    assert "Secret" not in repr(row)


# --- drumul până în eveniment -------------------------------------------------------------------


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="d", name="D"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="x"),
        conversation_id="conv",
    )


async def test_runner_pune_per_call_in_llm_usage():
    async def llm_stage(ctx, deps):
        usage.record_chat(_Resp(_Msg(), _u(700, 90)), "gpt-5.6-luna")
        usage.record_call(
            _Resp(_Msg(), _u(700, 90)), shape="tools", reasoning=False, ms=2900.0, ok=True
        )

    ctx = _ctx()
    await run_pipeline(ctx, PipelineDeps(conn=None), [llm_stage])
    (ev,) = [e for e in ctx.events if e.type == "llm_usage"]
    rows = ev.properties["per_call"]
    assert len(rows) == 1 and rows[0]["shape"] == "tools" and rows[0]["ms"] == 2900.0
    assert "per_call_dropped" not in ev.properties  # cheia apare doar când a fost depășire


def test_evenimentul_post_tur_are_aceeasi_forma():
    acc = usage.UsageAccumulator()
    acc.add("gpt-5.6-luna", 1384, 190, 0, reasoning=98)
    acc.add_call_row({"shape": "schema", "reasoning": True, "ok": True, "ms": 3222.0})
    props = _usage_event_props(acc, phase="post_turn")
    assert props["per_call"] == [{"shape": "schema", "reasoning": True, "ok": True, "ms": 3222.0}]


# --- proba: agregarea pe apel (pură) ------------------------------------------------------------


def _row(shape, reasoning, ms, tokens_in=1000, cached=0, ok=True):
    return {
        "shape": shape,
        "reasoning": reasoning,
        "ok": ok,
        "ms": ms,
        "tokens_in": tokens_in,
        "cached": cached,
        "tokens_out": 50,
        "reasoning_tokens": 0,
    }


def test_proba_grupeaza_pe_forma_si_rationament():
    from scripts.llm_call_budget_probe import per_call_summary

    s = per_call_summary(
        [
            _row("tools", False, 2900),
            _row("tools", False, 3100),
            _row("schema", True, 26000),
            _row("schema", True, 90000, ok=False),
        ]
    )
    assert s[("tools", False)]["n"] == 2
    assert s[("schema", True)]["n"] == 1 and s[("schema", True)]["failed"] == 1
    # Eșecul NU intră în percentile: 90s de retry nu e durata unui apel, e a unui eșec.
    assert s[("schema", True)]["ms_p90"] == 26000


def test_proba_panta_cere_minimum_cinci_puncte():
    from scripts.llm_call_budget_probe import _slope

    assert _slope([1, 2, 3], [1, 2, 3]) is None
    assert _slope([5, 5, 5, 5, 5], [1, 2, 3, 4, 5]) is None  # fără variație pe x
    # 2 ms per token necache-uit = 2.000 ms per 1.000
    assert _slope([100, 200, 300, 400, 500], [200, 400, 600, 800, 1000]) == pytest.approx(2000.0)


def test_importul_probei_nu_incarca_env():
    """Regresie: proba făcea `load_dotenv()` la import, deci orice test care o importa aprindea
    `.env`-ul local peste restul suitei (defectul deja plătit o dată în proiect)."""
    import ast
    from pathlib import Path

    src = Path("scripts/llm_call_budget_probe.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_calls = [
        n.value.func.id
        for n in tree.body
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert "load_dotenv" not in top_level_calls
