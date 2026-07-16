"""Audit-gate STATIC de calitate pt catalogul demo (pică seed-ul la incoerență). VERSIONAT:

- **contract="v2"** (NX-168a, default): R1-R6 — atribute canonice, nume curate, coerență
  nume↔categorie, categorySlugs în ramură, diferențiatori. Folosit de `seed_catalog_v2.py` ca
  poartă pre-flight → NU se schimbă logica (catalogul curent rămâne verde).
- **contract="v3"** (NX-168d): R1-R13 — peste v2 adaugă Product Contract v3 orientat pe
  recomandare: R7 contradicție desc↔finish, R8 proveniența claim-urilor, R9 SKU/GTIN, R10
  obligatorii per-categorie v3, R11 best_for, R12 ai_summary nefondat, R13 variante incomplete.

Rezultatul e `{"violations": {rule: [entry]}, "warnings": {rule: [entry]}}`, fiecare `entry` =
`{"message": str, "product_slugs": [str]}` (MACHINE-READABLE → downstream mapează violation→produse
fără să parseze text; duplicatele marchează TOATE slug-urile). Seed-ul + gate-ul CI numără DOAR
`violations`; `warnings` (negații/ambiguu) se logează dar NU pică gate-ul.

    python scripts/audit_catalog_v2.py                          # v2, db/seed/catalog_v2.json
    python scripts/audit_catalog_v2.py --contract v3            # gate v3 (contract de recomandare)
    python scripts/audit_catalog_v2.py db/seed/catalog.json --contract v3   # dovadă: PICĂ pe legacy

Exit 0 = zero violations; exit 1 = ≥1 violation. Regulile sunt determinist-derivate din date +
contract (vocabular canonic, familii de tip, arborele de categorii din fișier) — fără NLP liber.
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
SCHEMA_V3_PATH = ROOT / "db" / "seed" / "catalog_v3.schema.json"

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

# --- contract v3 (NX-168d) --------------------------------------------------------------------
# Obligatorii per-categorie v3 — semantică de OVERRIDE (slug bate root; unealta nu cere concerns).
# `best_for` e universal (R11 separat), deci NU apare aici.
REQUIRED_V3_BY_SLUG: dict[str, set[str]] = {
    "fond-de-ten": {"finish", "coverage", "suitable_for", "texture"},
    "creme-bb-si-cc": {"finish", "coverage", "suitable_for", "texture"},
    "cushion": {"finish", "coverage"},
    "anticearcan": {"coverage"},
    "pensule-si-bureti-de-machiaj": {"key_benefit", "differentiators"},
}
REQUIRED_V3_BY_ROOT: dict[str, set[str]] = {
    "ingrijirea-tenului": {"concerns", "texture", "usage", "key_ingredients"},
    "ingrijirea-parului": {"hair_type", "usage"},
    "machiaj": {"finish"},
}
PROVENANCE_KINDS = frozenset({"ingredient", "badge", "certification"})
# Vocabular canonic de ingrediente-activ pt R12 (audit offline — NU e NLP de pipeline). Un claim
# pozitiv de ingredient în ai_summary care nu e în key_ingredients = ai_summary inventat.
INGREDIENT_VOCAB = (
    "retinol",
    "niacinamida",
    "acid hialuronic",
    "vitamina c",
    "acid salicilic",
    "acid glicolic",
    "acid lactic",
    "ceramide",
    "peptide",
    "colagen",
    "panthenol",
    "cofeina",
    "bisabolol",
    "squalan",
)
# Fraze RO de concern → cheie canonică (R12): un ai_summary care afirmă „ten gras" fără `oily`
# în concerns/suitable_for = claim nefondat. Frazele sunt specifice (nu „gras" singur).
CONCERN_PHRASES: dict[str, str] = {
    "ten gras": "oily",
    "ten uscat": "dry",
    "ten sensibil": "sensitive",
    "ten mixt": "combination",
    "ten normal": "normal",
    "acnee": "acne",
    "riduri": "anti_aging",
    "anti-imbatranire": "anti_aging",
    "pete": "hyperpigmentation",
    "hidratare": "hydration",
}
# Semnale de finish în descriere (R7). Regex pe text normalizat (fără diacritice); `\bmat[ae]?\b`
# prinde „mat/mata/mate" ca CUVÂNT (nu „format"), iar „matifian" prinde matifiant/matifianta.
_FINISH_SIGNALS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"matifian"), "matte"),
    (re.compile(r"\bmat[ae]?\b"), "matte"),
    (re.compile(r"\bdewy\b"), "dewy"),
    (re.compile(r"satinat"), "satin"),
]


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


# --- findings: entry machine-readable + severitate ---------------------------------------------


def _f(message: str, *slugs: str, severity: str = "violation") -> dict[str, Any]:
    """Un finding: mesaj lizibil + slug-urile TOATE produsele implicate (duplicate → 2 slug-uri).
    `severity`: 'violation' (fatal) | 'warning' (non-fatal, negații/ambiguu)."""
    return {
        "message": message,
        "product_slugs": [s for s in slugs if s],
        "severity": severity,
    }


def _family(text: str) -> str | None:
    """Prima familie a cărei frază-cheie apare în text (normalizat). None = necunoscut."""
    t = _norm(text)
    for fam, phrases in FAMILIES.items():
        if any(ph in t for ph in phrases):
            return fam
    return None


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


def _is_active(p: dict[str, Any]) -> bool:
    return (p.get("status") or "active") == "active"


def _gtin_valid(gtin: str) -> bool:
    """Checksum GS1 mod-10. RESPINGE forme malformate (cratime/litere/spații): cere string
    DOAR-cifre de lungime validă (8/12/13/14) — NU curăță non-cifrele (altfel „4006-...", „EAN..."
    ar trece). Cifra de control = ultima; ponderi 3/1 de la dreapta."""
    s = gtin or ""
    if not re.fullmatch(r"\d{8}|\d{12}|\d{13}|\d{14}", s):
        return False
    ds = [int(c) for c in s]
    body = ds[:-1][::-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == ds[-1]


# --- R1-R6 (v2 + v3): logică NESCHIMBATĂ față de NX-168a, doar întorc findings ------------------


def rule_canonical_enums(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R1: concerns/finish/coverage TREBUIE valori canonice (bug-ul „ten uscat" în loc de `dry`)."""
    out = []
    for p in products:
        a = _attrs(p)
        slug = p.get("slug", "")
        bad_c = [v for v in (a.get("concerns") or []) if v not in CANONICAL_CONCERNS]
        if bad_c:
            out.append(_f(f"{slug}: concerns non-canonice {bad_c}", slug))
        if "finish" in a and a["finish"] not in CANONICAL_FINISH:
            out.append(_f(f"{slug}: finish non-canonic '{a['finish']}'", slug))
        if "coverage" in a and a["coverage"] not in CANONICAL_COVERAGE:
            out.append(_f(f"{slug}: coverage non-canonic '{a['coverage']}'", slug))
    return out


def rule_required_attrs(
    products: list[dict[str, Any]], roots: dict[str, str]
) -> list[dict[str, Any]]:
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
            out.append(
                _f(f"{p.get('slug')} ({primary}): lipsesc atribute {missing}", p.get("slug", ""))
            )
    return out


# Sufix de seed rezidual = număr (2-4 cifre) la final, precedat de un CUVÂNT lowercase (ex.
# „definire 250", „volum 001"). NU prinde specs legitime cu majuscule (SPF 50, PA 30) — acolo
# tokenul dinainte e uppercase. Lookbehind fix (3 litere) = suficient ca discriminator.
_SEED_SUFFIX_RE = re.compile(r"(?<=[a-zăâîșț]{3})\s+\d{2,4}\s*$")


def rule_clean_names(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R3: nume generate/duplicate — sufix numeric rezidual de seed (`... definire 250`) sau nume
    identic (normalizat) cu alt produs. NU semnalează specs legitime (`... SPF 50`)."""
    out = []
    norm_names: dict[str, str] = {}
    for p in products:
        slug = p.get("slug", "")
        name = p.get("name", "")
        stripped = _SEED_SUFFIX_RE.sub("", name)
        if stripped != name:
            out.append(_f(f"{slug}: nume cu sufix numeric rezidual — «{name}»", slug))
        key = re.sub(r"\s+", " ", _norm(stripped).strip())
        if key and key in norm_names and norm_names[key] != slug:
            out.append(
                _f(
                    f"{slug}: nume duplicat cu «{norm_names[key]}» — «{name}»",
                    slug,
                    norm_names[key],
                )
            )
        else:
            norm_names[key] = slug
    return out


def rule_name_category_coherence(
    products: list[dict[str, Any]], cat_names: dict[str, str]
) -> list[dict[str, Any]]:
    """R4: familia de TIP a numelui ≠ familia categoriei (ambele cunoscute) — „Pensula de machiaj"
    în „Fard de ochi", „Accesoriu par" prezentat ca machiaj etc."""
    out = []
    for p in products:
        primary = p.get("primaryCategorySlug", "")
        nf = _family(p.get("name", ""))
        cf = _family(cat_names.get(primary, primary))
        if nf and cf and nf != cf:
            out.append(
                _f(f"{p.get('slug')}: nume={nf} dar categoria «{primary}»={cf}", p.get("slug", ""))
            )
    return out


def rule_categoryslug_roots(
    products: list[dict[str, Any]], roots: dict[str, str]
) -> list[dict[str, Any]]:
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
                    _f(
                        f"{p.get('slug')}: categorySlug «{cs}» (ramura {croot}) ≠ primary {proot}",
                        p.get("slug", ""),
                    )
                )
    return out


def rule_comparison_differentiators(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                    _f(
                        f"{cat}: «{p.get('slug')}» ~ «{seen[sig]}» fără diferențiator "
                        "(preț/rating/pros/cons identice)",
                        p.get("slug", ""),
                        seen[sig],
                    )
                )
            else:
                seen[sig] = p.get("slug", "")
    return out


# --- R7-R13 (DOAR v3, NX-168d) -----------------------------------------------------------------


def rule_desc_attr_contradiction(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R7: cuvânt-semnal POZITIV de finish în ai_summary/shortDescription contrazice `finish`
    (ex. „matifiant" pe un `dewy`) → violation. NEGAȚIE (ex. „nu lasă finish mat") → warning."""
    out = []
    for p in products:
        fin = _attrs(p).get("finish")
        if not fin:
            continue
        slug = p.get("slug", "")
        text = _norm((p.get("ai_summary") or "") + " " + (p.get("shortDescription") or ""))
        for rx, implied in _FINISH_SIGNALS:
            if implied == fin:
                continue  # semnal consistent cu finish-ul → nu semnalăm
            for m in rx.finditer(text):
                # fereastră de ~24 caractere: prinde „nu lasă finish mat" (negația e la câteva
                # cuvinte de semnal). Preferăm warning la ambiguu — fatal doar pe pozitiv clar.
                window = text[max(0, m.start() - 24) : m.start()]
                negated = ("nu " in window) or ("fara " in window)
                if negated:
                    out.append(
                        _f(
                            f"{slug}: «{m.group()}» negat lângă finish={fin} (verifică)",
                            slug,
                            severity="warning",
                        )
                    )
                else:
                    out.append(
                        _f(
                            f"{slug}: descriere «{m.group()}»→{implied} contrazice finish={fin}",
                            slug,
                        )
                    )
    return out


def _provenance_index(a: dict[str, Any]) -> dict[str, set[str]]:
    """value-urile (normalizate) acoperite de claim_provenance, per kind, DOAR cu sursă completă."""
    cov: dict[str, set[str]] = {k: set() for k in PROVENANCE_KINDS}
    for e in a.get("claim_provenance") or []:
        if not isinstance(e, dict):
            continue
        kind, val = e.get("kind"), e.get("value")
        if kind in cov and val and e.get("source") and e.get("source_ref") and e.get("verified_at"):
            cov[kind].add(_norm(str(val)))
    return cov


def rule_claim_provenance(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R8 (determinist, fără duplicare): FIECARE key_ingredient și FIECARE badge cer o intrare
    `claim_provenance` (kind + sursă completă). `not_recommended_for` hard cere proveniență INLINE
    (source+source_ref+verified_at); soft cere măcar `reason`."""
    out = []
    for p in products:
        slug = p.get("slug", "")
        a = _attrs(p)
        cov = _provenance_index(a)
        for ing in a.get("key_ingredients") or []:
            if _norm(str(ing)) not in cov["ingredient"]:
                out.append(
                    _f(
                        f"{slug}: ingredient «{ing}» fără claim_provenance (kind=ingredient+sursă)",
                        slug,
                    )
                )
        for badge in a.get("badges") or []:
            if _norm(str(badge)) not in cov["badge"]:
                out.append(
                    _f(f"{slug}: badge «{badge}» fără claim_provenance (kind=badge+sursă)", slug)
                )
        for nrf in a.get("not_recommended_for") or []:
            if not isinstance(nrf, dict):
                out.append(_f(f"{slug}: not_recommended_for malformat (nu e obiect)", slug))
                continue
            if nrf.get("level") == "hard":
                if not (nrf.get("source") and nrf.get("source_ref") and nrf.get("verified_at")):
                    out.append(
                        _f(
                            f"{slug}: contraindicație hard «{nrf.get('value')}» fără sursă inline",
                            slug,
                        )
                    )
            elif not nrf.get("reason"):
                out.append(
                    _f(f"{slug}: contraindicație soft «{nrf.get('value')}» fără reason", slug)
                )
    return out


def rule_sku_gtin(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R9: SKU/GTIN duplicate în catalog (marchează TOATE slug-urile implicate), sau GTIN cu
    checksum GS1 invalid (pe variantă ori produs-fallback)."""
    out = []
    skus: dict[str, list[str]] = defaultdict(list)
    gtins: dict[str, list[str]] = defaultdict(list)

    def _collect_gtin(g: Any, slug: str, where: str) -> None:
        if not _gtin_valid(str(g)):
            out.append(_f(f"{slug}: GTIN {where}invalid/malformat «{g}»", slug))
            return  # invalid → NU intră în dedup (ar polua cheia canonică)
        gtins[str(g)].append(slug)  # valid = doar-cifre → cheia e deja canonică

    for p in products:
        slug = p.get("slug", "")
        for v in p.get("variants") or []:
            if v.get("sku"):
                skus[str(v["sku"])].append(slug)
            if v.get("gtin"):
                _collect_gtin(v["gtin"], slug, "")
        if _attrs(p).get("gtin"):
            _collect_gtin(_attrs(p)["gtin"], slug, "produs ")
    for sku, slugs in skus.items():
        if len(slugs) > 1:
            uniq = sorted(set(slugs))
            out.append(_f(f"SKU duplicat «{sku}» pe {uniq}", *uniq))
    for g, slugs in gtins.items():
        if len(slugs) > 1:
            uniq = sorted(set(slugs))
            out.append(_f(f"GTIN duplicat «{g}» pe {uniq}", *uniq))
    return out


def rule_required_attrs_v3(
    products: list[dict[str, Any]], roots: dict[str, str]
) -> list[dict[str, Any]]:
    """R10: obligatorii per-categorie v3 (OVERRIDE: slug bate root; unealta nu cere concerns).
    `best_for` e universal → R11 separat."""
    out = []
    for p in products:
        if not _is_active(p):
            continue
        primary = p.get("primaryCategorySlug", "")
        req = REQUIRED_V3_BY_SLUG.get(primary)
        if req is None:
            req = REQUIRED_V3_BY_ROOT.get(roots.get(primary, ""), set())
        a = _attrs(p)
        missing = [k for k in sorted(req) if not a.get(k)]
        if missing:
            out.append(
                _f(f"{p.get('slug')} ({primary}): lipsesc atribute v3 {missing}", p.get("slug", ""))
            )
    return out


def rule_missing_best_for(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R11: produs activ fără `best_for` concret (motiv de recomandare intrinsec)."""
    out = []
    for p in products:
        if _is_active(p) and not _attrs(p).get("best_for"):
            out.append(
                _f(f"{p.get('slug')}: lipsește best_for (motiv de recomandare)", p.get("slug", ""))
            )
    return out


def _emit_claim(out: list, slug: str, summ: str, needle: str, backed: bool, what: str) -> None:
    """Pt fiecare apariție a `needle` în ai_summary: dacă NU e susținut de atribute → violation
    (pozitiv) / warning (negat în fereastra de ~24 caractere). Nimic dacă e susținut."""
    if backed:
        return
    idx = summ.find(needle)
    while idx != -1:
        window = summ[max(0, idx - 24) : idx]
        if ("fara " in window) or ("nu " in window):
            out.append(
                _f(
                    f"{slug}: «{needle}» ({what}) negat în ai_summary (verifică)",
                    slug,
                    severity="warning",
                )
            )
        else:
            out.append(
                _f(f"{slug}: ai_summary afirmă «{needle}» ({what}) nesusținut de atribute", slug)
            )
        idx = summ.find(needle, idx + len(needle))


def rule_ai_summary_unfounded(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R12: ai_summary afirmă POZITIV un fapt (ingredient / finish / concern) care NU e susținut de
    `attributes` → violation; negație → warning. Finish: doar când atributul LIPSEȘTE (present +
    contradictoriu = R7). Concern: cheie canonică absentă din concerns∪suitable_for."""
    out = []
    for p in products:
        summ = _norm(p.get("ai_summary") or "")
        if not summ:
            continue
        slug = p.get("slug", "")
        a = _attrs(p)
        ki = {_norm(str(x)) for x in a.get("key_ingredients") or []}
        # ingrediente
        for ing in INGREDIENT_VOCAB:
            _emit_claim(out, slug, summ, ing, any(ing in k or k in ing for k in ki), "ingredient")
        # finish — DOAR dacă atributul lipsește (present+contradictoriu = R7)
        if not a.get("finish"):
            for rx, implied in _FINISH_SIGNALS:
                for m in rx.finditer(summ):
                    window = summ[max(0, m.start() - 24) : m.start()]
                    sev = "warning" if (("nu " in window) or ("fara " in window)) else "violation"
                    msg = (
                        f"{slug}: ai_summary afirmă finish «{m.group()}»→{implied} dar n-are finish"
                    )
                    out.append(_f(msg, slug, severity=sev))
        # concern — cheie canonică absentă din concerns ∪ suitable_for
        backed_concerns = {_norm(str(c)) for c in a.get("concerns") or []} | {
            _norm(str(s)) for s in a.get("suitable_for") or []
        }
        for phrase, canon in CONCERN_PHRASES.items():
            _emit_claim(out, slug, summ, phrase, canon in backed_concerns, "concern")
    return out


def rule_variants_incomplete(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R13: variantă fără label/sku/price(>0)/stock. `stock=0` e valid (falsy dar prezent)."""
    out = []
    for p in products:
        slug = p.get("slug", "")
        for v in p.get("variants") or []:
            missing = []
            if not v.get("label"):
                missing.append("label")
            if not v.get("sku"):
                missing.append("sku")
            price = v.get("price")
            if price is None or (isinstance(price, (int, float)) and price <= 0):
                missing.append("price")
            if v.get("stock") is None:
                missing.append("stock")
            if missing:
                lbl = v.get("label") or v.get("sku") or "?"
                out.append(_f(f"{slug}: variantă «{lbl}» incompletă {missing}", slug))
    return out


# --- orchestrare -------------------------------------------------------------------------------

RULES_V2 = [
    ("R1 concerns/finish/coverage canonice", "canonical_enums"),
    ("R2 atribute-cheie per categorie", "required_attrs"),
    ("R3 nume curate (fără sufix/duplicat)", "clean_names"),
    ("R4 coerență nume↔categorie", "name_category_coherence"),
    ("R5 categorySlugs în aceeași ramură", "categoryslug_roots"),
    ("R6 diferențiatori la comparație", "comparison_differentiators"),
]
RULES_V3 = RULES_V2 + [
    ("R7 contradicție desc↔finish", "desc_attr_contradiction"),
    ("R8 claims fără sursă (proveniență)", "claim_provenance"),
    ("R9 SKU/GTIN duplicat/invalid", "sku_gtin"),
    ("R10 obligatorii per-categorie (v3)", "required_attrs_v3"),
    ("R11 fără best_for", "missing_best_for"),
    ("R12 ai_summary nefondat", "ai_summary_unfounded"),
    ("R13 variante incomplete", "variants_incomplete"),
]
_ALL_KEYS = [key for _, key in RULES_V3]


def _rules_for(contract: str) -> list[tuple[str, str]]:
    return RULES_V3 if contract == "v3" else RULES_V2


def audit(data: dict[str, Any], contract: str = "v2") -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Rulează regulile contractului → `{"violations": {rule: [entry]}, "warnings": {...}}`.
    Fiecare entry = `{message, product_slugs}`. Pur (fără I/O), testabil pe dict. `contract="v2"`
    = R1-R6 (folosit de seed, logică neschimbată); `contract="v3"` = R1-R13."""
    products = data.get("products") or []
    categories = data.get("categories") or []
    roots = build_roots(categories)
    cat_names = _cat_names(categories)

    per_rule: dict[str, list[dict[str, Any]]] = {
        "canonical_enums": rule_canonical_enums(products),
        "required_attrs": rule_required_attrs(products, roots),
        "clean_names": rule_clean_names(products),
        "name_category_coherence": rule_name_category_coherence(products, cat_names),
        "categoryslug_roots": rule_categoryslug_roots(products, roots),
        "comparison_differentiators": rule_comparison_differentiators(products),
    }
    if contract == "v3":
        per_rule["desc_attr_contradiction"] = rule_desc_attr_contradiction(products)
        per_rule["claim_provenance"] = rule_claim_provenance(products)
        per_rule["sku_gtin"] = rule_sku_gtin(products)
        per_rule["required_attrs_v3"] = rule_required_attrs_v3(products, roots)
        per_rule["missing_best_for"] = rule_missing_best_for(products)
        per_rule["ai_summary_unfounded"] = rule_ai_summary_unfounded(products)
        per_rule["variants_incomplete"] = rule_variants_incomplete(products)

    violations: dict[str, list[dict[str, Any]]] = {k: [] for k in _ALL_KEYS}
    warnings: dict[str, list[dict[str, Any]]] = {k: [] for k in _ALL_KEYS}
    for rule_key, findings in per_rule.items():
        for fnd in findings:
            entry = {"message": fnd["message"], "product_slugs": fnd["product_slugs"]}
            bucket = warnings if fnd["severity"] == "warning" else violations
            bucket[rule_key].append(entry)
    return {"violations": violations, "warnings": warnings}


def _validate_schema(data: dict[str, Any], contract: str = "v2") -> list[str]:
    """Validare structurală opțională contra schemei versiunii (dacă `jsonschema` e instalat).
    `v2`→catalog_v2.schema.json, `v3`→catalog_v3.schema.json. Absent → skip."""
    try:
        import jsonschema  # noqa: PLC0415 — soft dep, doar dacă e prezent
    except ImportError:
        return []
    path = SCHEMA_V3_PATH if contract == "v3" else SCHEMA_PATH
    if not path.exists():
        return []
    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(data)][:20]


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("catalog", nargs="?", default=str(DEFAULT_CATALOG))
    ap.add_argument("--contract", choices=["v2", "v3"], default="v2", help="contractul de audit")
    ap.add_argument("--examples", type=int, default=6, help="câte exemple per regulă să listez")
    ap.add_argument("--no-schema", action="store_true", help="sări peste validarea de schemă")
    args = ap.parse_args()

    path = Path(args.catalog)
    if not path.exists():
        print(f"✗ Fișier inexistent: {path}")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"=== AUDIT CATALOG ({args.contract}) — {path.name} ===")
    n_prod = len(data.get("products") or [])
    n_cat = len(data.get("categories") or [])
    print(f"produse={n_prod}  categorii={n_cat}\n")

    schema_errs = [] if args.no_schema else _validate_schema(data, args.contract)
    if schema_errs:
        print(f"SCHEMA ({args.contract}): {len(schema_errs)} erori structurale (primele):")
        for e in schema_errs[: args.examples]:
            print(f"    ✗ {e}")
        print()

    res = audit(data, contract=args.contract)
    violations, warnings = res["violations"], res["warnings"]
    n_viol = sum(len(v) for v in violations.values()) + len(schema_errs)
    n_warn = sum(len(v) for v in warnings.values())

    for label, key in _rules_for(args.contract):
        v, w = violations.get(key, []), warnings.get(key, [])
        mark = "✓" if not v else "✗"
        extra = f" (+{len(w)} warn)" if w else ""
        print(f"{mark} {label}: {len(v)} violations{extra}")
        for entry in v[: args.examples]:
            print(f"      - {entry['message']}")

    print()
    if n_viol == 0:
        print(f"✓ CATALOG CURAT — audit {args.contract} trecut ({n_warn} warnings non-fatale).")
        return 0
    counts = Counter({k: len(v) for k, v in violations.items() if v})
    print(f"✗ AUDIT PICAT — {n_viol} violations total {dict(counts)} ({n_warn} warnings).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
