"""NX-289 — nicio sursă nu are voie să numească un obiect pe care o migrare l-a ȘTERS.

DE CE EXISTĂ testul ăsta (bug real, prins la verificarea de după livrare):
`src/db/queries/proactive.py` a rămas cu `template_id` în lista de coloane a claim-ului ȘI în
`INSERT`-ul de job, deși migrarea 051 dropează coloana. Nimic din suită nu-l prindea: testele de
proactiv stub-uiesc `conn`, iar pe DB-ul de azi coloana ÎNCĂ există (migrarea nu e aplicată).
Prima manifestare ar fi fost în producție, DUPĂ migrare, la primul claim de job proactiv —
`UndefinedColumnError` pe un drum care rulează în background, deci un mesaj proactiv pierdut tăcut.

Clasa de bug e generală și nu se acoperă prin teste normale: o migrare care șterge ceva are efect
ÎN VIITOR, iar suita rulează pe schema de ACUM. Poarta asta închide fereastra.

DOUĂ DECIZII care o fac utilă în loc de zgomotoasă:

  1. **Dropat ≠ dispărut.** O migrare care face `drop … / create …` pe același nume (tiparul
     `create or replace`, sau reconstrucția coloanei `search_tsv` din 046/049) NU șterge nimic.
     Comparăm ULTIMA migrare care dropează cu ULTIMA care creează; obiectul e mort doar dacă
     dropul e mai nou.
  2. **Codul, nu proza.** Un docstring care EXPLICĂ ce s-a șters e exact ce vrem să scrie cineva
     (`src/proactive/templates.py` spune de ce a plecat `wa_templates`). Poarta se uită la
     identificatorii din cod și la string-urile NE-docstring — acolo trăiește SQL-ul. Bug-ul real
     a fost într-un astfel de string (`_JOB_COLS`, o listă de coloane fără niciun cuvânt SQL),
     deci o poartă care ar cere „literal cu SELECT în el" l-ar fi ratat.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = ROOT / "src"

_IDENT = r"([a-z_][a-z0-9_]*)"
_DROP_TABLE = re.compile(rf"\bdrop\s+table\s+(?:if\s+exists\s+)?{_IDENT}", re.I)
_DROP_FUNC = re.compile(rf"\bdrop\s+function\s+(?:if\s+exists\s+)?{_IDENT}\s*\(", re.I)
_DROP_COL = re.compile(
    rf"\balter\s+table\s+[a-z_][a-z0-9_.]*\s+drop\s+column\s+(?:if\s+exists\s+)?{_IDENT}", re.I
)
_MK_TABLE = re.compile(rf"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?{_IDENT}", re.I)
_MK_FUNC = re.compile(rf"\bcreate\s+(?:or\s+replace\s+)?function\s+{_IDENT}\s*\(", re.I)
_MK_COL = re.compile(
    rf"\balter\s+table\s+[a-z_][a-z0-9_.]*\s+add\s+column\s+(?:if\s+not\s+exists\s+)?{_IDENT}",
    re.I,
)

#: Identificatori prea generici ca potrivirea pe text să însemne ceva (`payload` există pe
#: `messages`, `outbox`, `analytics_events`, `web_turns`…). O intrare aici e o DECIZIE, cu motiv.
_TOO_GENERIC = {"payload", "status", "kind", "name", "body", "id", "data", "value"}


def _migration_number(path: Path) -> int:
    return int(path.name.split("_", 1)[0])


def _dead_identifiers() -> dict[str, str]:
    """`identificator → migrarea care l-a omorât`, pentru cele care NU sunt recreate ulterior."""
    dropped: dict[str, tuple[int, str]] = {}
    created: dict[str, int] = {}
    for path in sorted(DOCS.glob("[0-9][0-9][0-9]_*.sql"), key=_migration_number):
        n = _migration_number(path)
        sql = path.read_text(encoding="utf-8")
        # Antetele descriu adesea ce a EXISTAT (inclusiv secțiuni de ROLLBACK cu `drop`/`create`).
        # Ne interesează ce FACE migrarea, nu ce povestește.
        body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
        for rx in (_DROP_TABLE, _DROP_FUNC, _DROP_COL):
            for raw in rx.findall(body):
                dropped[raw.lower()] = (n, path.name)
        for rx in (_MK_TABLE, _MK_FUNC, _MK_COL):
            for raw in rx.findall(body):
                created[raw.lower()] = max(created.get(raw.lower(), 0), n)
    return {
        name: src
        for name, (n, src) in dropped.items()
        if name not in _TOO_GENERIC and created.get(name, -1) < n
    }


DEAD = _dead_identifiers()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()`-urile nodurilor Constant care sunt docstring-uri (modul/clasă/funcție)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = next(iter(node.body), None)
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def _names_used(path: Path) -> set[str]:
    """Identificatorii pe care fișierul chiar îi FOLOSEȘTE: simboluri de cod + literale de string
    care nu sunt docstring-uri (acolo stă SQL-ul și listele de coloane)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                used.update(re.findall(r"[a-z_][a-z0-9_]*", node.value.lower()))
    return used


def _source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_parserul_de_migrari_chiar_vede_dropuri():
    """Poarta e inutilă dacă regexurile nu găsesc nimic — o regresie tăcută de parser."""
    assert DEAD, "niciun obiect mort găsit în docs/0NN_*.sql — parserul e rupt"
    # 051 e migrarea care a motivat testul; dacă iese din radar, poarta nu mai apără nimic.
    assert "wa_templates" in DEAD and "051" in DEAD["wa_templates"]
    assert "message_status_events" in DEAD and "in_24h_window" in DEAD
    # …și dovada că regula „dropat apoi recreat = viu" chiar funcționează:
    assert "search_tsv" not in DEAD, "search_tsv e reconstruit de 046/049, nu șters"


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_sursa_nu_mai_numeste_obiecte_moarte(path: Path):
    offenders = sorted(f"{n} (†{DEAD[n]})" for n in _names_used(path) & DEAD.keys())
    assert not offenders, (
        f"{path.relative_to(ROOT).as_posix()} folosește obiecte pe care o migrare le șterge: "
        f"{'; '.join(offenders)}. Codul ar crăpa DUPĂ aplicarea migrării, nu acum — "
        f"de-asta suita nu-l prinde altfel."
    )
