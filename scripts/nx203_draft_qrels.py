"""NX-203 — DRAFT de qrels dintr-un lot de candidati. `human_verified=false`, fara exceptie.

Ce e asta si ce NU e. Un draft e o PROPUNERE de note, ca omul sa aprobe sau sa corecteze, nu
adevar. Steagul `human_verified` ramane fals pana cand omul confirma explicit; niciun script nu-l
ridica.

REGULA DE NOTARE, NECIRCULARA. Nota NU depinde de cate metode au gasit produsul. Daca acordul cu
retrieval-ul ar ridica nota, benchmarkul ar rasplati exact ce sistemul gaseste deja. Nota vine din
PROPRIETATILE produsului fata de constrangerile query-ului, evaluate cu `constraints.evaluate`:

  3 (ideal)    — satisface TOATE constrangerile hard, niciuna necunoscuta
  2 (relevant) — satisface constrangerile care conteaza, dar are cel putin una `unknown`
                 (atribut nedeclarat: potrivire probabila, nu demonstrata)
  1 (marginal) — nu incalca nimic, dar nici nu satisface constrangerea principala (categoria)
  omis        — incalca EXPLICIT o constrangere. Evaluatorul trateaza produsele nejudecate ca
                 gain 0, deci o eticheta de zero n-ar adauga semnal metric.

`price` are un tratament aparte: pretul e mereu cunoscut, deci un produs peste prag INCALCA, si e
omis — nu coborat la 1.

Identitatea (family_id / split_group_id) vine din `_family_decisions.json`, nu se deriva aici:
verdictele sunt ale omului si trebuie sa fie auditabile separat de cod.

    PYTHONPATH=. python scripts/nx203_draft_qrels.py lot4 lot5a
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
GOLDEN = ROOT / "tests" / "golden"

from src.evals.retrieval.catalog import load_catalog  # noqa: E402
from src.evals.retrieval.constraints import (  # noqa: E402
    _TOLERANT_LISTS,
    SATISFIES,
    UNKNOWN,
    VIOLATES,
    evaluate,
)


def _canonical_ingredient(term: str, catalog) -> str | None:
    """Ortografia EXACTA din catalog pentru un termen de ingredient.

    Filtrul SQL cauta cu LIKE fara diacritice („niacinamid"), dar `constraints.evaluate` compara
    apartenenta EXACTA la lista. Fara traducerea asta, „niacinamid" nu se potriveste niciodata cu
    „niacinamidă", deci fiecare produs iesea `unknown` si nota maxima era 2 — un gold in care
    NIMIC nu poate fi ideal, la toate query-urile pe ingredient."""
    import unicodedata

    def fold(x: str) -> str:
        x = unicodedata.normalize("NFKD", x.lower())
        return "".join(c for c in x if not unicodedata.combining(c))

    needle = fold(term)
    for product in catalog.products.values():
        for value in product.get("attributes", {}).get("key_ingredients") or []:
            if needle in fold(value):
                return value
    return None


#: Traducerea spec-ului de filtru in constrangeri hard de qrels. Doar ce e MODELABIL: calificatorii
#: vagi („ieftine", „sa lumineze") raman in nota query-ului, nu devin constrangeri — un prag
#: inventat ar deveni adevar de gold fara ca userul sa-l fi spus.
def _constraints(spec: dict, catalog) -> list[dict]:
    out: list[dict] = []
    if v := spec.get("cat"):
        out.append({"facet": "category", "op": "eq" if isinstance(v, str) else "in", "value": v})
    if v := spec.get("cat_any"):
        out.append({"facet": "category", "op": "in", "value": list(v)})
    if v := spec.get("suitable_for"):
        out.append({"facet": "suitable_for", "op": "contains", "value": v})
    if v := spec.get("concern"):
        out.append({"facet": "concerns", "op": "contains", "value": v})
    if kv := spec.get("attr_eq"):
        facet, value = kv[0], kv[1]
        if facet == "spf":
            # `gte`, nu `eq`, si INT, nu string — ca in contractul deja confirmat (q-con-01).
            # „SPF 50" inseamna „cel putin 50": un SPF 50+ satisface cererea. Cu `eq` pe string,
            # aceeasi intentie primea alt contract in qrels si fuziunea de familie devenea
            # imposibila din motive de formatare, nu de sens.
            out.append(
                {
                    "facet": "spf",
                    "op": "gte",
                    "value": int(value),
                    "unknown_is_violation": True,
                    "note": "prag numeric de SIGURANTA: SPF necunoscut NU satisface o cerinta de 50",
                }
            )
        else:
            out.append({"facet": facet, "op": "eq", "value": value})
    if v := spec.get("price_max"):
        out.append({"facet": "price", "op": "lte", "value": v, "unit": "RON"})
    if v := spec.get("ingredient"):
        canonical = _canonical_ingredient(v, catalog)
        c = {"facet": "key_ingredients", "op": "contains", "value": canonical or v}
        if canonical and canonical != v:
            c["note"] = f"termen din filtru: {v} -> ortografia din catalog: {canonical}"
        out.append(c)
    return out


def _grade(product: dict, constraints: list[dict]) -> int | None:
    """Nota propusa, sau None daca produsul e omis (incalca ceva).

    Distinctia care conteaza la note (si pe care `evaluate` nu o face, fiindca pentru ea ambele
    sunt `unknown`): un atribut ABSENT inseamna potrivire nedemonstrata, dar un atribut PREZENT
    care nu contine valoarea ceruta e o nepotrivire declarata. Un sampon cu
    `suitable_for=['oily']` nu e „relevant 2" pentru „par vopsit" doar fiindca lista nu spune
    explicit ca NU e."""
    states = [evaluate(product, c) for c in constraints]
    if VIOLATES in states:
        return None
    if not states:
        return 2
    if all(s == SATISFIES for s in states):
        return 3
    attrs = product.get("attributes") or {}
    for state, c in zip(states, constraints):
        if state != UNKNOWN or c["facet"] not in _TOLERANT_LISTS:
            continue
        if attrs.get(c["facet"]):  # lista exista, dar nu contine valoarea ceruta
            return 1
    # categoria e constrangerea principala: fara ea satisfacuta, produsul e cel mult marginal
    cat = next((s for s, c in zip(states, constraints) if c["facet"] == "category"), SATISFIES)
    if cat != SATISFIES:
        return 1
    return 2


def _provenance(queries: list[dict]) -> dict[str, str]:
    """Provenienta vine din SURSA din manifest (trafic real vs qa_suite), nu din nota mea.

    Prima varianta o citea din campul de nota al filtrului — deci un query real ajungea „synthetic"
    daca nu scrisesem cuvantul „real" in comentariu. Provenienta e un invariant al setului: pe ea
    se sprijina cerinta de „minim query-uri reale per categorie"."""
    import re
    import unicodedata

    def fold(x: str) -> str:
        x = unicodedata.normalize("NFKD", x.lower())
        x = "".join(c for c in x if not unicodedata.combining(c))
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", x).split())

    manifest = json.loads((GOLDEN / "qrels_manifest_v1.json").read_text(encoding="utf-8"))
    by_text = {fold(e["text"]): e["source"] for e in manifest["entries"]}
    out = {}
    for q in queries:
        source = by_text.get(fold(q["query"]))
        out[q["id"]] = "real_sanitized" if source == "real_traffic" else "synthetic"
    return out


def _canonicalize_families(queries: list[dict], catalog) -> list[dict]:
    """O familie = UN contract canonic: aceleasi `hard_constraints`, aceleasi `judgments`, aceleasi
    exceptii, la toate variantele.

    De ce nu e optional. Metrica mediaza IN familie; daca variantele au gold-uri diferite, media nu
    mai e „acelasi contract masurat pe mai multe formulari", ci o amestecatura. Iar diferentele de
    aici NU sunt de contract — sunt de OBSERVATIE: fiecare formulare a scos alti candidati din
    lexical/semantic. A le lasa in qrels ar transforma un artefact de colectare in adevar.

    Pool-ul devine REUNIUNEA candidatilor tuturor variantelor, renotat identic. Daca doua variante
    au constrangeri DIFERITE, nu se unifica nimic: fuziunea e invalida si se raporteaza, fiindca
    atunci chiar sunt doua contracte."""
    by_family: dict[str, list[dict]] = {}
    for q in queries:
        by_family.setdefault(q["family_id"] or q["id"], []).append(q)

    for fam, group in by_family.items():
        if len(group) == 1:
            continue
        contracts = {json.dumps(q["hard_constraints"], sort_keys=True) for q in group}
        if len(contracts) > 1:
            for q in group:
                q["_fuziune_invalida"] = (
                    f"familia {fam} are {len(contracts)} contracte de constrangeri diferite — "
                    f"nu se poate canonicaliza, sunt contracte distincte"
                )
            continue
        pool: dict[str, int] = {}
        for q in group:
            for j in q["judgments"]:
                pool[j["product_id"]] = j["relevance"]
        # renotare din CATALOG, nu din nota cea mai mare observata: nota vine din proprietatile
        # produsului, deci trebuie sa fie aceeasi indiferent care varianta l-a scos la iveala.
        hard = group[0]["hard_constraints"]
        unified = []
        for pid in pool:
            grade = _grade(catalog.products[pid], hard)
            if grade is not None:
                unified.append({"product_id": pid, "relevance": grade})
        unified.sort(key=lambda j: (-j["relevance"], j["product_id"]))
        for q in group:
            adaugate = len(unified) - len(q["judgments"])
            q["judgments"] = [dict(j) for j in unified]
            q["_pool_unificat"] = (
                f"pool comun al familiei {fam} ({len(group)} variante): {len(unified)} produse"
                + (f", +{adaugate} fata de candidatii proprii" if adaugate else "")
            )
    return queries


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from src.db.connection import close_pool, tenant_conn  # noqa: PLC0415

    lots = [a for a in sys.argv[1:] if not a.startswith("--")]
    decisions = json.loads((GOLDEN / "_family_decisions.json").read_text(encoding="utf-8"))
    atribuiri = decisions["atribuiri"]

    async with tenant_conn(DEMO) as conn:
        catalog = await load_catalog(conn, DEMO)
    await close_pool()

    for lot in lots:
        cands = json.loads((GOLDEN / f"_{lot}_candidates.json").read_text(encoding="utf-8"))
        specs = {
            f"{lot}-{f['n']:02d}": f
            for f in json.loads((GOLDEN / f"_{lot}_filters.json").read_text(encoding="utf-8"))[
                "filters"
            ]
        }
        provenance = _provenance(cands["queries"])
        queries = []
        for q in cands["queries"]:
            spec = specs[q["id"]]["spec"]
            hard = _constraints(spec, catalog)
            judgments, omise = [], []
            for c in q["candidates"]:
                product = catalog.products.get(c["product_id"])
                if product is None:
                    omise.append((c["name"], "absent din snapshot"))
                    continue
                grade = _grade(product, hard)
                if grade is None:
                    omise.append((c["name"], "incalca o constrangere"))
                    continue
                judgments.append({"product_id": c["product_id"], "relevance": grade})
            ident = atribuiri.get(q["id"], {})
            queries.append(
                {
                    "id": q["id"],
                    "query": q["query"],
                    "locale": "ro",
                    "provenance": provenance.get(q["id"], "synthetic"),
                    "category": spec.get("cat") if isinstance(spec.get("cat"), str) else None,
                    "human_verified": False,
                    "family_id": ident.get("family_id"),
                    "split_group_id": ident.get("split_group_id"),
                    "catalog_version": catalog.version,
                    "judgments": sorted(
                        judgments, key=lambda j: (-j["relevance"], j["product_id"])
                    ),
                    "forbidden_products": [],
                    "forbidden_rationale": {},
                    "hard_constraints": hard,
                    "_nota_generatorului": specs[q["id"]].get("note", ""),
                    "_omise": [f"{n} ({de_ce})" for n, de_ce in omise],
                }
            )
        queries = _canonicalize_families(queries, catalog)
        out = GOLDEN / f"_{lot}_draft.json"
        out.write_text(
            json.dumps(
                {
                    "_meta": {
                        "ATENTIE": "DRAFT — human_verified=false peste tot. Notele sunt PROPUNERI.",
                        "regula_de_notare": "3=satisface tot · 2=are un atribut nedeclarat · "
                        "1=nu incalca nimic dar nu e din categoria ceruta · omis=incalca explicit",
                        "catalog_fingerprint": catalog.fingerprint,
                    },
                    "schema_version": 1,
                    "business_id": DEMO,
                    "queries": queries,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n=== {lot}: {len(queries)} query-uri -> {out.relative_to(ROOT)}")
        for q in queries:
            g = {3: 0, 2: 0, 1: 0}
            for j in q["judgments"]:
                g[j["relevance"]] += 1
            print(
                f"  {q['id']:10} 3:{g[3]:2} 2:{g[2]:2} 1:{g[1]:2} omise:{len(q['_omise']):2} "
                f"| {q['family_id']}"
            )
    return 0


raise SystemExit(asyncio.run(main()))
