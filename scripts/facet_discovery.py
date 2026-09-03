"""Propune fațete DIN catalog, per rădăcină de categorie. Nu scrie nimic — propune.

Locul unde era, până la NX-264, un dicționar scris de mână în cod. Diferența nu e de stil: o listă
dictată descrie catalogul pe care l-a văzut autorul, iar o derivare descrie catalogul care există.

**Regula centrală e DISCRIMINAREA, nu frecvența.** O valoare adevărată la tot raftul nu ajută pe
nimeni să aleagă. Măsurat pe catalogul SOLE: derivarea de nevoi a dat categoriei `Buze` o acoperire
de 92%, cu valorile „hidratare" și „luminozitate" — corecte, și complet irelevante, fiindcă nimeni
nu-și alege rujul după hidratare. Acoperirea poate fi mare și fațeta complet greșită; discriminarea
e cifra care prinde asta.

Deci un token devine valoare candidată doar dacă, **în interiorul rădăcinii lui**:

* apare la cel puțin `MIN_SUPPORT_RATIO` din produse (sub prag e o particularitate, nu o fațetă);
* apare la cel mult `MAX_DOMINANCE_RATIO` (peste prag descrie raftul, nu produsul).

Al doilea test e cel care face procedura generală: pe `machiaj` trece „mat"/„satin", pe `ten` trece
„crema"/„ser", pe `protectie solara` trece indicele SPF. Nicăieri nu scrie în cod ce e o textură.

    python scripts/facet_discovery.py --business <uuid>

Read-only. Ieșirea (`tests/facet_discovery.json`) e verificată de `tests/test_facet_discovery.py`:
rădăcinile mari trebuie să primească seturi DIFERITE, altfel derivarea repetă, nu descoperă.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.query_terms import content_terms  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402

# O rădăcină sub atâtea produse nu se poate judeca: pe 15 produse, orice token pare semnificativ.
MIN_ROOT_PRODUCTS = 50

# Sub pragul de suport, tokenul e o particularitate a câtorva produse.
MIN_SUPPORT_RATIO = 0.03

# Peste pragul de dominanță, tokenul descrie RAFTUL, nu produsul. Ăsta e testul care separă o fațetă
# utilă de una adevărată și inutilă.
MAX_DOMINANCE_RATIO = 0.70

# A treia poartă, și e cea care curăță cel mai mult: un token care apare peste tot ÎN CATALOG e
# boilerplate din șablonul de nume, oricât de cuminte ar arăta în interiorul unei rădăcini. Măsurat:
# „formulat", „mentinerea", „hidratarea", „confortului" trec pragul de dominanță al fiecărei
# rădăcini în parte, fiindcă sunt sub 70% peste tot — dar sunt la peste un sfert din tot catalogul,
# deci nu deosebesc nimic de nimic.
MAX_GLOBAL_SHARE = 0.25

MIN_TOKEN_LEN = 4
MAX_VALUES_PER_ROOT = 25


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--out", default="tests/facet_discovery.json")
    args = ap.parse_args()

    try:
        async with tenant_conn(args.business) as conn:
            rows = await conn.fetch(
                """select p.name,
                          coalesce(split_part(cat.slug, '-', 1), '') as root,
                          coalesce(cat.name, '') as category
                     from products p
                     left join categories cat on cat.id = p.primary_category_id
                    where p.business_id = $1 and p.status = 'active'""",
                args.business,
            )
    finally:
        await close_pool()

    per_root: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    root_size: collections.Counter = collections.Counter()
    global_count: collections.Counter = collections.Counter()
    for row in rows:
        root = row["root"] or "(fără rădăcină)"
        root_size[root] += 1
        # Numele, nu proza: acolo e forma produsului, o dată, fără context de rutină.
        tokens = {t for t in content_terms(row["name"], args.locale) if len(t) >= MIN_TOKEN_LEN}
        per_root[root].update(tokens)
        global_count.update(tokens)

    total_products = len(rows)
    boilerplate = {
        term
        for term, count in global_count.items()
        if total_products and count / total_products > MAX_GLOBAL_SHARE
    }

    proposals: dict[str, dict] = {}
    for root, counter in per_root.items():
        n = root_size[root]
        if n < MIN_ROOT_PRODUCTS:
            continue
        low, high = max(2, int(n * MIN_SUPPORT_RATIO)), int(n * MAX_DOMINANCE_RATIO)
        values = [
            {"value": term, "products": count, "share": round(count / n, 4)}
            for term, count in counter.most_common()
            if low <= count <= high and not term.isdigit() and term not in boilerplate
        ][:MAX_VALUES_PER_ROOT]
        rejected_dominant = [t for t, c in counter.items() if c > high]
        rejected_boilerplate = sorted(t for t in counter if t in boilerplate)
        proposals[root] = {
            "products": n,
            "band_products": [low, high],
            "values": values,
            "rejected_as_dominant": sorted(rejected_dominant)[:15],
            "rejected_as_boilerplate": rejected_boilerplate[:15],
        }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "_provenance": {
                    "business_id": args.business,
                    "locale": args.locale,
                    "source": "products.name (status='active'), prin content_terms",
                    "min_root_products": MIN_ROOT_PRODUCTS,
                    "support_band_ratio": [MIN_SUPPORT_RATIO, MAX_DOMINANCE_RATIO],
                    "max_global_share": MAX_GLOBAL_SHARE,
                    "boilerplate_rejected": len(boilerplate),
                    "regenerate": (f"python scripts/facet_discovery.py --business {args.business}"),
                    "_note": (
                        "PROPUNERI, nu configurație. Se ratifică de un om, o dată per tenant, și "
                        "abia apoi intră în domain_pack. O valoare cu share > "
                        f"{MAX_DOMINANCE_RATIO:.0%} e respinsă: descrie raftul, nu produsul."
                    ),
                },
                "roots": dict(sorted(proposals.items())),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"rădăcini cu ≥{MIN_ROOT_PRODUCTS} produse: {len(proposals)}\n")
    for root, data in sorted(proposals.items(), key=lambda kv: -kv[1]["products"]):
        top = ", ".join(v["value"] for v in data["values"][:10])
        print(f"  {root:14} ({data['products']:>4} produse)  {top}")
    print(f"\nscris: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
