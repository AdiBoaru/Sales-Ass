"""NX-123 — fiecare docs/0NN_*.sql are headerul aliniat la prefixul din nume.

Pur (doar I/O de fișiere, fără DB) → rulează pe PR. Prinde coliziuni de numerotare /
headere greșite (ex. `011_*.sql` cu header «010 — FIX», confirmat live)."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
_NAME_RE = re.compile(r"^(\d+)_")
# primul număr de 3 cifre dintr-o linie de comentariu SQL: `-- 014 ...`, `-- === 014`, `--=014`.
_HEADER_RE = re.compile(r"^\s*--[\s=]*(\d{3})\b")


def _migration_files() -> list[Path]:
    return sorted(p for p in DOCS.glob("*.sql") if _NAME_RE.match(p.name))


def _header_prefix(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            return m.group(1)
    return None


def test_migration_files_exist():
    assert _migration_files(), "nicio migrare docs/0NN_*.sql găsită"


def test_header_prefix_matches_filename():
    """Prefixul numeric din numele fișierului == primul număr din headerul de comentariu.
    Fișierele fără un număr în header sunt sărite (nu pică) — verificăm doar discrepanțele."""
    bad = []
    for p in _migration_files():
        file_prefix = _NAME_RE.match(p.name).group(1)
        header_prefix = _header_prefix(p)
        if header_prefix is not None and header_prefix != file_prefix:
            bad.append(f"{p.name}: header {header_prefix} ≠ prefix fișier {file_prefix}")
    assert not bad, "headere nealiniate (NX-123):\n" + "\n".join(bad)


def test_no_duplicate_version_prefixes():
    """Două fișiere cu același prefix numeric = coliziune (PK schema_migrations ar pica)."""
    seen: dict[str, str] = {}
    dupes = []
    for p in _migration_files():
        prefix = _NAME_RE.match(p.name).group(1)
        if prefix in seen:
            dupes.append(f"{prefix}: {seen[prefix]} + {p.name}")
        seen[prefix] = p.name
    assert not dupes, "prefixe de migrare duplicate:\n" + "\n".join(dupes)
