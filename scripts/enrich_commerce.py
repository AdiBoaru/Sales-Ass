"""NX-191 — stratul COMERCIAL al catalogului: reduceri cu fereastră, stoc realist, clasă de livrare.

Măsurat înainte: reducere reală 4/150 (2%), availability 150/150 `in_stock`, livrare inexistentă.
Adică mecanica de anchor („de la X ~~Y~~"), substitutul (222 relații), back-in-stock și
`restock_date` existau în cod și **nu aveau pe ce rula**. Scriptul le dă de lucru.

DETERMINIST, fără `random`: fiecare alegere derivă din sha256(slug + sare de scop). Re-rularea dă
exact același rezultat (idempotent, `--check` = gate CI), dar distribuția arată „naturală" pentru
că e răspândită pe categorii, nu pe primele N produse din fișier.

Datele calendaristice sunt RELATIVE (`endsInDays`, `restockInDays`), nu absolute: o promoție cu
`sale_end` fix ar expira și demo-ul ar arăta mort peste o lună. Seed-ul le convertește în date
reale la momentul rulării.

    python scripts/enrich_commerce.py           # scrie db/seed/catalog_v2.json
    python scripts/enrich_commerce.py --check   # exit 1 dacă fișierul ar fi modificat (gate CI)
    python scripts/enrich_commerce.py --report  # doar statistici, fără scriere
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "db" / "seed" / "catalog_v2.json"

# --- politica comercială (decizii luate explicit cu userul) ------------------------------------ #
DISCOUNT_SHARE = 0.30  # 30% din catalog, distribuit PE CATEGORII
# Procente VARIATE: dacă toate ar fi -20%, „care e cea mai bună ofertă?" primește un răspuns fals.
DISCOUNT_PCTS = (10, 15, 20, 25, 30, 35, 40)
# Ferestre variate: unele expiră în 2 zile („ultimele zile"), altele țin trei săptămâni.
SALE_ENDS_IN = (2, 3, 5, 7, 10, 14, 21)
SALE_STARTED_AGO = (1, 2, 3, 5, 8)

N_OUT_OF_STOCK = 8  # exercită substitutul + back-in-stock + restock_date
N_LOW_STOCK = 6  # exercită urgența onestă („au mai rămas 3 bucăți")
N_SHADE_GAPS = 5  # produse cu variante unde o NUANȚĂ e epuizată și alta pe terminate
LOW_STOCK_UNITS = (2, 3, 4)
RESTOCK_IN = (7, 10, 14, 21)
# Variat, și INTENȚIONAT în jurul pragului de 20: altfel toate produsele cad în aceeași clasă de
# livrare și demo-ul arată un singur scenariu.
DEFAULT_STOCK = (8, 12, 18, 24, 31, 42, 55, 67)

# Categorii care vin de la furnizor (nu se țin în depozit) → livrare mai lentă, indiferent de stoc.
SUPPLIER_CATEGORIES = {
    "pensule-si-bureti-de-machiaj",
    "uleiuri-pentru-par",
    "masti-de-par",
    "accesorii-pentru-par",
}
NEXT_DAY_MIN_STOCK = 20  # ce ținem în depozit în cantitate → poate pleca azi


def _h(slug: str, salt: str) -> int:
    """Hash STABIL per (produs, scop). Sare diferită → alegeri necorelate între dimensiuni
    (un produs cu reducere mare nu e sistematic și cel cu fereastra cea mai lungă)."""
    return int(hashlib.sha256(f"{salt}:{slug}".encode()).hexdigest()[:12], 16)


def _pick(seq, slug: str, salt: str):
    return seq[_h(slug, salt) % len(seq)]


def _price(p: dict) -> float:
    return float(p.get("price") or 0)


def _has_substitute(p: dict, by_cat: dict[str, list[dict]]) -> bool:
    """`substitute` = «aceeași categorie, preț ≤ ancoră» (seed_catalog_v2.derive_relations).
    Un produs care e CEL MAI IEFTIN din categoria lui n-are substitut → scos din stoc, botul n-ar
    avea ce propune. De aceea alegerea produselor epuizate se face DOAR dintre cele care au."""
    return any(q["slug"] != p["slug"] and _price(q) <= _price(p) for q in by_cat.get(_cat(p), []))


def _cat(p: dict) -> str:
    return p.get("primaryCategorySlug") or ""


def _apply_discount(p: dict, pct: int) -> None:
    """Reducerea se aplică ACOLO UNDE E PREȚUL. Pentru produsele cu variante, prețul efectiv e
    min(variantă) — o reducere pusă doar pe produs ar fi ignorată de read-path. Punem pe ambele,
    cu ACELAȘI procent, ca anchor-ul de pe card și prețul efectiv să spună același lucru."""
    p["salePrice"] = round(_price(p) * (1 - pct / 100), 2)
    for v in p.get("variants") or []:
        base = float(v.get("price") or 0)
        if base > 0:
            v["salePrice"] = round(base * (1 - pct / 100), 2)


def _clear_discount(p: dict) -> None:
    p.pop("salePrice", None)
    p.pop("saleWindow", None)
    for v in p.get("variants") or []:
        v.pop("salePrice", None)


def _set_stock(p: dict, units: int) -> None:
    """Stocul e pe produs; dacă are variante, se împarte între ele (varianta e unitatea vandabilă,
    deci «au mai rămas 3 bucăți» trebuie să fie adevărat pe NUANȚĂ, nu pe produs)."""
    p["stock"] = units
    variants = p.get("variants") or []
    if not variants:
        return
    base, rest = divmod(units, len(variants))
    for i, v in enumerate(variants):
        v["stock"] = base + (1 if i < rest else 0)


def _delivery_class(p: dict) -> str:
    """Derivată din FAPTE (stoc + categorie), nu aleatoriu — ca să fie explicabilă:
    ce nu e în stoc vine de la furnizor; ce ținem în cantitate poate pleca azi."""
    units = int(p.get("stock") or 0)
    if units == 0:
        return "supplier"
    if _cat(p) in SUPPLIER_CATEGORIES:
        return "supplier"
    if units >= NEXT_DAY_MIN_STOCK:
        return "next_day"
    return "standard"


def enrich(data: dict) -> dict[str, int]:
    products = data["products"]
    by_cat: dict[str, list[dict]] = {}
    for p in products:
        by_cat.setdefault(_cat(p), []).append(p)

    counts = {"discount": 0, "out_of_stock": 0, "low_stock": 0, "shade_gap": 0}

    # --- 1. STOC: mai întâi, pentru că reducerea evită produsele epuizate ---------------------- #
    # Epuizatele: doar produse cu substitut, cel mult unul per categorie (altfel o categorie
    # întreagă dispare din răspunsuri), în ordine stabilă de hash.
    # Preferăm produsele FĂRĂ variante: la unul cu nuanțe, „epuizat" pe tot produsul e o pierdere
    # de scenariu — lipsa pe NUANȚĂ (o nuanță 0, restul pe stoc) e și mai realistă, și mai utilă
    # în demo. Acelea sunt tratate separat, mai jos.
    eligible = sorted(
        (p for p in products if _has_substitute(p, by_cat) and not p.get("variants")),
        key=lambda p: _h(p["slug"], "oos"),
    )
    out_of_stock: list[dict] = []
    used_cats: set[str] = set()
    for p in eligible:
        if len(out_of_stock) >= N_OUT_OF_STOCK:
            break
        if _cat(p) in used_cats:
            continue
        used_cats.add(_cat(p))
        out_of_stock.append(p)

    low_pool = sorted(
        (p for p in products if p not in out_of_stock), key=lambda p: _h(p["slug"], "low")
    )
    low_stock: list[dict] = []
    low_cats: set[str] = set()
    for p in low_pool:
        if len(low_stock) >= N_LOW_STOCK:
            break
        if _cat(p) in low_cats or _cat(p) in used_cats:
            continue
        low_cats.add(_cat(p))
        low_stock.append(p)

    oos_slugs = {p["slug"] for p in out_of_stock}
    low_slugs = {p["slug"] for p in low_stock}

    for p in products:
        slug = p["slug"]
        if slug in oos_slugs:
            _set_stock(p, 0)
            p["restockInDays"] = _pick(RESTOCK_IN, slug, "restock")
            counts["out_of_stock"] += 1
        elif slug in low_slugs:
            _set_stock(p, _pick(LOW_STOCK_UNITS, slug, "lowunits"))
            p.pop("restockInDays", None)
            counts["low_stock"] += 1
        else:
            _set_stock(p, _pick(DEFAULT_STOCK, slug, "stock"))
            p.pop("restockInDays", None)

    # --- 2. REDUCERI: 30% din FIECARE categorie (nu primele 30% din fișier) -------------------- #
    # Cotă per categorie prin METODA CELUI MAI MARE REST: `ceil` per categorie ar umfla totalul
    # (38 de categorii × ceil(4×0.30)=2 → 50%, nu 30%). Aici partea întreagă se alocă direct, iar
    # resturile decid cine primește produsele rămase până la ținta GLOBALĂ. Determinist: la rest
    # egal, ordinea e pe slug-ul categoriei.
    pools = {
        cat: sorted(
            (p for p in group if p["slug"] not in oos_slugs), key=lambda p: _h(p["slug"], "sale")
        )
        for cat, group in by_cat.items()
    }
    target = round(len(products) * DISCOUNT_SHARE)
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for cat, pool in pools.items():
        exact = len(pool) * DISCOUNT_SHARE
        quotas[cat] = min(int(exact), len(pool))
        remainders.append((exact - int(exact), cat))
    leftover = target - sum(quotas.values())
    for _, cat in sorted(remainders, key=lambda t: (-t[0], t[1])):
        if leftover <= 0:
            break
        if quotas[cat] < len(pools[cat]):
            quotas[cat] += 1
            leftover -= 1

    discounted: set[str] = set()
    for cat, pool in pools.items():
        for p in pool[: quotas[cat]]:
            discounted.add(p["slug"])

    for p in products:
        slug = p["slug"]
        if slug in discounted:
            _apply_discount(p, _pick(DISCOUNT_PCTS, slug, "pct"))
            p["saleWindow"] = {
                "startsInDays": -_pick(SALE_STARTED_AGO, slug, "start"),
                "endsInDays": _pick(SALE_ENDS_IN, slug, "end"),
            }
            counts["discount"] += 1
        else:
            _clear_discount(p)

    # --- 2b. LIPSĂ PE NUANȚĂ ------------------------------------------------------------------- #
    # Produsul e pe stoc, dar o nuanță e epuizată și alta aproape. Ăsta e cazul REAL la machiaj și
    # singurul care demonstrează că unitatea vandabilă e VARIANTA: „Ivory e pe stoc, la Beige au mai
    # rămas 3, Sand e epuizat" nu se poate spune dintr-un stoc agregat pe produs.
    with_variants = sorted(
        (p for p in products if len(p.get("variants") or []) >= 3 and p["slug"] not in oos_slugs),
        key=lambda p: _h(p["slug"], "shade"),
    )
    for p in with_variants[:N_SHADE_GAPS]:
        variants = p["variants"]
        variants[_h(p["slug"], "gap_out") % len(variants)]["stock"] = 0
        low_idx = _h(p["slug"], "gap_low") % len(variants)
        if variants[low_idx].get("stock", 0) > 0:
            variants[low_idx]["stock"] = _pick(LOW_STOCK_UNITS, p["slug"], "gap_units")
        # stocul de produs rămâne suma variantelor → availability agregată nu minte
        p["stock"] = sum(int(v.get("stock") or 0) for v in variants)
        counts["shade_gap"] += 1

    # --- 3. LIVRARE: derivată din stoc + categorie --------------------------------------------- #
    for p in products:
        p["deliveryClass"] = _delivery_class(p)

    return counts


def report(data: dict) -> None:
    products = data["products"]
    n = len(products)
    disc = [p for p in products if p.get("salePrice")]
    print(f"produse: {n}")
    print(f"  cu reducere      : {len(disc)} ({100 * len(disc) // n}%)")
    pcts: dict[int, int] = {}
    for p in disc:
        pct = round((1 - float(p["salePrice"]) / float(p["price"])) * 100)
        pcts[pct] = pcts.get(pct, 0) + 1
    print(f"  procente         : {dict(sorted(pcts.items()))}")
    cats = {p.get("primaryCategorySlug") for p in disc}
    all_cats = {p.get("primaryCategorySlug") for p in products}
    print(f"  categorii atinse : {len(cats)} din {len(all_cats)}")
    ends: dict[int, int] = {}
    for p in disc:
        d = (p.get("saleWindow") or {}).get("endsInDays")
        ends[d] = ends.get(d, 0) + 1
    print(f"  expiră în (zile) : {dict(sorted(ends.items()))}")
    oos = [p for p in products if int(p.get("stock") or 0) == 0]
    low = [p for p in products if 0 < int(p.get("stock") or 0) <= 5]
    print(f"  epuizate         : {len(oos)}  (categorii: {len({_cat(p) for p in oos})})")
    print(f"  stoc mic         : {len(low)}")
    dc: dict[str, int] = {}
    for p in products:
        dc[p.get("deliveryClass")] = dc.get(p.get("deliveryClass"), 0) + 1
    print(f"  clasă livrare    : {dict(sorted(dc.items()))}")


def main() -> int:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    counts = enrich(data)
    after = json.dumps(data, ensure_ascii=False, sort_keys=True)

    if "--report" in sys.argv:
        report(data)
        return 0
    if "--check" in sys.argv:
        if before != after:
            print("✗ catalog_v2.json NU e la zi — rulează scripts/enrich_commerce.py")
            return 1
        print("✓ up-to-date")
        return 0

    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=== ENRICHMENT COMERCIAL ===")
    print(
        f"  reduceri: {counts['discount']} · epuizate: {counts['out_of_stock']} "
        f"· stoc mic: {counts['low_stock']}"
    )
    report(data)
    print(f"\n✓ scris {DATA.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
