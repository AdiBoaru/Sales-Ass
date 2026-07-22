"""NX-192 — revitalizează produse din ARHIVĂ la contractul v3 (varianta C: plafonat per categorie).

Arhiva (504 produse, seed-ul vechi templatat) NU e un depozit de produse gata de folosit:

  • 500 din 504 au literalmente „Produs fictiv de tip …" în descrierea vizibilă;
  • **207 sunt INCOERENTE** — „pensulă de machiaj" în categoria *Geluri de duș*, „gel de curățare"
    la *Șampoane*, „toner" la *Protecție solară*. Numele și categoria spun lucruri diferite;
  • atributele amestecă fapte canonice cu gunoi de import (`Cod EAN`, `Cod memoX`, `Pret unitar`);
  • `concerns` amestecă chei canonice cu text RO liber („uz zilnic", „calmare", „păr uscat").

Deci: revitalizăm DOAR subsetul coerent (tipul din text == categoria), **plafonat per categorie**.
Fără plafon, catalogul s-ar strâmba: arhiva are 54 de seruri și 51 de măști, iar catalogul activ
are 8, respectiv 3 — le-am fi triplat o singură ramură și am fi obținut exact „prea multe
asemănări" pe care le evităm.

Nu inventează fapte: tot ce scrie derivă din ce EXISTĂ pe produsul arhivat (categorie, brand,
ingrediente-cheie, concerns) sau din reguli per-categorie. Restul câmpurilor v3 (best_for, usage,
suitable_for, ai_summary, description) le derivă `enrich_catalog_v3.py`, care rulează DUPĂ.

    python scripts/revive_archived.py --dump <fisier.json> --report   # doar statistici
    python scripts/revive_archived.py --dump <fisier.json>            # scrie în catalog_v2.json
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

#: câte produse ținem, ca țintă, per categorie (activ + revitalizat). Peste asta nu mai adăugăm —
#: plafonul e mecanismul care ține catalogul echilibrat.
TARGET_PER_CATEGORY = 16

# tip declarat în textul arhivat → categoria CANONICĂ în care are voie să stea.
# Doar tipuri fără ambiguitate; restul produselor sunt sărite (nu ghicim).
TYPE2CAT = {
    "serum": "seruri-pentru-ten",
    "masca de fata": "masti-pentru-ten",
    "toner": "lotiuni-tonice",
    "spuma de curatare": "curatarea-tenului",
    "ulei de curatare": "curatarea-tenului",
    "gel de curatare": "curatarea-tenului",
    "balsam de curatare": "demachiante-pentru-ten",
    "apa micelara": "demachiante-pentru-ten",
    "exfoliant": "exfoliante-pentru-ten",
    "peeling": "exfoliante-pentru-ten",
    "sampon": "sampoane",
    "balsam de par": "balsamuri-de-par",
    "masca de par": "masti-de-par",
    "ulei de par": "uleiuri-pentru-par",
    "gel de dus": "geluri-de-dus",
    "lotiune de corp": "lotiuni-de-corp",
    "scrub": "scrub-de-corp",
    "deodorant": "deodorante",
    "crema de maini": "creme-de-maini",
    "fond de ten": "fond-de-ten",
    "anticearcan": "anticearcan",
    "ruj": "rujuri",
    "gloss": "gloss-de-buze",
    "mascara": "mascara",
    "pudra": "pudre",
    "iluminator": "iluminatoare",
    "bronzer": "bronzer",
    "fard de obraz": "fard-de-obraz",
    "fard de ochi": "farduri-de-ochi",
    "primer": "primer-pentru-machiaj",
    "spray de fixare": "spray-de-fixare",
    "crema de ochi": "creme-de-ochi",
    "stick spf": "protectie-solara",
    "balsam de buze": "buze",
    "pensula de machiaj": "pensule-si-bureti-de-machiaj",
    "burete": "pensule-si-bureti-de-machiaj",
    "mist": "mist-pentru-ten",
}

#: categoria → tipul RO scris corect, folosit la RECONSTRUCȚIA numelui. Numele arhivat conține
#: forme fără diacritice și inconsecvente („Masca", „Apa parfumata"); îl reconstruim ca
#: „<Brand> <Linie> <Tip>", păstrând linia de gamă (Balance/Calm/Hydra…) — acolo stă varietatea.
TYPE_RO = {
    "seruri-pentru-ten": "Ser pentru ten",
    "masti-pentru-ten": "Mască de față",
    "lotiuni-tonice": "Loțiune tonică",
    "curatarea-tenului": "Gel de curățare",
    "demachiante-pentru-ten": "Apă micelară",
    "exfoliante-pentru-ten": "Exfoliant pentru ten",
    "sampoane": "Șampon",
    "balsamuri-de-par": "Balsam de păr",
    "masti-de-par": "Mască de păr",
    "uleiuri-pentru-par": "Ulei de păr",
    "geluri-de-dus": "Gel de duș",
    "lotiuni-de-corp": "Loțiune de corp",
    "scrub-de-corp": "Scrub de corp",
    "deodorante": "Deodorant",
    "creme-de-maini": "Cremă de mâini",
    "fond-de-ten": "Fond de ten",
    "anticearcan": "Anticearcăn",
    "rujuri": "Ruj",
    "gloss-de-buze": "Gloss de buze",
    "mascara": "Mascara",
    "pudre": "Pudră",
    "iluminatoare": "Iluminator",
    "bronzer": "Bronzer",
    "fard-de-obraz": "Fard de obraz",
    "farduri-de-ochi": "Fard de ochi",
    "primer-pentru-machiaj": "Primer pentru machiaj",
    "spray-de-fixare": "Spray de fixare",
    "creme-de-ochi": "Cremă de ochi",
    "protectie-solara": "Stick cu SPF",
    "buze": "Balsam de buze",
    "pensule-si-bureti-de-machiaj": "Pensulă de machiaj",
    "mist-pentru-ten": "Mist pentru ten",
}

#: concerns arhivate (RO liber sau canonic) → cheia canonică de TEN. Ce nu e aici se aruncă:
#: „uz zilnic" / „calmare" nu sunt concerns, sunt umplutură.
CONCERN_CANON = {
    "hydration": "hydration",
    "hidratare": "hydration",
    "dry": "dry",
    "ten uscat": "dry",
    "uscat": "dry",
    "oily": "oily",
    "ten gras": "oily",
    "gras": "oily",
    "sensitive": "sensitive",
    "ten sensibil": "sensitive",
    "sensibil": "sensitive",
    "combination": "combination",
    "ten mixt": "combination",
    "mixt": "combination",
    "acne": "acne",
    "acnee": "acne",
    "anti_aging": "anti_aging",
    "riduri": "anti_aging",
    "antirid": "anti_aging",
    "hyperpigmentation": "hyperpigmentation",
    "pete": "hyperpigmentation",
    "normal": "normal",
    "ten normal": "normal",
}
#: concerns arhivate de PĂR → `hair_type` (nu sunt concerns de ten; enum-ul v3 e pentru ten)
HAIR_CANON = {
    "par uscat": "uscat",
    "par gras": "gras",
    "par vopsit": "vopsit",
    "par deteriorat": "deteriorat",
    "par fin": "fin",
    "par cret": "creț",
}
HAIR_ROOTS = ("sampoane", "balsamuri-de-par", "masti-de-par", "uleiuri-pentru-par")

#: UNELTE (pensule, bureți): accesorii, nu cosmetice. Arhiva le dăduse `key_ingredients` de
#: skincare și `concerns` de ten → „Pensulă de machiaj cu niacinamidă, pentru ten normal", ceea ce
#: e absurd. Aici se taie: o unealtă n-are ingrediente și n-are tip de ten.
TOOL_CATEGORIES = ("pensule-si-bureti-de-machiaj",)
#: machiajul de CULOARE nu moștenește ingrediente de îngrijire din arhivă (un ruj „cu panthenol"
#: ar fi un claim pe care nu-l putem susține). Fondul/anticearcănul păstrează tipul de ten.
NO_INGREDIENT_CATEGORIES = TOOL_CATEGORIES + (
    "rujuri",
    "gloss-de-buze",
    "mascara",
    "pudre",
    "iluminatoare",
    "bronzer",
    "fard-de-obraz",
    "farduri-de-ochi",
    "spray-de-fixare",
)
#: plafon mai mic pentru categoriile de accesorii — sunt umplutură de catalog, nu produse-vedetă
CAP_OVERRIDE = {"pensule-si-bureti-de-machiaj": 8}

#: Cerințe pe care revitalizarea NU le poate satisface din arhivă și pe care `enrich_catalog_v3`
#: NU le derivă: dacă lipsesc, produsul e SĂRIT (nu inventăm `finish` unui ruj sau ingrediente
#: unui ser). Restul obligatoriilor v3 (texture/usage/suitable_for/routine_step/net_content) sunt
#: derivate ulterior, deterministic, deci nu blochează selecția.
HARD_REQUIRED = {
    "ingrijirea-tenului": ("key_ingredients",),
    "ingrijirea-parului": ("hair_type",),
    "machiaj": ("finish",),
}
#: rădăcina de categorie (pentru HARD_REQUIRED) — derivat din prefixul cunoscut al catalogului
CAT_ROOT = {
    "seruri-pentru-ten": "ingrijirea-tenului",
    "masti-pentru-ten": "ingrijirea-tenului",
    "lotiuni-tonice": "ingrijirea-tenului",
    "curatarea-tenului": "ingrijirea-tenului",
    "demachiante-pentru-ten": "ingrijirea-tenului",
    "exfoliante-pentru-ten": "ingrijirea-tenului",
    "creme-de-ochi": "ingrijirea-tenului",
    "mist-pentru-ten": "ingrijirea-tenului",
    "sampoane": "ingrijirea-parului",
    "balsamuri-de-par": "ingrijirea-parului",
    "masti-de-par": "ingrijirea-parului",
    "uleiuri-pentru-par": "ingrijirea-parului",
    "rujuri": "machiaj",
    "gloss-de-buze": "machiaj",
    "pudre": "machiaj",
    "iluminatoare": "machiaj",
    "bronzer": "machiaj",
    "fard-de-obraz": "machiaj",
    "spray-de-fixare": "machiaj",
    "primer-pentru-machiaj": "machiaj",
    "fond-de-ten": "machiaj",
    "anticearcan": "machiaj",
}


def meets_hard_requirements(attrs: dict, cat: str) -> bool:
    """Poate produsul să atingă contractul v3 fără să inventăm? Categoriile cu cerințe pe care nu
    le putem deriva onest (finish la machiaj, hair_type la păr, ingrediente la skincare) filtrează
    aici — mai bine 69 de produse corecte decât 90 cu atribute fabricate."""
    root = CAT_ROOT.get(cat)
    if cat in TOOL_CATEGORIES:
        return bool(attrs.get("key_benefit"))
    for key in HARD_REQUIRED.get(root or "", ()):  # noqa: SIM110
        if not attrs.get(key):
            return False
    return True


#: chei de import care NU sunt fapte: pleacă din top-level în `specs` (afișabile) sau `_meta`.
TO_SPECS = {"Cod EAN", "Cod producator", "Pachetul contine", "Culoare"}
TO_META = {
    "Cod memoX",
    "enrich_v",
    "original_brand_replaced",
    "source_visible_image_count",
    "source_visible_variant_count",
    "_concerns_ro",
}
DROP = {"Pret unitar"}  # derivat din preț + gramaj (price_per_unit, generat de DB)


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _slugify(s: str) -> str:
    n = _norm(s)
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return re.sub(r"-{2,}", "-", n)


def detect_type(description: str) -> str | None:
    """Tipul declarat în textul generat („…exemplu de <tip>, cu accent pe…"). Cel mai LUNG tip
    care se potrivește câștigă („ulei de curatare" bate „ulei")."""
    m = re.search(r"exemplu de ([a-z ]+?), cu accent", _norm(description or ""))
    if not m:
        return None
    txt = m.group(1).strip()
    for key in sorted(TYPE2CAT, key=len, reverse=True):
        if key in txt:
            return key
    return None


def brand_line(name: str, brand: str) -> str:
    """Linia de gamă („Balance", „Calm", „Hydra") dintre brand și tipul de produs. E singura sursă
    reală de varietate în numele arhivate → o păstrăm."""
    rest = (name or "")[len(brand) :].strip() if name.startswith(brand) else (name or "")
    tokens = rest.split()
    if tokens and tokens[0][:1].isupper() and _norm(tokens[0]) not in ("pentru", "cu", "de"):
        return tokens[0]
    return ""


def clean_attributes(raw: dict, cat: str) -> dict:
    """Fapte canonice sus, specs afișabile în `specs`, telemetrie de import în `_meta`.
    `concerns` se canonicalizează; valorile de PĂR devin `hair_type` (enum-ul de concerns e de ten).
    Nu se inventează nimic: ce nu se poate mapa, se aruncă."""
    out: dict = {}
    specs: dict = {}
    meta: dict = {}
    concerns: list[str] = []
    hair: str | None = None

    for key, val in (raw or {}).items():
        if key in DROP:
            continue
        if key in TO_SPECS:
            specs[key] = val
            continue
        if key in TO_META:
            meta[key] = val
            continue
        if key == "concerns" and isinstance(val, list):
            for c in val:
                n = _norm(str(c))
                if n in HAIR_CANON:
                    hair = hair or HAIR_CANON[n]
                elif n in CONCERN_CANON and CONCERN_CANON[n] not in concerns:
                    concerns.append(CONCERN_CANON[n])
            continue
        out[key] = val

    if cat in NO_INGREDIENT_CATEGORIES:
        out.pop("key_ingredients", None)
        out.pop("claim_provenance", None)
    if cat in TOOL_CATEGORIES:
        # o unealtă n-are tip de ten și n-are tip de păr
        out.pop("concerns", None)
        out.pop("hair_type", None)
        concerns = []
        hair = None
    if cat in HAIR_ROOTS:
        # produsele de păr NU primesc concerns de ten; semnalul lor e hair_type
        if hair:
            out["hair_type"] = hair
        out.pop("concerns", None)
    elif concerns:
        out["concerns"] = concerns

    if specs:
        out["specs"] = specs
    if meta:
        meta["revived_from"] = "archive"
        out["_meta"] = meta
    else:
        out["_meta"] = {"revived_from": "archive"}
    return out


def short_description(type_ro: str, attrs: dict, cat: str) -> str:
    """Descriere scurtă din FAPTE (fără „produs fictiv", fără claim-uri). Formulare per axă:
    ten (concerns) / păr (hair_type) / neutru."""
    ing = attrs.get("key_ingredients") or []
    bits = [f"{type_ro} pentru uz zilnic."]
    if cat in TOOL_CATEGORIES:
        kb = (attrs.get("key_benefit") or "").rstrip(".")
        return f"{type_ro} pentru aplicarea machiajului." + (f" {kb}." if kb else "")
    if cat in HAIR_ROOTS and attrs.get("hair_type"):
        bits[0] = f"{type_ro} pentru păr {attrs['hair_type']}."
    elif attrs.get("concerns"):
        ro = {
            "hydration": "hidratare",
            "dry": "ten uscat",
            "oily": "ten gras",
            "sensitive": "ten sensibil",
            "combination": "ten mixt",
            "acne": "ten cu tendință acneică",
            "anti_aging": "ten matur",
            "hyperpigmentation": "ten cu pete",
            "normal": "ten normal",
        }
        vals = [ro[c] for c in attrs["concerns"] if c in ro][:2]
        if vals:
            bits[0] = f"{type_ro} pentru {' și '.join(vals)}."
    if ing:
        bits.append(f"Cu {', '.join(str(i) for i in ing[:3])}.")
    return " ".join(bits)


def build(dump: dict, existing: dict) -> tuple[list[dict], list[dict], list[dict], dict]:
    """→ (produse noi, categorii de adăugat, branduri de adăugat, statistici)."""
    active = existing["products"]
    active_by_cat: dict[str, int] = {}
    for p in active:
        active_by_cat[p["primaryCategorySlug"]] = active_by_cat.get(p["primaryCategorySlug"], 0) + 1
    taken_names = {_norm(p["name"]) for p in active}
    taken_slugs = {p["slug"] for p in active}
    known_cats = {c["slug"] for c in existing["categories"]}
    known_brands = {b["slug"] for b in existing["brands"]}

    # 1. filtrare pe coerență
    candidates: list[tuple[dict, str]] = []
    stats = {"total": len(dump["products"]), "coerente": 0, "incoerente": 0, "fara_tip": 0}
    for p in dump["products"]:
        typ = detect_type(p.get("description") or "")
        if not typ:
            stats["fara_tip"] += 1
            continue
        want = TYPE2CAT[typ]
        if want != p.get("cslug"):
            stats["incoerente"] += 1
            continue
        stats["coerente"] += 1
        candidates.append((p, want))

    # 2. plafon per categorie: completăm până la țintă, nu mai mult. Ordonare stabilă (rating desc,
    #    apoi slug) → alegem produsele cel mai bine cotate din arhivă, determinist.
    candidates.sort(key=lambda t: (-(t[0].get("rating") or 0), t[0]["slug"]))
    per_cat: dict[str, int] = {}
    picked: list[tuple[dict, str]] = []
    # R6: două produse din aceeași categorie cu preț ȘI rating identice n-au niciun diferențiator
    # — comparația botului ar fi „sunt la fel". Arhiva e plină de astfel de perechi (generator cu
    # aceleași valori). Păstrăm primul, îl sărim pe al doilea. Amprenta include și produsele
    # ACTIVE, nu doar cele revitalizate între ele.
    seen_shape: set[tuple] = {
        (
            q["primaryCategorySlug"],
            round(float(q.get("price") or 0), 2),
            round(float(q.get("rating") or 0), 2),
        )
        for q in active
    }
    for p, cat in candidates:
        shape = (cat, round(float(p.get("price") or 0), 2), round(float(p.get("rating") or 0), 2))
        if shape in seen_shape:
            continue
        cap = CAP_OVERRIDE.get(cat, TARGET_PER_CATEGORY)
        room = cap - active_by_cat.get(cat, 0) - per_cat.get(cat, 0)
        if room <= 0:
            continue
        seen_shape.add(shape)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        picked.append((p, cat))

    # 3. conversie la contractul v3
    out: list[dict] = []
    new_cats: list[dict] = []
    new_brands: list[dict] = []
    cat_index = {c["slug"]: c for c in dump["categories"]}
    brand_index = {b["slug"]: b for b in dump["brands"]}

    for p, cat in picked:
        attrs = clean_attributes(p.get("attributes") or {}, cat)
        if not meets_hard_requirements(attrs, cat):
            continue
        type_ro = TYPE_RO[cat]
        brand = p.get("bname") or ""
        line = brand_line(p.get("name") or "", brand)
        base_name = " ".join(x for x in (brand, line, type_ro) if x)
        name = base_name
        # dedupe pe ingredient-erou (aceeași tehnică ca NX-155): două măști ale aceleiași game
        # devin „…cu acid hialuronic" / „…cu niacinamidă", nu două nume identice.
        if _norm(name) in taken_names:
            for ing in attrs.get("key_ingredients") or []:
                cand = f"{base_name} cu {ing}"
                if _norm(cand) not in taken_names:
                    name = cand
                    break
        if _norm(name) in taken_names:
            continue  # n-avem cum să-l diferențiem onest → îl sărim
        taken_names.add(_norm(name))

        slug = _slugify(name)
        if slug in taken_slugs:
            continue
        taken_slugs.add(slug)

        variants = []
        for v in p.get("variants") or []:
            price = float(v.get("price") or p.get("price") or 0)
            if price <= 0:
                continue
            variants.append(
                {
                    "label": v.get("label") or "Standard",
                    # SKU NOU, nu cel arhivat: produsul vechi rămâne în DB (status='archived')
                    # și `unique(business_id, sku)` ar refuza inserarea. Determinist pe (slug,label)
                    # → re-rularea dă același SKU, deci seed-ul rămâne idempotent.
                    "sku": "REV-"
                    + hashlib.sha1(f"{slug}|{v.get('label') or 'Standard'}".encode())
                    .hexdigest()[:12]
                    .upper(),
                    "price": round(price, 2),
                    "stock": int(v.get("stock") or 0),
                }
            )

        out.append(
            {
                "slug": slug,
                "name": name,
                "brandSlug": p.get("bslug"),
                "primaryCategorySlug": cat,
                "categorySlugs": [cat],
                "price": round(float(p.get("price") or 0), 2),
                "currency": "RON",
                "rating": round(float(p.get("rating") or 0), 2),
                "reviewCount": int(p.get("review_count") or 0),
                "status": "active",
                "shortDescription": short_description(type_ro, attrs, cat),
                "attributes": attrs,
                **({"variants": variants} if variants else {}),
            }
        )
        if cat not in known_cats and cat in cat_index:
            new_cats.append(cat_index[cat])
            known_cats.add(cat)
        b = p.get("bslug")
        if b and b not in known_brands and b in brand_index:
            new_brands.append(brand_index[b])
            known_brands.add(b)

    stats["revitalizate"] = len(out)
    stats["per_categorie"] = dict(sorted(per_cat.items(), key=lambda kv: -kv[1]))
    return out, new_cats, new_brands, stats


def main() -> int:
    args = sys.argv[1:]
    if "--dump" not in args:
        print(__doc__)
        return 2
    dump_path = Path(args[args.index("--dump") + 1])
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    data = json.loads(DATA.read_text(encoding="utf-8"))

    products, cats, brands, stats = build(dump, data)

    print("=== REVITALIZARE ARHIVĂ (varianta C, plafonat) ===")
    print(f"  arhivate            : {stats['total']}")
    print(f"  coerente            : {stats['coerente']}")
    print(f"  incoerente (sărite) : {stats['incoerente']}")
    print(f"  fără tip (sărite)   : {stats['fara_tip']}")
    print(f"  REVITALIZATE        : {stats['revitalizate']}")
    print(f"  branduri noi        : {len(brands)} · categorii noi: {len(cats)}")
    print(f"  pe categorie        : {stats['per_categorie']}")

    if "--report" in args:
        for p in products[:5]:
            print(f"\n  {p['name']}\n    {p['shortDescription']}\n    {p['attributes']}")
        return 0

    data["products"].extend(products)
    data["categories"].extend(cats)
    data["brands"].extend(brands)
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ catalog_v2.json: {len(data['products'])} produse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
