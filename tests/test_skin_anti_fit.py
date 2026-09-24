"""NX-322b — anti-potrivirea: o nevoie `hard` scoate produsele marcate DOAR pentru tipul opus.

Turul real `b5d86b1a`: primul card al rutinei era ANUA Heartleaf (`skin_type = oily`) pentru un
client cu pielea uscată. Excluderea stă pe precizia etichetei `oily`, deci e sub
`SKIN_TYPE_ANTI_FIT_ENABLED` (stins) până la auditul preînregistrat (`skin_type.oily`).

Zero DB, zero model.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.catalog.need_menu import anti_fit_for, anti_fit_hit
from src.config import get_settings
from src.domain.facets import FacetConfigError, _build_one, build_facets
from src.tools import catalog_tools as ct

SKIN = {
    "key": "skin_type",
    "source": "attribute",
    "source_key": "skin_type",
    "value_type": "enum",
    "values": ["dry", "oily", "sensitive"],
    "operators": ["eq"],
    "binding": "partitioning",
    "anti_fit": {"dry": ["oily"]},
}
FACETS = build_facets([SKIN])


# ── Configurația ──────────────────────────────────────────────────────────────────────────────


def test_anti_fit_se_incarca():
    assert FACETS[0].anti_fit == {"dry": ("oily",)}


def test_anti_fit_cere_fateta_partitionanta():
    with pytest.raises(FacetConfigError):
        _build_one({**SKIN, "binding": "additive"})


def test_anti_fit_cu_valori_nedeclarate_e_respins():
    with pytest.raises(FacetConfigError):
        _build_one({**SKIN, "anti_fit": {"dry": ["mixt"]}})


# ── Funcțiile pure ────────────────────────────────────────────────────────────────────────────


def test_anti_fit_doar_pe_nevoi_hard():
    assert anti_fit_for(FACETS, {"skin_type": ["dry"]}) == {"skin_type": ["oily"]}
    assert anti_fit_for(FACETS, {}) == {}
    assert anti_fit_for(FACETS, {"concerns": ["hydration"]}) == {}


@pytest.mark.parametrize(
    ("attrs", "hit"),
    [
        ({"skin_type": "oily"}, True),
        ({"skin_type": "dry"}, False),
        ({}, False),  # necunoscut ≠ nepotrivit
        ({"skin_type": ["oily", "dry"]}, False),  # declarat și pentru tipul cerut
        ({"skin_type": ["oily"]}, True),
    ],
)
def test_anti_fit_hit(attrs, hit):
    assert anti_fit_hit(attrs, {"skin_type": ["oily"]}) is hit


# ── Căutarea: plasa de după fuziune ───────────────────────────────────────────────────────────


@pytest.fixture
def search(monkeypatch):
    from dataclasses import replace

    from tests.test_need_menu import PACK, VOCAB
    from tests.test_need_menu_tools import _ctx, _deps

    async def fake_lexical(conn, business_id, **kw):
        return [
            {"id": "anua", "name": "Ulei", "price": 30.0, "attributes": {"skin_type": "oily"}},
            {"id": "mizon", "name": "Crema", "price": 60.0, "attributes": {"skin_type": "dry"}},
            {"id": "x", "name": "Gel", "price": 40.0, "attributes": {}},
        ]

    async def no_embeddings(conn, business_id):
        return False

    async def fake_vocab(deps, business_id):
        return VOCAB

    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)
    monkeypatch.setattr(ct, "has_embeddings", no_embeddings)
    monkeypatch.setattr(ct, "fuse_candidates", lambda lex, vec, **k: list(lex))
    monkeypatch.setattr(ct, "get_vocabulary", fake_vocab)

    async def run(concerns, body="am ten uscat"):
        ctx = _ctx(body)
        ctx.business.domain_pack = replace(PACK, facets=build_facets([SKIN]))
        result = await ct.search_products_tool(
            ctx, _deps(), {"query": "crema", "concerns": concerns}
        )
        return ctx, [p["id"] for p in result.products]

    return run


async def test_cu_flag_hard_scoate_oily(search, monkeypatch):
    monkeypatch.setattr(get_settings(), "skin_type_anti_fit_enabled", True)
    ctx, ids = await search([{"key": "dry", "quote": "am ten uscat"}])
    assert "anua" not in ids and {"mizon", "x"} <= set(ids)
    (ev,) = [e.properties for e in ctx.events if e.type == "anti_fit_excluded"]
    assert ev["n_excluded"] == 1 and ev["values"] == ["oily"]


async def test_flag_stins_nu_scoate_nimic(search, monkeypatch):
    monkeypatch.setattr(get_settings(), "skin_type_anti_fit_enabled", False)
    _, ids = await search([{"key": "dry", "quote": "am ten uscat"}])
    assert "anua" in ids


async def test_nevoie_soft_nu_scoate_nimic(search, monkeypatch):
    """«mi se usuca pielea» nu conține un alias al lui `dry`: ordonează, nu exclude."""
    monkeypatch.setattr(get_settings(), "skin_type_anti_fit_enabled", True)
    _, ids = await search(
        [{"key": "dry", "quote": "mi se usuca pielea"}], body="mi se usuca pielea dupa dus"
    )
    assert "anua" in ids


# ── Rutina: redundanță declarată ──────────────────────────────────────────────────────────────
#
# Pe rutină, o nevoie `hard` intră deja în `facet_filters` (WHERE `skin_type = dry`) și rutina nu
# relaxează filtrele, deci produsele `oily` sunt oricum excluse. Anti-potrivirea contează doar pe
# căutare, unde scara de relaxare poate renunța la fațetă. Nu există cod de rutină de testat aici.


# ── Auditul: gărzile noi ──────────────────────────────────────────────────────────────────────


def _audit_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "derived_precision_audit.py"
    spec = importlib.util.spec_from_file_location("derived_precision_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cheie_de_valoare_esantioneaza_doar_valoarea():
    audit = _audit_module()
    derived = {
        "a": {"skin_type": ["oily"]},
        "b": {"skin_type": ["dry"]},
        "c": {"skin_type": ["dry", "oily"]},  # ambiguu: catalogul n-o scrie ca `oily`
    }
    assert audit._sample(derived, "skin_type.oily", 10, "seed") == ["a"]
    assert sorted(audit._sample(derived, "skin_type", 10, "seed")) == ["a", "b", "c"]


def test_kappa():
    audit = _audit_module()
    assert audit._cohen_kappa([]) is None
    assert audit._cohen_kappa([(True, True), (False, False)]) == 1.0
    assert audit._cohen_kappa([(True, False), (False, True)]) < 0


def test_raport_respinge_verdicte_din_afara_manifestului(tmp_path, monkeypatch):
    audit = _audit_module()
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    biz = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
    key = "skin_type.oily"
    (tmp_path / f"{biz[:8]}-{key}.manifest.json").write_text(
        json.dumps({"ids": ["p1"]}), encoding="utf-8"
    )
    (tmp_path / f"{biz[:8]}-{key}.json").write_text(
        json.dumps({"verdicts": {"p2": {"verdict": "correct"}}}), encoding="utf-8"
    )
    audit._report(type("A", (), {"business": biz})())
    report = json.loads((tmp_path / f"{biz[:8]}-report.json").read_text(encoding="utf-8"))
    assert report["facets"][key]["verdict"] == "INVALID"


def test_raport_cere_kappa_si_limiteaza_nedecisele(tmp_path, monkeypatch):
    audit = _audit_module()
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    biz = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
    key = "skin_type.oily"
    verdicts = {f"p{i}": {"verdict": "correct"} for i in range(150)}
    (tmp_path / f"{biz[:8]}-{key}.json").write_text(
        json.dumps({"verdicts": verdicts}), encoding="utf-8"
    )
    audit._report(type("A", (), {"business": biz})())
    report = json.loads((tmp_path / f"{biz[:8]}-report.json").read_text(encoding="utf-8"))
    # 150/150 corecte, dar fără al doilea etichetator nu există kappa ⇒ nu se poate aprinde.
    assert report["facets"][key]["verdict"] == "INSUFFICIENT"
    assert report["enforce_ready"] == []
