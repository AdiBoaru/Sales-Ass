"""Proba de FORMĂ a grafului `product_relations` — decide dacă traversarea are ce traversa.

De ce un script și nu un test: un test spune „traversarea e implementată", proba spune „ce se
poate parcurge AZI, pe datele astea". Al doilea e singurul care poate contrazice o presupunere de
produs cu o cifră, iar aici presupunerea costă scump: un lanț de 4 pași greșit e mai dăunător
decât o recomandare greșită, fiindcă arată ca expertiză.

**Nicio dimensiune de vocabular nu e numită în cod** (aceeași regulă ca
`src/catalog/vocabulary.py`).
Tipurile de muchie se DESCOPERĂ din datele tenantului, nu se enumeră aici: un tenant de beauty va
produce `routine_next`, unul de electrocasnice `requires`/`compatible_with`. Un script care ar
întreba „câte lanțuri `routine_next` există" ar fi el însuși cuplat la un vertical, iar atunci
instrumentul de măsură ar minți înaintea codului măsurat.

Citește DOAR (`SELECT`, tenant-scoped, prin `tenant_conn`). Zero OpenAI, zero scriere. Rulare:

    python scripts/relations_graph_probe.py
    python scripts/relations_graph_probe.py --business <uuid> --max-depth 6
    python scripts/relations_graph_probe.py --json reports/relations/graph_probe.json

Ce măsoară, per tip de muchie:

  • **inventar** — muchii, ancore distincte, ținte distincte;
  • **grad de ieșire** — mediana/p90/max al numărului de vecini direcți ai unei ancore;
  • **acoperire** — ce fracție din produsele ACTIVE are măcar o muchie de ieșire (denominator
    explicit, nu totalul optimist);
  • **lanțuri** — adâncimea REALĂ atinsă de un `WITH RECURSIVE ... CYCLE`, și câte ancore au lanț
    de lungime ≥ 2 și ≥ 3. Asta e cifra care decide totul: dacă toate lanțurile au lungime 1,
    traversarea nu adaugă nimic peste `get_complementary_products` de azi, iar problema e de
    CATALOG (NX-203), nu de cod;
  • **cicluri** — schema interzice self-relation (`check (product_id <> related_id)`) dar NU
    interzice A→B→C→A. Un ciclu nu e o curiozitate: e diferența dintre un query mărginit și unul
    care mănâncă deadline-ul de tur (NX-241);
  • **muchii moarte** — relații care țintesc produse inactive sau epuizate, deci care nu pot
    produce niciodată un rezultat vizibil. Ecoul lui „60 din 102 categorii n-aveau niciun produs";
  • **reciprocitate** — câte muchii A→B au și B→A. NU presupunem că vreun tip e simetric:
    `substitute` de obicei e, `requires` nu e niciodată. Cifra se MĂSOARĂ aici, ca specul de
    `relation_kinds` din DomainPack să fie scris pe dovezi, nu pe intuiție.

Fiecare tip primește o CLASIFICARE proprie (`chain` / `bounded` / `neighbors-only`) plus adâncimea
sugerată — exact specul de care are nevoie `relation_kinds` din DomainPack, derivat din dovezi, nu
din intuiție. Un verdict global unic ar fi ascuns adevărul acționabil: pe datele reale, `complement`
e ciclic prin NATURA relației (simetrică) în timp ce `routine_next` e o secvență curată, iar un
verdict care le amestecă ar fi cerut „reparații" acolo unde nu e nimic de reparat.

Verdictul global rămâne tri-state, în disciplina NX-238/NX-246: `NOT-READY` („niciun tip nu suportă
traversare") ≠ `PARTIAL` („doar mărginit") ≠ `READY`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # diacritice pe consola Windows

from src.db.connection import close_pool, tenant_conn  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Sub acest număr de ancore cu lanț real, tipul e clasificat `neighbors-only`, nu „gata". Câteva
# lanțuri izolate sunt o anecdotă, nu o capabilitate pe care poți construi un card. Prag ABSOLUT,
# nu procent din catalog: catalogul crește (150 → 300 între NX-177 și azi), iar un procent ar muta
# pragul sub picioarele măsurătorii de la o rulare la alta.
MIN_ANCHORS_WITH_CHAIN = 5

# Plafonul implicit de adâncime al probei. Deliberat MAI MARE decât cel pe care l-ar folosi
# producția (4): proba trebuie să poată răspunde „lanțurile sunt mai lungi decât plafonul propus?".
DEFAULT_MAX_DEPTH = 6


# --- SQL (tot tenant-scoped; `business_id = $1` în FIECARE interogare, inclusiv în pasul
# --- recursiv, altfel indexul `product_relations_anchor_idx` nu servește recursia) --------------

_KINDS_SQL = """
    select r.kind,
           count(*)                          as edges,
           count(distinct r.product_id)      as anchors,
           count(distinct r.related_id)      as targets
      from product_relations r
     where r.business_id = $1
     group by r.kind
     order by count(*) desc
"""

_DEGREE_SQL = """
    select r.kind, r.product_id::text as anchor, count(*) as degree
      from product_relations r
     where r.business_id = $1
     group by r.kind, r.product_id
"""

# Muchie „moartă": ținta există (FK-ul compus o garantează) dar nu poate ajunge niciodată în fața
# clientului. Le numărăm separat de inventar, fiindcă un tip cu 200 de muchii din care 190 moarte
# arată sănătos în inventar și e inert în producție.
_DEAD_EDGES_SQL = """
    select r.kind,
           count(*)                                                        as edges,
           count(*) filter (where p.id is null)                            as target_missing,
           count(*) filter (where p.status is distinct from 'active')      as target_inactive,
           count(*) filter (where p.availability = 'out_of_stock')         as target_out_of_stock
      from product_relations r
      left join products p
        on p.id = r.related_id and p.business_id = r.business_id
     where r.business_id = $1
     group by r.kind
"""

_RECIPROCAL_SQL = """
    select r.kind,
           count(*) as edges,
           count(*) filter (
               where exists (
                   select 1 from product_relations b
                    where b.business_id = r.business_id
                      and b.product_id  = r.related_id
                      and b.related_id  = r.product_id
                      and b.kind        = r.kind
               )
           ) as reciprocated
      from product_relations r
     where r.business_id = $1
     group by r.kind
"""

_COVERAGE_SQL = """
    select
        count(*)                                                       as active_products,
        count(*) filter (
            where exists (select 1 from product_relations r
                           where r.business_id = p.business_id and r.product_id = p.id)
        )                                                              as with_outgoing,
        count(*) filter (
            where exists (select 1 from product_relations r
                           where r.business_id = p.business_id and r.related_id = p.id)
        )                                                              as with_incoming
      from products p
     where p.business_id = $1 and p.status = 'active'
"""

# Traversarea. `CYCLE ... SET ... USING path` (SQL standard, Postgres 14+) face detecția de ciclu
# în MOTOR: A→B→C→A devine un rând marcat, nu un query nemărginit. `SEARCH BREADTH FIRST` dă
# ordonare deterministă, ceea ce la noi nu e cosmetică (proiectorul NX-240 e pur: două citiri
# trebuie să dea aceiași bytes).
#
# NOTĂ de acuratețe: `path` acumulează doar `related_id`, deci ancora însăși nu e în el. Un ciclu
# A→B→A e prins la depth 3, nu la 2. De aceea histograma de lungimi se calculează DOAR peste
# rândurile ne-ciclice — un „lanț" care se închide în el însuși nu e un lanț de recomandat.
_CHAIN_SQL = """
    with recursive chain as (
        select r.product_id as anchor_id,
               r.related_id,
               1            as depth
          from product_relations r
         where r.business_id = $1 and r.kind = $2
        union all
        select c.anchor_id,
               r.related_id,
               c.depth + 1
          from chain c
          join product_relations r
            on r.business_id = $1
           and r.product_id  = c.related_id
           and r.kind        = $2
         where c.depth < $3
    ) cycle related_id set is_cycle using path
    select c.anchor_id,
           max(c.depth) filter (where not c.is_cycle) as max_depth,
           bool_or(c.is_cycle)                        as has_cycle
      from chain c
     group by c.anchor_id
"""


def _percentile(values: list[int], q: float) -> int:
    """Percentila `q` (0..1) prin nearest-rank. Lista se presupune NEsortată."""
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


async def measure(business_id: str, max_depth: int) -> dict[str, Any]:
    async with tenant_conn(business_id) as conn:
        kinds = [dict(r) for r in await conn.fetch(_KINDS_SQL, business_id)]
        degrees = [dict(r) for r in await conn.fetch(_DEGREE_SQL, business_id)]
        dead = {r["kind"]: dict(r) for r in await conn.fetch(_DEAD_EDGES_SQL, business_id)}
        recip = {r["kind"]: dict(r) for r in await conn.fetch(_RECIPROCAL_SQL, business_id)}
        coverage = dict(await conn.fetchrow(_COVERAGE_SQL, business_id))

        chains: dict[str, list[dict[str, Any]]] = {}
        for row in kinds:
            rows = await conn.fetch(_CHAIN_SQL, business_id, row["kind"], max_depth)
            chains[row["kind"]] = [dict(r) for r in rows]

    by_kind: dict[str, list[int]] = {}
    for row in degrees:
        by_kind.setdefault(row["kind"], []).append(int(row["degree"]))

    report: dict[str, Any] = {
        "business_id": business_id,
        "max_depth_probed": max_depth,
        "active_products": int(coverage["active_products"]),
        "products_with_outgoing": int(coverage["with_outgoing"]),
        "products_with_incoming": int(coverage["with_incoming"]),
        "kinds": [],
    }

    for row in kinds:
        kind = row["kind"]
        degs = by_kind.get(kind, [])
        anchors = chains.get(kind, [])
        # Lungimile se numără DOAR pe ramuri ne-ciclice (vezi nota din `_CHAIN_SQL`).
        depths = [int(a["max_depth"]) for a in anchors if a["max_depth"] is not None]
        cyclic = sum(1 for a in anchors if a["has_cycle"])
        d = dead.get(kind, {})
        rc = recip.get(kind, {})
        edges = int(row["edges"])
        report["kinds"].append(
            {
                "kind": kind,
                "edges": edges,
                "anchors": int(row["anchors"]),
                "targets": int(row["targets"]),
                "out_degree": {
                    "median": _percentile(degs, 0.5),
                    "p90": _percentile(degs, 0.9),
                    "max": max(degs) if degs else 0,
                },
                "chain_max_depth": max(depths) if depths else 0,
                "anchors_with_chain_ge2": sum(1 for x in depths if x >= 2),
                "anchors_with_chain_ge3": sum(1 for x in depths if x >= 3),
                "anchors_hitting_probe_cap": sum(1 for x in depths if x >= max_depth),
                "cyclic_anchors": cyclic,
                "dead_edges": {
                    "target_missing": int(d.get("target_missing", 0)),
                    "target_inactive": int(d.get("target_inactive", 0)),
                    "target_out_of_stock": int(d.get("target_out_of_stock", 0)),
                },
                "reciprocated": int(rc.get("reciprocated", 0)),
                "reciprocity_pct": (100.0 * int(rc.get("reciprocated", 0)) / edges)
                if edges
                else 0.0,
            }
        )

    for k in report["kinds"]:
        k["traversal"], k["suggested_max_depth"], k["traversal_reason"] = _classify(k, max_depth)
    report["verdict"], report["verdict_reasons"] = _verdict(report)
    return report


def _classify(k: dict[str, Any], max_depth_probed: int) -> tuple[str, int, str]:
    """Clasifică un tip de muchie după CE SUPORTĂ, nu după ce ne-am dori să însemne.

    Ieșirea e exact specul de care are nevoie `relation_kinds` din DomainPack, derivat din dovezi:

    `chain`           — aciclic, cu lanțuri reale pe destule ancore ⇒ se poate traversa tranzitiv;
                        adâncimea sugerată e cea MĂSURATĂ, nu una rotundă aleasă de noi.
    `bounded`         — are lanțuri, dar și cicluri sau lanțuri care depășesc plafonul probei ⇒ se
                        traversează cu gardă de ciclu și plafon mic (2).
    `neighbors-only`  — lanțuri de lungime 1, eșantion prea mic, SAU cicluri care saturează ⇒ NU se
                        traversează tranzitiv.

    Nota care contează, și pe care măsurătoarea pe date reale a scos-o la iveală: **un ciclu într-un
    tip simetric nu e o stricăciune, e definiția lui.** „A merge bine cu B" implică „B merge bine cu
    A", deci `complement` e ciclic prin natura relației, nu prin greșeală de seed. Ciclurile sunt un
    defect DOAR pentru tipurile pe care vrei să le înlănțui. De aceea clasificarea e per tip, iar
    saturarea (toate ancorele ciclice) e citită ca „relație simetrică", nu ca „date de reparat".
    """
    if k["chain_max_depth"] <= 1:
        return "neighbors-only", 1, "toate lanțurile au lungime 1"
    if k["anchors_with_chain_ge2"] < MIN_ANCHORS_WITH_CHAIN:
        return (
            "neighbors-only",
            1,
            f"doar {k['anchors_with_chain_ge2']} ancore cu lanț ≥2 (prag {MIN_ANCHORS_WITH_CHAIN})",
        )
    if k["anchors"] and k["cyclic_anchors"] >= k["anchors"]:
        return (
            "neighbors-only",
            1,
            f"toate cele {k['anchors']} ancore sunt ciclice ⇒ relație simetrică, nu secvență",
        )
    if k["cyclic_anchors"] > 0 or k["anchors_hitting_probe_cap"] > 0:
        detail = (
            f"{k['cyclic_anchors']} ancore ciclice"
            if k["cyclic_anchors"]
            else f"lanțuri care ating plafonul probei ({max_depth_probed})"
        )
        return "bounded", 2, f"{detail} ⇒ gardă de ciclu + plafon mic"
    return (
        "chain",
        int(k["chain_max_depth"]),
        f"aciclic, adâncime măsurată {k['chain_max_depth']}, "
        f"{k['anchors_with_chain_ge3']} ancore cu lanț ≥3",
    )


def _verdict(report: dict[str, Any]) -> tuple[str, list[str]]:
    """Tri-state, în disciplina NX-238: „n-am ce traversa" ≠ „am măsurat și merge".

    `NOT-READY` — niciun tip nu suportă traversare ⇒ deblocarea e a catalogului, nu a codului.
    `PARTIAL`   — se poate traversa doar mărginit (adâncime 2), niciun lanț curat.
    `READY`     — cel puțin un tip e `chain`.
    """
    if not report["kinds"]:
        return "NOT-READY", ["`product_relations` nu are nicio muchie pentru acest tenant."]

    reasons = [
        f"`{k['kind']}` → {k['traversal']}: {k['traversal_reason']}" for k in report["kinds"]
    ]
    levels = {k["traversal"] for k in report["kinds"]}
    if "chain" in levels:
        return "READY", reasons
    if "bounded" in levels:
        return "PARTIAL", reasons
    return "NOT-READY", reasons


def render(report: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(
        f"business: {report['business_id']}   (adâncime probată: {report['max_depth_probed']})"
    )
    total = report["active_products"]
    out.append(
        f"produse active: {total}   cu muchii de ieșire: {report['products_with_outgoing']}"
        f" ({100 * report['products_with_outgoing'] / total:.1f}%)"
        if total
        else "produse active: 0"
    )
    out.append("")
    head = (
        f"{'tip':<16}{'muchii':>7}{'ancore':>7}{'deg max':>8}"
        f"{'adânc':>7}{'≥2':>5}{'≥3':>5}{'cicl':>6}{'moarte':>8}{'recipr':>8}"
        f"  {'traversare':<16}{'depth':>6}"
    )
    out.append(head)
    out.append("-" * len(head))
    for k in report["kinds"]:
        dead = (
            k["dead_edges"]["target_inactive"]
            + k["dead_edges"]["target_out_of_stock"]
            + k["dead_edges"]["target_missing"]
        )
        out.append(
            f"{k['kind']:<16}{k['edges']:>7}{k['anchors']:>7}"
            f"{k['out_degree']['max']:>8}"
            f"{k['chain_max_depth']:>7}{k['anchors_with_chain_ge2']:>5}"
            f"{k['anchors_with_chain_ge3']:>5}{k['cyclic_anchors']:>6}"
            f"{dead:>8}{k['reciprocity_pct']:>7.0f}%"
            f"  {k['traversal']:<16}{k['suggested_max_depth']:>6}"
        )
    out.append("")
    out.append(f"VERDICT: {report['verdict']}")
    for r in report["verdict_reasons"]:
        out.append(f"  · {r}")
    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--business", default=DEMO_BIZ, help="business_id (implicit: tenantul demo)")
    ap.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    ap.add_argument("--json", type=pathlib.Path, default=None, help="scrie raportul JSON aici")
    args = ap.parse_args()

    try:
        report = await measure(args.business, args.max_depth)
    finally:
        await close_pool()

    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nJSON: {args.json}")
    # Verdictul nu e un eșec de proces: proba și-a făcut treaba chiar când răspunsul e „nu".
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
