"""G7-1 — bucla de tool-calling din adaptor (`LLMClient.run_tool_loop`), cu mock OpenAI.

Validează mecanica: tool_calls → execute → mesaje `tool` → repetă; cap DUR la max_steps
(forțează text final fără tools); execuție a mai multor tool_calls dintr-un pas. ZERO OpenAI."""

from src.agent.llm import LLMClient


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
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


class _Completions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, *, model, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        return _Resp(self.script.pop(0))


class _Chat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, script):
        self.chat = _Chat(_Completions(script))


def _llm(script):
    return LLMClient(_FakeOpenAI(script), model_triage="t", model_agent="a")


async def test_loop_executes_tool_then_returns_final():
    llm = _llm(
        [
            _Msg(tool_calls=[_ToolCall("c1", "search_products", '{"query":"x"}')]),
            _Msg(content="Recomand X."),
        ]
    )
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "rezultat-tool"

    out = await llm.run_tool_loop("sys", "user", [{"t": 1}], execute)
    assert out == "Recomand X."
    assert executed == [("search_products", {"query": "x"})]


async def test_loop_caps_at_max_steps_then_forces_text():
    # 3 pași toți cu tool_calls → al 4-lea apel forțează text FĂRĂ tools
    llm = _llm(
        [
            _Msg(tool_calls=[_ToolCall("c1", "search_products", "{}")]),
            _Msg(tool_calls=[_ToolCall("c2", "search_products", "{}")]),
            _Msg(tool_calls=[_ToolCall("c3", "search_products", "{}")]),
            _Msg(content="Răspuns final forțat."),
        ]
    )
    n = 0

    async def execute(name, args):
        nonlocal n
        n += 1
        return "r"

    out = await llm.run_tool_loop("s", "u", [{}], execute, max_steps=3)
    assert out == "Răspuns final forțat."
    assert n == 3  # exact 3 execuții (cap dur)
    assert llm._client.chat.completions.calls[-1]["tools"] is None  # ultimul apel fără tools


async def test_loop_runs_multiple_tool_calls_in_one_step():
    llm = _llm(
        [
            _Msg(
                tool_calls=[
                    _ToolCall("c1", "search_products", "{}"),
                    _ToolCall("c2", "get_product_details", '{"product_id":"p1"}'),
                ]
            ),
            _Msg(content="gata"),
        ]
    )
    executed = []

    async def execute(name, args):
        executed.append(name)
        return "r"

    out = await llm.run_tool_loop("s", "u", [{}], execute)
    assert out == "gata"
    assert set(executed) == {"search_products", "get_product_details"}


async def test_loop_bad_json_args_become_empty_dict():
    llm = _llm(
        [
            _Msg(tool_calls=[_ToolCall("c1", "search_products", "{not json")]),
            _Msg(content="ok"),
        ]
    )
    seen = {}

    async def execute(name, args):
        seen["args"] = args
        return "r"

    await llm.run_tool_loop("s", "u", [{}], execute)
    assert seen["args"] == {}  # JSON invalid → {} (Pydantic din tool respinge restul)
