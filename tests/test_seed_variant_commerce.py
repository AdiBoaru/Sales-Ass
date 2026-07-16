"""NX-171a — contract seed→payload pentru coloanele comerciale de variantă (gtin + image).

Unit (CI-fast, fără DB): dovedește capătul care, la review-ul PR #226, era rupt — seed-ul CITEȘTE
cheile reale din `catalog_v2.json` (`gtin`/`image`, exact `.get`-urile din seed) și read-path-ul
le EXPUNE ca `gtin`/`image_url`. Fără cheile în JSON backfill-ul era no-op tăcut (GTIN/img → NULL).

Perechea end-to-end pe DB reală (payload chiar conține valorile) e în
`test_variant_tenant_isolation.py` (integration).
"""

import json
from pathlib import Path

from scripts.seed_catalog_v2 import clean_gtin
from src.db.queries.catalog import _VARIANTS_AGG

_JSON = Path(__file__).resolve().parents[1] / "db" / "seed" / "catalog_v2.json"


def _variants_with_commerce() -> list[dict]:
    data = json.loads(_JSON.read_text(encoding="utf-8"))
    out = []
    for p in data["products"]:
        for v in p.get("variants") or []:
            if v.get("gtin") or v.get("image"):
                out.append(v)
    return out


def test_json_carries_gtin_and_image_on_variants():
    """DoD «backfill din JSON»: cel puțin o variantă poartă efectiv `gtin` + `image` în JSON —
    altfel seed-ul n-ar avea ce citi și coloanele ar rămâne NULL (no-op tăcut)."""
    vs = _variants_with_commerce()
    assert vs, "niciun `gtin`/`image` în catalog_v2.json → backfill GTIN/imagine ar fi no-op"
    assert any(v.get("gtin") for v in vs), "lipsă `gtin` pe variante în JSON"
    assert any(v.get("image") for v in vs), "lipsă `image` pe variante în JSON"


def test_seed_reads_valid_gs1_gtin_from_json():
    """DoD «GTIN valid GS1»: cheia pe care seed-ul o citește (`v.get('gtin')` → `clean_gtin`) trece
    checksum-ul GS1 → seed-ul SCRIE codul, nu NULL. Prinde regresia GTIN malformat în JSON."""
    with_gtin = [v for v in _variants_with_commerce() if v.get("gtin")]
    assert with_gtin
    for v in with_gtin:
        # clean_gtin întoarce codul DOAR dacă trece checksum-ul GS1; altfel None (seed scrie NULL)
        assert clean_gtin(v["gtin"]) == v["gtin"], f"GTIN invalid GS1 în JSON: {v['gtin']}"


def test_read_path_exposes_gtin_and_image_url():
    """DoD «read-path le expune»: fragmentul partajat `_VARIANTS_AGG` (search + detail) proiectează
    `gtin`/`image_url` în payload. Guard structural contra scoaterii lor din SELECT."""
    assert "'gtin', v.gtin" in _VARIANTS_AGG
    assert "'image_url', v.image_url" in _VARIANTS_AGG
