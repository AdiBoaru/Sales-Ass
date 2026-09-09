"""NX-203 — pool-ul de candidați per familie, pentru etichetare umană.

Nimeni nu poate eticheta 2.758 de produse pentru fiecare familie, deci se etichetează un POOL. Cum
se construiește pool-ul decide ce poate măsura benchmarkul, iar aici e o capcană clasică: un pool
format doar din ce întoarce motorul nostru măsoară **re-rankingul**, nu **recall-ul**. Un produs
relevant pe care motorul nu-l găsește niciodată n-ar intra în pool, deci n-ar fi etichetat, deci ar
lipsi din numitor — iar sistemul ar avea recall perfect prin construcție.

De aceea pool-ul are DOUĂ brațe, și al doilea nu trece prin motorul nostru:

  1. **Retrieval real**, pe fraze, în două configurații: textul brut (ce face motorul cu cererea așa
     cum e scrisă) și textul cu constrângerile familiei aplicate (ce ar trebui să servească raftul).
     Rulat pe conexiune `bot_runtime`, cu RLS activ — planul de sub RLS e cel real, iar la SOLE el
     diferă de al superuserului (indexurile GIN sunt inerte, vezi §12.2 din DB-V3-SOLE-IMPORT).
  2. **Produsele afirmate de comerciant** — cele din a căror fișă provine fraza. Nu sunt o judecată
     de relevanță (le verifică omul), dar sunt independente de motor, deci exact acolo unde brațul 1
     are punctul orb.

Ordinea în care omul vede candidații NU e ordinea motorului. E o permutare deterministă, derivată
din `family_id`: dacă primele șase carduri ar fi mereu top-6-ul motorului, judecata umană
s-ar ancora
pe el și am eticheta motorul, nu marfa. Determinismul păstrează reproductibilitatea.

Zero apeluri externe: brațul semantic e stins (`SEARCH_SEMANTIC_ENABLED=false`), deci nu se cheltuie
niciun token.

    python scripts/nx203_build_pools.py --business <uuid>              # raport, nu scrie
    python scripts/nx203_build_pools.py --business <uuid> --limit 20   # doar primele N familii
    python scripts/nx203_build_pools.py --business <uuid> --write
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.catalog import search_products_lexical  # noqa: E402

DATA_DIR = ROOT / "tests" / "golden" / "nx203"
REPORT_DIR = ROOT / "reports"

#: Câți candidați vede omul per familie. Peste treizeci, atenția scade și ultimele judecăți sunt mai
#: proaste decât primele — iar o etichetă proastă în instrumentul de măsură e mai rea decât una
#: lipsă, fiindcă nu se vede că lipsește.
POOL_SIZE = 24

#: Câte formulări ale familiei se trimit prin motor. Toate ar fi risipă: formulările unei familii
#: sunt, prin definiție, aceeași cerere, deci pool-urile lor se suprapun aproape complet.
MAX_PHRASES_PER_FAMILY = 3

#: Cât cere fiecare interogare din motor. Mai mult decât pool-ul final, ca fuziunea celor două brațe
#: să aibă din ce alege.
FETCH_POOL = 30


def _rank_key(family_id: str, product_id: str) -> str:
    """Permutare deterministă, dar necorelată cu rangul motorului.

    Sămânța include `family_id`, ca același produs să apară pe poziții diferite în familii diferite:
    altfel ordinea ar fi o constantă globală, iar un evaluator care etichetează multe familii ar
    învăța-o.
    """
    return hashlib.sha256(f"{family_id}:{product_id}".encode()).hexdigest()


async def _retrieve(business_id: str, families: list[dict]) -> dict[str, dict[str, list[str]]]:
    """`family_id → {product_id → [surse]}` din motorul REAL, pe conexiune tenant-scoped."""
    found: dict[str, dict[str, list[str]]] = {}
    for fam in families:
        fid = fam["family_id"]
        hits: dict[str, list[str]] = {}
        phrases = fam["queries"][:MAX_PHRASES_PER_FAMILY]
        constraints = fam.get("hard_constraints") or []
        facet_filters: dict[str, list[str]] = {}
        for c in constraints:
            facet_filters.setdefault(c["facet"], []).append(c["value"])
        async with tenant_conn(business_id) as conn:
            for phrase in phrases:
                # Config A — textul brut, fără niciun filtru. Asta întoarce motorul azi pentru
                # cererea scrisă exact așa; e și singura configurație în care se vede dacă scara
                # lexicală cade pe o treaptă degradată.
                rows = await search_products_lexical(
                    conn, business_id, phrase, locale="ro", pool=FETCH_POOL
                )
                for rank, row in enumerate(rows):
                    hits.setdefault(str(row["id"]), []).append(f"raw#{rank}")
                # Config B — aceeași frază, dar cu constrângerile familiei impuse. Diferența dintre
                # A și B e chiar măsura cât de mult depinde răspunsul de rezolvarea vocabularului.
                if facet_filters:
                    rows_b = await search_products_lexical(
                        conn,
                        business_id,
                        phrase,
                        facet_filters=facet_filters,
                        locale="ro",
                        pool=FETCH_POOL,
                    )
                    for rank, row in enumerate(rows_b):
                        hits.setdefault(str(row["id"]), []).append(f"constrained#{rank}")
        found[fid] = hits
    return found


async def _hydrate(business_id: str, product_ids: set[str]) -> dict[str, dict]:
    """Câmpurile de care are nevoie ochiul uman ca să judece în trei secunde: nume, brand, preț,
    tip, o imagine. Un query, nu unul per produs."""
    if not product_ids:
        return {}
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(
            """select p.id::text as id,
                      p.name,
                      b.name as brand,
                      p.price,
                      p.sale_price,
                      p.availability,
                      p.product_url,
                      p.attributes -> 'product_type' as product_type,
                      (select i.url from product_images i
                        where i.product_id = p.id order by i.position limit 1) as image_url
                 from products p
                 left join brands b on b.id = p.brand_id
                where p.business_id = $1 and p.id = any($2::uuid[])""",
            business_id,
            list(product_ids),
        )
    return {
        r["id"]: {
            "product_id": r["id"],
            "name": r["name"],
            "brand": r["brand"],
            "price": float(r["price"]) if r["price"] is not None else None,
            "sale_price": float(r["sale_price"]) if r["sale_price"] is not None else None,
            "availability": r["availability"],
            "product_url": r["product_url"],
            "product_type": json.loads(r["product_type"]) if r["product_type"] else None,
            "image_url": r["image_url"],
        }
        for r in rows
    }


def _assemble(fam: dict, engine_hits: dict[str, list[str]], cap: int) -> list[dict]:
    """Cele două brațe → un pool plafonat, cu sursele păstrate pe fiecare candidat.

    Plafonarea NU taie brațul comerciantului: dacă ar tăia, punctul orb pe care brațul ăsta există
    ca să-l acopere s-ar întoarce exact în cazurile grele (familii pe care motorul le servește
    prost, deci cu multe rezultate proprii care ar împinge afară afirmațiile comerciantului).
    """
    merchant = list(fam.get("merchant_asserted_products") or [])
    entries: dict[str, dict] = {}
    for pid in merchant:
        entries[pid] = {"product_id": pid, "sources": ["merchant"]}
    for pid, sources in engine_hits.items():
        e = entries.setdefault(pid, {"product_id": pid, "sources": []})
        e["sources"] = e["sources"] + sources

    ordered = sorted(entries.values(), key=lambda e: _rank_key(fam["family_id"], e["product_id"]))
    if len(ordered) <= cap:
        return ordered
    keep = [e for e in ordered if "merchant" in e["sources"]][:cap]
    for e in ordered:
        if len(keep) >= cap:
            break
        if e not in keep:
            keep.append(e)
    return sorted(keep, key=lambda e: _rank_key(fam["family_id"], e["product_id"]))


def _stratify(families: list[dict], limit: int) -> list[dict]:
    """Primele `limit` familii, dar rotind pe tipul de produs, nu tăind de sus.

    Tăiat de sus, setul ar fi 48 de familii de șampon și zero de pastă de dinți: familiile cu multe
    formulări se îngrămădesc pe câteva rafturi. Un benchmark cu acoperire pe 6 tipuri nu poate
    detecta o regresie pe al șaptelea, iar cardul cere explicit stratificare. Rotația păstrează în
    interiorul fiecărui tip ordinea de atestare, deci familiile cele mai bine susținute intră întâi.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for fam in families:
        buckets[fam.get("product_type") or "?"].append(fam)
    picked: list[dict] = []
    while len(picked) < limit:
        added = False
        for key in sorted(buckets):
            if buckets[key]:
                picked.append(buckets[key].pop(0))
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
    return picked


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--business", required=True)
    ap.add_argument("--limit", type=int, default=0, help="doar primele N familii (0 = toate)")
    ap.add_argument("--cap", type=int, default=POOL_SIZE, help="candidați per familie")
    ap.add_argument(
        "--classes",
        default="colloquial,brand,negative,compound",
        help="clasele de familie incluse (exact are pool trivial)",
    )
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    draft_path = DATA_DIR / "families_draft.json"
    if not draft_path.exists():
        print(f"Lipsește {draft_path.relative_to(ROOT)} — rulează întâi nx203_extract_families.py")
        return 1
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    if draft["business_id"] != args.business:
        print("Draftul e al altui tenant. Refuz: un pool cross-tenant e o scurgere, nu un bug.")
        return 1

    wanted = {c.strip() for c in args.classes.split(",") if c.strip()}
    families = [f for f in draft["families"] if f["query_class"] in wanted]
    families.sort(key=lambda f: -len(f["queries"]))
    if args.limit:
        families = _stratify(families, args.limit)
    if not families:
        print("Nicio familie în clasele cerute.")
        return 1

    print(f"Rulez motorul pe {len(families)} familii ({args.cap} candidați fiecare)...")
    engine = await _retrieve(args.business, families)

    pools: dict[str, list[dict]] = {}
    for fam in families:
        pools[fam["family_id"]] = _assemble(fam, engine.get(fam["family_id"], {}), args.cap)

    all_ids = {e["product_id"] for pool in pools.values() for e in pool}
    products = await _hydrate(args.business, all_ids)
    await close_pool()

    stats = Counter()
    missing_image = 0
    for pool in pools.values():
        for e in pool:
            stats["candidates"] += 1
            if "merchant" in e["sources"]:
                stats["from_merchant"] += 1
            if any(s.startswith("raw#") for s in e["sources"]):
                stats["from_engine_raw"] += 1
            if any(s.startswith("constrained#") for s in e["sources"]):
                stats["from_engine_constrained"] += 1
            if len(e["sources"]) == 1 and e["sources"][0] == "merchant":
                stats["merchant_only"] += 1
            if not products.get(e["product_id"], {}).get("image_url"):
                missing_image += 1

    sizes = sorted(len(p) for p in pools.values())
    print()
    print(f"familii cu pool     : {len(pools)}")
    print(f"candidați total     : {stats['candidates']}")
    print(f"  mediana pool      : {sizes[len(sizes) // 2]}")
    print(f"  minim pool        : {sizes[0]}")
    print(f"din motor (brut)    : {stats['from_engine_raw']}")
    print(f"din motor (filtrat) : {stats['from_engine_constrained']}")
    print(f"din comerciant      : {stats['from_merchant']}")
    print(f"  DOAR comerciant   : {stats['merchant_only']}  ← punctul orb al motorului nostru")
    print(f"produse fără imagine: {missing_image}")
    print(f"judecăți de pus     : {stats['candidates']}")

    if args.write:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = DATA_DIR / "pools.json"
        out.write_text(
            json.dumps(
                {
                    "business_id": args.business,
                    "catalog_version": draft["catalog_version"],
                    "pool_cap": args.cap,
                    "pools": pools,
                    "products": products,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nScris: {out.relative_to(ROOT)}")
    else:
        print("\n(dry-run — nimic scris; adaugă --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
