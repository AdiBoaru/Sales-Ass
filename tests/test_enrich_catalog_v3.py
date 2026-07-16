"""NX-168e — teste pt tool-ul de enrichment determinist (siguranță usage, proveniență onestă,
best_for fără frecvențe generice, idempotency, output care trece evaluate(v3))."""

import json

from scripts.audit_catalog_v2 import CANONICAL_SUITABLE_FOR, evaluate
from scripts.enrich_catalog_v3 import DATA, _best_for, _hair_canon, _provenance, _usage, enrich


def _cats():
    return [
        {"slug": "ingrijirea-tenului", "name": "Îngrijirea tenului"},
        {"slug": "seruri-pentru-ten", "name": "Seruri", "parentSlug": "ingrijirea-tenului"},
        {"slug": "protectie-solara", "name": "SPF"},
        {"slug": "ingrijire-corp", "name": "Corp"},
        {"slug": "scrub-de-corp", "name": "Scrub", "parentSlug": "ingrijire-corp"},
    ]


def _retinol_serum():
    return {
        "slug": "x-retinol-ser",
        "name": "Auralis Retinol Ser de noapte",
        "brandSlug": "auralis",
        "primaryCategorySlug": "seruri-pentru-ten",
        "price": 80,
        "attributes": {
            "concerns": ["anti_aging"],
            "key_ingredients": ["retinol"],
            "key_benefit": "reduce ridurile",
        },
    }


# --- HIGH: usage sigur (nu instrucțiuni inventate) --------------------------------------------


def test_usage_retinol_is_evening_only():
    assert _usage(_retinol_serum(), "seruri-pentru-ten", "ingrijirea-tenului") == {
        "time": ["evening"]
    }


def test_usage_spf_is_morning():
    spf = {"name": "Solora SPF 50", "attributes": {"spf": 50}}
    assert _usage(spf, "protectie-solara", "ingrijirea-tenului") == {"time": ["morning"]}


def test_usage_generic_serum_ampm():
    ser = {"name": "Auralis Hydra Ser", "attributes": {"key_ingredients": ["acid hialuronic"]}}
    assert _usage(ser, "seruri-pentru-ten", "ingrijirea-tenului") == {
        "time": ["morning", "evening"]
    }


# --- HIGH: proveniență ONESTĂ (nu INCI fabricat) ----------------------------------------------


def test_provenance_honest_source():
    prov = _provenance({"key_ingredients": ["niacinamidă"]})
    assert len(prov) == 1
    assert prov[0]["source"] == "demo_catalog_authored"
    assert "INCI" not in prov[0]["source"]  # nu pretinde etichetă reală
    assert prov[0]["value"] == "niacinamidă" and prov[0]["verified_at"]


# --- HIGH: best_for fără frecvență generică ---------------------------------------------------


def test_best_for_no_generic_frequency():
    scrub = {
        "slug": "s",
        "name": "Bloom Scrub de corp",
        "primaryCategorySlug": "scrub-de-corp",
        "attributes": {},
    }
    bf = _best_for(scrub, "ingrijire-corp")
    assert "uz zilnic" not in bf and bf  # temei semantic per categorie


# --- MEDIUM: idempotency + output trece v3 ----------------------------------------------------


def _data():
    return {
        "brands": [{"slug": "auralis", "name": "A"}],
        "categories": _cats(),
        "products": [_retinol_serum()],
    }


def test_enrich_idempotent():
    data = _data()
    enrich(data)
    snap = json.dumps(data, ensure_ascii=False, sort_keys=True)
    enrich(data)  # a doua rulare NU mai schimbă nimic
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == snap


def test_enrich_output_passes_v3():
    data = _data()
    enrich(data)
    res = evaluate(data, "v3")
    assert sum(len(v) for v in res["violations"].values()) == 0
    # retinol → usage seara (nu dimineața)
    assert data["products"][0]["attributes"]["usage"] == {"time": ["evening"]}


# --- runda 10: derivări REGENERABILE (nu fill-once) -------------------------------------------


def _serum(**attrs):
    return {
        "brands": [{"slug": "x", "name": "X"}],
        "categories": _cats(),
        "products": [
            {
                "slug": "s",
                "name": "Hydra Ser",
                "brandSlug": "x",
                "primaryCategorySlug": "seruri-pentru-ten",
                "price": 50,
                "attributes": attrs,
            }
        ],
    }


def test_regenerable_when_source_facts_change():
    data = _serum(key_ingredients=["acid hialuronic"])
    enrich(data)
    assert data["products"][0]["attributes"]["usage"] == {"time": ["morning", "evening"]}
    # schimbă faptul sursă → rerun → usage + provenance se RECALCULEAZĂ
    data["products"][0]["attributes"]["key_ingredients"] = ["retinol"]
    enrich(data)
    a = data["products"][0]["attributes"]
    assert a["usage"] == {"time": ["evening"]}
    assert [e["value"] for e in a["claim_provenance"]] == ["retinol"]


def test_locked_override_preserved():
    data = _serum(key_ingredients=["retinol"], usage={"time": ["morning"]}, _locked=["usage"])
    enrich(data)
    assert data["products"][0]["attributes"]["usage"] == {"time": ["morning"]}  # override intact


def test_hair_type_normalization():
    assert _hair_canon("cret") == "curly"
    assert _hair_canon("creț") == "curly"
    assert _hair_canon("curly") == "curly"
    assert _hair_canon("deteriorat") == "damaged"
    assert _hair_canon("toate tipurile") is None  # universal → fără filtru
    assert _hair_canon("") is None


# --- runda 10: catalogul REAL în CI (nu doar fixture) -----------------------------------------


def test_real_catalog_full_v3():
    d = json.loads(DATA.read_text(encoding="utf-8"))
    p = d["products"]
    assert len(p) >= 150
    assert sum(len(v) for v in evaluate(d, "v3")["violations"].values()) == 0
    assert all(x.get("description") for x in p)  # descriptions pe TOATE

    def has_gramaj(x):
        if x["primaryCategorySlug"] == "pensule-si-bureti-de-machiaj":
            return True  # unelte = bucăți, fără gramaj (justificat)
        if x["attributes"].get("net_content"):
            return True
        vs = x.get("variants") or []
        return bool(vs) and all(v.get("net_content") for v in vs)

    assert all(has_gramaj(x) for x in p)  # gramaj pe toate produsele/variantele aplicabile
    for x in p:  # suitable_for CANONIC peste tot
        for s in x["attributes"].get("suitable_for") or []:
            assert s in CANONICAL_SUITABLE_FOR, f"{x['slug']}: suitable_for {s} necanonic"
