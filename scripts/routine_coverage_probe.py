"""Câte rutine COMPLETE poate compune catalogul de azi, și unde rămân găuri (NX-292, felia 1).

De ce un script și nu un test: un test spune „compunerea e implementată", sonda spune „ce se poate
compune AZI, pe datele astea". A doua e singura care poate contrazice o promisiune de produs cu o
cifră — iar aici promisiunea e scumpă. O rutină e singura clasă de cerere care pornește din start
cu un coș de 3-5 produse, deci e și cea mai profitabilă, și cea pe care sistemul o poate rata cel
mai încrezător: produsele sunt reale și prețurile sunt reale, deci validatorul (stagiul 8) și
`grounding_guard` (NX-240) o lasă să treacă oricum ar arăta.

Citește DOAR (`SELECT`, tenant-scoped). Zero OpenAI, zero scriere, zero DDL.

    python scripts/routine_coverage_probe.py --business <uuid>
    python scripts/routine_coverage_probe.py --business <uuid> --json reports/routine/coverage.json

## Ce măsoară, și de ce fiecare cifră

  • **inventar per pas** — produse cu pasul ăsta, din care VANDABILE. Denominator explicit: un pas
    cu 40 de produse din care 0 în stoc e un pas gol, nu un pas bogat.
  • **acoperire per familie** — sub profilul de bază și sub fiecare nevoie frecventă. Cifra care
    decide ce promitem în copy: dacă `fata` dă 6 din 6 dar `corp` dă 2 din 3, asta schimbă textul,
    nu arhitectura.
  • **prima gaură** — pasul care cade PRIMUL când se strâng constrângerile. Un raport care spune
    doar „4 din 6" trimite pe cineva să caute la întâmplare.
  • **headroom de swap** — câți candidați STRICT mai ieftini are fiecare slot ocupat, pe același
    pas. Asta e cifra care spune dacă butonul „ceva mai ieftin" are ce face. Un slot cu zero
    alternative mai ieftine transformă butonul în teatru, iar teatrul e mai rău decât absența:
    clientul apasă și nu se întâmplă nimic.
  • **cea mai ieftină rutină completă** — suma pick-urilor minime per pas. Răspunde direct la
    „rutină completă sub 200 lei", care e una dintre cele 13 fraze reale de client din NX-280.

Ordinea candidaților e cea DETERMINISTĂ din `build_relations._rank` (rating shrunk, apoi id),
importată ca să nu existe două formule care diverg. Nu e ranking-ul real al căutării (acela are
nevoie de o interogare); pentru o măsurătoare de ACOPERIRE nu contează care candidat intră în slot,
contează dacă există vreunul — dar contează ca a doua rulare să aleagă același.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import statistics
import sys
from decimal import Decimal
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.routine_compose import compose  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.domain.routine_steps import SEP, RoutineStepConfigError, build_spec  # noqa: E402
from src.jobs.build_relations import _rank, _sellable  # noqa: E402

SOLE_BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"

#: Câte nevoi frecvente primesc profil propriu. Nu e o preferință de raport: sub constrângere e
#: singurul loc unde acoperirea spune ceva despre PRODUS. Fără nevoie, „rutină pentru ten uscat"
#: și „rutină" ar da aceeași cifră, iar cifra n-ar măsura nimic.
TOP_CONCERNS = 5

_PACK_SQL = "select settings -> 'domain_pack' -> 'routine_steps' from businesses where id = $1"

# `attributes ? 'routine_step'` — doar produsele care POT ocupa un pas. Restul catalogului nu e
# relevant aici și l-ar dilua: raportul ar spune „26% din catalog n-are pas", ceea ce e adevărat
# și inutil (NX-280 a măsurat deja gaura, e cea a lui `product_type`).
_PRODUCTS_SQL = """
    select id::text as id,
           name,
           coalesce(sale_price, price) as price,
           availability,
           rating,
           review_count,
           attributes->>'routine_step' as routine_step,
           attributes->'concerns' as concerns
      from products
     where business_id = $1
       and status = 'active'
       and attributes ? 'routine_step'
     order by id
"""


def _concerns(row: dict[str, Any]) -> set[str]:
    raw = row.get("concerns")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return set()
    return {str(v) for v in raw} if isinstance(raw, list) else set()


def _price(row: dict[str, Any]) -> Decimal | None:
    value = row.get("price")
    return Decimal(str(value)) if value is not None else None


def _pools(rows: list[dict[str, Any]], spec: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """familie → pas → produse, în ordinea deterministă. Valorile necunoscute pachetului se IGNORĂ
    (un `routine_step` scris de o versiune veche a hărții n-are slot unde să meargă)."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        fam: {step: [] for step in steps} for fam, steps in spec.families.items()
    }
    for row in rows:
        family, _, step = str(row["routine_step"]).partition(SEP)
        bucket = out.get(family, {}).get(step)
        if bucket is not None:
            bucket.append(row)
    for steps in out.values():
        for bucket in steps.values():
            bucket.sort(key=_rank)
    return out


def _candidates_and_reasons(
    pool: dict[str, list[dict[str, Any]]], concern: str | None
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Filtrează pool-ul și RAPORTEAZĂ de ce s-a golit un pas.

    Cele trei motive sunt distincte pe bună dreptate: „pasul nu există în catalog", „există dar e
    epuizat" și „există în stoc dar nu pentru nevoia asta" cer trei reparații diferite. Un singur
    `no_candidate` le-ar amesteca și ar trimite pe cineva să caute în locul greșit.
    """
    candidates: dict[str, list[str]] = {}
    reasons: dict[str, str] = {}
    for step, products in pool.items():
        sellable = [p for p in products if _sellable(p)]
        kept = [p for p in sellable if not concern or concern in _concerns(p)]
        candidates[step] = [p["id"] for p in kept]
        if kept:
            continue
        if not products:
            reasons[step] = "no_candidate"
        elif not sellable:
            reasons[step] = "out_of_stock"
        else:
            reasons[step] = "filtered"
    return candidates, reasons


def _swap_headroom(
    plan: Any, pool: dict[str, list[dict[str, Any]]], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Câți candidați STRICT mai ieftini are fiecare slot ocupat, pe ACELAȘI pas.

    Definiția e cea din designul acțiunii `routine_swap_step`: același pas, preț strict mai mic,
    vandabil. Dacă cifra e 0 pe multe sloturi, butonul nu trebuie emis pe ele — un buton care nu
    poate face nimic e mai rău decât unul absent."""
    per_slot: list[dict[str, Any]] = []
    for slot in plan.covered_slots:
        current = _price(by_id[slot.product_id])
        cheaper = [
            p
            for p in pool[slot.step]
            if _sellable(p)
            and p["id"] != slot.product_id
            and (price := _price(p)) is not None
            and current is not None
            and price < current
        ]
        per_slot.append({"step": slot.step, "cheaper": len(cheaper)})
    counts = [s["cheaper"] for s in per_slot]
    return {
        "per_slot": per_slot,
        "slots_without_cheaper": sum(1 for c in counts if c == 0),
        "median_cheaper": statistics.median(counts) if counts else 0,
    }


def _cheapest_complete(pool: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Suma celor mai ieftini candidați vandabili, pas cu pas. `None` dacă vreun pas e gol: o sumă
    peste o rutină incompletă ar fi un preț pentru ceva ce nu se poate cumpăra."""
    total = Decimal(0)
    for products in pool.values():
        prices = [pr for p in products if _sellable(p) and (pr := _price(p)) is not None]
        if not prices:
            return {"total": None, "complete": False}
        total += min(prices)
    return {"total": float(total), "complete": True}


async def probe(business_id: str) -> dict[str, Any]:
    pool_conn = await get_pool()
    async with admin_conn(pool_conn) as conn:
        raw = await conn.fetchval(_PACK_SQL, business_id)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not raw:
        raise SystemExit(
            f"business {business_id} n-are `domain_pack.routine_steps` "
            "— vezi scripts/set_domain_pack.py"
        )
    try:
        spec = build_spec(raw)
    except RoutineStepConfigError as e:
        raise SystemExit(f"pachet invalid, sonda refuză să măsoare pe o hartă stricată: {e}") from e

    async with tenant_conn(business_id) as conn:
        rows = [dict(r) for r in await conn.fetch(_PRODUCTS_SQL, business_id)]

    by_id = {r["id"]: r for r in rows}
    pools = _pools(rows, spec)

    frequency = collections.Counter(c for r in rows if _sellable(r) for c in _concerns(r))
    concerns = [key for key, _ in frequency.most_common(TOP_CONCERNS)]

    families: dict[str, Any] = {}
    for family, pool in pools.items():
        inventory = {
            step: {
                "total": len(products),
                "sellable": sum(1 for p in products if _sellable(p)),
            }
            for step, products in pool.items()
        }

        profiles: dict[str, Any] = {}
        for label, concern in [("base", None), *((f"concern:{c}", c) for c in concerns)]:
            candidates, reasons = _candidates_and_reasons(pool, concern)
            plan = compose(family, spec, candidates=candidates, reasons=reasons)
            gaps = plan.uncovered_slots
            profiles[label] = {
                "covered": len(plan.covered_slots),
                "steps": len(plan.slots),
                "complete": plan.is_complete,
                "is_routine": plan.is_routine,
                # Prima gaură, nu toate: ea e cea care cade întâi când se strâng constrângerile.
                "first_gap": (
                    {"step": gaps[0].step, "reason": gaps[0].uncovered_reason} if gaps else None
                ),
                "gaps": [{"step": g.step, "reason": g.uncovered_reason} for g in gaps],
            }
            if label == "base":
                profiles[label]["swap"] = _swap_headroom(plan, pool, by_id)

        families[family] = {
            "steps": list(spec.families[family]),
            "inventory": inventory,
            "cheapest_complete": _cheapest_complete(pool),
            "profiles": profiles,
        }

    return {
        "business_id": business_id,
        "products_with_step": len(rows),
        "sellable_with_step": sum(1 for r in rows if _sellable(r)),
        "top_concerns": [{"key": k, "n": n} for k, n in frequency.most_common(TOP_CONCERNS)],
        "families": families,
    }


def render(report: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    add(f"business: {report['business_id']}")
    add(
        f"produse cu pas: {report['products_with_step']}   "
        f"vandabile: {report['sellable_with_step']}"
    )
    add("")

    for family, data in report["families"].items():
        add(f"── {family} ({len(data['steps'])} pași) " + "─" * 40)
        add("")
        add(f"  {'pas':<14}{'total':>8}{'vandabile':>12}")
        for step in data["steps"]:
            inv = data["inventory"][step]
            add(f"  {step:<14}{inv['total']:>8}{inv['sellable']:>12}")
        add("")

        cheapest = data["cheapest_complete"]
        add(
            "  cea mai ieftină rutină completă: "
            + (f"{cheapest['total']:.2f} lei" if cheapest["complete"] else "IMPOSIBILĂ (pas gol)")
        )
        add("")
        add(f"  {'profil':<28}{'acoperit':>10}{'rutină?':>10}   prima gaură")
        for label, prof in data["profiles"].items():
            gap = prof["first_gap"]
            gap_text = f"{gap['step']} ({gap['reason']})" if gap else "-"
            add(
                f"  {label:<28}{prof['covered']}/{prof['steps']:<8}"
                f"{('da' if prof['is_routine'] else 'NU'):>10}   {gap_text}"
            )
        swap = data["profiles"]["base"]["swap"]
        add("")
        add(
            f"  headroom de swap (profil base): mediana {swap['median_cheaper']} alternative mai "
            f"ieftine/slot, {swap['slots_without_cheaper']} slot(uri) fără niciuna"
        )
        add("")

    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--business", default=SOLE_BIZ, help="business_id (implicit: SOLE)")
    ap.add_argument("--json", type=pathlib.Path, default=None, help="scrie raportul JSON aici")
    args = ap.parse_args()

    try:
        report = await probe(args.business)
    finally:
        await close_pool()

    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"raport JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
