#!/usr/bin/env python3
"""Poartă anti-putrezire pentru documentația de arhitectură.

PROBLEMA pe care o rezolvă: `docs/ARCHITECTURE-WORKFLOWS.md` a fost scris pe
2026-07-02 cu o metodologie bună (fiecare muchie citată `file:line`) și a rămas în
urmă în ~6 săptămâni — rata complet `src/safety`, `src/knowledge`, `src/commerce`,
`answer_plan`, `match_gate`, `query_spec`, `faq_rerank`, apărute după acea dată.
(Scheletul pipeline-ului a rezistat: Diagram 4a avea deja toate cele 11 stagii.
CLAUDE.md, în schimb, descrie și azi 9.) O diagramă greșită e mai rea decât
niciuna: pe ea se iau decizii de arhitectură.

CE FACE: extrage din COD faptele care se pot enumera (stagiile pipeline-ului în
ordine, tool-urile înregistrate, procesele, rutele HTTP, flag-urile, migrările) și
le compară cu blocurile ```claim:<nume>``` din document. Divergență → exit 1.

CE **NU** GARANTEAZĂ (onest, ca să nu fie fals confort): verifică LISTELE, nu
săgețile. O diagramă poate avea toate stagiile corecte și o muchie greșită între
ele. Muchiile rămân în sarcina cititorului + `arch_explorer/verify.py`.

Utilizare:
    python scripts/verify_architecture_doc.py            # verifică (CI)
    python scripts/verify_architecture_doc.py --emit     # tipărește faptele (JSON)
    python scripts/verify_architecture_doc.py --emit-claims  # blocuri gata de lipit în doc

Zero dependențe externe, zero rețea, zero DB — rulează oriunde rulează CI-ul.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "ARCHITECTURE-WORKFLOWS.md"

# Mesajele au diacritice și săgeți; consola Windows e cp1252 și le-ar transforma în mojibake
# exact când scriptul dă FAIL — adică fix când trebuie citit. CI-ul (Linux) e deja UTF-8.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Extragerea faptelor din cod
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_stages() -> list[str]:
    """Ordinea stagiilor din `DEFAULT_STAGES` (src/worker/runner.py), prin AST.

    Ordinea CONTEAZĂ: un stagiu mutat înaintea altuia schimbă ce iese devreme.
    De aceea comparăm liste, nu mulțimi.
    """
    tree = ast.parse(_read(REPO / "src" / "worker" / "runner.py"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
        if target == "DEFAULT_STAGES" and isinstance(node.value, ast.List):
            return [e.id for e in node.value.elts if isinstance(e, ast.Name)]
    raise SystemExit("FATAL: DEFAULT_STAGES nu a fost găsit în src/worker/runner.py")


def extract_tools() -> list[str]:
    """Tool-urile agentului = decoratorii `@register("nume")` din src/tools/."""
    names: set[str] = set()
    for path in sorted((REPO / "src" / "tools").glob("*.py")):
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "register"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    names.add(str(dec.args[0].value))
    return sorted(names)


_SERVICE_RE = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*$")


def extract_processes() -> list[str]:
    """Serviciile din docker-compose.yml (indentare de 2 spații sub `services:`).

    Regex, nu PyYAML: nu adăugăm o dependență doar ca să citim 7 nume, iar CI-ul
    trebuie să ruleze fără instalări suplimentare.
    """
    lines = _read(REPO / "docker-compose.yml").splitlines()
    out: list[str] = []
    in_services = False
    for line in lines:
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and line and not line.startswith(" ") and not line.startswith("#"):
            break  # alt bloc de top-level (volumes:, networks:) → am ieșit din services
        m = _SERVICE_RE.match(line)
        if in_services and m:
            out.append(m.group(1))
    return sorted(out)


_ROUTE_RE = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.I
)


def extract_routes() -> list[str]:
    """Rutele HTTP declarate prin decoratori FastAPI, ca `METODĂ cale`.

    Calea e cea din decorator (fără prefixul routerului) — prefixele sunt
    documentate separat în doc, la nivelul procesului `webhook`.
    """
    out: set[str] = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        for method, route in _ROUTE_RE.findall(_read(path)):
            out.add(f"{method.upper()} {route}")
    return sorted(out)


def extract_flags() -> dict[str, bool]:
    """Flag-urile bool din Settings (src/config.py) + valoarea lor DEFAULT.

    Default-ul e jumătate din fapt: „există flag-ul X" nu spune nimic dacă nu
    știi dacă e pornit în producție. Un flag care trece din OFF în ON schimbă
    comportamentul fără să schimbe nicio linie de logică.
    """
    tree = ast.parse(_read(REPO / "src" / "config.py"))
    flags: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if not (isinstance(node.annotation, ast.Name) and node.annotation.id == "bool"):
            continue
        name = node.target.id
        default: bool | None = None
        val = node.value
        if isinstance(val, ast.Constant) and isinstance(val.value, bool):
            default = val.value
        elif isinstance(val, ast.Call):
            for kw in val.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, bool):
                        default = kw.value.value
        if default is not None:
            flags[name] = default
    return dict(sorted(flags.items()))


def extract_migrations() -> list[str]:
    """Migrările de pe disc, ordonate NUMERIC (010 > 009, nu lexicografic)."""
    files = [p.name for p in (REPO / "docs").glob("[0-9][0-9][0-9]_*.sql")]
    return sorted(files, key=lambda n: int(n.split("_", 1)[0]))


def extract_entrypoints() -> list[str]:
    """Modulele rulabile cu `python -m` (au `if __name__ == ...`)."""
    out: list[str] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        if re.search(r"^if\s+__name__\s*==", _read(path), re.M):
            out.append(path.relative_to(REPO).as_posix().removesuffix(".py").replace("/", "."))
    return out


def extract_models() -> dict[str, str]:
    """Modelele LLM configurate (`model_*` din Settings) + valorile default."""
    tree = ast.parse(_read(REPO / "src" / "config.py"))
    models: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if not name.startswith("model_"):
            continue
        val = node.value
        if isinstance(val, ast.Call):
            for kw in val.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    models[name] = str(kw.value.value)
    return dict(sorted(models.items()))


def collect_facts() -> dict[str, object]:
    return {
        "stages": extract_stages(),
        "tools": extract_tools(),
        "processes": extract_processes(),
        "routes": extract_routes(),
        "flags": extract_flags(),
        "migrations": extract_migrations(),
        "entrypoints": extract_entrypoints(),
        "models": extract_models(),
    }


# ---------------------------------------------------------------------------
# Comparația cu documentul
# ---------------------------------------------------------------------------

# Blocurile din doc arată așa (fence-ul poartă id-ul revendicării):
#     ```claim:stages
#     gates_stage
#     language_stage
#     ```
_CLAIM_RE = re.compile(r"^```claim:([a-z_]+)\s*$(.*?)^```\s*$", re.M | re.S)

# Revendicări în care ORDINEA e semnificativă (restul se compară ca mulțimi).
ORDERED = {"stages"}

# Revendicări de forma `nume = valoare` (flag-uri, modele).
KEYED = {"flags", "models"}


def parse_claims(doc_text: str) -> dict[str, list[str]]:
    claims: dict[str, list[str]] = {}
    for name, body in _CLAIM_RE.findall(doc_text):
        items = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
        claims[name] = items
    return claims


def _fact_as_lines(name: str, value: object) -> list[str]:
    if name in KEYED and isinstance(value, dict):
        return [f"{k} = {str(v).lower() if isinstance(v, bool) else v}" for k, v in value.items()]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"tip neașteptat pentru {name}")


def compare(facts: dict[str, object], claims: dict[str, list[str]]) -> list[str]:
    problems: list[str] = []
    for name, claimed in claims.items():
        if name not in facts:
            problems.append(
                f"[{name}] documentul revendică '{name}', dar extractorul nu cunoaște fapta "
                f"(revendicări valide: {', '.join(sorted(facts))})"
            )
            continue
        actual = _fact_as_lines(name, facts[name])
        if name in ORDERED:
            if claimed != actual:
                problems.append(
                    f"[{name}] ORDINEA diferă.\n"
                    f"    doc : {' → '.join(claimed)}\n"
                    f"    cod : {' → '.join(actual)}"
                )
            continue
        missing = sorted(set(actual) - set(claimed))  # în cod, absent din doc
        extra = sorted(set(claimed) - set(actual))  # în doc, absent din cod
        if missing:
            problems.append(f"[{name}] există în COD dar lipsește din doc: {', '.join(missing)}")
        if extra:
            problems.append(f"[{name}] documentat dar ABSENT din cod: {', '.join(extra)}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--emit", action="store_true", help="tipărește faptele extrase ca JSON")
    ap.add_argument(
        "--emit-claims", action="store_true", help="tipărește blocurile claim gata de lipit"
    )
    ap.add_argument("--doc", type=Path, default=DOC, help=f"documentul verificat (implicit: {DOC})")
    args = ap.parse_args()

    facts = collect_facts()

    if args.emit:
        print(json.dumps(facts, indent=2, ensure_ascii=False))
        return 0

    if args.emit_claims:
        for name in ("stages", "tools", "processes", "routes", "flags", "migrations"):
            print(f"```claim:{name}")
            for line in _fact_as_lines(name, facts[name]):
                print(line)
            print("```\n")
        return 0

    if not args.doc.exists():
        print(f"FAIL: documentul {args.doc} nu există", file=sys.stderr)
        return 1

    claims = parse_claims(_read(args.doc))
    if not claims:
        print(
            f"FAIL: {args.doc.name} nu conține niciun bloc ```claim:...```.\n"
            "      Poarta e inutilă fără revendicări — vezi --emit-claims.",
            file=sys.stderr,
        )
        return 1

    problems = compare(facts, claims)
    if problems:
        print(f"FAIL: {args.doc.name} a divergat de cod\n", file=sys.stderr)
        for p in problems:
            print(f"  • {p}", file=sys.stderr)
        print(
            "\nRepară documentul (nu codul) dacă schimbarea din cod e intenționată.\n"
            "`python scripts/verify_architecture_doc.py --emit-claims` tipărește blocurile bune.",
            file=sys.stderr,
        )
        return 1

    checked = ", ".join(f"{k}({len(v)})" for k, v in sorted(claims.items()))
    print(f"OK: {args.doc.name} e de acord cu codul — {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
