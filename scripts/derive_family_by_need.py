"""Familia de rutină a fiecărei nevoi, derivată din catalog (NX-386, `kernel.v7.1`).

De ce există. O rutină cerută fără subiect («fă-mi o rutină de seară pentru pete») n-are tip și
n-are raft, deci `routine_family` n-avea din ce lua familia, iar planul `bundle` cădea pe calea
v1, unde modelul făcea o căutare simplă (setul wide-2026-10-07: 5 ture). Legătura nevoie → familie
e o dată a TENANTULUI (P9): o nevoie de piele e a feței pe un catalog de cosmetice și a nimic pe
unul de electronice. Scriptul o MĂSOARĂ: pentru fiecare valoare de nevoie (fațetele de nevoi ale
pachetului, `concerns` și `skin_type` pe SOLE), câte produse servabile o poartă în fiecare familie
(`attributes.routine_step`, „familie:pas", scris de `derive_routine_step.py`). Familia intră doar
cu o majoritate CLARĂ: cel puțin `MIN_SHARE` din produsele cu nevoia și pas, și cel puțin
`MIN_COUNT` produse. O nevoie împărțită între față și corp (hidratarea) nu primește familie: acolo
planul rămâne pe calea de azi, nu ghicește.

Citește DOAR (`SELECT`, tenant-scoped). Zero OpenAI. Nu scrie în DB: tipărește harta și, cu
`--write <fișier>`, o pune în `routine_steps.family_by_need` al pachetului din fișier (seed-ul), pe
care Adi îl aplică cu `scripts/set_domain_pack.py --apply --backup`.

    python scripts/derive_family_by_need.py --business <uuid>
    python scripts/derive_family_by_need.py --business <uuid> --write <pachet.json>
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import sys
from collections.abc import Iterable, Mapping

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

#: Partea minimă a produselor cu nevoia (și cu pas de rutină) care trebuie să stea într-o familie.
MIN_SHARE = 0.8
#: Sub atâtea produse, o majoritate e zgomot.
MIN_COUNT = 15
#: Fațetele pachetului care țin nevoi (cheile de stare ale nevoilor de fațetă).
NEED_FACETS = ("concerns", "skin_type")


def family_by_need(
    rows: Iterable[tuple[str, str, str]],
    families: Iterable[str],
    *,
    min_share: float = MIN_SHARE,
    min_count: int = MIN_COUNT,
) -> dict[str, str]:
    """`(dimensiune, valoare, familie)` per produs → `dimensiune:valoare` → familie. PUR.

    Doar familiile declarate în pachet contează; o nevoie fără majoritate clară lipsește."""
    known = set(families)
    counts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for dimension, value, family in rows:
        if family in known and dimension and value:
            counts[f"{dimension}:{value}"][family] += 1
    out: dict[str, str] = {}
    for need, by_family in sorted(counts.items()):
        total = sum(by_family.values())
        family, n = by_family.most_common(1)[0]
        if total >= min_count and n / total >= min_share:
            out[need] = family
    return out


_SQL = """
select f.key as dimension, v.value as value,
       split_part(p.attributes->>'routine_step', ':', 1) as family
from products p
cross join lateral unnest($2::text[]) as f(key)
cross join lateral jsonb_array_elements_text(
    case jsonb_typeof(p.attributes->f.key)
        when 'array' then p.attributes->f.key
        when 'string' then jsonb_build_array(p.attributes->f.key)
        else '[]'::jsonb
    end
) as v(value)
where p.business_id = $1
  and p.status = 'active'
  and p.attributes ? 'routine_step'
"""


def family_counts_by_need(
    rows: Iterable[tuple[str, str, str]], families: Iterable[str]
) -> dict[str, dict[str, int]]:
    """NX-389: `(dimensiune, valoare, familie)` per produs → `dimensiune:valoare` → familie →
    câte produse. PUR. Doar familiile declarate; ordinea e deterministă (cheie, apoi familie)."""
    known = set(families)
    counts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for dimension, value, family in rows:
        if family in known and dimension and value:
            counts[f"{dimension}:{value}"][family] += 1
    return {need: dict(sorted(by_family.items())) for need, by_family in sorted(counts.items())}


_TOTALS_SQL = """
select split_part(p.attributes->>'routine_step', ':', 1) as family, count(*)::int as n
from products p
where p.business_id = $1
  and p.status = 'active'
  and p.attributes ? 'routine_step'
group by 1
"""


async def _rows(
    business_id: str,
) -> tuple[list[tuple[str, str, str]], tuple[str, ...], dict[str, int]]:
    from src.db.connection import tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415

    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
        rows = await conn.fetch(_SQL, business_id, list(NEED_FACETS))
        totals = await conn.fetch(_TOTALS_SQL, business_id)
    pack = getattr(biz, "domain_pack", None)
    families = tuple((getattr(getattr(pack, "routine_steps", None), "families", None) or {}).keys())
    by_family = {str(r["family"]): int(r["n"]) for r in totals}
    return [(r["dimension"], r["value"], r["family"]) for r in rows], families, by_family


def _write(
    path: pathlib.Path,
    mapping: Mapping[str, str],
    counts: Mapping[str, Mapping[str, int]],
    totals: Mapping[str, int],
) -> None:
    pack = json.loads(path.read_text(encoding="utf-8"))
    steps = pack.setdefault("routine_steps", {})
    steps["family_by_need"] = dict(mapping)
    steps["family_counts_by_need"] = {k: dict(v) for k, v in counts.items()}
    steps["family_counts"] = dict(totals)
    steps["min_family_products"] = MIN_COUNT
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _main(args: argparse.Namespace) -> int:
    rows, families, totals = await _rows(args.business)
    if args.write:
        seed = json.loads(pathlib.Path(args.write).read_text(encoding="utf-8"))
        families = list(((seed.get("routine_steps") or {}).get("families") or {}).keys())
    mapping = family_by_need(rows, families)
    counts = family_counts_by_need(rows, families)
    totals = {f: n for f, n in sorted(totals.items()) if f in set(families)}
    print(json.dumps(mapping, ensure_ascii=False, indent=2))
    print(f"{len(mapping)} nevoi cu familie, din {len({(d, v) for d, v, _ in rows})} măsurate")
    print(f"produse cu pas de rutină pe familie: {totals}")
    if args.write:
        _write(pathlib.Path(args.write), mapping, counts, totals)
        print(
            f"scris în {args.write} (routine_steps.family_by_need, family_counts_by_need, "
            "family_counts, min_family_products); aplicarea în DB e separată"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--business", required=True, help="business_id (uuid)")
    parser.add_argument("--write", help="fișierul de pachet (seed) în care se scrie harta")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
