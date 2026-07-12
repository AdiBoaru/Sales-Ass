"""NX-168a — audit-gate STATIC de calitate pt catalogul demo v2 (pică seed-ul la incoerență).

Rulează pe FIȘIERUL JSON (zero DB, zero retrieval) → poate fi gate în CI/pre-commit. Verifică
contractul care a lipsit catalogului vechi (500 templatate): atribute CANONICE, nume curate,
coerență nume↔categorie, categorySlugs în aceeași ramură, diferențiatori reali la comparație.

    python scripts/audit_catalog_v2.py                      # implicit db/seed/catalog_v2.json
    python scripts/audit_catalog_v2.py db/seed/catalog.json # dovadă: PICĂ pe legacy
    python scripts/audit_catalog_v2.py <fișier> --examples 8

Exit 0 = curat; exit 1 = ≥1 violare (gate). Regulile sunt determinist-derivate din date + contract
(fără wordlist NLP): enum-uri canonice, familii de tip din nume, arborele de categorii din fișier.
Auditul LIVE (retrieval real „makeup nu întoarce păr") e SEPARAT — vine în NX-168b (după seed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "db" / "seed" / "catalog_v2.json"
SCHEMA_PATH = ROOT / "db" / "seed" / "catalog_v2.schema.json"

# --- contract de atribute canonice (aliniat cu src/domain/defaults/beauty_salon.json) ---------
CANONICAL_CONCERNS = frozenset(
    {
        "oily",
        "dry",
        "sensitive",
        "combination",
        "acne",
        "anti_aging",
        "hyperpigmentation",
        "hydration",
        "normal",
    }
)
CANONICAL_FINISH = frozenset({"natural", "matte", "dewy", "satin"})
CANONICAL_COVERAGE = frozenset({"light", "medium", "full", "buildable"})

# atribute obligatorii per categorie (frunză) / per root-branch. Contract, nu wordlist NLP.
REQUIRED_ATTRS_BY_SLUG: dict[str, set[str]] = {
    "fond-de-ten": {"finish", "coverage"},
    "creme-bb-si-cc": {"finish", "coverage"},
    "cushion": {"finish", "coverage"},
    "anticearcan": {"coverage"},
}
REQUIRED_ATTRS_BY_ROOT: dict[str, set[str]] = {
    "ingrijirea-tenului": {"concerns"},
}

# Familie de TIP dedusă din nume/categorie (heuristică din audit_catalog_coherence, precizie mare).
FAMILIES: dict[str, list[str]] = {
    "unelte": ["pensula", "burete machiaj", "buretel", "aplicator", "set pensule"],
    "parfum": ["apa parfumata", "apa de toaleta", "apa de parfum", "eau de", "parfum"],
    "par": [
        "sampon",
        "balsam de par",
        "masca de par",
        "vopsea",
        "fixativ",
        "spuma de par",
        "ulei de par",
        "tratament de par",
        "ser de par",
        "spray de par",
        "accesoriu",
    ],
    "machiaj": [
        "fond de ten",
        "pudra",
        "ruj",
        "luciu de buze",
        "creion de",
        "rimel",
        "mascara",
        "fard",
        "paleta",
        "corector",
        "anticearcan",
        "iluminator",
        "primer",
        "tus de ochi",
    ],
    "ingrijire_corp": [
        "gel de dus",
        "lotiune de corp",
        "ulei de corp",
        "scrub de corp",
        "unt de corp",
        "crema de corp",
        "crema de maini",
        "sapun",
        "deodorant",
    ],
    "ingrijire_fata": [
        "ser",
        "contur ochi",
        "apa micelara",
        "tonic",
        "demachiant",
        "masca de fata",
        "crema de fata",
        "gel de curatare",
        "exfoliant",
        "crema hidratanta",
        "spf",
        "protectie solara",
        "crema anti",
        "crema de zi",
        "crema de noapte",
    ],
}


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _family(text: str) -> str | None:
    """Prima familie a cărei frază-cheie apare în text (normalizat). None = necunoscut."""
    t = _norm(text)
    for fam, phrases in FAMILIES.items():
        if any(ph in t for ph in phrases):
            return fam
    return None


def build_roots(categories: list[dict[str, Any]]) -> dict[str, str]:
    """slug → root-branch (primul strămoș fără parent), parcurgând `parentSlug`. Cicluri/lipsă →
    slug-ul propriu ca root (defensiv)."""
    parent = {c["slug"]: c.get("parentSlug") for c in categories}
    roots: dict[str, str] = {}
    for slug in parent:
        seen: set[str] = set()
        cur = slug
        while parent.get(cur) and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
        roots[slug] = cur
    return roots


def _cat_names(categories: list[dict[str, Any]]) -> dict[str, str]:
    return {c["slug"]: c.get("name", c["slug"]) for c in categories}


def _attrs(p: dict[str, Any]) -> dict[str, Any]:
    a = p.get("attributes")
    return a if isinstance(a, dict) else {}


# --- cele 6 reguli (fiecare: -> list[str] de violări lizibile) ---------------------------------


def rule_canonical_enums(products: list[dict[str, Any]]) -> list[str]:
    """R1: concerns/finish/coverage TREBUIE valori canonice (bug-ul „ten uscat" în loc de `dry`)."""
    out = []
    for p in products:
        a = _attrs(p)
        bad_c = [v for v in (a.get("concerns") or []) if v not in CANONICAL_CONCERNS]
        if bad_c:
            out.append(f"{p.get('slug')}: concerns non-canonice {bad_c}")
        if "finish" in a and a["finish"] not in CANONICAL_FINISH:
            out.append(f"{p.get('slug')}: finish non-canonic '{a['finish']}'")
        if "coverage" in a and a["coverage"] not in CANONICAL_COVERAGE:
            out.append(f"{p.get('slug')}: coverage non-canonic '{a['coverage']}'")
    return out


def rule_required_attrs(products: list[dict[str, Any]], roots: dict[str, str]) -> list[str]:
    """R2: atribute-cheie obligatorii per categorie (fond fără finish/coverage; skincare fără
    concerns)."""
    out = []
    for p in products:
        primary = p.get("primaryCategorySlug", "")
        req = set(REQUIRED_ATTRS_BY_SLUG.get(primary, set()))
        req |= REQUIRED_ATTRS_BY_ROOT.get(roots.get(primary, ""), set())
        a = _attrs(p)
        missing = [k for k in sorted(req) if not a.get(k)]
        if missing:
            out.append(f"{p.get('slug')} ({primary}): lipsesc atribute {missing}")
    return out


def rule_clean_names(products: list[dict[str, Any]]) -> list[str]:
    """R3: nume generate/duplicate — sufix numeric rezidual (`... 250`) sau nume identic
    (normalizat) cu alt produs."""
    out = []
    norm_names: dict[str, str] = {}
    for p in products:
        name = p.get("name", "")
        if re.search(r"\b\d{2,4}\s*$", name):
            out.append(f"{p.get('slug')}: nume cu sufix numeric rezidual — «{name}»")
        key = re.sub(r"\s+", " ", _norm(re.sub(r"\b\d{2,4}\s*$", "", name)).strip())
        if key and key in norm_names and norm_names[key] != p.get("slug"):
            out.append(f"{p.get('slug')}: nume duplicat cu «{norm_names[key]}» — «{name}»")
        else:
            norm_names[key] = p.get("slug", "")
    return out


def rule_name_category_coherence(
    products: list[dict[str, Any]], cat_names: dict[str, str]
) -> list[str]:
    """R4: familia de TIP a numelui ≠ familia categoriei (ambele cunoscute) — „Pensula de machiaj"
    în „Fard de ochi", „Accesoriu par" prezentat ca machiaj etc."""
    out = []
    for p in products:
        primary = p.get("primaryCategorySlug", "")
        nf = _family(p.get("name", ""))
        cf = _family(cat_names.get(primary, primary))
        if nf and cf and nf != cf:
            out.append(f"{p.get('slug')}: nume={nf} dar categoria «{primary}»={cf}")
    return out


def rule_categoryslug_roots(products: list[dict[str, Any]], roots: dict[str, str]) -> list[str]:
    """R5: `categorySlugs` incoerente — un slug dintr-o ALTĂ ramură top-level decât primary (ex.
    `par` pe un produs de `machiaj`)."""
    out = []
    for p in products:
        primary = p.get("primaryCategorySlug", "")
        proot = roots.get(primary)
        if not proot:
            continue
        for cs in p.get("categorySlugs") or []:
            croot = roots.get(cs)
            if croot and croot != proot:
                out.append(
                    f"{p.get('slug')}: categorySlug «{cs}» (ramura {croot}) ≠ primary {proot}"
                )
    return out


def rule_comparison_differentiators(products: list[dict[str, Any]]) -> list[str]:
    """R6: două produse din ACEEAȘI categorie-frunză fără NICIUN diferențiator (preț==, rating==,
    aceleași pros/cons) → comparația ar fi inutilă. Semnalăm perechea."""
    out = []
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in products:
        by_cat[p.get("primaryCategorySlug", "")].append(p)

    def _sig(p: dict[str, Any]) -> tuple:
        rs = p.get("reviewSummary") or {}
        return (
            round(float(p.get("salePrice") or p.get("price") or 0), 2),
            round(float(p.get("rating") or 0), 2),
            tuple(sorted(rs.get("topPros") or [])),
            tuple(sorted(rs.get("topCons") or [])),
        )

    for cat, items in by_cat.items():
        seen: dict[tuple, str] = {}
        for p in items:
            sig = _sig(p)
            if sig in seen:
                out.append(
                    f"{cat}: «{p.get('slug')}» ~ «{seen[sig]}» fără diferențiator "
                    "(preț/rating/pros/cons identice)"
                )
            else:
                seen[sig] = p.get("slug", "")
    return out


RULES = [
    ("R1 concerns/finish/coverage canonice", "canonical_enums"),
    ("R2 atribute-cheie per categorie", "required_attrs"),
    ("R3 nume curate (fără sufix/duplicat)", "clean_names"),
    ("R4 coerență nume↔categorie", "name_category_coherence"),
    ("R5 categorySlugs în aceeași ramură", "categoryslug_roots"),
    ("R6 diferențiatori la comparație", "comparison_differentiators"),
]


def audit(data: dict[str, Any]) -> dict[str, list[str]]:
    """Rulează toate regulile → {rule_key: [violări]}. Pur (fără I/O), testabil pe dict."""
    products = data.get("products") or []
    categories = data.get("categories") or []
    roots = build_roots(categories)
    cat_names = _cat_names(categories)
    return {
        "canonical_enums": rule_canonical_enums(products),
        "required_attrs": rule_required_attrs(products, roots),
        "clean_names": rule_clean_names(products),
        "name_category_coherence": rule_name_category_coherence(products, cat_names),
        "categoryslug_roots": rule_categoryslug_roots(products, roots),
        "comparison_differentiators": rule_comparison_differentiators(products),
    }


def _validate_schema(data: dict[str, Any]) -> list[str]:
    """Validare structurală opțională contra schemei (dacă `jsonschema` e instalat). Absent →
    skip (auditul de reguli e oricum self-sufficient)."""
    try:
        import jsonschema  # noqa: PLC0415 — soft dep, doar dacă e prezent
    except ImportError:
        return []
    if not SCHEMA_PATH.exists():
        return []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(data)][:20]


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("catalog", nargs="?", default=str(DEFAULT_CATALOG))
    ap.add_argument("--examples", type=int, default=6, help="câte exemple per regulă să listez")
    ap.add_argument("--no-schema", action="store_true", help="sări peste validarea de schemă")
    args = ap.parse_args()

    path = Path(args.catalog)
    if not path.exists():
        print(f"✗ Fișier inexistent: {path}")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"=== AUDIT CATALOG v2 (static) — {path.name} ===")
    n_prod = len(data.get("products") or [])
    n_cat = len(data.get("categories") or [])
    print(f"produse={n_prod}  categorii={n_cat}\n")

    schema_errs = [] if args.no_schema else _validate_schema(data)
    if schema_errs:
        print(f"SCHEMA: {len(schema_errs)} erori structurale (primele):")
        for e in schema_errs[: args.examples]:
            print(f"    ✗ {e}")
        print()

    results = audit(data)
    total = sum(len(v) for v in results.values()) + len(schema_errs)
    for label, key in RULES:
        v = results[key]
        mark = "✓" if not v else "✗"
        print(f"{mark} {label}: {len(v)} violări")
        for line in v[: args.examples]:
            print(f"      - {line}")

    print()
    if total == 0:
        print("✓ CATALOG CURAT — audit trecut.")
        return 0
    counts = Counter({k: len(v) for k, v in results.items() if v})
    print(f"✗ AUDIT PICAT — {total} violări total {dict(counts)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
