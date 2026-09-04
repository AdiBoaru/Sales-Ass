"""NX-264 — derivarea trebuie să DESCOPERE, nu să repete.

Testul de generalitate nu are nevoie de un catalog sintetic de alt vertical (decizie owner,
2026-09-03: lucrăm doar pe baza reală). Nu e nevoie, fiindcă baza nu e omogenă: catalogul SOLE
conține cinci forme diferite de decizie de cumpărare, măsurate pe rădăcinile de categorie.

| rădăcină     | produse | pe ce se decide cumpărarea |
|--------------|---------|----------------------------|
| `ten`        |   1.461 | nevoie + tip de ten        |
| `machiaj`    |     681 | nuanță + finish            |
| `par`        |     233 | tip de păr                 |
| `corp`       |     163 | nevoie                     |
| `protectie`  |     137 | SPF, număr exact           |

Dacă derivarea dă lui `machiaj` aceleași fațete ca lui `ten`, atunci nu descoperă nimic din date —
repetă o presupunere. Ăsta e testul, și e mai dur decât unul pe un catalog inventat, fiindcă rulează
pe marfa reală.

Verifică ARTEFACTUL (`tests/facet_discovery.json`), nu baza: CI n-are DB, iar un test care ar cere
Postgres ar fi sărit tăcut exact acolo unde contează. Artefactul se regenerează cu
`python scripts/facet_discovery.py --business <uuid>`.
"""

from __future__ import annotations

import itertools
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "tests" / "facet_discovery.json"

# Peste atâta suprapunere, două rădăcini au primit practic același set: derivarea n-a citit
# diferența dintre rafturi. Jaccard, nu număr absolut, ca pragul să nu depindă de cât de multe
# valori propune fiecare rădăcină.
MAX_JACCARD = 0.60

# Fiecare rădăcină mare trebuie să aibă și valori care sunt NUMAI ale ei. Suprapunerea e normală
# („acid" apare peste tot), dar o rădăcină fără nimic propriu n-a fost descrisă, doar numărată.
MIN_EXCLUSIVE_VALUES = 3


def _artifact() -> dict:
    if not ARTIFACT.exists():
        pytest.skip(
            "lipsește tests/facet_discovery.json — rulează "
            "`python scripts/facet_discovery.py --business <uuid>`"
        )
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def _values(root_data: dict) -> set[str]:
    return {v["value"] for v in root_data.get("values", [])}


def test_artefactul_are_provenance() -> None:
    prov = _artifact().get("_provenance") or {}
    for key in ("business_id", "source", "regenerate", "support_band_ratio"):
        assert prov.get(key), f"provenance incomplet: lipsește {key}"


def test_nicio_valoare_nu_descrie_tot_raftul() -> None:
    """Regula care separă o fațetă utilă de una adevărată și inutilă.

    Categoria `Buze` a ieșit la derivarea de nevoi cu 92% acoperire pe „hidratare" — corect, și
    irelevant. O valoare care acoperă aproape toată rădăcina nu ajută pe nimeni să aleagă."""
    art = _artifact()
    limit = (art["_provenance"].get("support_band_ratio") or [0, 0.7])[1]
    offenders = [
        (root, v["value"], v["share"])
        for root, data in art["roots"].items()
        for v in data["values"]
        if v["share"] > limit
    ]
    assert not offenders, f"valori care descriu raftul, nu produsul: {offenders}"


def test_fiecare_radacina_are_valori_proprii() -> None:
    art = _artifact()
    roots = {root: _values(data) for root, data in art["roots"].items()}
    assert len(roots) >= 3, f"prea puține rădăcini judecabile: {sorted(roots)}"
    for root, values in roots.items():
        others: set[str] = set().union(*(v for r, v in roots.items() if r != root))
        exclusive = values - others
        assert len(exclusive) >= MIN_EXCLUSIVE_VALUES, (
            f"rădăcina {root!r} n-a primit nimic propriu ({sorted(exclusive)}) — derivarea "
            "repetă un set generic în loc să citească raftul"
        )


def test_radacinile_nu_primesc_acelasi_set() -> None:
    """Testul de generalitate propriu-zis: `machiaj` nu poate arăta ca `ten`."""
    roots = {root: _values(data) for root, data in _artifact()["roots"].items()}
    too_similar = []
    for (a, va), (b, vb) in itertools.combinations(sorted(roots.items()), 2):
        union = va | vb
        if not union:
            continue
        jaccard = len(va & vb) / len(union)
        if jaccard > MAX_JACCARD:
            too_similar.append((a, b, round(jaccard, 2)))
    assert not too_similar, (
        f"rădăcini cu seturi aproape identice: {too_similar}. Derivarea nu descoperă fațete, "
        "repetă aceleași cuvinte peste rafturi diferite."
    )
