"""NX-161 guard CI — `deps.conn` / `PipelineDeps(conn=` nu mai au voie în `src/` după migrare.

Enforcement mecanic al invariantului „schimbarea proprietății resursei"
(docs/CONN-HOLD-ANALYSIS-2026.md): odată ce toate stagiile/tool-urile folosesc `deps.db()`,
conexiunea vie lungă (`deps.conn`) trebuie să dispară din `src/`. Guard-ul e:

  • LENIENT în 0B→migrare (default): raportează câte referințe legacy rămân, exit 0 (WARN). Pe
    măsură ce migrezi felii, numărul scade — un progress bar mecanic.
  • HARD-FAIL la Felia 7 (`--strict`): orice `deps.conn` / `PipelineDeps(conn=` în `src/` → exit 1.

Testele (`tests/`) sunt EXCLUSE — au voie `PipelineDeps(conn=...)` (puntea de compat le mapează la
provider static). Rulat în CI lângă ruff. Rulează local: `python scripts/check_no_raw_conn.py`.

Detecție prin AST (nu regex pe linii): se numără DOAR cod real (acces `deps.conn` + apel
`PipelineDeps(conn=...)`) — mențiunile din comentarii/docstring-uri NU intră în contor, deci
`--strict` la Felia 7 nu dă fals-pozitive pe documentație (observație review Codex #206).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Liniile-sursă raportate pot conține diacritice → forțează UTF-8 (consola Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — pe Linux/CI stdout e deja UTF-8
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _match(node: ast.AST) -> str | None:
    """Cod REAL care ține conn viu: acces `deps.conn` sau apel `PipelineDeps(conn=...)`. Restul
    (comentarii/docstring-uri) nu sunt noduri Attribute/Call → ignorate automat."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "conn"
        and isinstance(node.value, ast.Name)
        and node.value.id == "deps"
    ):
        return "deps.conn"
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name == "PipelineDeps" and any(kw.arg == "conn" for kw in node.keywords):
            return "PipelineDeps(conn=...)"
    return None


def scan() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # fișier ne-parsabil → sărit (nu blochează guard-ul)
            continue
        for node in ast.walk(tree):
            what = _match(node)
            if what is not None:
                hits.append((rel, node.lineno, what))
    return hits


def main() -> int:
    strict = "--strict" in sys.argv
    hits = scan()
    if not hits:
        print("check_no_raw_conn: OK - zero deps.conn / PipelineDeps(conn= in src/.")
        return 0
    print(f"check_no_raw_conn: {len(hits)} referinte legacy la conn viu in src/ (migrare NX-161).")
    if strict:
        for path, lineno, text in hits:
            print(f"  {path}:{lineno}: {text}")
        print("FAIL (--strict): deps.conn interzis in src/ dupa Felia 7.")
        return 1
    print("WARN: lenient (0B->migrare) - hard-fail cu --strict la Felia 7.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
