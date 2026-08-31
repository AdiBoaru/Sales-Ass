"""Verifică sintaxa fișierelor .sql cu PARSERUL Postgres, nu cu ochiul.

De ce: o migrare cu o greșeală de sintaxă se descoperă azi când o rulezi pe o bază reală — la
`scripts/migrate.py`, adică deja pe Supabase. Un parser o prinde în CI, în milisecunde, fără
conexiune. `pglast` împachetează exact `libpg_query`, adică parserul din Postgres, deci ce
acceptă el acceptă și serverul.

Ce PRINDE: sintaxă (identificatori neghilimelați cu spații, paranteze, clauze inversate,
`generated ... as identity` pe un tip care nu suportă).
Ce NU prinde, și nici nu pretinde: existența tabelelor, tipurile, ordinea dependențelor,
coerența FK-urilor. Alea le prinde doar aplicarea pe o bază reală.

Rulare:
    python scripts/check_sql_syntax.py docs/*.sql
    python scripts/check_sql_syntax.py --quiet docs/schema_v3_delta.sql
"""

from __future__ import annotations

import argparse
import glob
import sys

try:
    import pglast
    from pglast.parser import ParseError
except ImportError:
    sys.exit("lipseste pglast: python -m pip install pglast")


def line_of(sql: str, offset: int) -> int:
    return sql.count("\n", 0, max(0, offset)) + 1


SKIP_MARKER = "-- sqlcheck: skip"


def check(path: str, quiet: bool) -> tuple[int, int]:
    """Întoarce (statements, erori)."""
    with open(path, encoding="utf-8") as fh:
        sql = fh.read()

    # Opt-out EXPLICIT, cu motiv scris în fișier. Deliberat nu auto-detectăm variabile psql
    # (`:'nume'`): o detecție euristică ar înghiți tăcut și erorile reale care conțin `:`.
    if SKIP_MARKER in sql:
        reason = sql.split(SKIP_MARKER, 1)[1].split("\n", 1)[0].strip(" —-:")
        if not quiet:
            print(f"SKIP  {path}  ({reason or 'fara motiv declarat'})")
        return (0, 0)

    try:
        stmts = pglast.parse_sql(sql)
    except ParseError as exc:
        loc = getattr(exc, "location", None)
        where = f":{line_of(sql, loc)}" if isinstance(loc, int) else ""
        print(f"FAIL  {path}{where}\n      {exc}", file=sys.stderr)
        return (0, 1)

    if not quiet:
        kinds: dict[str, int] = {}
        for s in stmts:
            name = type(s.stmt).__name__.replace("Stmt", "")
            kinds[name] = kinds.get(name, 0) + 1
        top = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])[:8])
        print(f"OK    {path}  ({len(stmts)} statements: {top})")
    return (len(stmts), 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="fisiere .sql sau globuri")
    ap.add_argument("--quiet", action="store_true", help="afiseaza doar esecurile")
    args = ap.parse_args()

    files: list[str] = []
    for p in args.paths:
        hits = sorted(glob.glob(p))
        if not hits:
            print(f"fara potrivire: {p}", file=sys.stderr)
            return 2
        files.extend(hits)

    total, failed = 0, 0
    for f in files:
        n, err = check(f, args.quiet)
        total += n
        failed += err

    if failed:
        print(f"\n{failed} fisier(e) cu erori de sintaxa din {len(files)}", file=sys.stderr)
        return 1
    print(f"\n{len(files)} fisiere, {total} statements, zero erori de sintaxa")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
