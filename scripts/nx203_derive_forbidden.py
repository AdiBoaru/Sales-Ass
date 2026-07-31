"""NX-203 — deriva `forbidden_products` DIN CONSTRANGERI, nu din ce a gresit retrieval-ul.

De ce: construisem lista din fals-pozitivele OBSERVATE, deci doua query-uri echivalente primeau
liste diferite doar fiindca sistemul se comportase diferit. Gold-ul depindea de sistemul testat —
o circularitate care face benchmarkul sa masoare partial comportamentul curent, nu contractul
query-ului.

REGULA (politica inghetata dupa aplicarea retroactiva la loturile 1-3):
  · un produs intra in `forbidden` doar daca INCALCA EXPLICIT o constrangere hard;
  · atributul ABSENT ramane nejudecat — absenta nu demonstreaza incompatibilitatea;
  · setul se calculeaza contra catalogului `active` + `published`, deci e independent de retrieval;
  · rezultatele gresite observate raman semnal de AUDIT, nu sursa listei.

Ce genereaza forbidden, per tip de constrangere:
  category eq X   -> produsele dintr-o categorie DIFERITA (categoria lipsa -> nejudecat)
  price lte N     -> produsele cu pret > N
  spf gte N       -> produsele cu `spf` PREZENT si < N (absent -> nejudecat)
  finish eq X     -> produsele cu `finish` prezent si diferit
  coverage in [..]-> produsele cu `coverage` prezent si in afara listei
  fragrance_free eq true -> produsele cu `fragrance_free` = FALSE explicit
  suitable_for / concerns contains -> NIMIC. Lipsa unei valori nu e incompatibilitate; e exact
      greseala pentru care cele patru seruri fara `oily` au iesit din forbidden la lotul 1.
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

# Ratiuni pentru exceptiile care NU au reprezentare sigura in atributele catalogului. Fiecare e
# independenta de retrieval: descrie o proprietate a PRODUSULUI, nu o observatie despre ce a
# returnat sistemul.
RATIONALE: dict[str, str] = json.loads(
    (ROOT / "tests/golden/_forbidden_rationale.json").read_text(encoding="utf-8")
)["by_product_name"]

QRELS = ROOT / "tests" / "golden" / "qrels_confirmed.json"


from src.evals.retrieval.constraints import VIOLATES, evaluate  # noqa: E402


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from src.db.connection import close_pool, tenant_conn  # noqa: PLC0415

    async with tenant_conn(DEMO) as conn:
        rows = await conn.fetch(
            "select p.id::text as id, p.name, p.price, p.attributes, c.slug as category_slug "
            "from products p left join categories c on c.id = p.primary_category_id "
            "where p.business_id = $1::uuid and p.status = 'active' "
            "and p.content_status = 'published'",
            DEMO,
        )
    await close_pool()
    catalog = []
    for r in rows:
        d = dict(r)
        if isinstance(d["attributes"], str):
            d["attributes"] = json.loads(d["attributes"])
        catalog.append(d)
    names = {p["id"]: p["name"] for p in catalog}
    print(f"catalog active+published: {len(catalog)}\n")

    data = json.loads(QRELS.read_text(encoding="utf-8"))
    apply = "--apply" in sys.argv
    by_id = {p["id"]: p for p in catalog}
    tot_cov = tot_kept = 0
    for q in data["queries"]:
        # AUDIT, nu materializare: pentru fiecare intrare EXISTENTA, verificam daca o constrangere
        # explicita o reproduce. Cele acoperite ies din lista (sunt redundante — constrangerea le
        # deriva oricand); cele neacoperite raman ca EXCEPTII explicite, cu justificare.
        covered, kept = [], []
        for pid in q["forbidden_products"]:
            prod = by_id.get(pid)
            if prod and any(evaluate(prod, h) == VIOLATES for h in q["hard_constraints"]):
                covered.append(pid)
            else:
                kept.append(pid)
        tot_cov += len(covered)
        tot_kept += len(kept)
        print(
            f"{q['id']:11} {len(q['forbidden_products']):2d} -> acoperite de constrangeri: "
            f"{len(covered):2d} | ramase ca exceptii: {len(kept):2d}"
        )
        for pid in kept:
            print(f"      EXCEPTIE: {names.get(pid, pid)[:56]}")
        if apply:
            q["forbidden_products"] = kept
            # Ratiunea se salveaza IN DATE, langa exceptie — nu intr-un mesaj de commit. Altfel
            # nimeni nu mai poate distinge o exceptie justificata de un fals-pozitiv cules din
            # retrieval, adica exact confuzia pe care politica asta o elimina.
            q["forbidden_rationale"] = {
                pid: RATIONALE.get(names.get(pid, ""), "EXCEPTIE FARA RATIUNE — de completat")
                for pid in kept
            }
    if apply:
        QRELS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\napplied.")
    else:
        print("\n(dry-run — ruleaza cu --apply)")
    return 0


raise SystemExit(asyncio.run(main()))
