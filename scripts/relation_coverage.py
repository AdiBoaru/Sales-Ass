"""NX-270 — ce e CHIAR în graf, nu ce ar produce construcția. Read-only.

Există separat de raportul lui `build_relations` dintr-un motiv care s-a mai plătit o dată în
proiect: raportul jobului spune ce a calculat jobul, iar asta e o afirmație despre COD. Cele 222 de
relații de substitut de la NX-171b existau în bază și nu erau citite de nimeni luni de zile, fiindcă
nimeni nu se uita la tabelă — „nu mai avem" era răspunsul final deși alternativa era acolo.

Scriptul ăsta se uită la tabelă. Verifică trei lucruri pe care un raport de construcție nu le poate
verifica:

* **muchiile au provenance** — `source`, `rule_id`, `reason` nevide. Fără ele, o regulă dovedită
  greșită nu se poate șterge global, iar afirmația muchiei nu se poate explica nimănui;
* **capetele sunt vii** — o muchie către un produs inactiv sau nepublicat e o promisiune moartă;
* **graful e citibil** — câte ancore au muchii de fiecare tip, deci ce ar vedea efectiv un client.

    python scripts/relation_coverage.py --business <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.catalog import COMPLEMENTARY_KINDS  # noqa: E402

_BY_KIND = """
select kind,
       count(*)                                        as edges,
       count(distinct product_id)                      as anchors,
       count(*) filter (where source is null
                           or rule_id is null
                           or reason is null)          as without_provenance,
       count(distinct source)                          as sources
  from product_relations
 where business_id = $1
 group by kind
 order by kind
"""

# O muchie către un produs pe care nu-l poți cumpăra e o promisiune moartă. Nu e o eroare de
# integritate (FK-ul e satisfăcut), deci nimic altceva n-o semnalează.
_DEAD_ENDS = """
select r.kind, count(*) as dead
  from product_relations r
  join products p on p.id = r.related_id and p.business_id = r.business_id
 where r.business_id = $1
   and (p.status <> 'active' or p.availability not in ('in_stock', 'low_stock'))
 group by r.kind
 order by r.kind
"""

_OUT_OF_STOCK = """
select count(*) filter (where true)                                as total,
       count(*) filter (where sub.n is not null and sub.n > 0)     as with_substitute
  from products p
  left join lateral (
      select count(*) as n from product_relations r
       where r.business_id = p.business_id and r.product_id = p.id and r.kind = 'substitute'
  ) sub on true
 where p.business_id = $1 and p.status = 'active'
   and p.availability not in ('in_stock', 'low_stock')
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        async with tenant_conn(args.business) as conn:
            # Coloanele de provenance vin cu migrarea 048. Un raport care CRAPĂ pe o migrare
            # neaplicată e mai rău decât unul care o numește: mesajul „column source does not
            # exist" nu spune nimănui ce să facă.
            has_provenance = await conn.fetchval(
                "select exists(select 1 from information_schema.columns "
                "where table_name = 'product_relations' and column_name = 'source')"
            )
            if not has_provenance:
                print(
                    "migrarea 048 nu e aplicată: `product_relations` n-are `source`/`rule_id`/"
                    "`reason`, deci muchiile n-ar putea purta provenance.\n"
                    "  python scripts/migrate.py            # aplică migrările în ordine",
                    file=sys.stderr,
                )
                return 2
            by_kind = [dict(r) for r in await conn.fetch(_BY_KIND, args.business)]
            dead = {r["kind"]: r["dead"] for r in await conn.fetch(_DEAD_ENDS, args.business)}
            oos = dict(await conn.fetchrow(_OUT_OF_STOCK, args.business) or {})
            total_edges = sum(r["edges"] for r in by_kind)

            if not total_edges:
                print(
                    "graful e GOL. Codul de traversare există și e complet, dar n-are combustibil "
                    "— rulează `python -m src.jobs.build_relations --business <uuid> --apply`."
                )
            print(f"{'kind':16}{'muchii':>9}{'ancore':>9}{'fără prov.':>12}{'capete moarte':>15}")
            for row in by_kind:
                print(
                    f"{row['kind']:16}{row['edges']:>9}{row['anchors']:>9}"
                    f"{row['without_provenance']:>12}{dead.get(row['kind'], 0):>15}"
                )
            if oos.get("total"):
                rate = (oos.get("with_substitute") or 0) / oos["total"]
                print(
                    f"\nepuizate: {oos['total']} · cu substitut în graf: "
                    f"{oos.get('with_substitute') or 0} ({rate:.1%})"
                )
            unread = {r["kind"] for r in by_kind} - set(COMPLEMENTARY_KINDS) - {"substitute"}
            if unread:
                # Exact greșeala pe care o previne testul de vocabular din trei locuri, prinsă aici
                # pe date reale: muchii scrise pe care niciun consumator nu le citește.
                print(f"\n! tipuri SCRISE dar necitite de niciun consumator: {sorted(unread)}")

            out = pathlib.Path(
                args.out or ROOT / "reports" / f"relation-coverage-{args.business[:8]}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "business_id": args.business,
                        "total_edges": total_edges,
                        "by_kind": by_kind,
                        "dead_ends": dead,
                        "out_of_stock": oos,
                        "written_but_unread_kinds": sorted(unread),
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print(f"\nraport: {out}")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
