"""NX-203 — manifestul şi qrels-ul nu au voie să se contrazică.

De ce e test, nu instrucţiune într-un README. Reconcilierea a fost sărită o dată deja: 8 query-uri
scrise la lotul 3 au rămas în manifest ca `eligible`, cu `family_id` DIFERIT de cel din qrels.
Nimic nu s-a rupt vizibil — numărătoarea „cât mai avem" era doar umflată. Efectul real ar fi apărut
mult mai târziu: aceeaşi întrebare, etichetată a doua oară sub altă familie, ar fi cântărit dublu
în scorul headline. Exact distorsiunea pe care agregarea pe familie o previne, apărută însă în
DATE, unde codul n-o vede.

O regulă care depinde de „ţine minte să rulezi scriptul" e o regulă care se uită. Aici pică suita.
Reparaţie: `PYTHONPATH=. python scripts/nx203_reconcile_manifest.py --apply`
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata

MANIFEST = pathlib.Path(__file__).parent / "golden" / "qrels_manifest_v1.json"
QRELS = pathlib.Path(__file__).parent / "golden" / "qrels_confirmed.json"


def _norm(text: str) -> str:
    """Aceeaşi cheie ca în `scripts/nx203_reconcile_manifest.py`: variantele de formă (diacritice,
    punctuaţie, majuscule) sunt aceeaşi întrebare."""
    s = unicodedata.normalize("NFKD", text.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _load():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    qrels = json.loads(QRELS.read_text(encoding="utf-8"))["queries"]
    return manifest, {_norm(q["query"]): q for q in qrels}


def test_no_eligible_entry_is_already_in_qrels():
    manifest, by_text = _load()
    restante = [
        e["text"]
        for e in manifest["entries"]
        if e.get("disposition") == "eligible" and _norm(e["text"]) in by_text
    ]
    assert not restante, (
        f"{len(restante)} intrări marcate `eligible` sunt deja în qrels {restante[:3]} — "
        f"ar fi etichetate a doua oară, sub altă familie. Rulează "
        f"scripts/nx203_reconcile_manifest.py --apply"
    )


def test_manifest_and_qrels_agree_on_identity():
    """O întrebare, o identitate. Două `family_id` pentru acelaşi text înseamnă că benchmarkul o
    poate număra ca două contracte de adevăr diferite."""
    manifest, by_text = _load()
    divergente = [
        (e["text"][:50], e.get("family_id"), by_text[_norm(e["text"])].get("family_id"))
        for e in manifest["entries"]
        if _norm(e["text"]) in by_text
        and e.get("family_id") != by_text[_norm(e["text"])].get("family_id")
    ]
    assert not divergente, f"identitate divergentă manifest vs qrels: {divergente[:3]}"


def test_counts_match_entries():
    """`counts` a fost stale o dată, cu 8 dispoziţii greşite din 17 — iar totalul se potrivea, deci
    o verificare de sumă ar fi trecut."""
    manifest, _ = _load()
    real: dict[str, int] = {}
    for e in manifest["entries"]:
        real[e["disposition"]] = real.get(e["disposition"], 0) + 1
    assert manifest["counts"] == dict(sorted(real.items()))
