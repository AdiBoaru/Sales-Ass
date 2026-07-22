"""NX-197 — completările de date: variantă implicită, GTIN, recenzii, badges.

Cele patru goluri măsurate pe cele 300 de produse servite:

    produse cu variante   118/300   → fără SKU, fără gramaj, fără preț/unitate pe 182 de produse
    GTIN pe variante        2/743
    recenzii individuale  150/300   → produsele noi n-aveau niciuna
    badges                162/300

**Varianta implicită (decizia C4).** În e-commerce, unitatea vandabilă e varianta. Un produs fără
opțiuni are o singură variantă — nu zero. Uniformizarea dă SKU, gramaj și preț-pe-unitate pe TOATE
produsele, adică exact faptele cu care botul răspunde la «care e mai avantajos».
Riscul de interfață (un selector cu o singură opțiune) se rezolvă la afișare, nu în date:
`_card_variants` ascunde selectorul sub 2 opțiuni, dar modelul primește în continuare gramajul.

**GTIN** valid GS1 (mod-10), derivat determinist din SKU. Pe un catalog demo cu branduri fictive
codul e fictiv și el — dar VALID structural, altfel `clean_gtin` îl aruncă și auditul R9 îl
semnalează. Un cod invalid ar fi mai rău decât niciunul.

**Recenziile** se compun din fraze scrise (per fațetă: textură, finish, ingredient, tip de păr),
cu rating coerent cu media produsului. Nu inventează experiențe medicale și nu conțin prețuri.

Determinist, fără LLM, idempotent:

    python scripts/enrich_catalog_extras.py --report
    python scripts/enrich_catalog_extras.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "db" / "seed" / "catalog_v2.json"

#: prefix GS1 fictiv, dar coerent (594 = România). Ultima cifră e cea de control, calculată.
GTIN_PREFIX = "594"

#: recenzii: nume RO uzuale, fără diacritice problematice în afișare
AUTHORS = (
    "Ana M.",
    "Ioana P.",
    "Cristina V.",
    "Elena R.",
    "Maria D.",
    "Alexandra S.",
    "Andreea T.",
    "Diana C.",
    "Raluca N.",
    "Mihaela B.",
    "Bianca L.",
    "Roxana G.",
)

#: fraze de recenzie pe FAȚETĂ — se aleg după ce are produsul, deci recenzia vorbește despre
#: ceva real din fișă, nu despre generalități
REVIEW_BY_FACET = {
    "texture:gel": (
        "Se absoarbe rapid și nu lasă senzație lipicioasă.",
        "Textura de gel mi se pare ideală vara.",
    ),
    "texture:cremă": (
        "Textura e confortabilă, nu încarcă pielea.",
        "Cremoasă, dar nu grea — exact ce căutam.",
    ),
    "texture:fluid": (
        "Se întinde ușor și dispare repede.",
        "Fluid, se absoarbe în câteva secunde.",
    ),
    "texture:apă": (
        "Foarte ușor, se simte ca apa pe piele.",
        "Se aplică rapid, fără să ude prea tare.",
    ),
    "texture:ulei": (
        "O picătură-două ajung pentru toate lungimile.",
        "Nu lasă senzație grasă, m-a surprins.",
    ),
    "texture:spumă": ("Spumează bine cu puțin produs.", "Se clătește ușor, fără reziduu."),
    "texture:balsam": ("Se topește la contactul cu pielea.", "Confortabil, mai ales seara."),
    "finish:matte": ("Finishul mat ține bine toată ziua.", "Matifiază fără să usuce."),
    "finish:dewy": (
        "Aspectul luminos arată natural, nu gras.",
        "Îmi place cât de proaspăt arată pielea.",
    ),
    "finish:satin": (
        "Finish satinat, foarte purtabil zi de zi.",
        "Nu e nici mat, nici lucios — echilibrat.",
    ),
    "finish:natural": ("Arată natural, nu se vede că port ceva.", "Discret, exact cum voiam."),
    "concern:hydration": ("Pielea nu mai strânge după curățare.", "Hidratarea ține până seara."),
    "concern:dry": (
        "Am tenul uscat și mi-a rezolvat senzația de disconfort.",
        "Bun pentru iarnă, când pielea se descuamează.",
    ),
    "concern:oily": ("Ține luciul sub control fără să usuce.", "Zona T arată mult mai bine."),
    "concern:sensitive": (
        "Nu mi-a dat nicio reacție, deși pielea mea e pretențioasă.",
        "Blând, îl folosesc și în perioadele mai delicate.",
    ),
    "concern:anti_aging": (
        "După câteva săptămâni am observat textura mai netedă.",
        "Rezultatele vin lent, dar vin.",
    ),
    "concern:hyperpigmentation": (
        "Petele s-au estompat treptat.",
        "Tonul e mai uniform decât acum două luni.",
    ),
    "hair:uscat": ("Părul se simte mult mai moale.", "Nu se mai încâlcește la pieptănat."),
    "hair:deteriorat": ("Vârfurile arată vizibil mai bine.", "Se rupe mai puțin la periat."),
    "hair:vopsit": ("Culoarea ține mai mult între vopsiri.", "Nu-mi usucă părul vopsit."),
    "hair:fin": ("Nu îngreunează deloc părul fin.", "Volumul rezistă până seara."),
    "hair:gras": ("Scalpul rămâne curat mai mult timp.", "Pot să spăl părul mai rar."),
}
GENERIC_REVIEWS = (
    "Se vede că e gândit bine — îl recomand.",
    "Raport bun între ce promite și ce face.",
    "L-am cumpărat a doua oară, deci vorbește de la sine.",
    "Ambalajul e practic, se dozează ușor.",
)

#: badge-uri derivate din FAPTE. Fiecare are un temei verificabil în catalog — fără „premium".
BADGE_RULES = [
    ("fragrance_free", lambda a, p: bool(a.get("fragrance_free")), "Fără parfum"),
    ("spf", lambda a, p: bool(a.get("spf")), None),  # eticheta se compune cu valoarea
    (
        "sensitive",
        lambda a, p: "sensitive" in (a.get("suitable_for") or a.get("concerns") or []),
        "Potrivit pentru ten sensibil",
    ),
    ("bestseller", lambda a, p: int(p.get("reviewCount") or 0) >= 250, "Best-seller"),
    ("top_rated", lambda a, p: float(p.get("rating") or 0) >= 4.7, "Foarte bine cotat"),
    ("vegan_free", lambda a, p: bool(a.get("alcohol_free")), "Fără alcool"),
]


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _h(key: str, salt: str = "") -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:12], 16)


def _pick(seq, key: str, salt: str):
    return seq[_h(key, salt) % len(seq)]


def gtin_for(sku: str) -> str:
    """GTIN-13 valid GS1: 12 cifre derivate determinist din SKU + cifra de control mod-10.
    Structural corect — altfel `clean_gtin` îl aruncă și auditul R9 îl semnalează."""
    body = (GTIN_PREFIX + f"{_h(sku, 'gtin'):d}")[:12].ljust(12, "0")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def _slug_sku(slug: str) -> str:
    base = re.sub(r"[^A-Z0-9]+", "", _norm(slug).upper())[:12]
    return f"{base}-{_h(slug, 'sku') % 10000:04d}"


def default_variant(p: dict) -> dict:
    """Varianta implicită a unui produs fără opțiuni. Eticheta e gramajul, dacă îl știm — altfel
    „Standard". Prețul și stocul vin de pe produs, ca să nu apară două adevăruri diferite."""
    a = p.get("attributes") or {}
    nc = a.get("net_content") or {}
    if nc.get("value") and nc.get("unit"):
        val = nc["value"]
        val = int(val) if isinstance(val, float) and float(val).is_integer() else val
        label = f"{val} {nc['unit']}"
    else:
        label = "Standard"
    sku = _slug_sku(p["slug"])
    v = {
        "label": label,
        "sku": sku,
        "gtin": gtin_for(sku),
        "price": float(p.get("price") or 0),
        "stock": int(p.get("stock") or 0),
    }
    if p.get("salePrice"):
        v["salePrice"] = float(p["salePrice"])
    if nc.get("value") and nc.get("unit"):
        v["net_content"] = {"value": nc["value"], "unit": nc["unit"]}
    return v


def build_reviews(p: dict) -> dict:
    """`reviewSummary` (pros/cons + sumar) + recenzii individuale, compuse din fraze scrise pe
    FAȚETELE produsului — nu generalități. Ratingurile individuale gravitează în jurul mediei."""
    a = p.get("attributes") or {}
    slug = p["slug"]
    keys = []
    if a.get("texture"):
        keys.append(f"texture:{a['texture']}")
    if a.get("finish"):
        keys.append(f"finish:{a['finish']}")
    for c in a.get("concerns") or []:
        keys.append(f"concern:{c}")
    if a.get("hair_type"):
        keys.append(f"hair:{a['hair_type']}")

    phrases: list[str] = []
    for k in keys:
        pool = REVIEW_BY_FACET.get(k)
        if pool:
            phrases.append(_pick(pool, slug, k))
    while len(phrases) < 2:
        cand = _pick(GENERIC_REVIEWS, slug, f"gen{len(phrases)}")
        if cand not in phrases:
            phrases.append(cand)
        else:
            phrases.append(GENERIC_REVIEWS[(len(phrases) + 1) % len(GENERIC_REVIEWS)])

    rating = float(p.get("rating") or 4.5)
    items = []
    for i, text in enumerate(phrases[:3]):
        r = 5 if rating >= 4.6 or i == 0 else 4
        items.append({"author": _pick(AUTHORS, slug, f"a{i}"), "rating": r, "body": text})

    pros = [ph.rstrip(".").lower() for ph in phrases[:2]]
    cons_pool = (
        "ambalajul ar putea fi mai practic",
        "mi-ar plăcea și într-un format mai mare",
        "prețul e puțin peste ce plăteam înainte",
    )
    return {
        "summary": " ".join(phrases[:2]),
        "topPros": pros,
        "topCons": [_pick(cons_pool, slug, "con")],
        "items": items,
    }


def build_badges(p: dict) -> list[str]:
    a = p.get("attributes") or {}
    out: list[str] = []
    for key, rule, label in BADGE_RULES:
        if not rule(a, p):
            continue
        if key == "spf":
            out.append(f"Cu SPF {a['spf']}")
        elif label:
            out.append(label)
    return out[:4]


def enrich(data: dict) -> dict[str, int]:
    counts = {"variants": 0, "gtin": 0, "reviews": 0, "badges": 0}
    seen_sku: set[str] = set()
    for p in data["products"]:
        for v in p.get("variants") or []:
            if v.get("sku"):
                seen_sku.add(str(v["sku"]))

    for p in data["products"]:
        variants = p.get("variants") or []
        if not variants:
            v = default_variant(p)
            if v["sku"] in seen_sku:  # coliziune (teoretic imposibilă) → sufixăm determinist
                v["sku"] = f"{v['sku']}-{_h(p['slug'], 'dup') % 100:02d}"
                v["gtin"] = gtin_for(v["sku"])
            seen_sku.add(v["sku"])
            p["variants"] = [v]
            counts["variants"] += 1
            counts["gtin"] += 1
        else:
            for v in variants:
                if not v.get("gtin") and v.get("sku"):
                    v["gtin"] = gtin_for(str(v["sku"]))
                    counts["gtin"] += 1

        if not p.get("reviewSummary"):
            p["reviewSummary"] = build_reviews(p)
            counts["reviews"] += 1

        if not (p.get("attributes") or {}).get("badges") and not p.get("badges"):
            b = build_badges(p)
            if b:
                p["badges"] = b
                counts["badges"] += 1
    return counts


def report(data: dict) -> None:
    ps = data["products"]
    n = len(ps)
    with_var = sum(1 for p in ps if p.get("variants"))
    gtin = sum(1 for p in ps for v in p.get("variants") or [] if v.get("gtin"))
    tot_var = sum(len(p.get("variants") or []) for p in ps)
    revs = sum(1 for p in ps if p.get("reviewSummary"))
    badges = sum(1 for p in ps if p.get("badges"))
    print(f"produse: {n}")
    print(f"  cu variante        : {with_var}/{n}")
    print(f"  GTIN pe variante   : {gtin}/{tot_var}")
    print(f"  cu reviewSummary   : {revs}/{n}")
    print(f"  cu badges          : {badges}/{n}")


def main() -> int:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    counts = enrich(data)
    if "--report" in sys.argv:
        report(data)
        print("\n(ar adăuga)", counts)
        return 0
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=== COMPLETĂRI DE DATE ===")
    print(f"  variante implicite adăugate: {counts['variants']}")
    print(f"  GTIN generate              : {counts['gtin']}")
    print(f"  reviewSummary adăugate     : {counts['reviews']}")
    print(f"  badges adăugate            : {counts['badges']}")
    report(data)
    print(f"\n✓ scris {DATA.name}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
