"""NX-203 — audit de RECALL pe qrels-urile cu constrangere de buget.

De ce exista. Pana la fixul pretului efectiv, filtrul de candidati folosea `p.price`. Un produs de
100 lei vandut cu 60 nu aparea deloc printre candidatii pentru „sub 90", deci n-a fost niciodata
pus in fata omului care eticheta. Rezultatul e o gaura in GOLD, nu in sistem — si e invizibila
pentru benchmark, fiindca benchmarkul masoara CONTRA gold-ului: un retrieval care gaseste corect
produsul redus ar fi penalizat ca fals-pozitiv.

`nx203_derive_forbidden.py` NU acopera asta. El verifica interdictiile (ce n-are voie sa apara),
nu judecatile (ce ar fi trebuit sa apara). Faptul ca n-a schimbat nimic dupa fix spune doar ca
nicio interdictie nu era gresita.

Ce face. Pentru fiecare query cu prag de pret, reconstruieste pool-ul canonic sub AMBELE reguli si
raporteaza DELTA: produse care incalcau pragul pe pretul de LISTA, dar il satisfac pe cel EFECTIV,
fara sa incalce vreo alta constrangere hard a query-ului. Alea sunt exact intrarile care n-au avut
niciodata sansa sa fie etichetate.

Ce NU face. Nu scrie in qrels si nu marcheaza nimic `human_verified`. Scoate un fisier de
etichetare; decizia relevant/irelevant e a omului. Un produs nou-eligibil nu e automat relevant —
poate fi eligibil si prost.

    PYTHONPATH=. python scripts/nx203_audit_budget_recall.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"
QRELS = ROOT / "tests" / "golden" / "qrels_confirmed.json"
OUT = ROOT / "tests" / "golden" / "_budget_recall_gap.json"

from src.db.queries.catalog import _VARIANT_SALE_ON  # noqa: E402
from src.evals.retrieval.catalog import load_catalog  # noqa: E402
from src.evals.retrieval.constraints import SATISFIES, VIOLATES, evaluate  # noqa: E402

# Snapshotul VECHI, reconstruit deliberat gresit: `p.price` in loc de pretul efectiv. Singurul mod
# de a masura ce s-a pierdut e sa reproduci regula care l-a pierdut.
_SQL_LIST_PRICE = """
select p.id::text as id, p.name, p.price::float8 as price, p.attributes,
       c.slug as category_slug
from products p
left join categories c on c.id = p.primary_category_id
where p.business_id = $1::uuid and p.status = 'active' and p.content_status = 'published'
"""


def _pool(catalog: dict, query: dict) -> set[str]:
    """Produsele care SATISFAC pragul de pret si nu incalca nicio alta constrangere hard.

    `satisfies`, nu `not violates`, pe pret: un produs cu pretul necunoscut n-ar fi fost propus
    nici inainte, nici acum, deci nu face parte din delta."""
    pret = next(h for h in query["hard_constraints"] if h["facet"] == "price")
    altele = [h for h in query["hard_constraints"] if h["facet"] != "price"]
    out = set()
    for pid, prod in catalog.items():
        if evaluate(prod, pret) != SATISFIES:
            continue
        if any(evaluate(prod, h) == VIOLATES for h in altele):
            continue
        out.add(pid)
    return out


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from src.db.connection import close_pool, tenant_conn  # noqa: PLC0415

    async with tenant_conn(DEMO) as conn:
        nou = await load_catalog(conn, DEMO)
        rows = await conn.fetch(_SQL_LIST_PRICE, DEMO)
        # cate produse au pretul efectiv sub cel de lista — masura bruta a expunerii
        reduse = await conn.fetchval(
            "select count(*) from products p left join lateral ("
            f"  select min(case when {_VARIANT_SALE_ON} then v.sale_price else v.price end)"
            "   as price from product_variants v where v.product_id = p.id"
            "  and v.business_id = p.business_id) vp on true "
            "where p.business_id = $1::uuid and p.status='active' and p.content_status='published' "
            "and coalesce(vp.price, case when p.sale_price is not null and p.sale_price < p.price "
            "  and (p.sale_start is null or p.sale_start <= current_date) "
            "  and (p.sale_end is null or p.sale_end >= current_date) "
            "  then p.sale_price else p.price end) < p.price",
            DEMO,
        )
    await close_pool()

    vechi = {}
    for r in rows:
        d = dict(r)
        attrs = d["attributes"]
        vechi[d["id"]] = {
            "name": d["name"],
            "price": d["price"],
            "category_slug": d["category_slug"],
            "attributes": json.loads(attrs) if isinstance(attrs, str) else (attrs or {}),
        }

    print(f"catalog: {len(nou.products)} produse — {nou.fingerprint}")
    print(f"produse cu pret efectiv sub cel de lista: {reduse}\n")

    data = json.loads(QRELS.read_text(encoding="utf-8"))
    cu_buget = [
        q for q in data["queries"] if any(h["facet"] == "price" for h in q["hard_constraints"])
    ]
    print(f"qrels cu constrangere de buget: {len(cu_buget)} din {len(data['queries'])}\n")

    de_etichetat, total = [], 0
    for q in cu_buget:
        pool_vechi = _pool(vechi, q)
        pool_nou = _pool(nou.products, q)
        noi = sorted(pool_nou - pool_vechi)
        pierdute = sorted(pool_vechi - pool_nou)  # ar fi un bug de directie, nu o gaura de recall
        judecate = {j["product_id"] for j in q["judgments"]}
        neetichetate = [pid for pid in noi if pid not in judecate]
        total += len(neetichetate)
        prag = next(h for h in q["hard_constraints"] if h["facet"] == "price")["value"]
        # Pierderi = produse eligibile sub pretul de lista, dar nu sub cel efectiv. Imposibil daca
        # efectivul e mereu <= lista; daca apar, e un bug de directie in proiectie, nu o gaura.
        alarma = f" | BUG pierdute: {len(pierdute)}" if pierdute else ""
        print(
            f"{q['id']:12} prag<={prag:<4} pool {len(pool_vechi):3} -> {len(pool_nou):3} | "
            f"nou-eligibile {len(noi):2} | neetichetate {len(neetichetate):2}{alarma}"
        )
        for pid in neetichetate:
            p = nou.products[pid]
            print(f"      {p['name'][:46]:48} lista={p['list_price']} efectiv={p['price']}")
        if neetichetate:
            de_etichetat.append(
                {
                    "query_id": q["id"],
                    "query": q["query"],
                    "family_id": q.get("family_id"),
                    "split_group_id": q.get("split_group_id"),
                    "price_max": prag,
                    "candidates": [
                        {
                            "product_id": pid,
                            "name": nou.products[pid]["name"],
                            "list_price": nou.products[pid]["list_price"],
                            "effective_price": nou.products[pid]["price"],
                            "relevance": None,  # de completat de om: 0-3
                        }
                        for pid in neetichetate
                    ],
                }
            )

    print(f"\ntotal candidati nou-eligibili, neetichetati: {total}")
    if de_etichetat:
        OUT.write_text(
            json.dumps(
                {
                    "_meta": {
                        "de_ce": "produse invizibile pentru vechiul filtru pe `p.price`; "
                        "relevance=null => NEETICHETAT, decizia e a omului",
                        "catalog_fingerprint": nou.fingerprint,
                        "nu_scrie_in_qrels": True,
                    },
                    "queries": de_etichetat,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"scris pentru etichetare: {OUT.relative_to(ROOT)}")
    else:
        print("nicio gaura de recall pe qrels-urile existente.")
    return 0


raise SystemExit(asyncio.run(main()))
