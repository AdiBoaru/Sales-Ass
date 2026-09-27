"""NX-328 felia 1b — porțile AST ale contractului `kernel.v1.0`, armate înaintea codului păzit.

Contractul: „un invariant fără test care îl impune nu face parte din contract". Pasul 1 armează
porțile înainte să existe componentele (pașii 2-6), deci o componentă care încalcă I2/I3/I13/I14/
I20 sau ramifică pe textul brut pică la primul ei PR, nu la review.

Fiecare poartă are aici un exemplu care TREBUIE să pice și unul care trebuie să treacă: o poartă
care dă verde pe codul real, dar nu poate pica, e teatru. Excepțiile stau în
`tests/kernel_contract_allowlist.json`, fiecare cu motiv, iar o excepție care nu mai prinde nimic
pică testul (datoria doar scade)."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from tests import test_domain_leak as leak
from tests.kernel import gates

# --- registrul -----------------------------------------------------------------------------------


def test_every_registered_module_exists_and_has_one_role():
    seen: dict[str, str] = {}
    for role, files in gates.modules_by_role().items():
        assert role in gates.GATES_BY_ROLE, f"rol fără porți: {role}"
        for rel in files:
            assert (gates.ROOT / rel).is_file(), f"{rel} ({role}) nu există"
            assert rel not in seen, f"{rel} are două roluri: {seen[rel]} și {role}"
            seen[rel] = role


def test_a_planned_module_that_exists_must_be_registered_in_its_role():
    """Un modul de kernel creat fără să fie înscris ar scăpa de toate porțile. `planned` prinde
    uitarea: fișierul apare, rolul lipsește, testul pică."""
    registry = gates.load_modules()
    roles = gates.modules_by_role()
    for rel, role in registry["planned"].items():
        if (gates.ROOT / rel).is_file():
            assert rel in roles[role], f"{rel} există, dar nu e înscris în rolul `{role}`"


def test_the_contract_modules_of_step_1_are_pure():
    assert {
        "src/conversation/interpretation.py",
        "src/conversation/kernel_trace.py",
    } <= set(gates.modules_by_role()["pure"])


def test_the_step_4a_modules_are_pure_and_no_longer_planned():
    """NX-332: poarta de ambiguitate și politica de răspuns sunt componente pure: toate porțile
    rolului (I13, I2, I3, text brut, cititori declarați, I14) li se aplică."""
    registry = gates.load_modules()
    for rel in ("src/conversation/ambiguity_gate.py", "src/conversation/answer_policy.py"):
        assert rel in gates.modules_by_role()["pure"]
        assert rel not in registry["planned"]


# --- porțile pe codul real ------------------------------------------------------------------------


def _allowed(v: gates.Violation, entries: list[dict]) -> bool:
    return any(
        e["gate"] == v.gate and e["file"] == v.file and e["detail"] == v.detail for e in entries
    )


def test_the_kernel_modules_pass_every_gate():
    entries = gates.load_allowlist()
    found = [v for v in gates.run_all() if not _allowed(v, entries)]
    assert not found, "încălcări ale contractului kernel.v1.0:\n" + "\n".join(
        f"  {v}" for v in found
    )


def test_the_allowlist_has_no_dead_entries():
    entries = gates.load_allowlist()
    violations = gates.run_all()
    dead = [
        e
        for e in entries
        if not any(
            v.gate == e["gate"] and v.file == e["file"] and v.detail == e["detail"]
            for v in violations
        )
    ]
    assert not dead, f"excepții care nu mai prind nimic (șterge-le): {dead}"


def _loaded_model_clients(modules: list[str]) -> str:
    """Importă `modules` într-un proces curat și întoarce clienții de model încărcați."""
    code = textwrap.dedent(
        f"""
        import importlib, sys
        for m in {modules!r}:
            importlib.import_module(m)
        bad = sorted(m for m in sys.modules
                     if m == "openai" or m.startswith("openai.") or m == "src.agent.llm")
        print(",".join(bad))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=gates.ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def test_pure_modules_do_not_load_a_model_client_even_transitively():
    """I13, mai tare decât importul direct: un modul pur care trage `openai` printr-un import
    intermediar ar trece de verificarea pe AST."""
    roles = gates.modules_by_role()
    modules = [
        rel[:-3].replace("/", ".") for role in ("pure", "reducer") for rel in roles.get(role, [])
    ]
    loaded = _loaded_model_clients(modules)
    assert loaded == "", f"client de model încărcat tranzitiv: {loaded}"


def test_the_transitive_check_can_fail():
    assert "src.agent.llm" in _loaded_model_clients(["src.agent.llm"])


def _vertical_terms_in(source: str) -> list[tuple[int, str]]:
    """NX-264 pe o sursă, fără pragma și fără baseline."""
    terms = leak._domain_terms()
    patterns = {t: leak.re.compile(rf"(?<!\w){leak.re.escape(t)}(?!\w)") for t in terms}
    found = []
    for lineno, _, literal in leak._string_literals(leak.ast.parse(source)):
        normalized = leak._norm(literal)
        found += [(lineno, term) for term, p in patterns.items() if p.search(normalized)]
    return found


def test_i14_kernel_modules_hold_no_vertical_vocabulary_and_have_no_escape_hatch():
    """NX-264 extinsă pe kernel, FĂRĂ pragma și fără baseline: în nucleu datoria pornește de la
    zero și rămâne acolo. Executorii sunt calea de azi și îi păzește poarta NX-264 generală."""
    roles = gates.modules_by_role()
    found = [
        (rel, lineno, term)
        for role, files in roles.items()
        if role != "executor"
        for rel in files
        for lineno, term in _vertical_terms_in((gates.ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not found, f"vocabular de vertical în kernel: {found}"


def test_the_i14_check_can_fail_and_ignores_the_pragma():
    term = sorted(leak._domain_terms())[0]
    source = f'X = "{term}"  # domain-leak: ok — pragma nu scutește în kernel\n'
    assert _vertical_terms_in(source) == [(1, term)]


# --- fiecare poartă poate pica -------------------------------------------------------------------


def _check(gate: str, source: str) -> list[gates.Violation]:
    return gates.GATES[gate](textwrap.dedent(source), "fixture.py")


@pytest.mark.parametrize(
    "source",
    [
        "from src.agent.llm import LLMClient\n",
        "import openai\n",
        "from src.agent import llm\nimport src.agent.brain\n",
        "async def f(deps):\n    return await deps.llm.complete_schema('s', 'u', {})\n",
        "async def f(client):\n    return await client.run_tool_loop()\n",
    ],
)
def test_i13_gate_fails_on_a_model_client(source):
    assert _check("llm_calls", source)


def test_i13_gate_passes_on_pure_code():
    assert not _check("llm_calls", "import json\n\ndef f(x):\n    return ', '.join(x)\n")


@pytest.mark.parametrize(
    "source",
    [
        "from src.tools.catalog_tools import SearchArgs\nSearchArgs(query='x')\n",
        "from src.tools import catalog_tools\ncatalog_tools.SearchArgs(query='x')\n",
        "from src.tools.catalog_tools import SearchArgs\n"
        "SearchArgs.model_validate({'query': 'x'})\n",
    ],
)
def test_i2_gate_fails_when_search_args_is_built_outside_the_planner(source):
    assert _check("search_args", source)


def test_i2_gate_passes_on_a_type_annotation():
    source = """
    from src.tools.catalog_tools import SearchArgs

    def f(args: SearchArgs | None) -> SearchArgs | None:
        return args
    """
    assert not _check("search_args", source)


@pytest.mark.parametrize(
    "source",
    [
        "def f(ctx):\n    ctx.state.selected_product = 'p1'\n",
        "def f(ctx):\n    ctx.state_patch['active_search'] = None\n",
        "def f(ctx):\n    ctx.state.constraints.update({'budget_max': 100})\n",
    ],
)
def test_i3_gate_fails_on_a_direct_state_write(source):
    assert _check("state_writes", source)


def test_i3_gate_passes_on_a_reducer_proposal():
    source = """
    from src.conversation.state_reducer import StateUpdateProposal

    def f():
        return StateUpdateProposal("set_need", source="user_explicit", payload={"key": "k"})
    """
    assert not _check("state_writes", source)


def test_i20_gate_fails_when_an_executor_proposes_a_need_or_a_topic():
    source = """
    from src.conversation.state_reducer import StateUpdateProposal

    def serve(ctx):
        ctx.state_proposals.append(StateUpdateProposal("set_topic", source="catalog", payload={}))
    """
    assert _check("executor_needs", source)


def test_i20_gate_passes_on_the_search_session_and_references():
    source = """
    def serve(ctx, sess):
        ctx.state_patch["active_search"] = sess
    """
    assert not _check("executor_needs", source)


@pytest.mark.parametrize(
    "source",
    [
        "def f(quote):\n    return 'roșu' in quote\n",
        "def f(change):\n    return change.quote == 'da'\n",
        "def f(message):\n    body = message.body.lower()\n    return body.startswith('nu')\n",
        "def f(ref):\n    for word in ref.text.split():\n        if word in {'asta', 'acesta'}:\n"
        "            return True\n",
        "import re\ndef f(query):\n    return re.search(r'ieftin', query)\n",
        "import re\n_NEG = re.compile(r'fara')\ndef f(quote):\n    return _NEG.search(quote)\n",
    ],
)
def test_raw_text_gate_fails_on_matching_the_users_words(source):
    assert _check("raw_text", source)


@pytest.mark.parametrize(
    "source",
    [
        # o citire care ocolește tiparele porții `raw_text` (fără regex, fără literal)
        "def f(act, labels):\n    return [w for w in act.query.split() if w in labels]\n",
        "def f(change):\n    words = change.quote.lower()\n    return words\n",
        "def f(amb):\n    return {r: 1 for r in amb.readings}\n",
        # un argument INDIRECT al unui cititor nu e o citire declarată
        "def f(act):\n    return _read_query(act.query.lower(), (), None)\n",
    ],
)
def test_raw_readers_gate_fails_outside_a_declared_reader(source):
    assert _check("raw_readers", source)


def test_raw_readers_gate_passes_inside_or_straight_into_a_declared_reader():
    """Pe fișierul lui, cititorul declarat poate citi; oriunde, textul poate intra DIRECT într-un
    cititor declarat (`_read_quote(change.quote, ...)`)."""
    inside = (
        "def _read_query(query, labels, locale):\n    return query\n\ndef g(act):\n    return 1\n"
    )
    assert not gates.raw_readers(textwrap.dedent(inside), "src/conversation/answer_policy.py")
    assert gates.raw_readers(textwrap.dedent(inside), "src/conversation/other.py") == []
    direct = (
        "from src.conversation.answer_policy import _read_query\n\n"
        "def f(act):\n    return _read_query(act.query, (), None)\n"
    )
    assert not _check("raw_readers", direct)
    module = (
        "from src.conversation import answer_policy as ap\n\n"
        "def f(act):\n    return ap._read_query(act.query, (), None)\n"
    )
    assert not _check("raw_readers", module)
    elsewhere = "def f(act):\n    return act.query\n"
    assert gates.raw_readers(elsewhere, "src/conversation/answer_policy.py")


def test_raw_readers_gate_binds_a_reader_to_its_declared_file():
    """Recenzia NX-332: permisiunea era pe NUME. O funcție numită ca un cititor declarat, definită
    în alt fișier (`_change` e declarat doar în `kernel_trace.py`), trecea poarta, deci textul
    brut se putea citi oriunde sub un nume împrumutat."""
    borrowed = (
        "def _change(q):\n    return q.split()[0] == 'da'\n\n"
        "def g(change):\n    return _change(change.quote)\n"
    )
    assert gates.raw_readers(borrowed, "src/conversation/state_reducer.py")
    not_imported = "def f(act):\n    return _read_query(act.query, (), None)\n"
    assert _check("raw_readers", not_imported)


def test_every_declared_raw_text_reader_exists_and_is_in_a_kernel_module():
    """Un cititor declarat care a dispărut ar lăsa o excepție fără obiect (datoria doar scade)."""
    kernel = {rel for files in gates.modules_by_role().values() for rel in files}
    for entry in gates.load_raw_readers():
        assert entry["file"] in kernel, entry
        tree = leak.ast.parse((gates.ROOT / entry["file"]).read_text(encoding="utf-8"))
        names = {
            n.name
            for n in leak.ast.walk(tree)
            if isinstance(n, (leak.ast.FunctionDef, leak.ast.AsyncFunctionDef))
        }
        assert entry["function"] in names, entry


@pytest.mark.parametrize(
    "source",
    [
        # ramificare pe câmpuri STRUCTURATE: cod normal
        "def f(change):\n    return change.relation == 'lte'\n",
        "def f(ref):\n    return ref.kind == 'ordinal' and ref.ordinal == 2\n",
        # tiparele din tabelele CENTRALE per locale sunt singura excepție permisă
        "from src.catalog.query_terms import NEGATIONS\n"
        "def f(quote):\n    return NEGATIONS.search(quote)\n",
        # textul trecut printr-o funcție a tabelelor per locale
        "from src.catalog.query_terms import comparators\n"
        "def f(quote, locale):\n    return [c for c in comparators(locale)]\n",
    ],
)
def test_raw_text_gate_passes_on_structured_branching(source):
    assert not _check("raw_text", source)
