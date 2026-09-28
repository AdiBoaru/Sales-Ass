"""NX-340 — botul nu promite intervenția unui om, pe NICIO suprafață care ajunge la client.

Transferul la operator a fost scos din produs (PR #336): nu există consolă și nici om de gardă, deci
„verific cu un coleg" sau „te pun în legătură cu un operator" e o promisiune pe care n-o onorează
nimeni. #336 le-a căutat de mână și a ratat două (promptul buclei de vânzare, FAQ-ul demo). Poarta
e pe trei suprafețe, fiecare cu un exemplu care TREBUIE să pice:

1. literalii din `src/` (AST, fără docstring-uri; comentariile nu sunt în AST), deci orice prompt
   sau text determinist scris în cod;
2. prompturile RANDATE (textul care intră în prompt din date: pachet, categorii, aliasuri);
3. datele către client: FAQ-urile de seed și pachetele de domeniu (fără listele de DETECȚIE a
   riscului, care citesc ce scrie clientul, nu ce scrie botul).

Tiparul e pe FRAZA promisiunii, nu pe cuvinte izolate: „operator" e și un operator de fațetă
(`eq`/`lte`), iar „coleg" apare în texte care nu promit nimic. Lista trăiește în test, nu în
producție: e un detector, nu text către client (P11 e al textului către client)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.agent import prompt_builder
from src.catalog.clarify_menu import words_of

ROOT = Path(__file__).resolve().parents[1]

#: Frazele unei PROMISIUNI de intervenție (a unui om sau a unei reveniri pe care n-o face nimeni),
#: fără diacritice, pe cuvinte întregi.
PROMISES: tuple[str, ...] = (
    # ro
    "cu un coleg",
    "un coleg din echipa",
    "colegii mei",
    "pune in legatura",
    "pun in legatura",
    "operator uman",
    "te conectez",
    "te transfer",
    "revin la tine",
    "revenim la tine",
    "vei fi contactat",
    "te vom contacta",
    "te contactam",
    "te sunam",
    # en
    "a colleague",
    "connect you with",
    "transfer you",
    "get back to you",
    "will contact you",
    "will reach out",
    "human agent",
)

#: Surse care CITESC textul clientului (detecția riscului, telemetrie): conțin frazele prin
#: construcție, fiindcă asta detectează. Fișier → numele atribuirilor exceptate.
_DETECTION_LITERALS: dict[str, frozenset[str]] = {
    "src/worker/stages/gates.py": frozenset({"RISK_PATTERNS"}),
}
#: Cheile de pachet care sunt DETECȚIE sau note interne, nu text către client.
_NON_CUSTOMER_KEYS = ("risk_terms", "risk_patterns")


def promise_in(text: str) -> str | None:
    """Prima frază de promisiune din `text`, pe cuvinte întregi, fără diacritice."""
    padded = f" {' '.join(words_of(text or ''))} "
    return next((p for p in PROMISES if f" {p} " in padded), None)


def test_the_detector_catches_the_phrasings_that_slipped_through_336():
    assert promise_in("dacă tot lipsește, zi că verifici cu un coleg.") == "cu un coleg"
    assert promise_in("te pot pune în legătură cu un operator uman") is not None
    assert promise_in("Îți confirm imediat detaliile, revin la tine.") == "revin la tine"
    # nu prinde ce nu promite nimic
    assert promise_in("operatorul eq pe fațeta de preț") is None
    assert promise_in("consultă un medic sau un farmacist") is None
    assert promise_in("Pentru retur, vezi service-return.com") is None


# --- 1. literalii din src/ ------------------------------------------------------------------------


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _excluded_ids(tree: ast.AST, names: frozenset[str]) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                out.update(id(n) for n in ast.walk(node))
    return out


def literal_promises(path: Path, root: Path = ROOT) -> list[tuple[int, str]]:
    """(linia, fraza) pentru fiecare literal cu o promisiune, fără docstring-uri și fără
    listele de detecție declarate."""
    rel = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree) | _excluded_ids(tree, _DETECTION_LITERALS.get(rel, frozenset()))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if not any(ch.isspace() for ch in node.value):
                continue  # un identificator (`"human_agent"`, autorul din schemă), nu text
            found = promise_in(node.value)
            if found:
                hits.append((node.lineno, found))
    return hits


def test_no_customer_facing_literal_in_src_promises_a_human():
    hits = {
        f"{p.relative_to(ROOT).as_posix()}:{line}": phrase
        for p in sorted((ROOT / "src").rglob("*.py"))
        for line, phrase in literal_promises(p)
    }
    assert hits == {}, hits


def test_the_literal_scan_fails_on_a_promise_and_ignores_docstrings(tmp_path):
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir()
    source = (
        "def f():\n"
        '    """Docstring: te pun în legătură (ignorat)."""\n'
        '    author = "human_agent"\n'
        '    return "Revin la tine."\n'
    )
    target.write_text(source, encoding="utf-8")
    assert literal_promises(target, root=tmp_path) == [(4, "revin la tine")]


# --- 2. prompturile randate -----------------------------------------------------------------------


def _rendered_prompts() -> dict[str, str]:
    inp = prompt_builder.PromptInputs.build(
        business_name="Sole Demo",
        vertical="beauty",
        locale="ro",
        categories=["creme", "seruri"],
        aliases=[("crema fata", "creme")],
    )
    return {
        "agent": prompt_builder.build_agent_system(inp),
        "reco": prompt_builder.build_reco_system(inp),
        "rich": prompt_builder.build_rich_system(inp),
        "compare": prompt_builder.build_compare_system(inp),
        "compare_v2": prompt_builder.build_compare_system(inp, axes_v2=True),
        "order": prompt_builder.ORDER_RECO_SYSTEM,
    }


@pytest.mark.parametrize("name", sorted(_rendered_prompts()))
def test_no_rendered_prompt_promises_a_human(name):
    assert promise_in(_rendered_prompts()[name]) is None


#: Fiecare prompt din `src/` e fie randat mai sus, fie text literal (acoperit de scanarea 1).
#: Un prompt nou neînscris pică: altfel un al treilea `*_SYSTEM` ar scăpa ca `_TOOLS_BLOCK`.
PROMPT_REGISTRY: dict[str, str] = {
    "src/agent/prompt_builder.py:ORDER_RECO_SYSTEM": "rendered",
    "src/agent/prompt_builder.py:build_agent_system": "rendered",
    "src/agent/prompt_builder.py:build_reco_system": "rendered",
    "src/agent/prompt_builder.py:build_rich_system": "rendered",
    "src/agent/prompt_builder.py:build_compare_system": "rendered",
    "src/agent/answer_plan_runtime.py:_PLAN_SYSTEM": "literal",
    "src/agent/brain.py:_CHIP_LABELS_SYSTEM": "literal",
    "src/agent/brain.py:_PLAN_V2_SYSTEM": "literal",
    "src/agent/llm.py:_VISION_SYSTEM": "literal",
    "src/conversation/turn_interpreter.py:system_prompt": "literal",
    "src/jobs/seed_faqs.py:_GEN_SYSTEM": "literal",
    "src/worker/profile.py:_SYSTEM": "literal",
    "src/worker/profile.py:_SYSTEM_WITH_FACTS": "literal",
    "src/worker/profile.py:build_profile_prompt": "literal",
    "src/worker/summarizer.py:_SYSTEM": "literal",
    "src/worker/summarizer.py:build_summary_prompt": "literal",
}


def _prompt_definitions() -> set[str]:
    found: set[str] = set()
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                name = node.name
                if name == "system_prompt" or (
                    name.startswith("build_") and ("system" in name or "prompt" in name)
                ):
                    found.add(f"{rel}:{name}")
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and "SYSTEM" in t.id.split("_"):
                        found.add(f"{rel}:{t.id}")
    return found


def test_every_prompt_in_src_is_registered():
    assert _prompt_definitions() == set(PROMPT_REGISTRY)


# --- 3. datele către client -----------------------------------------------------------------------


def _strings(obj, path=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _NON_CUSTOMER_KEYS or str(key).startswith("_note"):
                continue
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _strings(value, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def data_promises(doc) -> dict[str, str]:
    return {p: found for p, text in _strings(doc) if (found := promise_in(text))}


_DATA_FILES = sorted(
    [
        *(ROOT / "db" / "seed").glob("faqs_*.json"),
        *(ROOT / "db" / "seed").glob("domain_pack_*.json"),
        *(ROOT / "src" / "domain" / "defaults").glob("*.json"),
    ]
)


@pytest.mark.parametrize("path", _DATA_FILES, ids=lambda p: p.name)
def test_no_seed_or_pack_text_promises_a_human(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert data_promises(doc) == {}


def test_the_data_scan_skips_detection_lists_and_fails_on_a_template():
    assert data_promises({"risk_terms": {"ro": {"human_request": ["operator uman"]}}}) == {}
    bad = {"chip_templates": {"ask_human": {"ro": "Un coleg te contactează, te pun în legătură"}}}
    assert data_promises(bad) == {".chip_templates.ask_human.ro": "pun in legatura"}


def test_the_demo_faq_seed_promises_nothing():
    from src.jobs.seed_faqs import BASE_FAQS_RO

    assert {q: found for q, a in BASE_FAQS_RO if (found := promise_in(a))} == {}
