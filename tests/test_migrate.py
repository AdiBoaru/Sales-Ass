"""NX-123 — teste pure pentru runner-ul de migrări (scripts/migrate.py).

Fără DB: verifică DESCOPERIREA + sortarea NUMERICĂ (010 > 009, nu lexicografic) și
calculul de checksum. Logica de aplicare/tracking pe DB e acoperită de
`test_grants_smoke.py` (integration) + rularea reală în CI."""

import hashlib

from scripts.migrate import discover_migrations


def _write(tmp_path, name, content="-- noop\nselect 1;\n"):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_discover_sorts_numerically_not_lexicographically(tmp_path):
    # ordine de scriere intenționat amestecată; 010 TREBUIE după 009, nu după 001.
    for n in ["010_z.sql", "003_a.sql", "009_b.sql", "014_c.sql", "001_x.sql"]:
        _write(tmp_path, n)
    versions = [m.version for m in discover_migrations(tmp_path)]
    assert versions == ["001", "003", "009", "010", "014"]


def test_discover_ignores_non_migration_files(tmp_path):
    _write(tmp_path, "003_real.sql")
    _write(tmp_path, "schema_v2_production.sql")  # fără prefix numeric → ignorat
    _write(tmp_path, "notes.sql")  # fără prefix → ignorat
    versions = [m.version for m in discover_migrations(tmp_path)]
    assert versions == ["003"]


def test_checksum_is_sha256_of_content(tmp_path):
    body = "-- mig\nalter table t add column c int;\n"
    _write(tmp_path, "007_x.sql", body)
    [m] = discover_migrations(tmp_path)
    assert m.checksum == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert m.filename == "007_x.sql"


def test_checksum_changes_when_file_edited(tmp_path):
    p = _write(tmp_path, "008_x.sql", "select 1;\n")
    c1 = discover_migrations(tmp_path)[0].checksum
    p.write_text("select 2;\n", encoding="utf-8")  # editat după „aplicare"
    c2 = discover_migrations(tmp_path)[0].checksum
    assert c1 != c2  # drift detectabil
