"""NX-327 pasul 0.5 al kernelului: inventarul MECANIC al scriitorilor de stare.

Parcurge `src/**/*.py` cu `ast` (nu grep -- memoria proiectului: grep-ul subestimează
membrii unei clase 3-4x) și raportează fiecare loc care scrie starea conversației, pe
v1 (`ConversationState` / `conversations.state` jsonb) sau pe v2 (propuneri typed spre
`state_reducer`), în cele 5 forme din card (`tasks/stage1/NX-327.md`):

  1. atribuire / augmentare / apel mutant pe un atribut al stării (`ctx.state.<f> = …`,
     inclusiv prin alias local legat în aceeași funcție: `s = ctx.state; s.<f>.update(…)`);
  2. scriere în `ctx.state_patch` (subscript, `.update(...)`, `ToolResult(state_patch=...)`);
  3. literal de dict cu cheia câmpului, într-o construcție `{**base, "<f>": …}` (forma din
     `src/worker/processor.py::_build_new_state`);
  4. `dataclasses.replace(state, <f>=…)` pe tipurile de stare (`ConversationStateV2`);
  5. constructori de propunere `StateUpdateProposal(op="<op>", …)`, plus apelurile către
     funcțiile care le construiesc direct (urmărite O SINGURĂ dată prin numele funcției,
     fără analiză de flux -- exact ce cere cardul).

Ce nu se poate rezolva static (acces dinamic: `setattr(ctx.state, name, …)`,
`ctx.state_patch[cheie_calculată] = …`, `state_patch.update(o_variabilă)`) se raportează
separat, ca `unresolved` -- NU se ignoră (altfel un extractor "orb" ar produce liniște, nu
un semnal).

Extractorul lucrează pe NUME (`ctx.state`, `state_patch`, `ToolResult`, `StateUpdateProposal`,
`replace`), nu pe rezolvare de import -- la fel ca aliasurile, „se prind prin nume".

Moduri:
  python scripts/state_writers.py             -> regenerează docs/STATE-WRITERS.md
  python scripts/state_writers.py --check      -> exit 1 dacă docul de pe disc diferă
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_ROOT = REPO_ROOT / "src"
DEFAULT_FIELDS_PATH = Path(__file__).resolve().parent / "state_writers_fields.json"
DEFAULT_FATE_PATH = Path(__file__).resolve().parent / "state_writers_fate.json"
DEFAULT_DOC_PATH = REPO_ROOT / "docs" / "STATE-WRITERS.md"

# Forma de scriere -- numele exacte cerute de Test Cases din card (`assign`, `mutating_call`)
# + restul, ca variantele fiecărei forme 1-5 să rămână distinse în doc/teste.
FORM_ASSIGN = "assign"
FORM_AUGASSIGN = "augassign"
FORM_SUBSCRIPT_ASSIGN = "subscript_assign"
FORM_MUTATING_CALL = "mutating_call"
FORM_PATCH_SUBSCRIPT = "patch_subscript"
FORM_PATCH_UPDATE = "patch_update"
FORM_PATCH_MUTATING_CALL = "patch_mutating_call"
FORM_TOOL_RESULT = "tool_result_kwarg"
FORM_DICT_LITERAL = "dict_literal"
FORM_DATACLASSES_REPLACE = "dataclasses_replace"
FORM_PROPOSAL_CONSTRUCTOR = "proposal_constructor"
FORM_PROPOSAL_CALL = "proposal_call"

SPEC_FORM_OF = {
    FORM_ASSIGN: 1,
    FORM_AUGASSIGN: 1,
    FORM_SUBSCRIPT_ASSIGN: 1,
    FORM_MUTATING_CALL: 1,
    FORM_PATCH_SUBSCRIPT: 2,
    FORM_PATCH_UPDATE: 2,
    FORM_PATCH_MUTATING_CALL: 2,
    FORM_TOOL_RESULT: 2,
    FORM_DICT_LITERAL: 3,
    FORM_DATACLASSES_REPLACE: 4,
    FORM_PROPOSAL_CONSTRUCTOR: 5,
    FORM_PROPOSAL_CALL: 5,
}

# Cui i se atribuie calea (v1/v2) fiecărei forme -- unde starea persistată ATERIZEAZĂ, nu unde
# rulează codul. `state_patch` aterizează verbatim în v1 (`_build_new_state` face merge cu el);
# consumul lui pe v2 (`_turn_proposals`) produce propriile lui scriitori (forma 5), separat.
PATH_OF_FORM = {
    FORM_ASSIGN: "v1",
    FORM_AUGASSIGN: "v1",
    FORM_SUBSCRIPT_ASSIGN: "v1",
    FORM_MUTATING_CALL: "v1",
    FORM_PATCH_SUBSCRIPT: "v1",
    FORM_PATCH_UPDATE: "v1",
    FORM_PATCH_MUTATING_CALL: "v1",
    FORM_TOOL_RESULT: "v1",
    FORM_DICT_LITERAL: "v1",
    FORM_DATACLASSES_REPLACE: "v2",
    FORM_PROPOSAL_CONSTRUCTOR: "v2",
    FORM_PROPOSAL_CALL: "v2",
}

# Numele (variabile) pe care `replace(...)`/`dataclasses.replace(...)` trebuie să le aibă ca
# PRIM argument ca să fie considerat scriitor de stare (forma 4) -- verificat împotriva tuturor
# apelurilor `replace(` din `src/` (2026-09-25): fiecare non-stare folosește alt nume
# (`rich`, `inp`, `conflict`, `self`, `it`, `snapshot`, `current`), niciodată `state`/`s`.
DATACLASSES_REPLACE_STATE_NAMES = frozenset({"state", "s"})

# `ast.Lambda` e deliberat AFARĂ: o lambdă definită direct în corpul unei funcții (ex. lista de
# `shrink` din `state_v2.serialize`) rămâne parte din scope-ul funcției care o conține -- altfel
# `replace(s, active_search=None)` din interiorul ei ar deveni invizibil (nicio funcție numită
# n-o vizitează separat), exact opusul a ce cere cardul.
_SCOPE_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class FieldGroup:
    field: str
    label_ro: str
    v1_attrs: frozenset[str]
    v2_ops: frozenset[str]
    v2_state_fields: frozenset[str]
    v2_payload_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FieldsConfig:
    groups: tuple[FieldGroup, ...]
    op_default_field: dict[str, str]
    payload_key_field_overrides: dict[str, dict[str, str]]
    mutating_methods: frozenset[str]
    dict_literal_state_names: frozenset[str]

    def group_order(self) -> list[str]:
        return [g.field for g in self.groups]

    def field_of_v1_attr(self, attr: str) -> str:
        for g in self.groups:
            if attr in g.v1_attrs:
                return g.field
        return attr  # necatalogat -- rămâne auto-grupat pe numele lui, nu se pierde (P6-style)

    def all_v1_attrs(self) -> frozenset[str]:
        out: set[str] = set()
        for g in self.groups:
            out |= set(g.v1_attrs)
        return frozenset(out)

    def field_of_v2_state_field(self, name: str) -> str:
        for g in self.groups:
            if name in g.v2_state_fields:
                return g.field
        return "other"

    def field_of_op(self, op: str, payload_key: str | None = None) -> str:
        if payload_key is not None:
            overrides = self.payload_key_field_overrides.get(op) or {}
            if payload_key in overrides:
                return overrides[payload_key]
        return self.op_default_field.get(op, op)


def load_fields(path: Path = DEFAULT_FIELDS_PATH) -> FieldsConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    groups = tuple(
        FieldGroup(
            field=g["field"],
            label_ro=g["label_ro"],
            v1_attrs=frozenset(g.get("v1_attrs") or []),
            v2_ops=frozenset(g.get("v2_ops") or []),
            v2_state_fields=frozenset(g.get("v2_state_fields") or []),
            v2_payload_keys=frozenset(g.get("v2_payload_keys") or []),
        )
        for g in raw["groups"]
    )
    return FieldsConfig(
        groups=groups,
        op_default_field=dict(raw.get("op_default_field") or {}),
        payload_key_field_overrides={
            op: dict(mapping)
            for op, mapping in (raw.get("payload_key_field_overrides") or {}).items()
        },
        mutating_methods=frozenset(raw.get("mutating_methods") or []),
        dict_literal_state_names=frozenset(raw.get("dict_literal_state_names") or []),
    )


@dataclass(frozen=True)
class Writer:
    file: str
    function: str
    field: str
    raw_key: str
    form: str
    path: str  # "v1" | "v2"
    lineno: int

    @property
    def spec_form(self) -> int:
        return SPEC_FORM_OF[self.form]

    @property
    def fate_key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.field)


@dataclass(frozen=True)
class Unresolved:
    file: str
    function: str
    reason: str
    lineno: int


@dataclass
class ExtractionResult:
    writers: list[Writer] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)

    def fate_keys(self) -> set[tuple[str, str, str]]:
        return {w.fate_key for w in self.writers}


def _qualname(stack: list[str]) -> str:
    return ".".join(stack) if stack else "<module>"


def _own_scope_statements(body: list[ast.stmt]):
    """Walk peste subtree-ul unei funcții care NU coboară în scope-uri noi (funcții imbricate)
    -- acelea au propriul lor pass, ca "aceeași funcție" din card să însemne exact asta, nu
    întregul subarbore lexical.

    `body` sunt STATEMENTELE proprii ale scope-ului de pornit (fie `tree.body` la nivel de
    modul, fie `node.body` al unei funcții anume) -- acelea se coboară necondiționat. Abia
    când un nod ÎNTÂLNIT ÎN COBORÂRE e el însuși o funcție/metodă (imbricată), oprim: îl
    raportăm (yield), dar nu-i mai enumerăm corpul -- altfel scope-ul de modul „ar vedea" prin
    fiecare `def` toate atribuirile din corpul ei, exact bug-ul pe care regula asta îl evită."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_DEFS):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _is_attr(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == name


def _collect_aliases(statements: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """Nume locale legate la `ctx.state` / `ctx.state_patch` prin `x = ctx.state`. Prinse pe
    NUME (spec), o singură trecere, fără ordonare -- un alias folosit oriunde în funcție
    contează (așa cere edge case-ul din card)."""
    state_aliases: set[str] = set()
    patch_aliases: set[str] = set()
    for node in statements:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        target_name = node.targets[0].id
        if _is_attr(node.value, "state"):
            state_aliases.add(target_name)
        elif _is_attr(node.value, "state_patch"):
            patch_aliases.add(target_name)
    return state_aliases, patch_aliases


def _root_kind(node: ast.AST, state_aliases: set[str], patch_aliases: set[str]) -> str | None:
    if _is_attr(node, "state"):
        return "state"
    if _is_attr(node, "state_patch"):
        return "state_patch"
    if isinstance(node, ast.Name):
        if node.id in state_aliases:
            return "state"
        if node.id in patch_aliases:
            return "state_patch"
    return None


def _callee_name(node: ast.AST) -> str | None:
    """Numele „gol" al apelantului -- `foo(...)` sau `mod.foo(...)` -> `"foo"`. Rezoluție pe
    nume, ca restul extractorului (nu pe import)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dict_literal_str_keys(node: ast.AST) -> list[str] | None:
    """`{"a": .., "b": ..}` -> `["a","b"]`. `None` dacă nu e un literal de dict SAU are vreo
    cheie ne-constantă/ne-string (nu putem rezolva static -- apelantul decide ce face cu asta)."""
    if not isinstance(node, ast.Dict):
        return None
    keys: list[str] = []
    for k in node.keys:
        if k is None:  # `**spread`
            continue
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
        else:
            return None
    return keys


def _has_dict_spread(node: ast.Dict) -> bool:
    return any(k is None for k in node.keys)


def _dict_spread_names(node: ast.Dict) -> set[str]:
    """Numele (`ast.Name.id`) ale expresiilor `**expr` dintr-un literal de dict. Un `**` peste
    altceva decât un nume simplu (`**steps[-1]`, `**self.x`) nu contează -- forma 3 e "literal de
    dict cu cheia câmpului", nu orice dict care întâmplător are o cheie omonimă (vezi
    `_relax_ladder`: `{**steps[-1], "constraints": ...}` construiește un pas de relaxare a
    căutării, nu starea persistată -- „constraints" e coincidență de nume, nu stare)."""
    return {
        v.id
        for k, v in zip(node.keys, node.values, strict=True)
        if k is None and isinstance(v, ast.Name)
    }


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


class _FunctionWalker:
    """O trecere peste UN fișier: găsește fiecare funcție/metodă (inclusiv imbricate) ca scope
    separat, colectează scriitorii direcți (formele 1-4 + 5-direct) + `unresolved`, și
    înregistrează „fabricile" de propuneri (funcții cu un `StateUpdateProposal(op=...)` direct
    în corp) pentru pasul 2 (apeluri indirecte, forma 5)."""

    def __init__(self, fields: FieldsConfig, rel_path: str):
        self.fields = fields
        self.rel_path = rel_path
        self.writers: list[Writer] = []
        self.unresolved: list[Unresolved] = []
        # nume_funcție -> listă de (field, raw_key) produse DIRECT în corpul ei
        # nume_funcție -> set unic de (field, raw_key) -- o fabrică care construiește ACELAȘI
        # `op`/câmp de mai multe ori (ex. `_need_proposals` pe `set_need`, o dată pe buclă) nu
        # trebuie să dubleze rândul de la fiecare apelant indirect.
        self.factories: dict[str, set[tuple[str, str]]] = defaultdict(set)

    def walk_module(self, tree: ast.Module) -> None:
        self._walk_scope(tree.body, stack=[])
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk_function(node, stack=self._enclosing_stack(tree, node))

    def _enclosing_stack(self, tree: ast.Module, target) -> list[str]:
        # Reconstruiește stack-ul de nume ale funcțiilor-părinte, pe scurt (ast nu dă părinți).
        stack: list[str] = []

        def find(node, trail):
            if node is target:
                stack.extend(trail)
                return True
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if find(child, [*trail, child.name]):
                        return True
                else:
                    if find(child, trail):
                        return True
            return False

        find(tree, [])
        return stack[:-1]  # ultimul e chiar `target`, adăugat separat în `_walk_function`

    def _walk_function(self, node, stack: list[str]) -> None:
        full_stack = [*stack, node.name]
        self._walk_scope(node.body, stack=full_stack)

    def _walk_scope(self, body: list[ast.stmt], stack: list[str]) -> None:
        func_name = _qualname(stack)
        bare_name = stack[-1] if stack else "<module>"
        # `_own_scope_statements` deja aplatizează TOT subarborele propriu (inclusiv Assign-uri
        # din if/for/with/try), fără să coboare în scope-uri noi -- un singur pas peste el ajunge
        # și pentru aliasuri, și pentru scriitori.
        statements = list(_own_scope_statements(body))
        state_aliases, patch_aliases = _collect_aliases(
            [n for n in statements if isinstance(n, ast.Assign)]
        )

        for node in statements:
            self._visit_node(node, func_name, bare_name, state_aliases, patch_aliases)

    def _record(self, func: str, field_id: str, raw_key: str, form: str, lineno: int) -> None:
        self.writers.append(
            Writer(
                file=self.rel_path,
                function=func,
                field=field_id,
                raw_key=raw_key,
                form=form,
                path=PATH_OF_FORM[form],
                lineno=lineno,
            )
        )

    def _record_unresolved(self, func: str, reason: str, lineno: int) -> None:
        self.unresolved.append(
            Unresolved(file=self.rel_path, function=func, reason=reason, lineno=lineno)
        )

    def _visit_node(self, node, func: str, bare_name: str, state_aliases, patch_aliases) -> None:
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            self._visit_assign(node, func, state_aliases, patch_aliases)
        elif isinstance(node, ast.Call):
            self._visit_call(node, func, bare_name, state_aliases, patch_aliases)
        elif isinstance(node, ast.Dict) and _has_dict_spread(node):
            if _dict_spread_names(node) & self.fields.dict_literal_state_names:
                self._visit_dict_literal(node, func)

    # -- forma 1 ------------------------------------------------------------------------------

    def _visit_assign(self, node, func: str, state_aliases, patch_aliases) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        form = FORM_ASSIGN if isinstance(node, ast.Assign) else FORM_AUGASSIGN
        for target in targets:
            if isinstance(target, ast.Attribute):
                if _root_kind(target.value, state_aliases, patch_aliases) == "state":
                    field_id = self.fields.field_of_v1_attr(target.attr)
                    self._record(func, field_id, target.attr, form, node.lineno)
            elif isinstance(target, ast.Subscript):
                base = target.value
                if isinstance(base, ast.Attribute):
                    root = _root_kind(base.value, state_aliases, patch_aliases)
                    if root == "state":
                        field_id = self.fields.field_of_v1_attr(base.attr)
                        self._record(func, field_id, base.attr, FORM_SUBSCRIPT_ASSIGN, node.lineno)
                        continue
                root = _root_kind(base, state_aliases, patch_aliases)
                if root == "state_patch":
                    key = _str_const(_subscript_key(target))
                    if key is None:
                        self._record_unresolved(func, "dynamic_subscript_key", node.lineno)
                    else:
                        field_id = self.fields.field_of_v1_attr(key)
                        self._record(func, field_id, key, FORM_PATCH_SUBSCRIPT, node.lineno)

    # -- forma 1 (apel mutant) + forma 2 (.update/.pop/...) + forma 2 (ToolResult) --------------
    # -- forma 4 (dataclasses.replace) + forma 5 (StateUpdateProposal + setattr dinamic) --------

    def _visit_call(
        self, node: ast.Call, func: str, bare_name: str, state_aliases, patch_aliases
    ) -> None:
        callee = _callee_name(node.func)

        # setattr dinamic -- unresolved
        if callee == "setattr" and node.args:
            root = _root_kind(node.args[0], state_aliases, patch_aliases)
            if root in ("state", "state_patch"):
                self._record_unresolved(func, "setattr_dynamic", node.lineno)
            return

        # apel mutant pe `ctx.state.<f>` / alias, sau pe `ctx.state_patch` / alias
        if isinstance(node.func, ast.Attribute) and node.func.attr in self.fields.mutating_methods:
            method = node.func.attr
            base = node.func.value
            if isinstance(base, ast.Attribute):
                root = _root_kind(base.value, state_aliases, patch_aliases)
                if root == "state":
                    field_id = self.fields.field_of_v1_attr(base.attr)
                    self._record(func, field_id, base.attr, FORM_MUTATING_CALL, node.lineno)
                    return
            root = _root_kind(base, state_aliases, patch_aliases)
            if root == "state_patch":
                self._visit_patch_mutating_call(node, func, method)
                return

        # `ToolResult(state_patch={...})`
        if callee == "ToolResult":
            payload = _call_kwarg(node, "state_patch")
            if payload is not None:
                keys = _dict_literal_str_keys(payload)
                if keys is None:
                    self._record_unresolved(func, "dynamic_tool_result_state_patch", node.lineno)
                else:
                    for key in keys:
                        field_id = self.fields.field_of_v1_attr(key)
                        self._record(func, field_id, key, FORM_TOOL_RESULT, node.lineno)

        # `dataclasses.replace(state, f=...)` / `replace(state, f=...)`
        if callee == "replace" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in DATACLASSES_REPLACE_STATE_NAMES:
                for kw in node.keywords:
                    if kw.arg is None:
                        continue
                    field_id = self.fields.field_of_v2_state_field(kw.arg)
                    self._record(func, field_id, kw.arg, FORM_DATACLASSES_REPLACE, node.lineno)

        # `StateUpdateProposal(op="...", ..., payload={...})`
        if callee == "StateUpdateProposal":
            self._visit_proposal_constructor(node, func, bare_name)

    def _visit_patch_mutating_call(self, node: ast.Call, func: str, method: str) -> None:
        if method == "update":
            if not node.args:
                self._record_unresolved(func, "dynamic_patch_update_arg", node.lineno)
                return
            keys = _dict_literal_str_keys(node.args[0])
            if keys is None:
                self._record_unresolved(func, "dynamic_patch_update_arg", node.lineno)
                return
            for key in keys:
                field_id = self.fields.field_of_v1_attr(key)
                self._record(func, field_id, key, FORM_PATCH_UPDATE, node.lineno)
            return
        # .pop("x", ...) / .setdefault("x", ...) / .popitem() / .clear() / ...
        key = _str_const(node.args[0]) if node.args else None
        if key is None:
            self._record_unresolved(func, "dynamic_patch_mutating_call", node.lineno)
            return
        field_id = self.fields.field_of_v1_attr(key)
        self._record(func, field_id, key, FORM_PATCH_MUTATING_CALL, node.lineno)

    def _visit_proposal_constructor(self, node: ast.Call, func: str, bare_name: str) -> None:
        op_node = _call_kwarg(node, "op")
        if op_node is None and node.args:
            op_node = node.args[0]
        op = _str_const(op_node)
        if op is None:
            self._record_unresolved(func, "dynamic_proposal_op", node.lineno)
            return

        payload_node = _call_kwarg(node, "payload")
        payload_keys: list[str] | None = None
        if payload_node is not None:
            payload_keys = _dict_literal_str_keys(payload_node)

        overrides = self.fields.payload_key_field_overrides.get(op)
        if overrides and payload_keys:
            for key in payload_keys:
                field_id = self.fields.field_of_op(op, payload_key=key)
                raw_key = f"{op}.{key}"
                self._record(func, field_id, raw_key, FORM_PROPOSAL_CONSTRUCTOR, node.lineno)
                self.factories[bare_name].add((field_id, raw_key))
        else:
            field_id = self.fields.field_of_op(op)
            self._record(func, field_id, op, FORM_PROPOSAL_CONSTRUCTOR, node.lineno)
            self.factories[bare_name].add((field_id, op))

    # -- forma 3 --------------------------------------------------------------------------------

    def _visit_dict_literal(self, node: ast.Dict, func: str) -> None:
        known = self.fields.all_v1_attrs()
        for k in node.keys:
            if k is None:
                continue
            key = _str_const(k)
            if key is not None and key in known:
                field_id = self.fields.field_of_v1_attr(key)
                self._record(func, field_id, key, FORM_DICT_LITERAL, node.lineno)


def _subscript_key(node: ast.Subscript) -> ast.AST:
    sl = node.slice
    # Python 3.9+: `slice` e direct expresia (fără `ast.Index`).
    return sl


def _rel_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def scan_source(source: str, filename: str, fields: FieldsConfig) -> ExtractionResult:
    tree = ast.parse(source, filename=filename)
    walker = _FunctionWalker(fields, filename)
    walker.walk_module(tree)
    return _apply_indirect_calls(tree, walker)


def _apply_indirect_calls(tree: ast.Module, walker: _FunctionWalker) -> ExtractionResult:
    """Pasul 2 al formei 5: apeluri către o fabrică de propuneri, urmărite o singură dată prin
    numele funcției. Un apel ÎN INTERIORUL fabricii nu se auto-numără (nu e „un alt scriitor")."""
    if not walker.factories:
        return ExtractionResult(writers=walker.writers, unresolved=walker.unresolved)

    extra: list[Writer] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Apelurile din CORPUL PROPRIU al acestei funcții (nu al copiilor -- ca „aceeași
        # funcție" să rămână adevărat și aici, o fabrică nu-și numără propriile apeluri
        # recursive ca „apel indirect"). Refolosim helper-ul de scope.
        own_calls = [n for n in _own_scope_statements(node.body) if isinstance(n, ast.Call)]
        for call in own_calls:
            callee = _callee_name(call.func)
            if callee is None or callee == node.name:
                continue
            produced_by_callee = walker.factories.get(callee)
            if not produced_by_callee:
                continue
            func_name = _qualname_for_node(tree, node)
            for field_id, raw_key in sorted(produced_by_callee):
                extra.append(
                    Writer(
                        file=walker.rel_path,
                        function=func_name,
                        field=field_id,
                        raw_key=f"{callee}() -> {raw_key}",
                        form=FORM_PROPOSAL_CALL,
                        path=PATH_OF_FORM[FORM_PROPOSAL_CALL],
                        lineno=call.lineno,
                    )
                )
    return ExtractionResult(writers=[*walker.writers, *extra], unresolved=walker.unresolved)


def _qualname_for_node(tree: ast.Module, target) -> str:
    stack: list[str] = []

    def find(node, trail):
        if node is target:
            stack.extend(trail)
            return True
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if find(child, [*trail, child.name]):
                    return True
            else:
                if find(child, trail):
                    return True
        return False

    find(tree, [])
    return _qualname(stack)


def scan_paths(paths: list[Path], fields: FieldsConfig, *, root: Path) -> ExtractionResult:
    writers: list[Writer] = []
    unresolved: list[Unresolved] = []
    for path in sorted(paths):
        source = path.read_text(encoding="utf-8")
        rel = _rel_posix(path, root)
        result = scan_source(source, rel, fields)
        writers.extend(result.writers)
        unresolved.extend(result.unresolved)
    writers.sort(key=lambda w: (w.file, w.function, w.lineno, w.form))
    unresolved.sort(key=lambda u: (u.file, u.function, u.lineno))
    return ExtractionResult(writers=writers, unresolved=unresolved)


def scan_src(
    src_root: Path = DEFAULT_SRC_ROOT, fields: FieldsConfig | None = None
) -> ExtractionResult:
    fields = fields or load_fields()
    files = sorted(src_root.rglob("*.py"))
    return scan_paths(files, fields, root=src_root.parent)


# ---------------------------------------------------------------------------------------------
# Soarta declarată (`state_writers_fate.json`) + generarea docului
# ---------------------------------------------------------------------------------------------

FATE_VALUES = frozenset({"executor_output", "becomes_proposal", "retired", "stays"})


@dataclass(frozen=True)
class FateEntry:
    file: str
    function: str
    field: str
    why: str
    fate: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.field)


def load_fate(path: Path = DEFAULT_FATE_PATH) -> dict[tuple[str, str, str], FateEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw["entries"] if isinstance(raw, dict) else raw
    out: dict[tuple[str, str, str], FateEntry] = {}
    for item in entries:
        entry = FateEntry(
            file=item["file"],
            function=item["function"],
            field=item["field"],
            why=item["why"],
            fate=item["fate"],
        )
        out[entry.key] = entry
    return out


def generate_doc(
    result: ExtractionResult,
    fields: FieldsConfig,
    fate: dict[tuple[str, str, str], FateEntry],
) -> str:
    label_of = {g.field: g.label_ro for g in fields.groups}
    order = fields.group_order()

    def group_rank(field_id: str) -> tuple[int, str]:
        if field_id in order:
            return (order.index(field_id), field_id)
        return (len(order), field_id)

    by_field: dict[str, list[Writer]] = defaultdict(list)
    for w in result.writers:
        by_field[w.field].append(w)

    lines: list[str] = []
    lines.append("<!-- GENERAT de scripts/state_writers.py -- nu edita de mână. -->")
    lines.append("<!-- Rulează `python scripts/state_writers.py` ca să-l regenerezi. -->")
    lines.append("")
    lines.append("# Inventarul scriitorilor de stare (NX-327, pasul 0.5 al kernelului)")
    lines.append("")
    lines.append(
        "Fiecare loc din `src/**/*.py` care scrie starea conversației (v1 sau v2), derivat "
        "MECANIC prin `ast` (nu grep). Pregătește I3/I20 din "
        "[`docs/KERNEL-CONTRACT-v1.md`](KERNEL-CONTRACT-v1.md): pasul 3 nu poate face din "
        "reducer singurul scriitor peste o stare pe care o mai scriu și alții, fără să știe "
        "cine sunt aceia."
    )
    lines.append("")
    total_writers = len(result.writers)
    total_unresolved = len(result.unresolved)
    fate_counts: dict[str, int] = defaultdict(int)
    for w in result.writers:
        entry = fate.get(w.fate_key)
        if entry is not None:
            fate_counts[entry.fate] += 1
    lines.append(
        f"**Totaluri:** {total_writers} scriitori găsiți, {len(fate)} intrări de soartă "
        f"declarate, {total_unresolved} `unresolved`."
    )
    for value in sorted(FATE_VALUES):
        lines.append(f"- `{value}`: {fate_counts.get(value, 0)}")
    lines.append("")

    for field_id in sorted(by_field, key=group_rank):
        label = label_of.get(field_id, field_id)
        lines.append(f"## `{field_id}` -- {label}")
        lines.append("")
        lines.append(
            "| Scriitor (fișier:funcție) | Cale | Formă AST | Cheie brută | De ce | Soartă |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        rows = sorted(
            by_field[field_id], key=lambda w: (w.path, w.file, w.function, w.form, w.lineno)
        )
        for w in rows:
            entry = fate.get(w.fate_key)
            why = entry.why if entry else "**LIPSEȘTE din state_writers_fate.json**"
            fate_label = f"`{entry.fate}`" if entry else "**LIPSEȘTE**"
            lines.append(
                f"| `{w.file}:{w.function}` (L{w.lineno}) | {w.path} | `{w.form}` | "
                f"`{w.raw_key}` | {why} | {fate_label} |"
            )
        lines.append("")

    if result.unresolved:
        lines.append("## `unresolved` -- acces dinamic, nu se poate rezolva static")
        lines.append("")
        lines.append("| Fișier:funcție | Motiv | Linie |")
        lines.append("| --- | --- | --- |")
        for u in result.unresolved:
            lines.append(f"| `{u.file}:{u.function}` | `{u.reason}` | {u.lineno} |")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 dacă docul de pe disc diferă")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC_ROOT)
    parser.add_argument("--fields", type=Path, default=DEFAULT_FIELDS_PATH)
    parser.add_argument("--fate", type=Path, default=DEFAULT_FATE_PATH)
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC_PATH)
    args = parser.parse_args(argv)

    fields = load_fields(args.fields)
    result = scan_src(args.src, fields)
    fate = load_fate(args.fate)
    doc = generate_doc(result, fields, fate)

    if args.check:
        current = args.doc.read_text(encoding="utf-8") if args.doc.exists() else ""
        if current != doc:
            sys.stderr.write(
                f"{args.doc} nu corespunde cu ce generează scriptul. "
                "Rulează `python scripts/state_writers.py` și comite diff-ul.\n"
            )
            return 1
        print(
            f"OK: {args.doc} e la zi "
            f"({len(result.writers)} scriitori, {len(result.unresolved)} unresolved)."
        )
        return 0

    args.doc.parent.mkdir(parents=True, exist_ok=True)
    args.doc.write_text(doc, encoding="utf-8")
    print(
        f"Scris {args.doc} ({len(result.writers)} scriitori, {len(result.unresolved)} unresolved)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
