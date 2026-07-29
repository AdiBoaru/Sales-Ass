"""NX-203 — PROPUNERE de candidati pentru qrels (NU qrels, NU adevar)

REGULA DE NOTARE, NECIRCULARA (audit Codex, PR #251):
  Gradul de relevanta (0-3) NU depinde de cate metode au gasit produsul. Daca acordul cu
  retrieval-ul ar ridica nota, benchmarkul ar rasplati exact ce sistemul gaseste deja — un examen
  in care elevul isi scrie baremul. Gradul vine din INTENTIA query-ului fata de PROPRIETATILE
  produsului, si il da un OM.
  Masinile produc doar MULTIMEA de candidati de examinat. Atat.

CE E SQL-UL DE AICI: un generator de candidati. Filtrele sunt alese de mine per query, deci sunt
o opinie exprimata in SQL — nu o masuratoare independenta. Nu are drept de vot asupra adevarului.

NX-203 lot 1, varianta corectata: a treia sursa NU depinde de motorul de cautare.

Prima varianta propunea judecatile din ce returnau lexical+semantic. Gresit: acolo unde retrieval-ul
esueaza, propunerea codifica esecul ca adevar. Masurat pe „sunt insarcinata, ce crema antirid pot
folosi?" — zero creme antirid returnate, deci propunerea ar fi fost inutila SI ar fi inghetat
comportamentul gresit in benchmark.

A treia sursa e o interogare SQL directa pe catalog (categorie + attributes), care raspunde la
„ce ar TREBUI sa se potriveasca", independent de cum cauta sistemul azi.
"""

import asyncio
import json
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.llm import get_llm  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.catalog import search_products_lexical, search_products_semantic  # noqa: E402

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"
CATALOG_VERSION = "demo-2026-07-22"

# Al treilea vot: ce spune CATALOGUL ca s-ar potrivi. `cat_like` filtreaza pe slug de categorie,
# `attr` pe chei din attributes, `name_like` pe nume. Toate SQL pur, fara scoring.
QUERIES = [
    (
        "q-cat-01",
        "ce cremă hidratantă e bună pentru ten uscat?",
        "real",
        "skincare",
        "categorie",
        {"name_like": "%hidratant%", "attr_concern": "dry"},
    ),
    (
        "q-cat-02",
        "caut o cremă hidratantă pentru ten uscat",
        "real",
        "skincare",
        "categorie",
        {"name_like": "%hidratant%", "attr_concern": "dry"},
    ),
    (
        "q-cat-03",
        "ce șampon aveți?",
        "real",
        "haircare",
        "categorie+diacritice",
        {"cat_like": "sampoane"},
    ),
    (
        "q-cat-04",
        "ce sampon aveti?",
        "real",
        "haircare",
        "categorie fara diacritice",
        {"cat_like": "sampoane"},
    ),
    (
        "q-self-01",
        "am tenul gras, ce ser îmi recomanzi?",
        "real",
        "skincare",
        "descriere de sine",
        {"cat_like": "seruri-pentru-ten", "attr_concern": "oily"},
    ),
    (
        "q-self-02",
        "sunt însărcinată, ce cremă antirid pot folosi?",
        "real",
        "skincare",
        "descriere de sine + SAFETY",
        {"attr_concern": "anti_aging", "_expected": "safety_refusal"},
    ),
    (
        "q-self-03",
        "sunt cu ten sensibil, ce curățare îmi trebuie?",
        "paraphrase",
        "skincare",
        "descriere de sine",
        {"cat_like": "curatarea-tenului", "attr_concern": "sensitive"},
    ),
    (
        "q-con-01",
        "ai protecție solară spf 50?",
        "real",
        "skincare",
        "constrangere atribut",
        {"cat_like": "protectie-solara"},
    ),
    (
        "q-con-02",
        "caut un fond de ten cu acoperire medie, am subton cald.",
        "real",
        "makeup",
        "constrangeri multiple",
        {"cat_like": "fond-de-ten"},
    ),
    (
        "q-con-03",
        "vreau o cremă de mâini, le am cam uscate",
        "real",
        "bodycare",
        "exprimare naturala",
        {"name_like": "%m%ini%"},
    ),
    # ATENTIE: originalul din trafic — „ai si o varianta fara parfum?" — e o replica de
    # CONTINUARE: „o varianta" a CE? Evaluata singura, produce gold fals (audit Codex #251).
    # Pastram intentia, dar de sine statatoare, deci proveninta devine `paraphrase`, nu `real`.
    (
        "q-con-04",
        "caut o cremă de față fără parfum",
        "paraphrase",
        "skincare",
        "constrangere negativa (reformulat de sine statator)",
        {"attr_fragrance_free": True},
    ),
    (
        "q-con-05",
        "ceva pentru prevenirea coșurilor",
        "real",
        "skincare",
        "concern",
        {"attr_concern": "acne"},
    ),
    (
        "q-con-06",
        "ser cu vitamina C sub 150 lei",
        "paraphrase",
        "skincare",
        "ingredient + pret",
        {"attr_ingredient": "vitamina c", "price_max": 150},
    ),
    (
        "q-ing-01",
        "ser cu acid hialuronic",
        "paraphrase",
        "skincare",
        "ingredient",
        {"attr_ingredient": "acid hialuronic", "cat_like": "seruri-pentru-ten"},
    ),
    (
        "q-ing-02",
        "ceva cu niacinamidă pentru pori",
        "paraphrase",
        "skincare",
        "ingredient + concern",
        {"attr_ingredient": "niacinamid"},
    ),
    (
        "q-ing-03",
        "produse cu retinol",
        "synthetic",
        "skincare",
        "ingredient",
        {"attr_ingredient": "retinol"},
    ),
    (
        "q-lex-01",
        "sampon anti matreata",
        "paraphrase",
        "haircare",
        "typo/fara diacritice",
        {"cat_like": "sampoane", "name_like": "%matrea%"},
    ),
    (
        "q-lex-02",
        "masca de par pentru par uscat",
        "paraphrase",
        "haircare",
        "lexical",
        {"cat_like": "masti-de-par"},
    ),
    (
        "q-lex-03",
        "balsam de buze",
        "paraphrase",
        "makeup",
        "categorie scurta",
        {"name_like": "%balsam%buze%"},
    ),
    ("q-neg-01", "asdfgh qwerty 12345", "real", "zgomot", "input fara sens", {}),
]


async def catalog_lookup(conn, spec: dict) -> list[tuple[str, str]]:
    """Ce spune CATALOGUL ca s-ar potrivi. SQL pur — zero scoring, zero motor de cautare."""
    if not spec:
        return []
    conds = ["p.business_id = $1", "p.status = 'active'"]
    params: list = [DEMO]

    def ph(v):
        params.append(v)
        return f"${len(params)}"

    if v := spec.get("cat_like"):
        conds.append(f"c.slug = {ph(v)}")
    if v := spec.get("name_like"):
        conds.append(f"ro_unaccent(p.name) like ro_unaccent({ph(v)})")
    if v := spec.get("attr_concern"):
        conds.append(f"p.attributes->'concerns' ? {ph(v)}")
    if spec.get("attr_fragrance_free"):
        conds.append("(p.attributes->>'fragrance_free')::boolean is true")
    if v := spec.get("attr_ingredient"):
        conds.append(
            "exists (select 1 from jsonb_array_elements_text("
            "case when jsonb_typeof(p.attributes->'key_ingredients')='array' "
            "then p.attributes->'key_ingredients' else '[]'::jsonb end) ki "
            f"where ro_unaccent(ki) like ro_unaccent({ph(f'%{v}%')}))"
        )
    if v := spec.get("price_max"):
        conds.append(f"p.price <= {ph(v)}")

    rows = await conn.fetch(
        "select p.id::text as id, p.name from products p "
        "left join categories c on c.id = p.primary_category_id "
        f"where {' and '.join(conds)} order by p.name limit 12",
        *params,
    )
    return [(r["id"], r["name"]) for r in rows]


async def main() -> int:
    llm = get_llm()
    out = []
    async with tenant_conn(DEMO) as conn:
        for qid, q, prov, cat, dim, spec in QUERIES:
            votes: dict[str, set[str]] = defaultdict(set)
            names: dict[str, str] = {}

            for p in await search_products_lexical(conn, DEMO, q, pool=20):
                votes[str(p["id"])].add("lexical")
                names[str(p["id"])] = p["name"]
            try:
                vec = (await llm.embed([q]))[0]
                for p in await search_products_semantic(conn, DEMO, vec, limit=8, pool=8):
                    votes[str(p["id"])].add("semantic")
                    names[str(p["id"])] = p["name"]
            except Exception as e:  # noqa: BLE001
                print(f"  ! semantic esuat {qid}: {type(e).__name__}")

            # SQL-ul GENEREAZA candidati; NU voteaza adevarul. Filtrele sunt alese de mine per
            # query, deci sunt o opinie exprimata in SQL — nu o masuratoare (audit Codex #251).
            catalog = await catalog_lookup(conn, spec)
            for pid, nm in catalog:
                votes[pid].add("catalog")
                names[pid] = nm

            ranked = sorted(
                votes.items(),
                key=lambda kv: (-("catalog" in kv[1]), -len(kv[1]), names.get(kv[0], "")),
            )
            out.append(
                {
                    "id": qid,
                    "query": q,
                    "provenance": prov,
                    "category": cat,
                    "_dim": dim,
                    "_catalog_hits": len(catalog),
                    "candidates": [
                        {"product_id": pid, "name": names.get(pid, "?"), "methods": sorted(m)}
                        for pid, m in ranked[:10]
                    ],
                }
            )
            only_cat = sum(1 for _, m in ranked if m == {"catalog"})
            miss = "  <-- retrieval RATEAZA ce zice catalogul" if only_cat and catalog else ""
            print(f"{qid:11} catalog={len(catalog):2d}  doar-catalog={only_cat:2d}{miss}")

    await close_pool()
    path = ROOT / "tests" / "golden" / "_qrels_batch1_candidates.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"catalog_version": CATALOG_VERSION, "queries": out}, f, ensure_ascii=False, indent=2
        )
    print(f"\nscris: {path}")
    return 0


raise SystemExit(asyncio.run(main()))
