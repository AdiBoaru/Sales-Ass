"""NX-265 pasul 3 — rulează setul judecat pe sistemul curent și scoate cifrele.

Patru metrici, alese pentru că niciuna nu se poate umfla:

* **top-3 / top-6** — în câte cazuri apare cel puțin un produs judecat `corect`;
* **rata de zero rezultate** — cifra care a scos la iveală 13 fraze moarte din 18 la auditul din
  2026-09-03. Zero rezultate nu e o opinie;
* **rata de contradicție** — în câte cazuri apare în top 6 un produs judecat `greșit`. Asta măsoară
  exact clasa de defect NX-257: un adevăr nepotrivit, pe care nicio poartă de adevăr nu-l prinde.
  E singura metrică pe care o poți înrăutăți „îmbunătățind" recall-ul;
* **MRR** — poziția primului corect, ca semnal fin.

Fără NDCG cu grade: setul e mic și judecata e binară, iar o metrică sofisticată pe date sărace dă o
falsă precizie.

**Refuză să publice o metrică pe eșantion insuficient.** Sub `MIN_PER_CLASS` cazuri judecate,
clasa iese `INSUFFICIENT`, nu o cifră care pare un rezultat. Aceeași disciplină ca la NX-238 și
NX-246 felia 3: patru verdicte, nu două.

    python scripts/goldset_report.py --business <uuid>
    python scripts/goldset_report.py --business <uuid> --label dupa-reranker
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.catalog import search_products_lexical  # noqa: E402
from src.evals.retrieval.snapshot import (  # noqa: E402
    CatalogSnapshot,
    compare,
    read_snapshot,
)

GOLDSET_DIR = ROOT / "tests" / "golden" / "retrieval_goldset"
CASES = GOLDSET_DIR / "cases.json"
MANIFEST = GOLDSET_DIR / "manifest.json"

MIN_PER_CLASS = 10
TOP_POOL = 12


def _manifest_field(key: str):
    """Un câmp din manifest, sau `None` dacă manifestul e mai vechi decât câmpul."""
    if not MANIFEST.exists():
        return None
    return json.loads(MANIFEST.read_text(encoding="utf-8")).get(key)


def _sealed_snapshot() -> CatalogSnapshot | None:
    """Catalogul sub care a fost judecat setul, dacă manifestul îl poartă."""
    if not MANIFEST.exists():
        return None
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return CatalogSnapshot.from_dict(raw.get("catalog_snapshot"))


_JUDGED_SQL = """
select id::text as id, status
  from products
 where business_id = $1 and id = any($2::uuid[])
"""


async def _judged_products_still_valid(conn, business_id: str, ids: set[str]) -> dict:
    """Produsele judecate care nu mai pot fi găsite de nimeni.

    Fără verificarea asta, un produs judecat „corect" și dezactivat între timp se comportă în raport
    exact ca o ratare de relevanță: nu apare în top, deci scade top-3. Cifra ar fi corectă și
    concluzia complet greșită — ai căuta o regresie de motor acolo unde s-a mișcat raftul.

    Se raportează, NU se corectează scorul. A scoate tăcut cazurile afectate ar face setul să se
    micșoreze singur în timp, iar metrica pe un eșantion care se subțiază de la sine e mai
    periculoasă decât una care spune „aici am o problemă de date"."""
    if not ids:
        return {"missing": [], "inactive": []}
    rows = await conn.fetch(_JUDGED_SQL, business_id, sorted(ids))
    found = {r["id"]: r["status"] for r in rows}
    return {
        "missing": sorted(ids - set(found)),
        "inactive": sorted(pid for pid, st in found.items() if st != "active"),
    }


def _verify_manifest(cases: list[dict]) -> str:
    """Amprenta trebuie să corespundă conținutului. Fail-closed: un set editat fără regenerare ar
    face raportul să compare două lucruri diferite și să numească diferența „progres"."""
    if not MANIFEST.exists():
        raise SystemExit("lipsește manifestul — rulează scripts/goldset_annotate.py")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda c: c["fingerprint"]):
        digest.update(case["fingerprint"].encode())
        digest.update(",".join(sorted(case.get("correct", []))).encode())
        digest.update(",".join(sorted(case.get("wrong", []))).encode())
    actual = digest.hexdigest()
    if actual != manifest.get("sha256"):
        raise SystemExit(
            f"setul nu corespunde manifestului ({actual[:12]} vs "
            f"{str(manifest.get('sha256'))[:12]}). Regenerează cu goldset_annotate.py"
        )
    return actual


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--label", default="baseline")
    args = ap.parse_args()

    if not CASES.exists():
        print(f"lipsește setul ({CASES}). Rulează goldset_sample.py apoi goldset_annotate.py")
        return 2
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    sha = _verify_manifest(cases)

    # Un caz fără verdict utilizabil nu se numără nicăieri: nici ca succes, nici ca eșec. Altfel
    # frazele nejudecate ar apărea ca ratări, iar raportul ar arăta mai rău cu cât ai judecat mai
    # puțin — exact invers decât trebuie.
    usable = [c for c in cases if c.get("correct") or c.get("expect_empty")]
    if not usable:
        print("niciun caz cu verdict utilizabil — nimic de măsurat")
        return 2

    per_class: dict[str, list[dict]] = collections.defaultdict(list)
    drift: dict = {}
    integrity: dict = {}
    try:
        async with tenant_conn(args.business) as conn:
            drift = compare(_sealed_snapshot(), await read_snapshot(conn, args.business))
            integrity = await _judged_products_still_valid(
                conn,
                args.business,
                {pid for c in cases for pid in (c.get("correct") or ())},
            )
            for case in usable:
                rows = await search_products_lexical(
                    conn,
                    args.business,
                    case["query"],
                    sort_mode="relevance",
                    locale=args.locale,
                    pool=TOP_POOL,
                )
                ids = [r["id"] for r in rows]
                correct = set(case.get("correct") or ())
                wrong = set(case.get("wrong") or ())
                first = next((i for i, pid in enumerate(ids) if pid in correct), None)
                per_class[case["class"]].append(
                    {
                        "empty": not ids,
                        # Un caz `expect_empty` reușește exact când nu întoarce nimic: acolo
                        # succesul E tăcerea onestă, nu un produs.
                        "hit3": (not ids)
                        if case.get("expect_empty")
                        else (first is not None and first < 3),
                        "hit6": (not ids)
                        if case.get("expect_empty")
                        else (first is not None and first < 6),
                        "contradiction": any(pid in wrong for pid in ids[:6]),
                        "rr": (1 / (first + 1)) if first is not None else 0.0,
                    }
                )
    finally:
        await close_pool()

    def _agg(items: list[dict]) -> dict:
        n = len(items)
        if n < MIN_PER_CLASS:
            return {"n": n, "verdict": "INSUFFICIENT"}
        return {
            "n": n,
            "verdict": "MEASURED",
            "top3": round(sum(i["hit3"] for i in items) / n, 4),
            "top6": round(sum(i["hit6"] for i in items) / n, 4),
            "zero_rate": round(sum(i["empty"] for i in items) / n, 4),
            "contradiction_rate": round(sum(i["contradiction"] for i in items) / n, 4),
            "mrr": round(sum(i["rr"] for i in items) / n, 4),
        }

    everything = [i for items in per_class.values() for i in items]
    report = {
        "_provenance": {
            "business_id": args.business,
            "label": args.label,
            "goldset_sha256": sha,
            "cases_total": len(cases),
            "cases_usable": len(usable),
            "min_per_class": MIN_PER_CLASS,
            "path": "lexical",
            # Momentul JUDECĂȚII, citit din manifest — nu ceasul de acum. Cardul cere și
            # `measured_at`, și rulări bit-identice; un timestamp proaspăt în artefact le-ar
            # face imposibil de îndeplinit pe amândouă, iar cel util e oricum primul: „ce
            # vechime are judecata din spatele cifrei ăsteia".
            "goldset_measured_at": _manifest_field("measured_at"),
        },
        # Amândouă sunt OBSERVAȚII, nu porți: catalogul unui client trebuie să se schimbe. Ce n-are
        # voie e ca schimbarea să treacă drept diferență de calitate.
        "catalog_drift": drift,
        "judged_products": integrity,
        "overall": _agg(everything),
        "by_class": {cls: _agg(items) for cls, items in sorted(per_class.items())},
    }
    out = ROOT / "reports" / f"goldset-{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    o = report["overall"]
    print(f"set: {len(usable)} cazuri utilizabile din {len(cases)} · amprentă {sha[:12]}\n")
    # Driftul se spune ÎNAINTEA cifrelor, nu într-o notă de subsol: dacă raftul s-a mișcat, cifra
    # de dedesubt nu mai măsoară ce crede cititorul că măsoară.
    if drift.get("verdict") == "drifted":
        moved = ", ".join(
            f"{k} {v['sealed']} → {v['current']}" for k, v in drift["changed"].items()
        )
        print(f"  ATENȚIE: catalogul s-a mișcat de la judecarea setului — {moved}")
    elif drift.get("verdict") == "unknown":
        print("  amprenta catalogului lipsește din manifest (set sigilat înainte de amprentă)")
    if integrity.get("missing") or integrity.get("inactive"):
        print(
            f"  {len(integrity['missing'])} produse judecate au DISPĂRUT, "
            f"{len(integrity['inactive'])} nu mai sunt active — cifrele de mai jos le numără drept "
            "ratări, deci o scădere poate fi de DATE, nu de relevanță"
        )
    if drift.get("verdict") != "same" or integrity.get("missing") or integrity.get("inactive"):
        print()
    if o["verdict"] == "MEASURED":
        print(f"  top-3 {o['top3']:.1%} · top-6 {o['top6']:.1%} · zero {o['zero_rate']:.1%}")
        print(f"  contradicție {o['contradiction_rate']:.1%} · MRR {o['mrr']:.3f}\n")
    else:
        print(f"  {o['verdict']} (n={o['n']})\n")
    print(f"  {'clasă':16}{'n':>4}{'top3':>8}{'top6':>8}{'zero':>8}{'contra':>8}")
    for cls, agg in report["by_class"].items():
        if agg["verdict"] != "MEASURED":
            print(f"  {cls:16}{agg['n']:>4}   {agg['verdict']}")
            continue
        print(
            f"  {cls:16}{agg['n']:>4}{agg['top3']:>8.0%}{agg['top6']:>8.0%}"
            f"{agg['zero_rate']:>8.0%}{agg['contradiction_rate']:>8.0%}"
        )
    print(f"\nraport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
