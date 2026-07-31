"""NX-203 — PROPUNERE de candidati pentru qrels (NU qrels, NU adevar).

REGULA DE NOTARE, NECIRCULARA (audit Codex, PR #251):
  Gradul de relevanta (0-3) NU depinde de cate metode au gasit produsul. Daca acordul cu
  retrieval-ul ar ridica nota, benchmarkul ar rasplati exact ce sistemul gaseste deja — un examen
  in care elevul isi scrie baremul. Gradul vine din INTENTIA query-ului fata de PROPRIETATILE
  produsului, si il da un OM. Masinile produc doar MULTIMEA de candidati de examinat.

CE E SQL-UL DE AICI: un generator de candidati. Filtrele sunt alese de mine per query, deci sunt
o opinie exprimata in SQL — nu o masuratoare independenta. Nu are drept de vot asupra adevarului.

FARA ETICHETE `relevance=0`: evaluatorul trateaza deja produsele nejudecate ca gain 0
(`metrics.ndcg_at_k`: `rmap.get(pid, 0)`), deci o eticheta explicita de zero nu adauga semnal
metric. Incalcarile de constrangere se pun in `forbidden_products`, deliberat si separat.

FARA PLAFOANE TACUTE: orice truncare ascunde candidati de ochii omului INAINTE sa-i vada. Varianta
anterioara avea `limit 12` in SQL plus `[:10]` la iesire, deci la „ce sampon aveti?" elimina 4 din
cele 13 sampoane existente. Acum se raporteaza tot; daca s-ar atinge plafonul de avarie, se spune
explicit in `_truncated`.
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
from src.db.queries.catalog import (  # noqa: E402
    _EFFECTIVE_PRICE,
    _VARIANT_SALE_ON,
    search_products_lexical,
    search_products_semantic,
)

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"
CATALOG_VERSION = "demo-2026-07-22"
SAFETY_CAP = 60  # plafon de avarie; daca se atinge, se RAPORTEAZA, nu se taie tacut

# Filtrele sunt STRANSE: categorie reala + valoarea exacta a atributului. Varianta anterioara
# folosea `name_like '%hidratant%'` FARA categorie si scotea „Ser hidratant" / „Balsam de buze
# hidratant" la o cerere de CREMA — apoi eu raportam „retrieval rateaza", cand de fapt filtrul meu
# era gresit. Categoria `creme-hidratante` exista in catalog; trebuia folosita de la inceput.
QUERIES = [
    (
        "q-cat-01",
        "ce cremă hidratantă e bună pentru ten uscat?",
        "real",
        "skincare",
        "categorie",
        {"cat": "creme-hidratante", "suitable_for": "dry"},
    ),
    (
        "q-cat-02",
        "caut o cremă hidratantă pentru ten uscat",
        "real",
        "skincare",
        "categorie",
        {"cat": "creme-hidratante", "suitable_for": "dry"},
    ),
    (
        "q-cat-03",
        "ce șampon aveți?",
        "real",
        "haircare",
        "categorie+diacritice",
        {"cat": ["sampoane", "sampon-uscat"]},
    ),
    (
        "q-cat-04",
        "ce sampon aveti?",
        "real",
        "haircare",
        "categorie fara diacritice",
        {"cat": ["sampoane", "sampon-uscat"]},
    ),
    (
        "q-self-01",
        "am tenul gras, ce ser îmi recomanzi?",
        "real",
        "skincare",
        "descriere de sine",
        {"cat": "seruri-pentru-ten", "suitable_for": "oily"},
    ),
    # q-self-02 („sunt insarcinata, ce crema antirid pot folosi?") a fost SCOS din qrels-ul de
    # retrieval. Raspunsul corect nu e o lista de produse, e un raspuns de siguranta: absenta
    # retinolului NU dovedeste ca produsul e sigur in sarcina. Cazul apartine benchmarkului
    # safety/conversational, nu unuia de relevanta. Vezi PR #251.
    (
        "q-self-03",
        "sunt cu ten sensibil, ce curățare îmi trebuie?",
        "paraphrase",
        "skincare",
        "descriere de sine",
        {"cat": "curatarea-tenului", "suitable_for": "sensitive"},
    ),
    (
        "q-con-01",
        "ai protecție solară spf 50?",
        "real",
        "skincare",
        "constrangere atribut",
        {"cat": "protectie-solara", "attr_eq": ("spf", "50")},
    ),
    (
        "q-con-02",
        "caut un fond de ten cu acoperire medie, am subton cald.",
        "real",
        "makeup",
        "constrangeri multiple",
        {"cat": "fond-de-ten", "attr_eq": ("coverage", "medium")},
    ),
    (
        "q-con-03",
        "vreau o cremă de mâini, le am cam uscate",
        "real",
        "bodycare",
        "exprimare naturala",
        {"cat": "creme-de-maini"},
    ),
    # Originalul din trafic — „ai si o varianta fara parfum?" — e replica de CONTINUARE („o
    # varianta" a CE?). Evaluat singur produce gold fals. Reformulat de sine statator, deci
    # provenienta coboara onest de la `real` la `paraphrase`.
    (
        "q-con-04",
        "caut o cremă hidratantă fără parfum",
        "paraphrase",
        "skincare",
        "constrangere negativa",
        {"cat": "creme-hidratante", "fragrance_free": True},
    ),
    (
        "q-con-05",
        "ceva pentru prevenirea coșurilor",
        "real",
        "skincare",
        "concern",
        {"concern": "acne"},
    ),
    (
        "q-con-06",
        "ser cu vitamina C sub 150 lei",
        "paraphrase",
        "skincare",
        "ingredient + pret",
        {"cat": "seruri-pentru-ten", "ingredient": "vitamina c", "price_max": 150},
    ),
    (
        "q-ing-01",
        "ser cu acid hialuronic",
        "paraphrase",
        "skincare",
        "ingredient",
        {"cat": "seruri-pentru-ten", "ingredient": "acid hialuronic"},
    ),
    (
        "q-ing-02",
        "ceva cu niacinamidă pentru pori",
        "paraphrase",
        "skincare",
        "ingredient + concern",
        {"ingredient": "niacinamid", "concern": "oily"},
    ),
    (
        "q-ing-03",
        "produse cu retinol",
        "synthetic",
        "skincare",
        "ingredient",
        {"ingredient": "retinol"},
    ),
    # SCOASE din lot (audit runda 3): „sampon anti matreata" (catalogul n-are asa ceva) si
    # „asdfgh qwerty 12345" nu au NICIUN raspuns corect. Sunt incompatibile cu formatul:
    # `integrity_issues` cere cel putin un produs cu relevance>0, iar `recall_at_k` intoarce 1.0
    # cand nu exista relevante — deci un retrieval care intoarce gunoi ar primi scor perfect.
    # Apartin unei suite de ABSTENTION (a sti sa nu raspunzi), nu unui benchmark de relevanta.
    (
        "q-lex-02",
        "masca de par pentru par uscat",
        "paraphrase",
        "haircare",
        "lexical",
        {"cat": "masti-de-par", "suitable_for": "dry"},
    ),
    (
        "q-lex-03",
        "balsam de buze",
        "paraphrase",
        "makeup",
        "categorie scurta",
        {"name_like": "%balsam%buze%"},
    ),
    (
        "q-sku-01",
        "PETALAFRESHM-2057",
        "synthetic",
        "identificator",
        "SKU exact",
        {"sku": "PETALAFRESHM-2057"},
    ),
    (
        "q-sku-02",
        "PETALAFRESHM-2O57",
        "synthetic",
        "identificator",
        "SKU cu typo (O in loc de 0)",
        {"sku": "PETALAFRESHM-2057"},
    ),
]


async def catalog_lookup(conn, spec: dict) -> tuple[list[tuple[str, str]], bool]:
    """Ce spune CATALOGUL ca s-ar potrivi. SQL pur. Intoarce (rezultate, s-a atins plafonul)."""
    if not spec:
        return [], False
    # `content_status='published'`: calea de discovery live filtreaza pe el (NX-171c). Fara el,
    # candidatii ar putea include produse pe care cautarea nu are voie sa le arate — iar gold-ul ar
    # cere retrieval-ului exact ce ii e interzis. Azi toate cele 300 sunt published, deci nu schimba
    # rezultatul; conteaza in ziua in care nu mai e asa.
    conds = ["p.business_id = $1", "p.status = 'active'", "p.content_status = 'published'"]
    params: list = [DEMO]

    def ph(v):
        params.append(v)
        return f"${len(params)}"

    if v := spec.get("cat"):
        # Lista, nu doar un slug: „ce sampon aveti?" trebuie sa acopere SI `sampon-uscat` — samponul
        # uscat e tot sampon. Un bazin care exclude o subcategorie intreaga produce gold incomplet,
        # iar retrieval-ul ar fi penalizat pentru raspunsuri corecte.
        slugs = [v] if isinstance(v, str) else list(v)
        conds.append(f"c.slug = any({ph(slugs)})")
    if v := spec.get("name_like"):
        conds.append(f"ro_unaccent(p.name) like ro_unaccent({ph(v)})")
    if v := spec.get("suitable_for_all"):
        # INTERSECTIE, nu reuniune: „ten sensibil SI uscat" cere ambele, nu oricare. Cu `or` ar fi
        # intors 44 in loc de 19 — un gold umflat cu produse care satisfac o singura conditie.
        for val in v:
            pa, pb = ph(val), ph(val)
            conds.append(
                f"(p.attributes->'suitable_for' ? {pa} or p.attributes->'concerns' ? {pb})"
            )
    if v := spec.get("cat_any"):
        conds.append(f"c.slug = any({ph(list(v))})")
    if v := spec.get("routine_step"):
        conds.append(f"ro_unaccent(p.attributes->>'routine_step') = ro_unaccent({ph(v)})")
    if v := spec.get("concern"):
        conds.append(f"(p.attributes->'concerns' ? {ph(v)})")
    if v := spec.get("suitable_for"):
        # `suitable_for` = pentru CINE; `concerns` = ce trateaza. „pentru ten uscat" e despre
        # destinatar, dar contractul v3 le suprapune, deci se accepta oricare din cele doua.
        p1, p2 = ph(v), ph(v)
        conds.append(f"(p.attributes->'suitable_for' ? {p1} or p.attributes->'concerns' ? {p2})")
    if kv := spec.get("attr_eq"):
        k, v = kv[0], kv[1]
        conds.append(f"p.attributes->>{ph(k)} = {ph(v)}")
    if spec.get("fragrance_free"):
        conds.append("(p.attributes->>'fragrance_free')::boolean is true")
    if v := spec.get("ingredient"):
        conds.append(
            "exists (select 1 from jsonb_array_elements_text("
            "case when jsonb_typeof(p.attributes->'key_ingredients')='array' "
            "then p.attributes->'key_ingredients' else '[]'::jsonb end) ki "
            f"where ro_unaccent(ki) like ro_unaccent({ph(f'%{v}%')}))"
        )
    if v := spec.get("price_max"):
        # Pretul EFECTIV, ca la client (promotie in fereastra + minim variante). Pe `p.price`,
        # un produs de 100 vandut cu 60 lipsea dintre candidatii pentru „sub 90" — o gaura de
        # recall in GOLD, adica exact eroarea pe care benchmarkul n-are cum s-o detecteze.
        conds.append(f"{_EFFECTIVE_PRICE} <= {ph(v)}")
    if v := spec.get("sku"):
        conds.append(
            "exists (select 1 from product_variants pv where pv.product_id = p.id "
            f"and pv.business_id = p.business_id and pv.sku = {ph(v)})"
        )

    rows = await conn.fetch(
        "select p.id::text as id, p.name from products p "
        "left join categories c on c.id = p.primary_category_id "
        "left join lateral ("
        f"  select min(case when {_VARIANT_SALE_ON} then v.sale_price else v.price end) as price"
        "  from product_variants v"
        "  where v.product_id = p.id and v.business_id = p.business_id"
        ") vp on true "
        f"where {' and '.join(conds)} order by p.name limit {SAFETY_CAP + 1}",
        *params,
    )
    if len(rows) > SAFETY_CAP:
        # Un lot incomplet e mai rau decat niciun lot: omul eticheteaza ce vede si crede ca a vazut
        # tot. Oprim, nu livram pe jumatate.
        raise SystemExit(
            f"NX-203: filtrul a intors peste {SAFETY_CAP} candidati ({len(rows)}). Lotul ar fi "
            f"incomplet, iar omul n-ar avea cum sa afle. Strange filtrul sau ridica plafonul "
            f"DELIBERAT. spec={spec}"
        )
    return [(r["id"], r["name"]) for r in rows], False


def _load_queries() -> tuple[list, pathlib.Path]:
    """Lotul de procesat: din `--filters <fisier>` daca e dat, altfel lotul 1 hardcodat.

    Filtrele stau intr-un fisier SEPARAT de cod tocmai ca sa poata fi contestate unul cate unul la
    review — sunt opinia mea exprimata in SQL, nu o masuratoare."""
    argv = sys.argv[1:]
    if "--filters" not in argv:
        return QUERIES, ROOT / "tests" / "golden" / "_qrels_batch1_candidates.json"
    path = pathlib.Path(argv[argv.index("--filters") + 1])
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in data["filters"]:
        spec = dict(f["spec"])
        out.append(
            (f"lot3-{f['n']:02d}", f["query"], "pending", "pending", f.get("note") or "", spec)
        )
    return out, path.with_name(path.name.replace("_filters", "_candidates"))


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    queries, out_path = _load_queries()
    llm = get_llm()
    out = []
    async with tenant_conn(DEMO) as conn:
        for qid, q, prov, cat, dim, spec in queries:
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

            catalog, hit_cap = await catalog_lookup(conn, spec)
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
                    # daca ar fi true, omul NU vede tot — dar acum se opreste inainte
                    "_truncated": hit_cap,
                    # TOTI candidatii, fara `[:N]`: o truncare aici ii ascunde de ochii omului.
                    "candidates": [
                        {"product_id": pid, "name": names.get(pid, "?"), "methods": sorted(m)}
                        for pid, m in ranked
                    ],
                }
            )
            only_cat = sum(1 for _, m in ranked if m == {"catalog"})
            flag = "  TRUNCAT" if hit_cap else ""
            print(
                f"{qid:11} catalog={len(catalog):3d} total={len(ranked):3d} "
                f"doar-catalog={only_cat:3d}{flag}"
            )

    await close_pool()
    path = out_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "catalog_version": CATALOG_VERSION,
                "_method": "candidati propusi; gradul si human_verified se dau de OM",
                "queries": out,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\nscris: {path}")
    return 0


raise SystemExit(asyncio.run(main()))
