"""NX-265 — setul de evaluare nu are voie să se schimbe pe furiș.

Un instrument de măsură care se poate edita fără urmă nu mai e instrument. Dacă cineva adaugă un
produs „corect" la un caz după ce a văzut rezultatul, raportul de mâine compară altceva decât cel de
azi și numește diferența „progres".

Manifestul e amprenta peste (fingerprint, corecte, greșite) în ordine. Testul cere doar să
corespundă — nu judecă dacă etichetele sunt bune, aia e treaba omului.

Se sare dacă setul nu există încă: până la ziua de adnotare, cardul e în lucru, nu stricat.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDSET_DIR = ROOT / "tests" / "golden" / "retrieval_goldset"
CASES = GOLDSET_DIR / "cases.json"
MANIFEST = GOLDSET_DIR / "manifest.json"

MIN_PER_CLASS = 10


def _cases() -> list[dict]:
    if not CASES.exists():
        pytest.skip("setul de evaluare nu e încă adnotat (scripts/goldset_annotate.py)")
    return json.loads(CASES.read_text(encoding="utf-8")).get("cases", [])


def _digest(cases: list[dict]) -> str:
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda c: c["fingerprint"]):
        digest.update(case["fingerprint"].encode())
        digest.update(",".join(sorted(case.get("correct", []))).encode())
        digest.update(",".join(sorted(case.get("wrong", []))).encode())
    return digest.hexdigest()


def test_manifestul_corespunde_continutului() -> None:
    cases = _cases()
    assert MANIFEST.exists(), "setul există fără manifest — regenerează cu goldset_annotate.py"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert _digest(cases) == manifest.get("sha256"), (
        "setul a fost modificat fără regenerarea manifestului. Un instrument de măsură editabil în "
        "tăcere nu mai măsoară nimic."
    )
    assert manifest.get("cases") == len(cases)


def test_fiecare_caz_are_verdict_explicit() -> None:
    """Un caz fără verdict nu e o ratare, e o lipsă. Trebuie să se vadă ca lipsă."""
    cases = _cases()
    orphans = [
        c["query"]
        for c in cases
        if not c.get("correct") and not c.get("expect_empty") and not c.get("wrong")
    ]
    # Nu e eșec: adnotarea poate fi în curs. Dar trebuie NUMĂRAT, nu ignorat.
    if orphans:
        pytest.skip(f"{len(orphans)} cazuri încă nejudecate din {len(cases)}")


def test_clasele_au_esantion_suficient_sau_sunt_declarate_insuficiente() -> None:
    """Sub pragul de eșantion, o clasă nu poate produce o cifră care pare rezultat."""
    cases = _cases()
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["class"]] = counts.get(case["class"], 0) + 1
    thin = {cls: n for cls, n in counts.items() if n < MIN_PER_CLASS}
    if thin:
        pytest.skip(f"clase sub pragul de {MIN_PER_CLASS}: {thin} (raportul le dă INSUFFICIENT)")
