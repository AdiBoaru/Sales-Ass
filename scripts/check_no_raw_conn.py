"""NX-161 guard CI — `deps.conn` / `PipelineDeps(conn=` nu mai au voie în `src/` după migrare.

Enforcement mecanic al invariantului „schimbarea proprietății resursei"
(docs/CONN-HOLD-ANALYSIS-2026.md): odată ce toate stagiile/tool-urile folosesc `deps.db()`,
conexiunea vie lungă (`deps.conn`) trebuie să dispară din `src/`. Guard-ul e:

  • LENIENT în 0B→migrare (default): raportează câte referințe legacy rămân, exit 0 (WARN). Pe
    măsură ce migrezi felii, numărul scade — un progress bar mecanic.
  • HARD-FAIL la Felia 7 (`--strict`): orice `deps.conn` / `PipelineDeps(conn=` în `src/` → exit 1.

Testele (`tests/`) sunt EXCLUSE — au voie `PipelineDeps(conn=...)` (puntea de compat le mapează la
provider static). Rulat în CI lângă ruff. Rulează local: `python scripts/check_no_raw_conn.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Liniile-sursă raportate pot conține diacritice → forțează UTF-8 (consola Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — pe Linux/CI stdout e deja UTF-8
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# `PipelineDeps(conn=` = construcție legacy; `deps.conn` = citire de conn viu în stagiu/tool.
PATTERNS = [
    re.compile(r"PipelineDeps\(\s*conn="),
    re.compile(r"\bdeps\.conn\b"),
]


def scan() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pat.search(line) for pat in PATTERNS):
                hits.append((str(path.relative_to(ROOT)).replace("\\", "/"), lineno, line.strip()))
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
