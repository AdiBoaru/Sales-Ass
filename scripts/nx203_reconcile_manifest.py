"""NX-203 — reconciliaza manifestul cu qrels-ul scris. De rulat DUPA fiecare lot.

Problema pe care o rezolva. Triajul manifestului s-a facut o data, inainte de lotul 3. Query-urile
scrise ulterior in `qrels_confirmed.json` au ramas in manifest cu `disposition=eligible`, deci
apareau ca „nefolosite". Doua consecinte, amandoua tacute:

  · numaratoarea „cate mai avem" era umflata;
  · mai grav, aceeasi intrebare avea DOUA identitati — `family_id` din manifest si `family_id` din
    qrels, diferite. Daca ar fi intrat intr-un lot nou, ar fi fost etichetata a doua oara sub alta
    familie, iar benchmarkul ar fi numarat de doua ori acelasi contract de adevar. Exact
    distorsiunea pe care agregarea pe familie exista ca s-o elimine — aparuta insa in DATE, unde
    codul n-o vede.

Ce face. Potriveste pe text NORMALIZAT (fara diacritice, fara punctuatie, minuscule — variantele
de forma sunt aceeasi intrebare) si, pentru fiecare potrivire, marcheaza intrarea din manifest
`already_labeled`, cu id-ul de qrels in motiv si cu identitatea CANONICA preluata din qrels.

Sursa de adevar pentru identitate e QRELS-ul, nu manifestul: acolo `split_group_id` a fost pus
deliberat de om (uneori legand doua familii diferite), pe cand in manifest e derivat automat.

Ce NU face. Nu sterge nimic, nu schimba dispozitii care nu sunt `eligible`, nu atinge qrels-ul si
nu marcheaza nimic `human_verified`.

    PYTHONPATH=. python scripts/nx203_reconcile_manifest.py           # dry-run
    PYTHONPATH=. python scripts/nx203_reconcile_manifest.py --apply
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "golden" / "qrels_manifest_v1.json"
QRELS = ROOT / "tests" / "golden" / "qrels_confirmed.json"


def norm(text: str) -> str:
    """Cheia de potrivire: aceeasi intrebare, indiferent de diacritice/punctuatie/majuscule."""
    s = unicodedata.normalize("NFKD", text.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    qrels = json.loads(QRELS.read_text(encoding="utf-8"))["queries"]
    by_text = {norm(q["query"]): q for q in qrels}

    apply = "--apply" in sys.argv
    schimbate, divergente = [], []
    for e in manifest["entries"]:
        if e.get("disposition") != "eligible":
            continue
        q = by_text.get(norm(e["text"]))
        if q is None:
            continue
        if e.get("family_id") != q.get("family_id"):
            divergente.append((q["id"], e.get("family_id"), q.get("family_id")))
        schimbate.append((q["id"], e["text"]))
        if apply:
            e["disposition"] = "already_labeled"
            e["reason"] = f"deja prezent in qrels_confirmed.json ca {q['id']}"
            # Identitatea canonica vine din QRELS. Fara asta, manifestul ar continua sa poarte o
            # familie concurenta pentru aceeasi intrebare.
            e["family_id"] = q.get("family_id")
            e["split_group_id"] = q.get("split_group_id")

    print(f"intrari `eligible` care sunt DEJA in qrels: {len(schimbate)}")
    for qid, text in schimbate:
        print(f"   {qid:10} {text[:66]}")
    if divergente:
        print(f"\ndintre ele, cu identitate DIVERGENTA (manifest != qrels): {len(divergente)}")
        for qid, fam_m, fam_q in divergente:
            print(f"   {qid:10} manifest={fam_m} -> canonic={fam_q}")

    if apply:
        counts = collections.Counter(e.get("disposition") for e in manifest["entries"])
        manifest["counts"] = dict(sorted(counts.items()))
        manifest["family_count"] = len({e["family_id"] for e in manifest["entries"]})
        manifest["split_group_count"] = len({e["split_group_id"] for e in manifest["entries"]})
        MANIFEST.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ramase = counts.get("eligible", 0)
        print(f"\napplied. `eligible` ramase: {ramase}")
    elif schimbate:
        print("\n(dry-run — ruleaza cu --apply)")
    else:
        print("\nmanifestul e deja reconciliat.")
    return 0


raise SystemExit(main())
