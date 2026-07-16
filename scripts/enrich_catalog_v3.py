"""NX-168e — enrichment DETERMINIST al catalogului la contractul v3 (tool de autoring offline).

Derivă câmpurile v3 din faptele EXISTENTE (concerns/finish/hair_type/key_ingredients/key_benefit) +
reguli per-categorie — NU inventează ingrediente/beneficii, NU pune instrucțiuni nesigure. Output
static, revizuibil. Câmpurile pur derivate (best_for/ai_summary/description) se REGENERĂ; restul e
idempotent (adaugă doar unde lipsește). Rulează sub gate-ul v3 (evaluate).

    python scripts/enrich_catalog_v3.py            # scrie db/seed/catalog_v2.json
    python scripts/enrich_catalog_v3.py --check    # exit 1 dacă fișierul ar fi modificat (gate CI)

Reguli de SIGURANȚĂ (review Codex): usage din semnale reale (retinol/„de noapte"→seara, SPF/„de
zi"→dimineața) NU un default orb; proveniența e ONESTĂ (`demo_catalog_authored`, nu INCI fabricat);
best_for NU folosește frecvențe generice („uz zilnic") ci un temei semantic per categorie.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_catalog_v2 import build_roots  # noqa: E402

DATA = ROOT / "db" / "seed" / "catalog_v2.json"
BUILD_DATE = "2026-07-16"
# sursă ONESTĂ pt proveniență: catalog demo autorat, NU o etichetă INCI reală (nu fabricăm dovezi)
PROV_SOURCE = "demo_catalog_authored"
PROV_REF = "NX-168e enrichment (fapt de catalog demo — nu etichetă de produs reală)"

CONCERN_RO = {
    "oily": "gras",
    "dry": "uscat",
    "sensitive": "sensibil",
    "combination": "mixt",
    "normal": "normal",
    "acne": "cu tendință acneică",
    "anti_aging": "matur",
    "hyperpigmentation": "cu pete",
    "hydration": "deshidratat",
}
FINISH_RO = {"matte": "mat", "dewy": "luminos", "satin": "satinat", "natural": "natural"}
# hair_type RO → canonic (pt suitable_for; hair_type rămâne RO). Cheile sunt NORMALIZATE
# (fără diacritice), ca „cret"/„creț" să mapeze identic; formele deja canonice (curly) trec.
HAIR_RO_TO_CANON = {
    "uscat": "dry",
    "dry": "dry",
    "gras": "oily",
    "oily": "oily",
    "normal": "normal",
    "deteriorat": "damaged",
    "damaged": "damaged",
    "vopsit": "colored",
    "colored": "colored",
    "cret": "curly",  # _norm(„creț") == „cret"
    "curly": "curly",
    "fin": "fine",
    "fine": "fine",
    "gros": "thick",
    "thick": "thick",
    "frizzy": "frizzy",
}
# hair_type „universal" → fără filtru suitable_for (nu e o valoare canonică)
HAIR_UNIVERSAL = {"toate tipurile", "toate", "orice tip", "orice", "all"}


def _hair_canon(ht: str | None) -> str | None:
    """hair_type RO/canonic (normalizat) → cheie canonică; universal/necunoscut → None."""
    n = _norm(ht or "")
    if not n or n in HAIR_UNIVERSAL:
        return None
    return HAIR_RO_TO_CANON.get(n)


# temei best_for per categorie pt produsele FĂRĂ concerns/finish/hair_type (NU frecvență generică)
CATEGORY_BEST_FOR = {
    "scrub-de-corp": "exfolierea blândă a corpului",
    "lotiuni-de-corp": "hidratarea corpului",
    "geluri-de-dus": "curățarea corpului la duș",
    "deodorante": "prospețime de lungă durată",
    "creme-de-maini": "îngrijirea mâinilor",
    "buze": "îngrijirea buzelor",
    "gloss-de-buze": "buze cu luciu",
    "rujuri": "culoare pe buze",
    "mascara": "definirea și volumul genelor",
    "creioane-si-tusuri-de-ochi": "conturarea privirii",
    "farduri-de-ochi": "machiajul ochilor",
    "pensule-si-bureti-de-machiaj": "aplicarea uniformă a machiajului",
    "pudre": "fixarea și matifierea machiajului",
    "iluminatoare": "un punct de lumină pe ten",
    "bronzer": "un aspect însorit",
    "fard-de-obraz": "obraji cu culoare",
    "spray-de-fixare": "fixarea machiajului",
    "primer-pentru-machiaj": "o bază netedă pentru machiaj",
    "anticearcan": "acoperirea cearcănelor și imperfecțiunilor",
}
ROOT_BEST_FOR = {
    "ingrijirea-tenului": "îngrijirea tenului",
    "machiaj": "un machiaj reușit",
    "ingrijirea-parului": "îngrijirea părului",
    "ingrijire-corp": "îngrijirea corpului",
    "protectie-solara": "protecția solară zilnică",
    "buze": "îngrijirea buzelor",
}

TEXTURE_NORMALIZE = {
    "ser": "fluid",
    "cremă bogată": "cremă",
    "cremă fluidă": "fluid",
    "mască": "cremă",
    "mască cremoasă": "cremă",
    "gel-cremă": "gel",
    "scrub": "cremă",
    "mist": "apă",
}
TEXTURE_DEFAULT = {
    "fond-de-ten": "fluid",
    "creme-bb-si-cc": "fluid",
    "seruri-pentru-ten": "fluid",
    "creme-hidratante": "cremă",
    "creme-de-ochi": "cremă",
    "lotiuni-tonice": "apă",
    "demachiante-pentru-ten": "apă",
    "curatarea-tenului": "gel",
    "exfoliante-pentru-ten": "gel",
    "masti-pentru-ten": "cremă",
    "tratament-local": "gel",
    "mist-pentru-ten": "apă",
}
# usage default per frunză (unde semnalele nu decid). Sigure: AM+PM doar pt produse ne-active-seara.
USAGE_LEAF = {
    "protectie-solara": ["morning"],
    "exfoliante-pentru-ten": ["evening"],
    "tratament-local": ["evening"],
    "masti-pentru-ten": ["occasional"],
    "masti-de-par": ["occasional"],
    "curatarea-tenului": ["morning", "evening"],
    "demachiante-pentru-ten": ["evening"],
    "sampoane": ["daily"],
    "balsamuri-de-par": ["daily"],
    "sampon-uscat": ["occasional"],
    "uleiuri-pentru-par": ["occasional"],
    "ingrijire-fara-clatire": ["daily"],
}
USAGE_ROOT = {"ingrijirea-tenului": ["morning", "evening"], "ingrijirea-parului": ["daily"]}
# semnale de ingredient/nume care impun SEARA (fotosensibilizante / „de noapte")
EVENING_SIGNALS = ("retinol", "retinal", "noapte", "acid glicolic", "acid salicilic", "aha", "bha")

ROUTINE_STEP = {
    "curatarea-tenului": "cleanse",
    "demachiante-pentru-ten": "cleanse",
    "lotiuni-tonice": "tone",
    "seruri-pentru-ten": "treat",
    "tratament-local": "treat",
    "exfoliante-pentru-ten": "treat",
    "masti-pentru-ten": "treat",
    "creme-hidratante": "moisturize",
    "creme-de-ochi": "moisturize",
    "mist-pentru-ten": "moisturize",
    "protectie-solara": "protect",
    "fond-de-ten": "makeup_base",
    "creme-bb-si-cc": "makeup_base",
    "primer-pentru-machiaj": "makeup_base",
    "anticearcan": "makeup_base",
    "rujuri": "makeup_color",
    "gloss-de-buze": "makeup_color",
    "mascara": "makeup_color",
    "farduri-de-ochi": "makeup_color",
    "creioane-si-tusuri-de-ochi": "makeup_color",
    "fard-de-obraz": "makeup_color",
    "bronzer": "makeup_color",
    "iluminatoare": "makeup_color",
    "pudre": "makeup_color",
    "spray-de-fixare": "finish",
}
# gramaj demo per categorie (value, unit) — dată de catalog demo, nu specificație reală
NETCONTENT_DEFAULT = {
    "fond-de-ten": (30, "ml"),
    "creme-bb-si-cc": (40, "ml"),
    "anticearcan": (7, "ml"),
    "seruri-pentru-ten": (30, "ml"),
    "creme-hidratante": (50, "ml"),
    "creme-de-ochi": (15, "ml"),
    "lotiuni-tonice": (200, "ml"),
    "demachiante-pentru-ten": (200, "ml"),
    "curatarea-tenului": (150, "ml"),
    "exfoliante-pentru-ten": (100, "ml"),
    "masti-pentru-ten": (75, "ml"),
    "tratament-local": (15, "ml"),
    "mist-pentru-ten": (100, "ml"),
    "protectie-solara": (50, "ml"),
    "sampoane": (250, "ml"),
    "balsamuri-de-par": (250, "ml"),
    "masti-de-par": (200, "ml"),
    "uleiuri-pentru-par": (50, "ml"),
    "ingrijire-fara-clatire": (150, "ml"),
    "sampon-uscat": (150, "ml"),
    "creme-de-maini": (75, "ml"),
    "lotiuni-de-corp": (250, "ml"),
    "geluri-de-dus": (250, "ml"),
    "scrub-de-corp": (200, "ml"),
    "deodorante": (50, "ml"),
    "rujuri": (4, "g"),
    "gloss-de-buze": (6, "ml"),
    "buze": (15, "ml"),
    "pudre": (10, "g"),
    "fard-de-obraz": (5, "g"),
    "bronzer": (8, "g"),
    "iluminatoare": (6, "g"),
    "mascara": (10, "ml"),
    "spray-de-fixare": (100, "ml"),
    "primer-pentru-machiaj": (30, "ml"),
    "creioane-si-tusuri-de-ochi": (1.2, "g"),
    "farduri-de-ochi": (10, "g"),
}
# categorii FĂRĂ gramaj (unelte — numărate în bucăți, nu volum/masă): justificat, nu lipsă
NO_NETCONTENT = {"pensule-si-bureti-de-machiaj"}
USAGE_RO = {
    "morning": "dimineața",
    "evening": "seara",
    "daily": "zilnic",
    "occasional": "ocazional",
}
MAKEUP_COLOR_ROOT = "machiaj"
TOOLS_SLUG = "pensule-si-bureti-de-machiaj"


def _a(p: dict) -> dict:
    return p.setdefault("attributes", {})


def _norm(s: str) -> str:
    import unicodedata

    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _best_for(p: dict, root: str) -> str:
    """best_for CANONIC din fapte (concerns→RO, finish→RO, hair_type); fallback = temei SEMANTIC per
    categorie (NU frecvență „uz zilnic"). Fără text liber → nu introduce claim-uri R12."""
    a = _a(p)
    slug = p.get("primaryCategorySlug", "")
    ro = [CONCERN_RO[c] for c in (a.get("concerns") or []) if c in CONCERN_RO]
    if root == "ingrijirea-parului":
        ht = a.get("hair_type")
        return (
            f"păr {ht}" if ht else CATEGORY_BEST_FOR.get(slug, ROOT_BEST_FOR.get(root, "îngrijire"))
        )
    if slug == TOOLS_SLUG:
        return CATEGORY_BEST_FOR[TOOLS_SLUG]
    if root == MAKEUP_COLOR_ROOT and a.get("finish"):
        fin = FINISH_RO.get(a["finish"], a["finish"])
        base = f"cine vrea un finish {fin}"
        return f"{base}, pe ten {' și '.join(ro)}" if ro else base
    if ro:
        return f"ten {' și '.join(ro)}"
    return CATEGORY_BEST_FOR.get(slug) or ROOT_BEST_FOR.get(root, "îngrijire generală")


def _usage(p: dict, slug: str, root: str) -> dict | None:
    """usage din SEMNALE reale: retinol/„de noapte"/acizi → seara; SPF/„de zi" → dimineața; altfel
    default per frunză/rădăcină. NU pune „dimineața" pe un produs de noapte."""
    a = _a(p)
    text = (
        _norm(p.get("name", "")) + " " + " ".join(_norm(x) for x in a.get("key_ingredients") or [])
    )
    evening = any(s in text for s in EVENING_SIGNALS) or slug in (
        "exfoliante-pentru-ten",
        "tratament-local",
    )
    morning = bool(a.get("spf")) or slug == "protectie-solara" or "spf" in text or "de zi" in text
    if slug in USAGE_LEAF:
        base = USAGE_LEAF[slug]
    elif root in USAGE_ROOT:
        base = list(USAGE_ROOT[root])
    else:
        return None
    if evening and not morning:
        return {"time": ["evening"]}
    if morning and not evening and root == "ingrijirea-tenului":
        return {"time": ["morning"]}
    return {"time": base}


def _suitable_for(p: dict, root: str) -> list[str] | None:
    """suitable_for CANONIC: din concerns (skincare/makeup) sau hair_type→canonic (păr; universal
    → None = fără filtru)."""
    a = _a(p)
    if a.get("concerns"):
        return list(a["concerns"])
    if root == "ingrijirea-parului":
        c = _hair_canon(a.get("hair_type"))
        return [c] if c else None
    return None


def _provenance(a: dict) -> list[dict]:
    """Proveniență ONESTĂ demo per key_ingredient — sursă = catalog demo autorat (nu INCI real)."""
    return [
        {
            "kind": "ingredient",
            "value": ing,
            "source": PROV_SOURCE,
            "source_ref": PROV_REF,
            "verified_at": BUILD_DATE,
        }
        for ing in a.get("key_ingredients") or []
    ]


def _ai_summary(p: dict) -> str:
    """ai_summary DOAR din fapte canonice (best_for + texture + key_ingredients) — fără nume/text
    liber (ar reintroduce claim-uri R12). Fact-backed."""
    a = _a(p)
    parts = []
    if a.get("best_for"):
        parts.append(f"Recomandat pentru {a['best_for']}.")
    if a.get("texture"):
        parts.append(f"Textură {a['texture']}.")
    ki = a.get("key_ingredients") or []
    if ki:
        parts.append(f"Ingrediente-cheie: {', '.join(ki[:3])}.")
    return " ".join(parts) or "Produs de îngrijire/machiaj."


def _description(p: dict) -> str:
    """description lungă PDP (free text, NU verificată de R12): nume + beneficiu + temei + usage."""
    a = _a(p)
    parts = [p.get("name", "").strip() + "."]
    if a.get("key_benefit"):
        kb = a["key_benefit"]
        parts.append(kb if kb.endswith(".") else kb + ".")
    if a.get("best_for"):
        parts.append(f"Recomandat pentru {a['best_for']}.")
    u = (a.get("usage") or {}).get("time") or []
    if u:
        parts.append("Se folosește " + ", ".join(USAGE_RO.get(t, t) for t in u) + ".")
    ki = a.get("key_ingredients") or []
    if ki:
        parts.append("Ingrediente-cheie: " + ", ".join(ki) + ".")
    return " ".join(parts)


def enrich(data: dict) -> dict[str, int]:
    roots = build_roots(data["categories"])
    counts: dict[str, int] = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    for p in data["products"]:
        a = _a(p)
        slug = p["primaryCategorySlug"]
        root = roots.get(slug, slug)

        if a.get("texture") in TEXTURE_NORMALIZE:
            a["texture"] = TEXTURE_NORMALIZE[a["texture"]]
            bump("texture_normalized")
        if slug == "farduri-de-ochi" and "palet" in p.get("name", "").lower() and a.get("finish"):
            a.pop("finish")
            bump("finish_removed_palette")

        # override-uri umane: câmpurile din `attributes._locked` NU se regenerează
        locked = set(a.get("_locked") or [])

        def put(key: str, value: object, a=a, locked=locked) -> None:
            """Setează un câmp DERIVAT (regenerabil) — respectă _locked; bump doar la schimbare."""
            if key in locked or value is None:
                return
            if a.get(key) != value:
                bump(key)
            a[key] = value

        # texture: fapt-sursă → default doar dacă LIPSEȘTE (nu suprascrie o textură autorată)
        if (
            slug in ("fond-de-ten", "creme-bb-si-cc") or root == "ingrijirea-tenului"
        ) and not a.get("texture"):
            put("texture", TEXTURE_DEFAULT.get(slug))

        # --- DERIVATE REGENERABILE (recalculate din faptele sursă la FIECARE rulare) ---
        if root in ("ingrijirea-tenului", "ingrijirea-parului"):
            put("usage", _usage(p, slug, root))
        put("suitable_for", _suitable_for(p, root))
        if slug in ROUTINE_STEP:
            put("routine_step", ROUTINE_STEP[slug])
        # claim_provenance: regenerat din key_ingredients; dacă nu mai sunt, se scoate
        if a.get("key_ingredients"):
            put("claim_provenance", _provenance(a))
        elif "claim_provenance" not in locked and a.pop("claim_provenance", None) is not None:
            bump("claim_provenance_removed")
        if slug == TOOLS_SLUG:
            kb = (a.get("key_benefit") or "").rstrip(".")
            diffs = [s.strip() for s in kb.split(",") if s.strip()][:3] or ["calitate profesională"]
            put("differentiators", diffs)

        # net_content: pe VARIANTE dacă există, altfel FALLBACK pe produs (104 fără variante).
        nc = NETCONTENT_DEFAULT.get(slug)
        if nc:
            val = {"value": nc[0], "unit": nc[1]}
            variants = p.get("variants") or []
            if variants:
                for v in variants:
                    if "net_content" in set(v.get("_locked") or []):
                        continue
                    if v.get("net_content") != val:
                        v["net_content"] = val
                        bump("variant_net_content")
            else:
                put("net_content", val)

        # DERIVATE pure text → regenerate mereu (după ce faptele de mai sus sunt setate)
        if "best_for" not in locked:
            a["best_for"] = _best_for(p, root)
        p["ai_summary"] = _ai_summary(p)
        p["description"] = _description(p)

    return counts


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    check = "--check" in sys.argv
    original = DATA.read_text(encoding="utf-8")
    data = json.loads(original)
    counts = enrich(copy.deepcopy(data) if check else data)
    if check:
        # re-derivă pe o copie și compară cu fișierul → exit 1 dacă ar fi modificat (gate CI)
        enriched = json.loads(original)
        enrich(enriched)
        would_change = json.dumps(enriched, ensure_ascii=False, indent=2) + "\n" != original
        print("=== ENRICHMENT v3 --check ===")
        for k, n in sorted(counts.items()):
            print(f"  {n:3d}  {k}")
        status = (
            "✗ fișierul AR FI modificat (rulează fără --check)" if would_change else "✓ up-to-date"
        )
        print(f"\n{status}")
        return 1 if would_change else 0
    print("=== ENRICHMENT v3 (derivat determinist) ===")
    for k, n in sorted(counts.items()):
        print(f"  +{n:3d}  {k}")
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ scris {DATA.name} ({len(data['products'])} produse).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
