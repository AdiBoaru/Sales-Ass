"""NX-203 — revizuire ADVERSARIALĂ a etichetelor, cu altă metodă decât cea care le-a produs.

O a doua trecere care re-citește aceleași nume nu prinde nimic: ar repeta exact judecata care a
produs eroarea. Revizuirea de aici folosește o sursă pe care etichetatorul NU a văzut-o — atributele
STRUCTURATE ale catalogului (`attributes.product_type`, `attributes.key_ingredients`) — și confruntă
fiecare notă cu ele.

Asta contează fiindcă etichetarea s-a făcut pe numele trunchiat la 76 de caractere. Numele SOLE au
191 de caractere în medie și poartă o coadă de marketing, deci ingredientul cerut putea fi ORICUM
dincolo de tăietură: o notă de 3 pusă pe „pare că are" e indistinctă de una pusă pe „chiar are".

Trei verificări, toate independente de cum s-a etichetat:

  `type_mismatch`  — notă ≥2 pe un produs al cărui `product_type` DIFERĂ de tipul cerut de familie.
                     „Relevant" pentru alt tip de produs e exact defectul pe care corpusul există
                     să-l măsoare; dacă e în ETICHETE, corpusul îl consfințește.
  `ingredient_unsupported` — notă de 3 într-o familie cu ingredient cerut, pe un produs al cărui
                     `key_ingredients` nu-l conține. 3 înseamnă „exact ce a cerut", inclusiv
                     ingredientul.
  `flat_family`    — toate notele familiei sunt egale. Nu e o eroare de etichetă, e o familie care
                     nu DISCRIMINEAZĂ: orice ordine a rezultatelor primește același scor, deci nu
                     poate deosebi un motor bun de unul prost.

Verdictele NU se aplică automat. Un `type_mismatch` poate fi corect (`product_type` acoperă 75,7%
din catalog, iar absența nu e nepotrivire), iar `UNKNOWN` nu e `MISMATCH`. Scriptul RAPORTEAZĂ; ce
se schimbă trece prin etichetare, cu autorul înregistrat.

    python scripts/nx203_review.py --business <uuid>
    python scripts/nx203_review.py --business <uuid> --write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from src.catalog.product_type import fold  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DATA_DIR = ROOT / "tests" / "golden" / "nx203"
REPORT = ROOT / "reports" / "nx203-review.md"
OUT = DATA_DIR / "review_findings.json"

#: De la ce notă în sus produsul e declarat un răspuns la cerere. Sub ea (marginal) nepotrivirea de
#: tip e acceptabilă: „înrudit, dar alt subtip" e chiar definiția lui 1.
_ANSWERS = 2


async def _attributes(business_id: str, ids: set[str]) -> dict[str, dict]:
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        rows = await conn.fetch(
            """select p.id::text as id,
                      p.name,
                      p.attributes -> 'product_type' as ptype,
                      p.attributes -> 'key_ingredients' as ingredients
                 from products p
                where p.business_id = $1 and p.id = any($2::uuid[])""",
            business_id,
            list(ids),
        )
    await close_pool()
    out: dict[str, dict] = {}
    for r in rows:
        ing = json.loads(r["ingredients"]) if r["ingredients"] else []
        out[r["id"]] = {
            "name": r["name"],
            "product_type": json.loads(r["ptype"]) if r["ptype"] else None,
            "key_ingredients": [fold(str(x)) for x in ing] if isinstance(ing, list) else [],
        }
    return out


def _constraints(family: dict) -> tuple[str | None, list[str]]:
    ptype = None
    ingredients: list[str] = []
    for c in family.get("hard_constraints") or []:
        if c["facet"] == "product_type":
            ptype = str(c["value"])
        elif c["facet"] == "key_ingredients":
            ingredients.append(fold(str(c["value"])))
    return ptype, ingredients


def review(judgments: dict, families: dict, attrs: dict) -> list[dict]:
    findings: list[dict] = []
    for fid, judged in judgments.items():
        family = families.get(fid)
        if family is None:
            continue
        want_type, want_ing = _constraints(family)
        grades = [v for v in judged.values()]
        if len(set(map(str, grades))) == 1:
            findings.append(
                {
                    "family_id": fid,
                    "kind": "flat_family",
                    "detail": f"toate cele {len(grades)} note sunt {grades[0]!r}",
                    "query": family["queries"][0],
                }
            )
        for pid, grade in judged.items():
            if grade == "forbidden":
                continue
            a = attrs.get(pid)
            if a is None:
                continue
            got_type = a["product_type"]
            # `UNKNOWN` nu e `MISMATCH`: fațeta acoperă 75,7% din catalog, iar absența ei înseamnă
            # „nu știm", nu „alt tip". A raporta absența ca eroare ar îneca semnalul real.
            if int(grade) >= _ANSWERS and want_type and got_type and got_type != want_type:
                findings.append(
                    {
                        "family_id": fid,
                        "kind": "type_mismatch",
                        "product_id": pid,
                        "grade": grade,
                        "detail": f"cerut `{want_type}`, produsul e `{got_type}`",
                        "query": family["queries"][0],
                        "name": (a["name"] or "")[:70],
                    }
                )
            if int(grade) == 3 and want_ing:
                missing = [w for w in want_ing if w not in a["key_ingredients"]]
                if missing and a["key_ingredients"]:
                    findings.append(
                        {
                            "family_id": fid,
                            "kind": "ingredient_unsupported",
                            "product_id": pid,
                            "grade": grade,
                            "detail": f"nota 3 dar `key_ingredients` n-are {missing}",
                            "query": family["queries"][0],
                            "name": (a["name"] or "")[:70],
                        }
                    )
    return findings


def render(findings: list[dict], n_judgments: int, n_families: int) -> str:
    by_kind = Counter(f["kind"] for f in findings)
    lines = [
        "# NX-203 — revizuire adversarială a etichetelor",
        "",
        f"Etichete verificate: **{n_judgments}** pe **{n_families}** familii.",
        "Metoda e alta decât cea care le-a produs: atributele STRUCTURATE ale catalogului",
        "(`product_type`, `key_ingredients`), pe care etichetatorul nu le-a văzut — el a judecat",
        "din numele trunchiat la 76 de caractere, iar numele SOLE au 191 în medie.",
        "",
        "| tip de semnalare | n |",
        "| --- | ---: |",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(by_kind.items())]
    lines += [
        "",
        "`UNKNOWN` nu e `MISMATCH`: produsele fără `product_type` (24,3% din catalog) nu sunt",
        "raportate ca nepotrivire, fiindcă absența înseamnă nu știm.",
        "",
    ]
    # Nepotrivirile se raporteaza pe PERECHI de tipuri, nu ca lista de produse. Lista bruta a iesit
    # de 167 de randuri si arata ca 167 de greseli de etichetare; pe perechi se vede ca sunt 28 de
    # relatii intre chei de tip, iar majoritatea nu-s greseli: `crema de ochi` si `crema contur
    # ochi` sunt acelasi raft sub doua chei, iar un `cushion` E un fond de ten. Diferenta conteaza:
    # in primul caz repari etichetele, in al doilea repari VOCABULARUL.
    pairs: Counter = Counter()
    for f in findings:
        if f["kind"] != "type_mismatch":
            continue
        m = re.match(r"cerut `(.+?)`, produsul e `(.+?)`", f["detail"])
        if m:
            pairs[(m.group(1), m.group(2))] += 1
    if pairs:
        lines += [
            f"## Nepotriviri de tip, pe PERECHI: {len(pairs)} relatii, {sum(pairs.values())} note",
            "",
            "O pereche in care o cheie e prefix al celeilalte (`balsam` /",
            "`balsam de curatare`) sau in care ambele numesc acelasi raft (`crema de ochi` /",
            "`crema contur ochi`) nu e o",
            "eticheta gresita: e VOCABULARUL care trateaza ca disjuncte doua chei ale aceluiasi",
            "lucru. Constrangerea dura pe una o exclude pe cealalta, si asta se vede direct in",
            "baseline: regimul `with_constraints` pierde recall (0,574 la 0,475) tocmai fiindca",
            "filtreaza dur pe o cheie care are frate.",
            "",
            "| note | cerut | produsul e | relatie |",
            "| ---: | --- | --- | --- |",
        ]
        for (want, got), n in pairs.most_common():
            rel = "ierarhie" if (want in got or got in want) else ""
            lines.append(f"| {n} | `{want}` | `{got}` | {rel} |")
        lines.append("")

    for kind in ("ingredient_unsupported", "flat_family"):
        items = [f for f in findings if f["kind"] == kind]
        if not items:
            continue
        lines += [f"## `{kind}` — {len(items)}", ""]
        for f in items[:20]:
            lines.append(f"- „{f['query'][:58]}” · {f['detail']}")
            if f.get("name"):
                lines.append(f"  ↳ {f['name']}")
        if len(items) > 20:
            lines.append(f"- … încă {len(items) - 20}")
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--business", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    state = json.loads((DATA_DIR / "judgments.json").read_text(encoding="utf-8"))
    families = {
        f["family_id"]: f
        for f in json.loads((DATA_DIR / "families_draft.json").read_text(encoding="utf-8"))[
            "families"
        ]
    }
    judgments = state["judgments"]
    ids = {pid for d in judgments.values() for pid in d}
    attrs = await _attributes(args.business, ids)

    findings = review(judgments, families, attrs)
    n = sum(len(d) for d in judgments.values())
    text = render(findings, n, len(judgments))
    print(text)

    if args.write:
        OUT.write_text(json.dumps({"findings": findings}, ensure_ascii=False, indent=1), "utf-8")
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"Scris: {OUT.relative_to(ROOT)} · {REPORT.relative_to(ROOT)}")
    else:
        print("(dry-run — nimic scris; adaugă --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
