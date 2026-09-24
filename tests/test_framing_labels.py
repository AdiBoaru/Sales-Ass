"""NX-325 — încadrarea de rezervă vorbește ca un om: pașii rutinei și etichete cu diacritice.

Turul real `b5d86b1a`: intro-ul modelului a căzut, iar rezerva NX-299 a spus „Ți-am ales ulei de
curatare, masca de fata și crema de fata" (chei de catalog fără diacritice, fără protecția solară,
fiindcă IUNIK SPF are tipul „crema de fata"). Zero DB, zero model.
"""

from __future__ import annotations

import glob
import json
import types
from pathlib import Path

from src.agent import answer_shape
from src.domain.loader import load_domain_pack
from src.domain.pack import DomainPack, FacetSpec
from src.tools.routine_tools import RoutineStepRef, RoutineView
from src.worker import compose
from tests.test_compose import _ctx, _settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "framing": {"ro": "Ți-am ales {types}."},
    "routine_framing": {"ro": "Rutina ta, în ordine: {steps}."},
    "list_glue": {"ro": " și "},
}
PACK = DomainPack(
    vertical="ecommerce",
    answer_shape_templates=TEMPLATES,
    comparison_facets=(FacetSpec(key="skin_type", value_labels={"dry": {"ro": "ten uscat"}}),),
    facet_labels=(
        FacetSpec(key="skin_type", value_labels={"dry": {"ro": "ten uscat"}}),
        FacetSpec(
            key="product_type",
            in_comparison=False,
            value_labels={
                "ulei de curatare": {"ro": "ulei de curățare"},
                "masca de fata": {"ro": "mască de față"},
                "crema de fata": {"ro": "cremă de față"},
            },
        ),
    ),
)

#: Turul real: patru pași acoperiți, cu etichetele localizate din pachetul SOLE.
_STEPS = (
    ("A", 1, "curatare", "Curățare", "ulei de curatare"),
    ("I", 4, "tratament", "Tratament", "masca de fata"),
    ("S", 5, "hidratare", "Hidratare", "crema de fata"),
    ("U", 6, "protectie", "Protecție solară", "crema de fata"),
)
_RETRIEVED = [
    {
        "id": pid,
        "name": f"Produs {pid}",
        "price": 30.0,
        "availability": "in_stock",
        "attributes": {"product_type": ptype},
    }
    for pid, _, _, _, ptype in _STEPS
]


def _routine():
    return RoutineView(
        family="fata",
        steps=tuple(
            RoutineStepRef(position=pos, step=step, label=label, product_id=pid)
            for pid, pos, step, label, _ in _STEPS
        ),
    )


def _run(monkeypatch, *, routine=True, labels=True, intro=""):
    monkeypatch.setattr(
        compose,
        "get_settings",
        lambda: _settings(answer_shape_enabled=True, framing_labels_enabled=labels),
    )
    ctx = _ctx()
    ctx.business = types.SimpleNamespace(vertical="beauty", domain_pack=PACK)
    events: list = []
    ctx.emit = lambda kind, **props: events.append((kind, props))
    if routine:
        ctx.routine = _routine()
    j = {
        "intro": intro,
        "items": [{"product_id": pid, "fit_clause": "potrivit"} for pid, *_ in _STEPS],
        "education": "",
        "suggestions": [],
    }
    return compose.assemble(ctx, j, [dict(p) for p in _RETRIEVED]), events


# ── Rutina ────────────────────────────────────────────────────────────────────────────────────


def test_turul_real_rezerva_numeste_pasii(monkeypatch):
    rich, events = _run(monkeypatch)
    assert rich.intro == "Rutina ta, în ordine: Curățare, Tratament, Hidratare și Protecție solară."
    filled = [p for k, p in events if k == "answer_shape_filled"]
    assert filled and filled[0]["source"] == "routine"


def test_flag_stins_fraza_de_dinainte(monkeypatch):
    rich, _ = _run(monkeypatch, labels=False)
    assert rich.intro == "Ți-am ales ulei de curatare, masca de fata și crema de fata."


def test_intro_bun_al_modelului_ramane(monkeypatch):
    rich, _ = _run(monkeypatch, intro="Pentru pielea uscată, pornește cu uleiul.")
    assert rich.intro.startswith("Pentru pielea uscată")


# ── În afara rutinei: etichetele tipurilor ────────────────────────────────────────────────────


def test_fara_rutina_tipurile_au_diacritice(monkeypatch):
    rich, events = _run(monkeypatch, routine=False)
    assert rich.intro == "Ți-am ales ulei de curățare, mască de față și cremă de față."
    assert not [p for k, p in events if k == "framing_label_missing"]


def test_eticheta_lipsa_cade_pe_cheie_si_se_numara():
    labels, missing = answer_shape.type_labels(PACK, "ro", ["crema de fata", "tonic"])
    assert labels == ["cremă de față", "tonic"] and missing == 1


def test_limba_fara_eticheta_nu_cade_pe_romana():
    labels, missing = answer_shape.type_labels(PACK, "en", ["crema de fata"])
    assert labels == ["crema de fata"] and missing == 1


def test_sablon_absent_rezerva_de_dinainte():
    assert answer_shape.routine_framing_text(DomainPack(vertical="x"), "ro", ["Curățare"]) is None


# ── Pachetul: un singur proprietar al etichetelor ─────────────────────────────────────────────


def test_intrarea_fara_comparatie_nu_intra_in_tabel():
    pack = load_domain_pack(
        types.SimpleNamespace(
            vertical="ecommerce",
            settings={
                "domain_pack": {
                    "comparison_facets": [
                        {"key": "skin_type", "value_labels": {"dry": {"ro": "ten uscat"}}},
                        {
                            "key": "product_type",
                            "in_comparison": False,
                            "value_labels": {"crema de fata": {"ro": "cremă de față"}},
                        },
                    ]
                }
            },
        )
    )
    assert [f.key for f in pack.comparison_facets] == ["skin_type"]
    assert pack.value_label("product_type", "crema de fata", "ro") == "cremă de față"
    assert pack.value_label("skin_type", "dry", "ro") == "ten uscat"


def test_pachetul_sole_are_etichete_pentru_toate_tipurile():
    raw = json.loads((ROOT / "db/seed/domain_pack_sole_ro.json").read_text(encoding="utf-8"))
    pack = load_domain_pack(
        types.SimpleNamespace(vertical="ecommerce", settings={"domain_pack": raw})
    )
    ptype = next(f for f in pack.facets if f.key == "product_type")
    missing = [v for v in ptype.values if not pack.value_label("product_type", v, "ro")]
    assert missing == []
    assert "product_type" not in [f.key for f in pack.comparison_facets]


# ── Vocea: șabloanele noi respectă P13 ────────────────────────────────────────────────────────


def test_sabloanele_de_forma_fara_liniuta_si_punct_si_virgula():
    for path in glob.glob(str(ROOT / "src/domain/defaults/*.json")):
        templates = json.loads(Path(path).read_text(encoding="utf-8")).get(
            "answer_shape_templates", {}
        )
        for key in ("framing", "routine_framing"):
            for text in (templates.get(key) or {}).values():
                assert ";" not in text and " — " not in text and " – " not in text, (path, key)
