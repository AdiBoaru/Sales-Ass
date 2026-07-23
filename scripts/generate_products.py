"""NX-193 — completează catalogul până la ținta de produse, GENERÂND în categoriile subțiri.

De ce generăm și nu mai revitalizăm: arhiva coerentă s-a epuizat sub plafon. Din 228 de produse
coerente, 207 stăteau în 5 categorii (54 seruri, 51 măști, 36 pensule…), iar plafonul le-a oprit
exact ca să nu strâmbe catalogul. Restul categoriilor au 3-6 produse — prea puțin ca botul să aibă
ce compara, ce recomanda ca alternativă și ce pune într-o rutină.

Generarea e DETERMINISTĂ (sha256 pe cheia produsului) și **derivată din rețete per categorie**, nu
din text liber: fiecare produs primește exact faptele pe care contractul v3 le cere categoriei lui.
Nu se scrie nicio propoziție de marketing — `enrich_catalog_v3` compune ulterior best_for,
ai_summary, usage și description din faptele astea.

Varietatea vine din combinarea a patru axe reale (brand × linie de gamă × ingredient-erou ×
fațetă), nu din sinonime: două produse din aceeași categorie diferă prin ingredient, textură,
preț ȘI rating — altfel auditul R6 le-ar semnala ca „fără diferențiator", pe bună dreptate.

    python scripts/generate_products.py --report     # ce ar genera, fără scriere
    python scripts/generate_products.py --target 300 # completează până la 300 și scrie
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

DEFAULT_TARGET = 300
#: nicio categorie sub atât — sub 7 produse nu ai ce compara și n-ai alternativă la stoc epuizat
MIN_PER_CATEGORY = 7

#: linii de gamă, grupate pe intenție. Sunt cuvintele care dau varietate numelui fără să mintă.
LINES_CARE = ("Hydra", "Calm", "Repair", "Balance", "Pure", "Nutri", "Revive", "Soft")
LINES_MAKEUP = ("Velvet", "Nude", "Matte", "Glow", "Silk", "Luxe", "Prime", "Aura")
LINES_BODY = ("Fresh", "Bloom", "Silk", "Pure", "Nutri", "Sun", "Ritual", "Clean")

#: ingrediente-erou pe intenție. Toate din vocabularul recunoscut de audit (R12) sau uzuale RO.
ING_HYDRATION = ("acid hialuronic", "glicerină", "panthenol", "squalan")
ING_SOOTHING = ("bisabolol", "centella", "alantoină", "ovăz coloidal")
ING_OILY = ("niacinamidă", "acid salicilic", "argilă", "zinc")
ING_AGING = ("peptide", "retinol", "vitamina C", "colagen")
ING_BARRIER = ("ceramide", "unt de shea", "ulei de jojoba", "vitamina E")
ING_HAIR = ("keratină", "ulei de argan", "panthenol", "proteine din mătase")

#: rețeta per categorie: ce fapte primește un produs nou ca să treacă contractul v3 al categoriei.
#: `concerns`/`hair_type`/`finish`/`coverage`/`spf` sunt cele pe care auditul le cere EXPLICIT;
#: restul (texture/usage/routine_step/net_content/best_for/ai_summary) le derivă enrich-ul.
RECIPES: dict[str, dict] = {
    # --- îngrijirea tenului (cer: concerns + key_ingredients; texture/usage derivate) ---
    "creme-de-ochi": {
        "t": "Cremă de ochi",
        "l": LINES_CARE,
        "p": (69, 129),
        "c": (("hydration", "dry"), ("anti_aging",), ("sensitive",)),
        "i": (ING_HYDRATION, ING_AGING, ING_SOOTHING),
    },
    "exfoliante-pentru-ten": {
        "t": "Exfoliant pentru ten",
        "l": LINES_CARE,
        "p": (55, 99),
        "c": (("oily", "acne"), ("combination",), ("hyperpigmentation",)),
        "i": (ING_OILY, ING_AGING, ING_HYDRATION),
    },
    "tratament-local": {
        "t": "Tratament local",
        "l": LINES_CARE,
        "p": (45, 89),
        "c": (("acne", "oily"), ("acne",)),
        "i": (ING_OILY, ING_SOOTHING),
    },
    "mist-pentru-ten": {
        "t": "Mist pentru ten",
        "l": LINES_CARE,
        "p": (39, 79),
        "c": (("hydration",), ("sensitive", "hydration")),
        "i": (ING_HYDRATION, ING_SOOTHING),
    },
    "creme-hidratante": {
        "t": "Cremă hidratantă",
        "l": LINES_CARE,
        "p": (65, 139),
        "c": (("dry", "hydration"), ("normal",), ("sensitive", "dry")),
        "i": (ING_BARRIER, ING_HYDRATION, ING_SOOTHING),
    },
    "demachiante-pentru-ten": {
        "t": "Apă micelară",
        "l": LINES_CARE,
        "p": (35, 69),
        "c": (("sensitive",), ("normal", "hydration")),
        "i": (ING_SOOTHING, ING_HYDRATION),
    },
    # --- protecție solară (cere spf) ---
    "protectie-solara": {
        "t": "Cremă cu SPF",
        "l": LINES_CARE,
        "p": (69, 129),
        "c": (("sensitive",), ("normal",), ("hyperpigmentation",)),
        "i": (ING_SOOTHING, ING_BARRIER, ING_AGING),
        "spf": (30, 50),
    },
    # --- păr (cere hair_type; usage derivat) ---
    "masti-de-par": {
        "t": "Mască de păr",
        "l": LINES_CARE,
        "p": (49, 99),
        "h": ("uscat", "deteriorat", "vopsit", "creț"),
        "i": (ING_HAIR,),
    },
    "balsamuri-de-par": {
        "t": "Balsam de păr",
        "l": LINES_CARE,
        "p": (39, 79),
        "h": ("uscat", "fin", "vopsit", "normal"),
        "i": (ING_HAIR,),
    },
    "uleiuri-pentru-par": {
        "t": "Ulei de păr",
        "l": LINES_CARE,
        "p": (45, 89),
        "h": ("uscat", "deteriorat", "creț"),
        "i": (ING_HAIR,),
    },
    "ingrijire-fara-clatire": {
        "t": "Cremă leave-in",
        "l": LINES_CARE,
        "p": (42, 85),
        "h": ("creț", "deteriorat", "fin", "uscat"),
        "i": (ING_HAIR,),
    },
    "sampon-uscat": {
        "t": "Șampon uscat",
        "l": LINES_CARE,
        "p": (35, 69),
        "h": ("gras", "fin", "normal"),
        "i": (ING_HAIR,),
    },
    # --- corp (fără cerințe dure) ---
    "lotiuni-de-corp": {
        "t": "Loțiune de corp",
        "l": LINES_BODY,
        "p": (39, 85),
        "c": (("dry", "hydration"), ("sensitive",)),
        "i": (ING_BARRIER, ING_SOOTHING),
    },
    "scrub-de-corp": {
        "t": "Scrub de corp",
        "l": LINES_BODY,
        "p": (39, 79),
        "c": (("dry",), ("normal",)),
        "i": (ING_BARRIER, ING_HYDRATION),
    },
    "geluri-de-dus": {
        "t": "Gel de duș",
        "l": LINES_BODY,
        "p": (25, 55),
        "c": (("sensitive",), ("normal", "hydration")),
        "i": (ING_SOOTHING, ING_HYDRATION),
    },
    "deodorante": {
        "t": "Deodorant",
        "l": LINES_BODY,
        "p": (29, 59),
        "c": (("sensitive",), ("normal",)),
        "i": (ING_SOOTHING,),
    },
    "creme-de-maini": {
        "t": "Cremă de mâini",
        "l": LINES_BODY,
        "p": (25, 49),
        "c": (("dry", "hydration"),),
        "i": (ING_BARRIER, ING_HYDRATION),
    },
    "buze": {
        "t": "Balsam de buze",
        "l": LINES_BODY,
        "p": (19, 45),
        "c": (("dry", "hydration"),),
        "i": (ING_BARRIER, ING_HYDRATION),
    },
    # --- machiaj (cere finish; complexion cere și coverage + suitable_for + texture) ---
    "fond-de-ten": {
        "t": "Fond de ten",
        "l": LINES_MAKEUP,
        "p": (69, 139),
        "c": (("normal", "combination"), ("dry",), ("oily",)),
        "f": ("natural", "matte", "dewy", "satin"),
        "cov": ("light", "medium", "full"),
        "tex": "fluid",
        "shades": True,
    },
    "creme-bb-si-cc": {
        "t": "Cremă BB",
        "l": LINES_MAKEUP,
        "p": (59, 109),
        "c": (("normal",), ("dry", "hydration")),
        "f": ("natural", "dewy"),
        "cov": ("light", "medium"),
        "tex": "fluid",
        "shades": True,
    },
    "anticearcan": {
        "t": "Anticearcăn",
        "l": LINES_MAKEUP,
        "p": (45, 89),
        "c": (("normal",), ("dry",)),
        "f": ("natural", "satin"),
        "cov": ("medium", "full"),
        "shades": True,
    },
    "pudre": {"t": "Pudră", "l": LINES_MAKEUP, "p": (45, 95), "f": ("matte", "natural")},
    "iluminatoare": {"t": "Iluminator", "l": LINES_MAKEUP, "p": (45, 89), "f": ("dewy", "satin")},
    "bronzer": {"t": "Bronzer", "l": LINES_MAKEUP, "p": (49, 95), "f": ("matte", "satin")},
    "fard-de-obraz": {
        "t": "Fard de obraz",
        "l": LINES_MAKEUP,
        "p": (39, 85),
        "f": ("matte", "satin", "dewy"),
        "shades": True,
    },
    "rujuri": {
        "t": "Ruj",
        "l": LINES_MAKEUP,
        "p": (35, 79),
        "f": ("matte", "satin", "dewy"),
        "shades": True,
    },
    "gloss-de-buze": {
        "t": "Gloss de buze",
        "l": LINES_MAKEUP,
        "p": (29, 65),
        "f": ("dewy",),
        "shades": True,
    },
    "spray-de-fixare": {
        "t": "Spray de fixare",
        "l": LINES_MAKEUP,
        "p": (45, 89),
        "f": ("matte", "natural", "dewy"),
    },
    "primer-pentru-machiaj": {
        "t": "Primer pentru machiaj",
        "l": LINES_MAKEUP,
        "p": (49, 99),
        "f": ("matte", "natural", "dewy"),
    },
    # --- ochi: cer key_benefit, NU finish (paletele au finishuri mixte) ---
    "mascara": {
        "t": "Mascara",
        "l": LINES_MAKEUP,
        "p": (39, 85),
        "kb": (
            "Volum și separare bună a genelor.",
            "Alungire vizibilă, fără aglomerare.",
            "Curbare de durată, rezistentă la umezeală.",
        ),
    },
    "creioane-si-tusuri-de-ochi": {
        "t": "Creion de ochi",
        "l": LINES_MAKEUP,
        "p": (25, 59),
        "kb": (
            "Linie precisă, ușor de estompat.",
            "Pigment intens dintr-o singură trecere.",
            "Vârf fin pentru contur exact.",
        ),
    },
    "farduri-de-ochi": {
        "t": "Fard de ochi",
        "l": LINES_MAKEUP,
        "p": (35, 89),
        "kb": (
            "Pigmentare bogată și estompare ușoară.",
            "Nuanțe care se pot suprapune fără să se tulbure.",
            "Textură fină, fără praf la aplicare.",
        ),
    },
    # --- unelte: cer key_benefit + differentiators ---
    "pensule-si-bureti-de-machiaj": {
        "t": "Pensulă de machiaj",
        "l": LINES_MAKEUP,
        "p": (29, 79),
        "kb": (
            "Fire dense pentru aplicare uniformă.",
            "Formă conică pentru estompare precisă.",
            "Mâner echilibrat, control bun al presiunii.",
        ),
        "diff": (
            ("fire sintetice", "estompare uniformă"),
            ("vârf conic", "control precis"),
            ("densitate mare", "acoperire rapidă"),
        ),
    },
    # --- restul de skincare ---
    "seruri-pentru-ten": {
        "t": "Ser pentru ten",
        "l": LINES_CARE,
        "p": (79, 159),
        "c": (("hydration",), ("anti_aging",), ("oily", "acne"), ("hyperpigmentation",)),
        "i": (ING_HYDRATION, ING_AGING, ING_OILY),
    },
    "lotiuni-tonice": {
        "t": "Loțiune tonică",
        "l": LINES_CARE,
        "p": (39, 79),
        "c": (("hydration",), ("oily",), ("sensitive",)),
        "i": (ING_HYDRATION, ING_OILY, ING_SOOTHING),
    },
    "masti-pentru-ten": {
        "t": "Mască de față",
        "l": LINES_CARE,
        "p": (45, 95),
        "c": (("hydration", "dry"), ("oily",), ("sensitive",)),
        "i": (ING_HYDRATION, ING_OILY, ING_SOOTHING),
    },
    "curatarea-tenului": {
        "t": "Gel de curățare",
        "l": LINES_CARE,
        "p": (35, 79),
        "c": (("oily", "acne"), ("sensitive",), ("normal",)),
        "i": (ING_OILY, ING_SOOTHING, ING_HYDRATION),
    },
    "sampoane": {
        "t": "Șampon",
        "l": LINES_CARE,
        "p": (35, 79),
        "h": ("uscat", "gras", "vopsit", "fin", "deteriorat"),
        "i": (ING_HAIR,),
    },
}

SHADES = (
    ("01 Porcelain", "#F6E3D5"),
    ("02 Ivory", "#F0D9C4"),
    ("03 Beige", "#E3C3A5"),
    ("05 Sand", "#D2A97F"),
    ("07 Amber", "#B98455"),
)
LIP_SHADES = (
    ("Rosewood", "#B25A62"),
    ("Nude Peach", "#D89A82"),
    ("Cherry", "#9E2231"),
    ("Mauve", "#A9707F"),
)

#: aceeași cheie canonică se spune DIFERIT după zona de aplicare: „ten uscat" pe față, dar
#: „piele uscată" pe corp și „buze uscate" pe buze. Fără asta ies formulări false ca
#: „Balsam de buze pentru ten uscat" sau „Deodorant pentru ten normal".
CONCERN_RO_BY_ZONE: dict[str, dict[str, str]] = {
    "ten": {
        "hydration": "hidratare",
        "dry": "ten uscat",
        "oily": "ten gras",
        "sensitive": "ten sensibil",
        "combination": "ten mixt",
        "acne": "ten cu tendință acneică",
        "anti_aging": "ten matur",
        "hyperpigmentation": "ten cu pete",
        "normal": "ten normal",
    },
    "corp": {
        "hydration": "hidratare",
        "dry": "piele uscată",
        "sensitive": "piele sensibilă",
        "normal": "piele normală",
    },
    "buze": {
        "hydration": "hidratare",
        "dry": "buze uscate",
        "sensitive": "buze sensibile",
        "normal": "îngrijire zilnică",
    },
}
#: zona de aplicare per categorie (implicit „ten")
ZONE = {
    "lotiuni-de-corp": "corp",
    "scrub-de-corp": "corp",
    "geluri-de-dus": "corp",
    "deodorante": "corp",
    "creme-de-maini": "corp",
    "buze": "buze",
}


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _slugify(s: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", _norm(s)).strip("-"))


def _h(key: str, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:12], 16)


def _pick(seq, key: str, salt: str):
    return seq[_h(key, salt) % len(seq)]


def _price(rng: tuple[int, int], key: str) -> float:
    lo, hi = rng
    span = max(1, hi - lo)
    # ...,99 / ...,49 — prețuri care arată a magazin, nu a generator
    base = lo + _h(key, "price") % span
    return round(base + (0.99 if _h(key, "cents") % 2 else 0.49), 2)


def build_product(cat: str, recipe: dict, brand: dict, idx: int, taken: set[str]) -> dict | None:
    key = f"{cat}|{brand['slug']}|{idx}"
    line = _pick(recipe["l"], key, "line")
    type_ro = recipe["t"]

    attrs: dict = {}
    ing_pool = recipe.get("i")
    hero = None
    if ing_pool:
        pool = _pick(ing_pool, key, "ingpool")
        hero = _pick(pool, key, "hero")
        second = pool[(pool.index(hero) + 1) % len(pool)]
        attrs["key_ingredients"] = [hero, second]
    if "c" in recipe:
        attrs["concerns"] = list(_pick(recipe["c"], key, "concern"))
    if "h" in recipe:
        attrs["hair_type"] = _pick(recipe["h"], key, "hair")
    if "f" in recipe:
        attrs["finish"] = _pick(recipe["f"], key, "finish")
    if "cov" in recipe:
        attrs["coverage"] = _pick(recipe["cov"], key, "cov")
    if "tex" in recipe:
        attrs["texture"] = recipe["tex"]
    if "spf" in recipe:
        attrs["spf"] = _pick(recipe["spf"], key, "spf")
    if "kb" in recipe:
        attrs["key_benefit"] = _pick(recipe["kb"], key, "kb")
    if "diff" in recipe:
        attrs["differentiators"] = list(_pick(recipe["diff"], key, "diff"))
    attrs["_meta"] = {"generated": True}

    # numele: brand + linie + tip; dacă se lovește, adaugă ingredientul-erou sau fațeta
    base_name = f"{brand['name']} {line} {type_ro}"
    name = base_name
    if _norm(name) in taken and hero:
        name = f"{base_name} cu {hero}"
    if _norm(name) in taken and attrs.get("finish"):
        name = f"{base_name} {attrs['finish']}"
    if _norm(name) in taken and attrs.get("hair_type"):
        name = f"{base_name} pentru păr {attrs['hair_type']}"
    if _norm(name) in taken:
        return None
    slug = _slugify(name)

    price = _price(recipe["p"], key)
    rating = round(4.1 + (_h(key, "rating") % 9) / 10, 2)
    reviews = 18 + _h(key, "rev") % 380

    # descrierea scurtă: DIN FAPTE, fără marketing
    # „Exfoliant pentru ten pentru ten mixt" — tipul care conține deja zona nu o repetă
    short_type = re.sub(r"\s+pentru (ten|păr|corp)$", "", type_ro)
    if attrs.get("hair_type"):
        lead = f"{short_type} pentru păr {attrs['hair_type']}."
    elif attrs.get("concerns"):
        vocab = CONCERN_RO_BY_ZONE[ZONE.get(cat, "ten")]
        ro = [vocab[c] for c in attrs["concerns"] if c in vocab][:2]
        lead = f"{short_type} pentru {' și '.join(ro)}." if ro else f"{short_type}."
    elif attrs.get("finish"):
        lead = f"{type_ro} cu finish {attrs['finish']}."
    else:
        lead = f"{type_ro}."
    tail = f" Cu {', '.join(attrs['key_ingredients'])}." if attrs.get("key_ingredients") else ""

    product: dict = {
        "slug": slug,
        "name": name,
        "brandSlug": brand["slug"],
        "primaryCategorySlug": cat,
        "categorySlugs": [cat],
        "price": price,
        "currency": "RON",
        "rating": rating,
        "reviewCount": reviews,
        "status": "active",
        "shortDescription": (lead + tail).strip(),
        "attributes": attrs,
    }

    if recipe.get("shades"):
        pool = LIP_SHADES if cat in ("rujuri", "gloss-de-buze", "fard-de-obraz") else SHADES
        n = 3 + _h(key, "nshades") % 2
        start = _h(key, "shadestart") % len(pool)
        variants = []
        for i in range(n):
            label, hexv = pool[(start + i) % len(pool)]
            variants.append(
                {
                    "label": label,
                    "sku": "GEN-"
                    + hashlib.sha1(f"{slug}|{label}".encode()).hexdigest()[:12].upper(),
                    "price": price,
                    "stock": 6 + _h(f"{key}|{i}", "vstock") % 40,
                    "colorHex": hexv,
                    "attributes": {"shade": _slugify(label.split(" ", 1)[-1])},
                }
            )
        product["variants"] = variants
    return product


def generate(data: dict, target: int) -> tuple[list[dict], dict]:
    products = data["products"]
    brands = data["brands"]
    counts: dict[str, int] = {}
    for p in products:
        counts[p["primaryCategorySlug"]] = counts.get(p["primaryCategorySlug"], 0) + 1
    taken_names = {_norm(p["name"]) for p in products}
    taken_slugs = {p["slug"] for p in products}
    # R6: două produse din aceeași categorie cu preț+rating identice n-au diferențiator
    shapes = {
        (
            p["primaryCategorySlug"],
            round(float(p.get("price") or 0), 2),
            round(float(p.get("rating") or 0), 2),
        )
        for p in products
    }

    need = target - len(products)
    out: list[dict] = []
    if need <= 0:
        return out, {"generate": 0}

    # ordinea: cele mai subțiri categorii întâi, apoi rotim — așa nu îngrășăm o singură ramură
    order = sorted(
        (c for c in RECIPES if counts.get(c, 0) < MIN_PER_CATEGORY),
        key=lambda c: (counts.get(c, 0), c),
    )
    if not order:
        order = sorted(RECIPES, key=lambda c: (counts.get(c, 0), c))

    idx = 0
    guard = 0
    while len(out) < need and guard < need * 40:
        guard += 1
        cat = order[idx % len(order)]
        idx += 1
        recipe = RECIPES[cat]
        brand = brands[_h(f"{cat}|{len(out)}|{guard}", "brand") % len(brands)]
        p = build_product(cat, recipe, brand, guard, taken_names)
        if not p or p["slug"] in taken_slugs:
            continue
        shape = (cat, p["price"], p["rating"])
        if shape in shapes:
            continue
        shapes.add(shape)
        taken_names.add(_norm(p["name"]))
        taken_slugs.add(p["slug"])
        counts[cat] = counts.get(cat, 0) + 1
        out.append(p)
        # recalculează ordinea când o categorie a atins pragul
        if counts[cat] >= MIN_PER_CATEGORY:
            order = [c for c in order if counts.get(c, 0) < MIN_PER_CATEGORY] or sorted(
                RECIPES, key=lambda c: (counts.get(c, 0), c)
            )
            idx = 0

    return out, {"generate": len(out), "per_cat": counts}


def main() -> int:
    args = sys.argv[1:]
    target = DEFAULT_TARGET
    if "--target" in args:
        target = int(args[args.index("--target") + 1])
    data = json.loads(DATA.read_text(encoding="utf-8"))
    new, stats = generate(data, target)

    print("=== GENERARE PRODUSE ===")
    print(f"  existente : {len(data['products'])}")
    print(f"  generate  : {len(new)}")
    print(f"  total     : {len(data['products']) + len(new)}")
    thin = {c: n for c, n in sorted(stats.get("per_cat", {}).items(), key=lambda kv: kv[1])[:8]}
    print(f"  cele mai subțiri după generare: {thin}")

    if "--report" in args:
        for p in new[:6]:
            print(f"\n  {p['name']}  ({p['price']} lei, {p['rating']}★)")
            print(f"    {p['shortDescription']}")
            print(f"    {p['attributes']}")
        return 0

    data["products"].extend(new)
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ catalog_v2.json: {len(data['products'])} produse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
