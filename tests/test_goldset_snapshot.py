"""NX-265 — a doua direcție de drift: catalogul se mișcă sub set.

`test_goldset_manifest` apără setul de editare. Testele astea apără interpretarea cifrei: un raport
care nu poate distinge „motorul s-a înrăutățit" de „raftul s-a schimbat" produce concluzii false cu
aceeași încredere ca unele adevărate.

Totul e PUR (dataclass + comparație), deci rulează în CI fără bază de date. Citirea din DB e o
singură interogare read-only, verificată separat pe baza live.
"""

from __future__ import annotations

import json
import pathlib

from src.evals.retrieval.snapshot import CatalogSnapshot, compare

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "scripts" / "goldset_report.py"
ANNOTATE = ROOT / "scripts" / "goldset_annotate.py"
MANIFEST = ROOT / "tests" / "golden" / "retrieval_goldset" / "manifest.json"

_NOW = CatalogSnapshot(products_active=2758, max_synced_at="2026-08-27 18:23:47+00")


def test_amprenta_lipsa_e_necunoscut_nu_neschimbat():
    """Verdictul are TREI valori. Un manifest sigilat înainte ca amprenta să existe nu înseamnă „nu
    s-a schimbat nimic" — înseamnă că nu se poate ști, iar confuzia dintre cele două e chiar falsa
    liniște pe care instrumentul o combate (aceeași disciplină ca `NOT-READY` ≠ `FAIL` la
    NX-238)."""
    assert compare(None, _NOW)["verdict"] == "unknown"


def test_acelasi_catalog_nu_produce_drift():
    assert compare(_NOW, _NOW) == {"verdict": "same"}


def test_driftul_spune_CE_s_a_miscat_nu_doar_CA_s_a_miscat():
    """Un „da/nu" ar trimite cititorul să caute singur. Diferența numită („2.758 → 3.050") explică
    de una singură de ce s-a mișcat cifra, fără nicio investigație."""
    sealed = CatalogSnapshot(products_active=3050, max_synced_at=_NOW.max_synced_at)
    out = compare(sealed, _NOW)
    assert out["verdict"] == "drifted"
    assert out["changed"] == {"products_active": {"sealed": 3050, "current": 2758}}
    assert "max_synced_at" not in out["changed"], "ce n-a mișcat nu se raportează ca mișcat"


def test_manifest_stricat_nu_devine_o_amprenta_plauzibila():
    """Fail-closed pe formă: mai bine `unknown` decât o amprentă inventată dintr-un JSON stricat,
    care ar compara două lucruri diferite și ar numi diferența rezultat."""
    for bad in (None, [], {"products_active": "multe"}, {"max_synced_at": "azi"}, "2758"):
        assert CatalogSnapshot.from_dict(bad) is None


def test_amprenta_face_dus_intors_prin_json():
    """Manifestul e JSON pe disc: dacă serializarea și citirea nu sunt inverse, driftul ar apărea
    din senin la prima recitire, iar semnalul ar deveni zgomot pe care nimeni nu-l mai citește."""
    assert CatalogSnapshot.from_dict(json.loads(json.dumps(_NOW.as_dict()))) == _NOW


def test_raportul_nu_pune_ceasul_in_artefact():
    """Cardul cere `measured_at` ȘI rulări bit-identice. Un timestamp proaspăt în raport le-ar face
    imposibil de îndeplinit pe amândouă, deci raportul citește momentul JUDECĂȚII din manifest."""
    source = REPORT.read_text(encoding="utf-8")
    assert '"goldset_measured_at": _manifest_field("measured_at")' in source
    assert "datetime.now" not in source, "raportul trebuie să rămână reproductibil bit-identic"


def test_adnotarea_sigileaza_momentul_si_catalogul():
    """Amprenta se ia la ÎNCEPUTUL sesiunii de adnotare: un import care intră în timpul zilei de
    judecată e exact cazul pe care manifestul trebuie să-l facă vizibil."""
    source = ANNOTATE.read_text(encoding="utf-8")
    assert '"measured_at"' in source and '"catalog_snapshot"' in source
    assert source.index("snapshot = await read_snapshot") < source.index("for index, (cls, entry)")


def test_manifestul_curent_e_citibil_oricum_ar_fi():
    """Manifestul din repo e cel de dinaintea adnotării (0 cazuri). Nu trebuie să fie valid ca
    amprentă — trebuie doar să nu arunce, ca poarta să spună „necunoscut", nu să crape."""
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert compare(CatalogSnapshot.from_dict(raw.get("catalog_snapshot")), _NOW)["verdict"] in (
        "unknown",
        "same",
        "drifted",
    )
