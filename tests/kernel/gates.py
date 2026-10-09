"""NX-328 felia 1b — porțile AST ale contractului `kernel.v1.0` (§„Enforcement and change control").

Fiecare poartă e o funcție PURĂ `(source, filename) -> list[Violation]`, ca aceeași verificare să
ruleze pe codul real și pe exemplele din `tests/test_kernel_contract.py` care TREBUIE să pice. O
poartă fără un exemplu care pică e oarbă și dă verde pe orice.

| Poartă | Invariant |
|---|---|
| `llm_calls` | I13: un modul pur nu importă și nu cheamă un client de model |
| `search_args` | I2: `SearchArgs` se construiește doar în planner |
| `state_writes` | I3: un modul de kernel scrie starea doar prin propuneri (extractorul NX-327) |
| `executor_needs` | I20: executorii nu scriu nevoi sau subiect |
| `raw_text` | Enforcement: nicio ramificare pe textul brut al clientului |

Limitele, declarate: analiza e intra-funcție și pe nume (fără flux între funcții), deci o valoare
de text brut trecută printr-un apel își pierde marcajul. E direcția sigură pentru poartă: codul
care trece textul prin tabelele per locale (`src/catalog/query_terms.py`) e exact ce contractul
permite."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES_FILE = ROOT / "tests" / "kernel_modules.json"
ALLOWLIST_FILE = ROOT / "tests" / "kernel_contract_allowlist.json"


@dataclass(frozen=True, order=True)
class Violation:
    file: str
    lineno: int
    gate: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.lineno}  [{self.gate}] {self.detail}"


# --- I13: niciun client de model într-un modul pur ------------------------------------------------

#: Modulele care SUNT clientul de model sau îl poartă. `src.agent.brain` e pe listă fiindcă e al
#: doilea „un apel decide tot", înghețat ca benchmark (contractul, runda 1).
LLM_MODULES: tuple[str, ...] = ("openai", "anthropic", "src.agent.llm", "src.agent.brain")
#: Metodele prin care se cheamă modelul. Se prind și ca nume importate, și ca apeluri pe un obiect
#: primit ca parametru (`deps.llm.complete_schema(...)`), care n-ar lăsa niciun import în urmă.
#: NX-336: și `complete_schema_raw` (adaptorul), `run_tool_loop_structured` (creierul unic),
#: `moderate` și `describe_image`, fiecare cu exemplul care pică: lista era incompletă față de
#: metodele `LLMClient`.
LLM_CALLS: frozenset[str] = frozenset(
    {
        "complete",
        "complete_schema",
        "complete_schema_raw",
        "classify_json",
        "run_tool_loop",
        "run_tool_loop_structured",
        "tool_round",
        "respond_round",
        "embed",
        "moderate",
        "describe_image",
    }
)


def _is_llm_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in LLM_MODULES)


def llm_calls(source: str, filename: str) -> list[Violation]:
    out: list[Violation] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_llm_module(alias.name):
                    out.append(
                        Violation(filename, node.lineno, "llm_calls", f"import {alias.name}")
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_llm_module(module):
                out.append(Violation(filename, node.lineno, "llm_calls", f"from {module} import"))
            for alias in node.names:
                if alias.name in LLM_CALLS | {"LLMClient"}:
                    out.append(
                        Violation(filename, node.lineno, "llm_calls", f"import {alias.name}")
                    )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in LLM_CALLS and not _is_str_method_receiver(node.func.value):
                out.append(Violation(filename, node.lineno, "llm_calls", f".{node.func.attr}(...)"))
    return out


def _is_str_method_receiver(node: ast.AST) -> bool:
    # `"".join(...)` și prietenii nu sunt apeluri de model; niciuna dintre metodele de mai sus nu
    # există pe `str`, dar un literal ca receptor nu poate fi niciodată un client.
    return isinstance(node, ast.Constant)


# --- I2: SearchArgs doar în planner ---------------------------------------------------------------

_BUILDERS = frozenset({"model_validate", "model_validate_json", "model_construct"})


def _names_search_args(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "SearchArgs") or (
        isinstance(node, ast.Attribute) and node.attr == "SearchArgs"
    )


def search_args(source: str, filename: str) -> list[Violation]:
    out: list[Violation] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        built = _names_search_args(func) or (
            isinstance(func, ast.Attribute)
            and func.attr in _BUILDERS
            and _names_search_args(func.value)
        )
        if built:
            out.append(Violation(filename, node.lineno, "search_args", "SearchArgs construit"))
    return out


# --- I3 / I20: scrierile de stare, prin extractorul NX-327 ----------------------------------------

_PROPOSAL_FORMS = frozenset({"proposal_constructor", "proposal_call"})


def _writers(source: str, filename: str):
    from scripts import state_writers as sw  # noqa: PLC0415 — scriptul e sursa unică a formelor

    return sw.scan_source(source, filename, sw.load_fields())


def state_writes(source: str, filename: str) -> list[Violation]:
    """I3: în afara reducerului, un modul de kernel NU scrie starea direct. O propunere
    (`StateUpdateProposal`) e permisă: e chiar forma prin care o scriere ajunge la reducer."""
    result = _writers(source, filename)
    out = [
        Violation(filename, w.lineno, "state_writes", f"{w.field} prin {w.form}")
        for w in result.writers
        if w.form not in _PROPOSAL_FORMS
    ]
    out += [
        Violation(filename, u.lineno, "state_writes", f"nerezolvabil static: {u.reason}")
        for u in result.unresolved
    ]
    return out


def executor_needs(source: str, filename: str) -> list[Violation]:
    """I20: ieșirea unui executor scrie doar `references` și `active_search`, niciodată nevoi sau
    subiect (altfel rezultatele căutării ar decide ce a cerut clientul)."""
    return [
        Violation(filename, w.lineno, "executor_needs", f"{w.field} prin {w.form}")
        for w in _writers(source, filename).writers
        if w.field == "needs_topic"
    ]


# --- ramificarea pe textul brut -------------------------------------------------------------------

#: Numele (variabile, parametri, atribute) care poartă textul BRUT al clientului sau al modelului
#: care îl citează: `message.body`, `StateChange.quote`, `Reference.text`, `Act.query`.
RAW_TEXT_NAMES: frozenset[str] = frozenset({"body", "quote", "text", "query", "readings"})
_REGEX_METHODS = frozenset({"search", "match", "fullmatch", "findall", "finditer", "sub", "split"})
_TEXT_TESTS = frozenset({"startswith", "endswith", "find", "rfind", "index", "rindex", "count"})
#: Singura sursă permisă de tipare peste textul brut: tabelele centrale per locale.
MARKER_TABLE_MODULES: tuple[str, ...] = ("src.catalog.query_terms",)


def _is_str_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_str_literal(e) for e in node.elts)
    return False


class _Taint:
    """Marcajul de text brut, într-o funcție. Pe nume: un parametru sau o variabilă numită ca
    textul brut e marcată, iar marcajul trece prin atribuiri și bucle."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def expr(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and (sub.id in self.names or sub.id in RAW_TEXT_NAMES):
                return True
            if isinstance(sub, ast.Attribute) and sub.attr in RAW_TEXT_NAMES:
                return True
        return False

    def bind(self, target: ast.AST) -> None:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                self.names.add(sub.id)


def _module_regexes(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(tiparele compilate LOCAL la nivel de modul, numele importate din tabelele de markeri)."""
    compiled: set[str] = set()
    allowed: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "compile":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        compiled.add(target.id)
        if isinstance(node, ast.ImportFrom) and (node.module or "") in MARKER_TABLE_MODULES:
            allowed.update(alias.asname or alias.name for alias in node.names)
    return compiled, allowed


def raw_text(source: str, filename: str) -> list[Violation]:
    """Contractul: componentele pure nu fac pattern-matching (regex, `in`, `startswith`,
    comparație cu un literal) pe textul brut. Ramificarea pe câmpuri structurate
    (`change.relation == "lte"`) e cod normal și trece."""
    tree = ast.parse(source, filename=filename)
    compiled, allowed = _module_regexes(tree)
    out: list[Violation] = []

    def flag(node: ast.AST, detail: str) -> None:
        out.append(Violation(filename, node.lineno, "raw_text", detail))

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        taint = _Taint()
        args = fn.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if arg.arg in RAW_TEXT_NAMES:
                taint.names.add(arg.arg)
        body = fn.body if isinstance(fn.body, list) else [fn.body]
        for node in (n for stmt in body for n in ast.walk(stmt)):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if taint.expr(node.value):
                    for target in targets:
                        taint.bind(target)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if taint.expr(node.iter):
                    taint.bind(node.target)
            elif isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(taint.expr(o) for o in operands) and any(
                    _is_str_literal(o) for o in operands
                ):
                    flag(node, "comparație a textului brut cu un literal")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr, receiver = node.func.attr, node.func.value
                call_args = [*node.args, *(k.value for k in node.keywords)]
                if (
                    attr in _TEXT_TESTS
                    and taint.expr(receiver)
                    and any(_is_str_literal(a) for a in call_args)
                ):
                    flag(node, f"text brut .{attr}(literal)")
                if attr in _REGEX_METHODS and any(taint.expr(a) for a in call_args):
                    root = receiver.id if isinstance(receiver, ast.Name) else None
                    if root == "re" or (root in compiled and root not in allowed):
                        flag(node, f"regex pe textul brut ({root}.{attr})")
    return sorted(set(out))


# --- NX-332: textul brut se citește O DATĂ, într-un cititor declarat ------------------------------


def load_raw_readers() -> list[dict]:
    """Cititorii declarați ai textului brut (`raw_text_readers` din allowlist): `file`, `function`,
    `reason` nevid. Tiparul e `provenance._read_quote`: textul intră într-o funcție numită, iese ca
    dovadă structurată, iar deciziile de după nu mai văd cuvintele."""
    if not ALLOWLIST_FILE.exists():
        return []
    entries = json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8")).get("raw_text_readers", [])
    for entry in entries:
        missing = [k for k in ("file", "function", "reason") if not entry.get(k)]
        if missing:
            raise ValueError(f"cititor de text brut fără {missing}: {entry}")
    return entries


def _module_of(filename: str) -> str:
    return filename.removesuffix(".py").replace("/", ".")


def _callee(func: ast.expr) -> tuple[str, ...] | None:
    """`f(...)` ⇒ `("f",)`; `mod.f(...)` ⇒ `("mod", "f")`; altceva ⇒ None."""
    if isinstance(func, ast.Name):
        return (func.id,)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def _reachable_readers(
    tree: ast.Module, declared: list[dict], own: set[str]
) -> set[tuple[str, ...]]:
    """Apelurile care ajung la un cititor DECLARAT, legate de fișierul lui (recenzia NX-332: după
    nume, orice funcție numită `_change` sau `_read_query`, definită oriunde, trecea poarta):
    definit în fișierul curent, importat prin `from <modulul declarat> import f [as g]`, sau apelat
    ca `m.f` unde `m` e modulul declarat, importat."""
    by_module: dict[str, set[str]] = {}
    for entry in declared:
        by_module.setdefault(_module_of(entry["file"]), set()).add(entry["function"])
    reachable: set[tuple[str, ...]] = {(name,) for name in own}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in by_module:
            for alias in node.names:
                if alias.name in by_module[node.module]:
                    reachable.add((alias.asname or alias.name,))
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                module = f"{node.module}.{alias.name}"
                for function in by_module.get(module, ()):
                    reachable.add((alias.asname or alias.name, function))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    for function in by_module.get(alias.name, ()):
                        reachable.add((alias.asname, function))
    return reachable


def raw_readers(source: str, filename: str) -> list[Violation]:
    """Un câmp de text brut (`.quote`, `.query`, `.readings`, `.text`, `.body`) se citește doar
    (a) în interiorul unui cititor declarat pentru fișierul lui, sau (b) ca argument DIRECT al
    unui apel către un cititor declarat. Poarta `raw_text` prinde tiparele cunoscute (regex, `in`,
    literal); asta prinde orice altă citire, deci o excepție de la poarta de text brut e o
    declarație cu motiv, nu o scăpare."""
    declared = load_raw_readers()
    own = {e["function"] for e in declared if e["file"] == filename}
    tree = ast.parse(source, filename=filename)
    readers = _reachable_readers(tree, declared, own)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node.func) in readers:
            allowed.update(id(a) for a in (*node.args, *(k.value for k in node.keywords)))
    out: list[Violation] = []

    def visit(node: ast.AST, inside_reader: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, inside_reader or child.name in own)
                continue
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.ctx, ast.Load)
                and child.attr in RAW_TEXT_NAMES
                and not inside_reader
                and id(child) not in allowed
            ):
                out.append(
                    Violation(
                        filename,
                        child.lineno,
                        "raw_readers",
                        f".{child.attr} în afara unui cititor",
                    )
                )
            visit(child, inside_reader)

    visit(tree, False)
    return sorted(set(out))


# --- registrul și allowlistul ---------------------------------------------------------------------

GATES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "pure": ("llm_calls", "search_args", "state_writes", "raw_text", "raw_readers"),
    "planner": ("llm_calls", "state_writes", "raw_text", "raw_readers"),
    "adapter": ("search_args", "state_writes"),
    "reducer": ("llm_calls", "search_args", "raw_text", "raw_readers"),
    "executor": ("executor_needs",),
    # NX-336: orchestratorul turului interpretat cheamă adaptorul și executorii (deci nu i se aplică
    # I13), dar nu construiește `SearchArgs`, nu scrie starea (în afara restaurării declarate) și
    # citește textul brut doar în cititorii declarați. I14 i se aplică prin testul general.
    "orchestrator": ("search_args", "state_writes", "raw_text", "raw_readers"),
}
GATES = {
    "llm_calls": llm_calls,
    "search_args": search_args,
    "state_writes": state_writes,
    "executor_needs": executor_needs,
    "raw_text": raw_text,
    "raw_readers": raw_readers,
}


def load_modules() -> dict:
    return json.loads(MODULES_FILE.read_text(encoding="utf-8"))


def modules_by_role() -> dict[str, list[str]]:
    roles = load_modules()["roles"]
    return {role: list(spec["modules"]) for role, spec in roles.items()}


def load_allowlist() -> list[dict]:
    """Excepțiile, fiecare cu `gate`, `file`, `detail` și `reason` nevid (tiparul
    `scripts/conn_allowlist.json`)."""
    if not ALLOWLIST_FILE.exists():
        return []
    entries = json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8")).get("entries", [])
    for entry in entries:
        missing = [k for k in ("gate", "file", "detail", "reason") if not entry.get(k)]
        if missing:
            raise ValueError(f"intrare de allowlist fără {missing}: {entry}")
    return entries


def run_all() -> list[Violation]:
    out: list[Violation] = []
    for role, files in modules_by_role().items():
        for rel in files:
            source = (ROOT / rel).read_text(encoding="utf-8")
            for gate in GATES_BY_ROLE[role]:
                out += GATES[gate](source, rel)
    return sorted(set(out))
