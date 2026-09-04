"""NX-265 pasul 2 — judecata umană, cu candidați propuși de sistemul REAL.

Singura parte care nu se poate automatiza. Un evaluator care e tot un model măsoară acordul cu el
însuși, nu adevărul — de aia aici stă un om.

Ce face unealta ca ziua aia să fie doar bifat:

* rulează calea de căutare REALĂ pentru fiecare frază și arată primele N produse;
* primește etichete scurte: `c` = corect, `g` = greșit (contrazice o cerință exprimată), restul
  rămân neutre. **`g` e cea mai valoroasă**: măsoară exact clasa de defect din NX-257 — un adevăr
  nepotrivit, pe care nicio poartă de adevăr nu-l prinde;
* permite adăugarea unui produs care NU apare în listă, prin căutare pe nume. Fără asta, setul ar
  canoniza exact ratările de azi: un produs corect invizibil ar deveni „nu există";
* **salvează după fiecare frază.** O zi de muncă nu se poate pierde la o întrerupere;
* marchează automat `fara_rezultat` frazele care întorc zero — măsurat, nu presupus.

Candidații vin din brațul LEXICAL, nu din calea hibridă completă, și e o alegere: adnotarea nu are
voie să consume credite la fiecare reluare, iar un produs pe care numai brațul vectorial îl găsește
se adaugă oricum manual cu `+`. Măsurătoarea pe calea completă e treaba lui `goldset_report.py`,
unde se rulează o dată, nu de zeci de ori.

    python scripts/goldset_annotate.py --business <uuid>            # reia de unde ai rămas
    python scripts/goldset_annotate.py --business <uuid> --review    # revede ce ai judecat deja
"""

from __future__ import annotations

import argparse
import asyncio
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

CANDIDATES = ROOT / "reports" / "goldset-candidates.json"
GOLDSET_DIR = ROOT / "tests" / "golden" / "retrieval_goldset"
CASES = GOLDSET_DIR / "cases.json"

TOP_N = 12


def _load_cases() -> dict[str, dict]:
    if not CASES.exists():
        return {}
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return {c["fingerprint"]: c for c in data.get("cases", [])}


def _save_cases(cases: dict[str, dict], business_id: str, locale: str) -> None:
    """Scriere ATOMICĂ după fiecare frază: fișier temporar, apoi `replace`.

    Un `write_text` întrerupt la mijloc ar lăsa un JSON trunchiat, adică exact ziua de muncă pe care
    salvarea incrementală trebuia s-o apere."""
    GOLDSET_DIR.mkdir(parents=True, exist_ok=True)
    ordered = sorted(cases.values(), key=lambda c: (c["class"], c["fingerprint"]))
    payload = {
        "_provenance": {
            "business_id": business_id,
            "locale": locale,
            "judged_by": "human",
            "_note": (
                "Judecată binară + `gresit`. Fără grade: setul e mic, iar o metrică sofisticată pe "
                "date sărace dă o falsă precizie."
            ),
        },
        "cases": ordered,
    }
    tmp = CASES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CASES)


def _write_manifest(cases: dict[str, dict]) -> None:
    """Amprentă peste cazurile ORDONATE. Dacă setul se schimbă sub raport, se vede."""
    digest = hashlib.sha256()
    for case in sorted(cases.values(), key=lambda c: c["fingerprint"]):
        digest.update(case["fingerprint"].encode())
        digest.update(",".join(sorted(case.get("correct", []))).encode())
        digest.update(",".join(sorted(case.get("wrong", []))).encode())
    (GOLDSET_DIR / "manifest.json").write_text(
        json.dumps(
            {
                "cases": len(cases),
                "sha256": digest.hexdigest(),
                "_note": (
                    "Amprenta acoperă (fingerprint, corecte, greșite) în ordine. "
                    "Conținut modificat fără regenerare ⇒ test roșu."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        return "q"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--review", action="store_true", help="revede cazurile deja judecate")
    args = ap.parse_args()

    if not CANDIDATES.exists():
        print(f"lipsesc candidații ({CANDIDATES}). Rulează întâi scripts/goldset_sample.py")
        return 2
    candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    cases = _load_cases()

    queue: list[tuple[str, dict]] = []
    for cls, data in candidates["classes"].items():
        for entry in data["queries"]:
            done = entry["fingerprint"] in cases
            if done != args.review:
                continue
            queue.append((cls, entry))

    if not queue:
        print("nimic de judecat" if not args.review else "niciun caz judecat încă")
        return 0

    print(
        f"{len(queue)} de judecat\n"
        "  0 2 3      produsele corecte, după index\n"
        "  g:1        produsul 1 e GREȘIT (contrazice o cerință din frază)\n"
        "  +<text>    caută pe nume și adaugă un produs care nu apare în listă\n"
        "  enter      niciunul corect · s sari · q ieși (progresul e salvat)\n"
    )

    try:
        async with tenant_conn(args.business) as conn:
            for index, (cls, entry) in enumerate(queue, 1):
                query = entry["query"]
                rows = await search_products_lexical(
                    conn,
                    args.business,
                    query,
                    sort_mode="relevance",
                    locale=args.locale,
                    pool=TOP_N,
                )
                print(f"\n[{index}/{len(queue)}] ({cls})  {query}")
                if not rows:
                    print("   ZERO rezultate — se marchează automat `fara_rezultat`")
                    cases[entry["fingerprint"]] = {
                        **entry,
                        "class": "fara_rezultat",
                        "original_class": cls,
                        "correct": [],
                        "wrong": [],
                        "expect_empty": True,
                    }
                    _save_cases(cases, args.business, args.locale)
                    continue
                for i, row in enumerate(rows):
                    price = row.get("price")
                    print(f"   {i:>2}. {str(row.get('name'))[:78]}  ({price})")

                correct: list[str] = []
                wrong: list[str] = []
                quit_now = False
                while True:
                    raw = _prompt("   > ")
                    if raw.lower() == "q":
                        quit_now = True
                        break
                    if raw.lower() == "s":
                        correct, wrong = [], []
                        break
                    # `+` caută pe nume și EXTINDE lista. Fără el, setul ar canoniza exact ratările
                    # de azi: un produs corect pe care căutarea nu-l găsește ar deveni „nu există",
                    # iar metrica n-ar mai putea crește niciodată peste ce găsim acum.
                    if raw.startswith("+"):
                        extra = await search_products_lexical(
                            conn,
                            args.business,
                            raw[1:].strip(),
                            sort_mode="relevance",
                            locale=args.locale,
                            pool=TOP_N,
                        )
                        new = [r for r in extra if r["id"] not in {x["id"] for x in rows}]
                        if not new:
                            print("      nimic nou pentru textul ăsta")
                            continue
                        for row in new:
                            rows.append(row)
                            print(f"   {len(rows) - 1:>2}. {str(row.get('name'))[:78]}  (adăugat)")
                        continue
                    for token in raw.split():
                        negative = token.startswith("g:")
                        token = token[2:] if negative else token
                        if token.isdigit() and int(token) < len(rows):
                            (wrong if negative else correct).append(rows[int(token)]["id"])
                    break
                if quit_now:
                    break
                cases[entry["fingerprint"]] = {
                    **entry,
                    "class": cls,
                    "correct": correct,
                    "wrong": wrong,
                    "expect_empty": False,
                }
                _save_cases(cases, args.business, args.locale)
    finally:
        await close_pool()

    _save_cases(cases, args.business, args.locale)
    _write_manifest(cases)
    judged = sum(1 for c in cases.values() if c.get("correct") or c.get("expect_empty"))
    print(f"\nsalvat: {CASES}")
    print(f"cazuri: {len(cases)} · cu verdict utilizabil: {judged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
